"""Doctor-owned question/task commands and replay-safe Telegram receipt completion."""

from datetime import datetime
from secrets import token_urlsafe
from typing import TYPE_CHECKING

from pydantic import BaseModel, StrictBool, ValidationError

from sanad.auth.service import revise
from sanad.concierge import templates
from sanad.concierge.policy import DRAFT_CONCIERGE_POLICY as POLICY
from sanad.concierge.text import contains
from sanad.domain import (
    Mission,
    PatientScope,
    Principal,
    ReviewAction,
    ReviewObligation,
    TenantScope,
)
from sanad.domain import events as ev
from sanad.domain.boundaries import NonblankStr, _BoundaryValue
from sanad.domain.deadlines import ExplicitTiming, format_local
from sanad.domain.entities import TERMINAL_STATES, QuestionDetails, TaskDetails
from sanad.domain.predicates import PredicateResult
from sanad.domain.transitions import transition_mission, transition_review
from sanad.safety import validate_patient_output, wants_treatment_change
from sanad.safety.models import OrderSummary, OutputContext
from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT as SAFETY
from sanad.safety.validator import typed_numbers
from sanad.scribe.extract import OrderCandidate
from sanad.scribe.patients import panel
from sanad.scribe.records import CareOrderVersion
from sanad.steward.apply import CommitBuilder, EffectsRejected, make_intent
from sanad.steward.types import records
from sanad.store import keys
from sanad.store.records import (
    CommandEnvelope,
    Doctor,
    DoctorAuthority,
    OutboundIntent,
    Patient,
    PatientProfile,
    canonical_json,
    from_record,
    to_record,
)

if TYPE_CHECKING:
    from sanad.channels.telegram.router import RouteResult
    from sanad.liaison.records import ReusableAnswer, ReuseOffer
    from sanad.scribe.proposal import Proposal
    from sanad.scribe.turn import ScribeTurn
    from sanad.store.protocol import Store
    from sanad.store.records import Authorization, Claim, InboundReceipt


class AnswerQuestion(_BoundaryValue):
    mission_id: NonblankStr
    answer_text: str = ""
    close_only: StrictBool = False
    listing_token: str = ""
    expected_version: int | None = None


def active_orders(store: "Store", scope: PatientScope) -> tuple[CareOrderVersion, ...]:
    values = []
    for row in records(store, scope, "care_order_head"):
        if row.body.get("status") != "active" or row.body.get("type") != "medication":
            continue
        version = store.get(scope, "care_order_version", str(row.body["current_version_id"]))
        authority = store.get(scope, "care_order", row.id)
        if version and authority and authority.body.get("status") == "active":
            value = from_record(version, CareOrderVersion)
            if authority.version == value.order_version:
                values.append(value)
    return tuple(values)


def output_context(store: "Store", scope: PatientScope, patient: Patient) -> OutputContext:
    orders = active_orders(store, scope)
    return OutputContext(
        active_orders=tuple(
            OrderSummary(
                order_ref=to_record(o, scope).ref,
                drug_names=(o.structured_instruction.drug,),
            )
            for o in orders
            if isinstance(o.structured_instruction, OrderCandidate)
        ),
        allowed_numbers=tuple(
            value
            for o in orders
            if isinstance(o.structured_instruction, OrderCandidate)
            for value in (o.structured_instruction.dose, o.structured_instruction.frequency)
            if value
        ),
        mode="plan_explanation",
        language=patient.language,
    )


def treatment_change(text: str, context: OutputContext) -> bool:
    return wants_treatment_change(text, policy=SAFETY) or (
        any(contains(text, name) for order in context.active_orders for name in order.drug_names)
        and (
            any(kind == "dose" for _, kind, _ in typed_numbers(text))
            or contains(
                text,
                "start",
                "stop",
                "begin",
                "discontinue",
                "increase",
                "decrease",
                "ابدأ",
                "ابدا",
                "وقف",
                "بطل",
                "زود",
                "قلل",
            )
        )
    )


def reviews(builder: CommitBuilder, mission: Mission) -> tuple[ReviewObligation, ...]:
    return tuple(
        from_record(r, ReviewObligation)
        for r in records(builder.store, builder.scope, "review")
        if r.body.get("source_mission_id") == mission.id
        and r.body.get("review_kind") == "question_answer"
        and r.body.get("state") != "resolved"
    )


def patient_intent(
    builder: CommitBuilder,
    mission: Mission,
    profile: PatientProfile,
    template: str,
    text: str,
) -> bool:
    """Persist delivery failure honestly, independently of clinical completion."""
    doctor_row = builder.store.get(builder.scope, "doctor_authority", builder.scope.doctor_id)
    patient_row = builder.store.get(builder.scope, "patient", builder.scope.patient_id)
    if not doctor_row or not patient_row:
        raise EffectsRejected("patient_missing")
    doctor, patient = from_record(doctor_row, DoctorAuthority), from_record(patient_row, Patient)
    pending = (
        not profile.binding_active
        or not profile.consent_active
        or not profile.recipient_ref
        or patient.contact_status in {"unreachable", "frozen", "awaiting_link"}
    )
    if not profile.recipient_ref:
        builder.audit(
            "QUESTION_DELIVERY_PENDING",
            keys.digest(builder.command.command_id + ":delivery"),
            (to_record(mission, builder.scope).ref,),
        )
        return True
    intent = make_intent(
        builder.scope,
        builder.command.command_id,
        (to_record(mission, builder.scope).ref,),
        "solicited_reply",
        builder.command.command_id,
        builder.now,
        builder.policy,
        doctor,
        profile,
        audience="patient",
        template_id=template,
    )
    payload = {"text": text}
    intent = intent.model_copy(
        update={
            "eligibility_class": "question_reply",
            "payload": payload,
            "payload_digest": keys.digest(canonical_json(payload).decode()),
            "recipient_subject": profile.recipient_subject,
            **(
                {
                    "status": "suppressed",
                    "suppression_reason": "question_delivery_pending",
                    "work_clock": None,
                }
                if pending
                else {}
            ),
        }
    )
    builder.intents[intent.id] = to_record(intent, builder.scope)
    return pending


def prepare_answer(
    builder: CommitBuilder,
    profile: PatientProfile,
    *,
    release: bool = False,
    args: AnswerQuestion | None = None,
) -> str:
    try:
        args = args or AnswerQuestion.model_validate(
            {k: v for k, v in builder.command.payload.items() if k != "type"}
        )
    except ValidationError as error:
        raise EffectsRejected("invalid_answer_payload") from error
    row = builder.store.get(builder.scope, "mission", args.mission_id)
    patient_row = builder.store.get(builder.scope, "patient", builder.scope.patient_id)
    if not row or not patient_row:
        raise EffectsRejected("question_missing")
    mission, patient = from_record(row, Mission), from_record(patient_row, Patient)
    if not isinstance(mission.details, QuestionDetails) or mission.state in TERMINAL_STATES:
        raise EffectsRejected("question_not_open")
    if args.expected_version is not None and args.expected_version != mission.version:
        raise EffectsRejected("question_stale")
    obligations = reviews(builder, mission)
    if not obligations:
        raise EffectsRejected("question_review_missing")
    builder.command = builder.command.model_copy(
        update={
            "expected_versions": tuple(
                dict.fromkeys(
                    (
                        *builder.command.expected_versions,
                        row.ref,
                        patient_row.ref,
                        *(to_record(r, builder.scope).ref for r in obligations),
                    )
                )
            )
        }
    )
    actor = builder.command.principal.subject
    if release:
        detail = mission.details
        authority_row = builder.store.get(
            builder.scope, "doctor_authority", builder.scope.doctor_id
        )
        authority = from_record(authority_row, DoctorAuthority) if authority_row else None
        amendment = detail.held_answer_amendment_ref
        if not (
            detail.held_answer
            and detail.held_answer_ready
            and detail.held_answer_consumed_at is None
            and amendment
            and authority
            and authority.approved
            and detail.held_answer_by == authority.subject
            and builder.store.get(builder.scope, amendment.entity_type, amendment.id)
        ):
            raise EffectsRejected("held_answer_not_ready")
        actor = detail.held_answer_by
        key = "patient_question_plan_updated"
        body = templates.render(key, patient.language)
    elif args.close_only:
        key = "patient_question_closed"
        body = templates.render(key, patient.language)
    else:
        answer = args.answer_text.strip()
        if not answer:
            raise EffectsRejected("answer_required")
        if len(answer) > POLICY.reply_max_chars:
            raise EffectsRejected(f"reply_cap_{POLICY.reply_max_chars}")
        context = output_context(builder.store, builder.scope, patient)
        order_sources = (
            *records(builder.store, builder.scope, "care_order_head"),
            *records(builder.store, builder.scope, "care_order"),
        )
        builder.command = builder.command.model_copy(
            update={
                "expected_versions": tuple(
                    dict.fromkeys(
                        (*builder.command.expected_versions, *(r.ref for r in order_sources))
                    )
                )
            }
        )
        if treatment_change(answer, context):
            held = mission.details.model_copy(
                update={
                    "held_answer": answer,
                    "held_answer_by": actor,
                    "held_answer_ready": False,
                    "held_answer_amendment_ref": None,
                    "held_answer_consumed_at": None,
                }
            )
            changed = revise(mission, builder.now, details=held)
            builder.put(to_record(changed, builder.scope))
            builder.audit(
                "QUESTION_ANSWER_HELD",
                keys.digest(builder.command.command_id),
                (to_record(changed, builder.scope).ref,),
            )
            patient_intent(
                builder,
                changed,
                profile,
                "patient_question_plan_pending",
                templates.render("patient_question_plan_pending", patient.language),
            )
            return "doctor_question_held"
        key = "patient_question_answered"
        body = templates.render(key, patient.language, answer=answer)
        if len(body) > POLICY.reply_max_chars:
            raise EffectsRejected(f"reply_cap_{POLICY.reply_max_chars}")
        verdict = validate_patient_output(answer, context=context, policy=SAFETY)
        if not verdict.ok:
            raise EffectsRejected(", ".join(dict.fromkeys(v.reason for v in verdict.violations)))
        whole = validate_patient_output(body, context=context, policy=SAFETY)
        if not whole.ok:
            raise EffectsRejected(", ".join(dict.fromkeys(v.reason for v in whole.violations)))
    if args.close_only:
        event: ev.ObjectiveFulfilled | ev.DoctorCloseUnfulfilled = ev.DoctorCloseUnfulfilled(
            event_id=builder.command.command_id,
            actor_id=actor,
            reason="doctor_closed_question",
            open_incident=any(
                r.body.get("state") == "open"
                for r in records(builder.store, builder.scope, "incident")
            ),
        )
    else:
        event = ev.ObjectiveFulfilled(
            event_id=builder.command.command_id,
            fulfillment_event_id=builder.command.command_id,
            actor_kind="doctor",
            danger_flag=False,
            objective_received_at=builder.now,
            predicate_result=PredicateResult(
                satisfied=True,
                evaluated_at=builder.now,
                detail="Doctor answered through a confirmed plan amendment."
                if release
                else "Doctor answered.",
            ),
        )
    result = transition_mission(mission, event, builder.now, builder.policy.timing)
    if isinstance(result, ev.TransitionResult) and isinstance(result.aggregate, Mission):
        if mission.details.held_answer:
            result = result.model_copy(
                update={
                    "aggregate": result.aggregate.model_copy(
                        update={
                            "details": mission.details.model_copy(
                                update={
                                    "held_answer_ready": False,
                                    "held_answer_consumed_at": builder.now,
                                }
                            ),
                        }
                    )
                }
            )
    builder.add(result)
    if not isinstance(result, ev.TransitionResult) or not isinstance(result.aggregate, Mission):
        raise EffectsRejected("question_transition_refused")
    for review in obligations:
        builder.add(
            transition_review(
                review,
                ev.ResolveReview(
                    event_id=builder.command.command_id + ":review:" + review.id,
                    actor_id=actor,
                    action=ReviewAction.close if args.close_only else ReviewAction.answer,
                    expected_source_version=review.source_version,
                    reason="doctor_closed_question"
                    if args.close_only
                    else "doctor_answered_question",
                ),
                builder.now,
                builder.policy.timing,
            )
        )
    if not release and not args.close_only:
        from sanad.concierge.reuse import issue

        issue(builder, mission, result.aggregate, args.answer_text.strip(), args.listing_token)
    pending = patient_intent(builder, result.aggregate, profile, key, body)
    return "doctor_question_delivery_pending" if pending else "doctor_question_recorded"


def flag_amended_answers(
    store: "Store",
    proposal: "Proposal",
    models: tuple[BaseModel, ...],
    at: datetime,
) -> tuple[Mission, ...]:
    if not proposal.selected_patient_id or not any(
        a.head_version and a.old and not a.noop and not proposal.blocked(a.item)
        for a in proposal.amendments
    ):
        return ()
    scope = PatientScope(doctor_id=proposal.doctor_id, patient_id=proposal.selected_patient_id)
    amendments = tuple(
        m for m in models if isinstance(m, CareOrderVersion) and m.supersedes_version
    )
    if not amendments:
        return ()
    changed = []
    for row in records(store, scope, "mission"):
        m = from_record(row, Mission)
        if (
            isinstance(m.details, QuestionDetails)
            and m.state not in TERMINAL_STATES
            and (m.details.held_answer and not m.details.held_answer_ready and m.work_clock)
            and m.details.held_answer_consumed_at is None
        ):
            changed.append(
                revise(
                    m,
                    at,
                    details=m.details.model_copy(
                        update={
                            "held_answer_ready": True,
                            "held_answer_amendment_ref": to_record(amendments[0], scope).ref,
                        }
                    ),
                    work_clock=m.work_clock.model_copy(update={"next_action_at": at}),
                )
            )
    return tuple(changed)


def prepare_task(builder: CommitBuilder, profile: PatientProfile) -> str:
    row = builder.store.get(
        builder.scope, "outbound_intent", str(builder.command.payload.get("intent_id", ""))
    )
    if not row:
        raise EffectsRejected("task_card_missing")
    intent = from_record(row, OutboundIntent)
    token = str(builder.command.payload.get("token_hash", ""))
    accept = builder.command.payload.get("type") == "AcceptTask"
    action_id = keys.digest("task-action:" + intent.id)
    if (
        not token
        or token != (intent.task_accept_token_hash if accept else intent.task_reopen_token_hash)
        or builder.store.get(builder.scope, "audit_event", action_id)
        or not intent.task_action_expires_at
        or intent.task_action_expires_at <= builder.now
        or intent.recipient_auth_epoch_seen != builder.command.principal.auth_epoch
        or intent.notification_purpose != "DONE:FULFILLMENT"
    ):
        raise EffectsRejected("task_card_stale")
    ref = next((r for r in intent.source_versions if r.entity_type == "mission"), None)
    mission_row = builder.store.get(builder.scope, "mission", ref.id) if ref else None
    if not mission_row or mission_row.ref != ref:
        raise EffectsRejected("task_card_stale")
    mission = from_record(mission_row, Mission)
    if (
        not isinstance(mission.details, TaskDetails)
        or mission.state != "fulfilled"
        or mission.fulfillment_validity != "valid"
    ):
        raise EffectsRejected("task_not_fulfilled")
    builder.command = builder.command.model_copy(
        update={
            "expected_versions": tuple(
                dict.fromkeys(
                    (
                        *builder.command.expected_versions,
                        row.ref,
                        mission_row.ref,
                    )
                )
            )
        }
    )
    if accept:
        # This immutable audit fact records the doctor's action, not new patient clinical data.
        # Its stable card key consumes both buttons atomically, even after delivery.
        builder.audit("DoctorAccepted", action_id, (mission_row.ref,))
        return "doctor_task_accepted"
    at = builder.now + POLICY.task_reopen_offset
    result = transition_mission(
        mission,
        ev.DoctorReopen(
            event_id=builder.command.command_id,
            actor_id=builder.command.principal.subject,
            reason="doctor_not_satisfied",
            timing=ExplicitTiming(
                instant=at,
                timezone=mission.timezone,
                original_expression="doctor_not_satisfied: task_reopen_offset",
            ),
            consent_active=profile.consent_active and profile.binding_active,
            new_objective_predicate=mission.objective_predicate,
            new_order_refs=mission.order_refs,
            current_active_order_refs=tuple(
                r.ref
                for r in records(builder.store, builder.scope, "care_order")
                if r.body.get("status") == "active"
            ),
        ),
        builder.now,
        builder.policy.timing,
    )
    builder.add(result)
    if not isinstance(result, ev.TransitionResult) or not isinstance(result.aggregate, Mission):
        raise EffectsRejected("task_reopen_refused")
    builder.audit(
        "DoctorTaskReopened", action_id, (to_record(result.aggregate, builder.scope).ref,)
    )
    from sanad.contact.scheduler import prime

    builder.puts[("mission", mission.id)] = to_record(
        prime(result.aggregate, builder.now), builder.scope
    )
    patient_row = builder.store.get(builder.scope, "patient", builder.scope.patient_id)
    assert patient_row
    patient = from_record(patient_row, Patient)
    key = "patient_task_reopened"
    body = templates.render(
        key,
        patient.language,
        instruction=mission.details.instruction,
        due_local=format_local(at, patient.timezone),
    )
    patient_intent(builder, result.aggregate, profile, key, body)
    return "doctor_task_reopened"


def owned_questions(store: "Store", doctor_id: str) -> list[tuple[Patient, Mission]]:
    values = [
        (p, from_record(r, Mission))
        for p in panel(store, TenantScope(doctor_id=doctor_id))
        for r in records(store, p.scope, "mission")
        if r.body.get("kind") == "QUESTION" and r.body.get("state") not in TERMINAL_STATES
    ]
    return sorted(values, key=lambda pair: (pair[1].created_at, pair[1].id))


def maintain_question_review(builder: CommitBuilder, before: Mission, changed: Mission) -> None:
    from sanad.domain import ReviewKind

    open_reviews = reviews(builder, before)
    for review in open_reviews:
        assert review.work_clock
        generation = review.last_work_generation + 1
        revised = revise(
            review,
            builder.now,
            review_at=changed.due_at,
            last_work_generation=generation,
            work_clock=review.work_clock.model_copy(
                update={
                    "next_action_at": changed.due_at,
                    "work_generation": generation,
                }
            ),
        )
        builder.put(to_record(revised, builder.scope))
        builder.audit(
            "QUESTION_REVIEW_EXTENDED",
            keys.digest(builder.command.command_id + ":" + review.id),
            (to_record(revised, builder.scope).ref,),
        )
    if not open_reviews:
        builder.effect(
            ev.CreateReview(
                event_id=builder.command.command_id,
                source_type="mission",
                source_id=changed.id,
                source_version=changed.version,
                review_kind=ReviewKind.question_answer,
                source_mission_id=changed.id,
                owner_doctor_id=changed.doctor_id,
                patient_id=changed.patient_id,
                review_at=changed.due_at,
            ),
            changed,
        )


def listing_text(
    store: "Store",
    selected: list[tuple[Patient, Mission]],
    doctor: Doctor,
    at: datetime,
    page: int,
    pages: int,
    answers: tuple["ReusableAnswer", ...] | None = None,
) -> str:
    from sanad.concierge.reuse import answer_set

    answers = answer_set(store, doctor.id) if answers is None else answers
    lines = [
        templates.render("doctor_questions_page", doctor.language, page=str(page), pages=str(pages))
    ]
    for i, (patient, mission) in enumerate(selected, 1):
        orders = active_orders(store, patient.scope)
        from sanad.concierge.plan import order_line

        plan = (
            order_line(orders[0], doctor.language)
            if orders
            else templates.render("doctor_questions_no_plan", doctor.language)
        )
        assert isinstance(mission.details, QuestionDetails)
        age = max(0, int((at - mission.created_at).total_seconds() // 3600))
        age_text = templates.render("doctor_questions_age", doctor.language, hours=str(age))
        lines.append(
            f"{i}. {patient.display_name[:40]} · {age_text}\n{plan[:55]}\n"
            f"{mission.details.question_text[:95]}"
        )
        from sanad.concierge.reuse import proposal_line

        lines.append(
            proposal_line(store, mission, i, answers=answers) + f" · /defer {i} · /close {i}"
        )
        if mission.details.held_answer:
            lines.append(
                templates.render(
                    "doctor_questions_held",
                    doctor.language,
                    answer=mission.details.held_answer[:80],
                )
            )
    if not selected:
        lines.append(templates.render("doctor_questions_empty", doctor.language))
    lines.append(templates.render("doctor_questions_usage", doctor.language))
    return "\n\n".join(lines)


def doctor_command(
    turn: "ScribeTurn",
    receipt: "InboundReceipt",
    actor: Principal,
    claim: "Claim",
    doctor: Doctor,
    command: str,
    argument: str,
) -> "RouteResult":
    from sanad.channels.telegram.router import RouteResult

    if command in {"/inbox", "/resolve"}:
        from sanad.concierge.inbox import doctor_command as inbox_command

        return inbox_command(turn, receipt, actor, claim, doctor, command, argument)
    # The listing snapshot belongs to the doctor's private Scribe conversation.
    # Its token/targets travel atomically with that session's saved listing reply.
    from sanad.concierge import reuse

    listings = reuse.listings(turn.repo.store, doctor.id)
    if command == "/reuse":
        from sanad.liaison.records import ReuseOffer

        offer = next(
            (
                from_record(r, ReuseOffer)
                for r in records(turn.repo.store, doctor.scope, "reuse_offer")
                if r.body.get("consumed_by") == "question-reuse:" + receipt.id
            ),
            None,
        )
        offer = offer or reuse.newest_offer(
            turn.repo.store, doctor.id, turn.repo.clock(), actor.auth_epoch
        )
        if argument or offer is None:
            return turn._reply(
                receipt,
                actor,
                claim,
                "doctor_question_list_stale",
                "invalid_input",
                text="No available answer to reuse. Answer a question first.",
            )
        reuse_result = turn.runtime.steward.handle(
            CommandEnvelope(
                command_id="question-reuse:" + receipt.id,
                scope=PatientScope(doctor_id=doctor.id, patient_id=offer.patient_id),
                principal=actor,
                requested_at=turn.repo.clock(),
                payload={"type": "ReuseAnswer", "offer_id": offer.id},
            )
        )
        turn.checkpoint("doctor_clinical_committed")
        return reuse_reply(turn, receipt, actor, claim, offer, reuse_result.status)
    if command == "/questions":
        if argument and (not argument.isdigit() or int(argument) < 1):
            return turn._reply(
                receipt,
                actor,
                claim,
                "doctor_questions_usage",
                "invalid_input",
                text=templates.render("doctor_questions_usage", doctor.language),
            )
        page = int(argument or "1")
        values = owned_questions(turn.repo.store, doctor.id)
        page_size = 6
        pages = max(1, (len(values) + page_size - 1) // page_size)
        if page > pages:
            return turn._reply(
                receipt,
                actor,
                claim,
                "doctor_questions_usage",
                "invalid_page",
                text=templates.render("doctor_questions_usage", doctor.language),
            )
        selected = values[(page - 1) * page_size : page * page_size]
        answers = reuse.answer_set(turn.repo.store, doctor.id)
        intent = turn.repo.intent(
            doctor,
            "doctor_questions",
            {
                "text": listing_text(
                    turn.repo.store, selected, doctor, turn.repo.clock(), page, pages, answers
                ),
            },
            "questions:" + receipt.id,
            sequence=1 + max((i.conversation_sequence for i in listings), default=0),
        )
        intent = intent.model_copy(
            update={
                "question_listing_token": keys.digest(token_urlsafe(32)),
                "question_listing_targets": tuple((p.id, m.id) for p, m in selected),
                "question_bindings": tuple(
                    reuse.binding(turn.repo.store, m, answers) for _, m in selected
                ),
                "question_listing_expires_at": turn.repo.clock() + POLICY.question_list_ttl,
            }
        )
        result = turn.repo.commit(
            actor, "ScribeReply", "questions:" + receipt.id, intents=(intent,), claim=claim
        )
        return RouteResult(route="doctor", status=result.status, template_id="doctor_questions")
    parts = argument.split(maxsplit=1)
    if (
        not parts
        or not parts[0].isdigit()
        or int(parts[0]) < 1
        or (
            command == "/answer"
            and len(parts) != 2
            or command in {"/close", "/send", "/defer"}
            and len(parts) != 1
        )
    ):
        return turn._reply(
            receipt,
            actor,
            claim,
            "doctor_questions_usage",
            "invalid_input",
            text=templates.render("doctor_questions_usage", doctor.language),
        )
    command_id = "question-doctor:" + receipt.id
    target: tuple[str, str] | None = None
    # Reconstruct the committed clinical target before checking a possibly expired/new listing.
    for patient in panel(turn.repo.store, doctor.scope):
        for event in records(turn.repo.store, patient.scope, "audit_event"):
            if event.body.get("command_id") == command_id:
                refs = event.body.get("aggregate_refs", [])
                if isinstance(refs, list):
                    ref = next(
                        (
                            r
                            for r in refs
                            if isinstance(r, dict) and r.get("entity_type") == "mission"
                        ),
                        None,
                    )
                    if ref:
                        target = (patient.id, str(ref["id"]))
                        break
        if target:
            break
    latest = (
        max(listings, key=lambda i: (i.created_at, i.conversation_sequence, i.id))
        if listings
        else None
    )
    if target is None and listings:
        latest = max(listings, key=lambda i: (i.created_at, i.conversation_sequence, i.id))
        n = int(parts[0]) - 1
        if (
            latest.question_listing_expires_at
            and latest.question_listing_expires_at > turn.repo.clock()
            and (
                latest.recipient_auth_epoch_seen == actor.auth_epoch
                and n < len(latest.question_listing_targets)
            )
        ):
            target = latest.question_listing_targets[n]
    if target is None:
        return turn._reply(
            receipt,
            actor,
            claim,
            "doctor_question_list_stale",
            "expired_listing",
            text=templates.render("doctor_question_list_stale", doctor.language),
        )
    args = AnswerQuestion(
        mission_id=target[1],
        answer_text=parts[1] if len(parts) > 1 else "",
        close_only=command == "/close",
        listing_token=latest.question_listing_token or "" if latest else "",
    )
    payload = {"type": "AnswerQuestion", **args.model_dump(mode="json")}
    if command in {"/send", "/defer"}:
        bound = (
            latest.question_bindings[int(parts[0]) - 1]
            if latest and int(parts[0]) <= len(latest.question_bindings)
            else None
        )
        payload = {
            "type": "DeferQuestion",
            "mission_id": target[1],
            "expected_version": bound.mission_version if bound else 0,
        }
        if command == "/send":
            payload = {
                "type": "SendQuestion",
                "mission_id": target[1],
                "listing_token": latest.question_listing_token if latest else "",
                "n": int(parts[0]),
                "mission_version": bound.mission_version if bound else 0,
                "reusable_id": bound.reusable_id if bound else None,
                "reusable_version": bound.reusable_version if bound else None,
            }
        # Recover the immutable committed payload, not a newly rendered proposal.
    for event in records(
        turn.repo.store, PatientScope(doctor_id=doctor.id, patient_id=target[0]), "audit_event"
    ):
        saved_payload = event.body.get("question_command")
        if event.body.get("command_id") == command_id and isinstance(saved_payload, dict):
            payload = saved_payload
            break
    result_command = turn.runtime.steward.handle(
        CommandEnvelope(
            command_id=command_id,
            scope=PatientScope(doctor_id=doctor.id, patient_id=target[0]),
            principal=actor,
            requested_at=turn.repo.clock(),
            payload=payload,
        )
    )
    turn.checkpoint("doctor_clinical_committed")
    key = (
        result_command.reason_code
        if result_command.status == "accepted"
        else "doctor_question_refused"
    )
    key = key or "doctor_question_recorded"
    body = templates.render(
        key,
        doctor.language,
        **(
            {"reason": result_command.reason_code or result_command.status}
            if key == "doctor_question_refused"
            else {}
        ),
    )
    offer_ref = next(
        (r for r in result_command.resulting_versions if r.entity_type == "reuse_offer"), None
    )
    if offer_ref:
        from sanad.liaison.records import ReuseOffer

        offer_row = turn.repo.store.get(doctor.scope, "reuse_offer", offer_ref.id)
        if offer_row:
            offer = from_record(offer_row, ReuseOffer)
            body += "\n/reuse saves this answer for: " + offer.question_text[:160]
            intent = turn.repo.intent(
                doctor,
                key,
                {
                    "text": body,
                    "reply_markup": {
                        "inline_keyboard": [
                            [
                                {
                                    "text": "Reuse this answer for similar questions",
                                    "callback_data": offer.id,
                                }
                            ]
                        ]
                    },
                },
                "reply:" + receipt.id,
            )
            result = turn.repo.commit(
                actor,
                "ScribeReply",
                "reply:" + receipt.id,
                intents=(intent,),
                claim=claim,
                reason=result_command.status,
            )
            return RouteResult(route="doctor", status=result.status, template_id=key)
    return turn._reply(receipt, actor, claim, key, result_command.status, text=body)


def task_route(
    turn: "ScribeTurn", receipt: "InboundReceipt", auth: "Authorization"
) -> "RouteResult | None":
    from sanad.concierge.inbox import callback as inbox_callback

    inbox_result = inbox_callback(turn, receipt, auth)
    if inbox_result is not None:
        return inbox_result
    if receipt.kind != "callback" or auth.principal.actor_kind != "doctor":
        return None
    token = str((receipt.payload or {}).get("callback_token_hash", ""))
    from sanad.concierge.reuse import reuse_callback

    reused = reuse_callback(turn, receipt, auth, token)
    if reused is not None:
        return reused
    match = next(
        (
            (p, from_record(r, OutboundIntent))
            for p in panel(turn.repo.store, TenantScope(doctor_id=auth.principal.doctor_id or ""))
            for r in records(turn.repo.store, p.scope, "outbound_intent")
            if token
            and token
            in {r.body.get("task_accept_token_hash"), r.body.get("task_reopen_token_hash")}
        ),
        None,
    )
    if not match:
        return None
    claimed = turn._claim(receipt)
    if not claimed:
        from sanad.channels.telegram.router import RouteResult

        return RouteResult(route="busy", status="processing")
    receipt, claim = claimed
    patient, intent = match
    actor = auth.principal
    doctor = turn.claims.doctor(actor)
    if not doctor:
        turn.runtime.accounts.finish_receipt(receipt, claim, result_code="authority_changed")
        from sanad.channels.telegram.router import RouteResult

        return RouteResult(route="refused", status="authority_changed")
    result = turn.runtime.steward.handle(
        CommandEnvelope(
            command_id="task-doctor:" + receipt.id,
            scope=patient.scope,
            principal=actor,
            requested_at=turn.repo.clock(),
            payload={
                "type": "AcceptTask" if token == intent.task_accept_token_hash else "ReopenTask",
                "intent_id": intent.id,
                "token_hash": token,
            },
        )
    )
    turn.checkpoint("doctor_clinical_committed")
    key = result.reason_code if result.status == "accepted" else "doctor_task_stale"
    key = key or "doctor_task_stale"
    turn.runtime.transport.answer_callback(
        str((receipt.payload or {}).get("callback_query_id", "")), ""
    )
    return turn._reply(
        receipt, actor, claim, key, result.status, text=templates.render(key, doctor.language)
    )


def reuse_reply(
    turn: "ScribeTurn",
    receipt: "InboundReceipt",
    actor: Principal,
    claim: "Claim",
    offer: "ReuseOffer",
    status: str,
) -> "RouteResult":
    patient = turn.repo.store.get(
        PatientScope(doctor_id=offer.doctor_id, patient_id=offer.patient_id),
        "patient",
        offer.patient_id,
    )
    name = str(patient.body.get("display_name", "")) if patient else ""
    body = (
        "Saved for similar questions"
        if status == "accepted"
        else "This reuse offer is no longer available"
    ) + f": {name} · {offer.question_text[:160]}"
    return turn._reply(
        receipt,
        actor,
        claim,
        "doctor_answer_reused" if status == "accepted" else "doctor_question_refused",
        status,
        text=body,
    )
