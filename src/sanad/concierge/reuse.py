"""Doctor-approved answer library and exact, fenced question actions."""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sanad.auth.service import revise
from sanad.concierge.education import _STOP
from sanad.concierge.text import normalized
from sanad.domain import Mission, PatientScope, TenantScope
from sanad.domain import events as ev
from sanad.domain.deadlines import ExplicitTiming
from sanad.domain.entities import DRAFT_POLICY_2026_09, TERMINAL_STATES, QuestionDetails
from sanad.domain.transitions import transition_mission
from sanad.liaison.records import QuestionBinding, ReusableAnswer, ReuseOffer
from sanad.steward.apply import CommitBuilder, EffectsRejected
from sanad.steward.types import StewardPolicy, records
from sanad.store import keys
from sanad.store.keys import IntakeScope
from sanad.store.records import (
    AuditEvent,
    CommitRequest,
    OutboundIntent,
    PatientProfile,
    from_record,
    to_record,
)

if TYPE_CHECKING:
    from sanad.channels.telegram.router import RouteResult
    from sanad.scribe.turn import ScribeTurn
    from sanad.store._base import Check, StoreBase
    from sanad.store.protocol import Store
    from sanad.store.records import Authorization, InboundReceipt

# OWNER_REVIEW_PENDING: proposal ranking only; automatic reuse is exact-only.
REUSE_PROPOSAL_MIN_OVERLAP = 0.6
COMMANDS = {"SendQuestion", "DeferQuestion", "ReuseAnswer"}


def answer_set(store: "Store", doctor_id: str) -> tuple[ReusableAnswer, ...]:
    return tuple(
        from_record(r, ReusableAnswer)
        for r in records(store, TenantScope(doctor_id=doctor_id), "reusable_answer")
    )


def best(
    store: "Store", doctor_id: str, question: str, *, exact: bool = False
) -> ReusableAnswer | None:
    return choose(answer_set(store, doctor_id), question, exact=exact)


def choose(
    answers: tuple[ReusableAnswer, ...], question: str, *, exact: bool = False
) -> ReusableAnswer | None:
    query = normalized(question)
    words = set(query.split()) - _STOP
    ranked = []
    for answer in answers:
        same = answer.normalized_question == query
        other = set(answer.normalized_question.split()) - _STOP
        score = len(words & other) / len(words | other) if words | other else 0.0
        if same or (not exact and score >= REUSE_PROPOSAL_MIN_OVERLAP):
            ranked.append((same, score, answer.created_at, answer.id, answer))
    return max(ranked, key=lambda r: r[:4])[-1] if ranked else None


def binding(
    store: "Store", mission: Mission, answers: tuple[ReusableAnswer, ...] | None = None
) -> QuestionBinding:
    assert isinstance(mission.details, QuestionDetails)
    answer = (
        best(store, mission.doctor_id, mission.details.question_text)
        if answers is None
        else choose(answers, mission.details.question_text)
    )
    return QuestionBinding(
        patient_id=mission.patient_id,
        mission_id=mission.id,
        mission_version=mission.version,
        reusable_id=answer.id if answer else None,
        reusable_version=answer.version if answer else None,
    )


def proposal_line(
    store: "Store",
    mission: Mission,
    n: int,
    limit: int = 160,
    answers: tuple[ReusableAnswer, ...] | None = None,
) -> str:
    assert isinstance(mission.details, QuestionDetails)
    answer = (
        best(store, mission.doctor_id, mission.details.question_text)
        if answers is None
        else choose(answers, mission.details.question_text)
    )
    if answer is None:
        return f"No proposed reply; /answer {n} your text."
    text = answer.answer_text
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return f"Proposed reply: {text}\n/send {n}"


def listings(store: "Store", doctor_id: str) -> list[OutboundIntent]:
    from sanad.scribe.patients import panel

    tenant = TenantScope(doctor_id=doctor_id)
    scopes = [
        tenant,
        IntakeScope(doctor_id=doctor_id, intake_id="scribe"),
        *(p.scope for p in panel(store, tenant)),
    ]
    return [
        from_record(r, OutboundIntent)
        for scope in scopes
        for r in records(store, scope, "outbound_intent")
        if r.body.get("question_listing_token")
    ]


def newest_offer(
    store: "Store", doctor_id: str, now: datetime, epoch: int | None
) -> ReuseOffer | None:
    offers = [
        from_record(r, ReuseOffer)
        for r in records(store, TenantScope(doctor_id=doctor_id), "reuse_offer")
    ]
    return max(
        (
            o
            for o in offers
            if o.consumed_at is None and o.expires_at > now and o.auth_epoch == epoch
        ),
        key=lambda o: (o.created_at, o.id),
        default=None,
    )


def issue(builder: CommitBuilder, before: Mission, after: Mission, text: str, token: str) -> None:
    assert isinstance(before.details, QuestionDetails)
    offer = ReuseOffer(
        id=keys.digest("reuse:" + builder.command.command_id),
        scope=TenantScope(doctor_id=before.doctor_id),
        doctor_id=before.doctor_id,
        auth_epoch=builder.command.principal.auth_epoch or 0,
        question_text=before.details.question_text,
        answer_text=text,
        mission_id=before.id,
        mission_version=after.version,
        patient_id=before.patient_id,
        listing_token=token,
        expires_at=builder.now + timedelta(hours=1),
        created_at=builder.now,
        updated_at=builder.now,
    )
    builder.put(to_record(offer, offer.scope))


def prepare(builder: CommitBuilder, profile: PatientProfile) -> str:
    from sanad.concierge.answer_command import (
        AnswerQuestion,
        maintain_question_review,
        prepare_answer,
    )

    payload = builder.command.payload
    kind = payload.get("type")
    tenant = TenantScope(doctor_id=builder.scope.doctor_id)
    if kind == "ReuseAnswer":
        if set(payload) != {"type", "offer_id"}:
            raise EffectsRejected("invalid_reuse_payload")
        row = builder.store.get(tenant, "reuse_offer", str(payload["offer_id"]))
        if row is None:
            raise EffectsRejected("reuse_offer_missing")
        offer = from_record(row, ReuseOffer)
        source_mission = builder.store.get(builder.scope, "mission", offer.mission_id)
        if (
            offer.patient_id != builder.scope.patient_id
            or offer.doctor_id != tenant.doctor_id
            or offer.auth_epoch != builder.command.principal.auth_epoch
            or offer.consumed_at is not None
            or offer.expires_at <= builder.now
            or source_mission is None
            or source_mission.version != offer.mission_version
            or source_mission.body.get("state") != "fulfilled"
        ):
            raise EffectsRejected("reuse_offer_stale")
        builder.command = builder.command.model_copy(
            update={
                "expected_versions": tuple(
                    dict.fromkeys((*builder.command.expected_versions, row.ref, source_mission.ref))
                )
            }
        )
        builder.put(
            to_record(
                revise(
                    offer,
                    builder.now,
                    consumed_by=builder.command.command_id,
                    consumed_at=builder.now,
                ),
                tenant,
            )
        )
        answer = ReusableAnswer(
            id=offer.id,
            scope=tenant,
            question_text=offer.question_text,
            normalized_question=normalized(offer.question_text),
            answer_text=offer.answer_text,
            source_mission_id=offer.mission_id,
            source_patient_id=offer.patient_id,
            created_at=builder.now,
            updated_at=builder.now,
        )
        builder.put(to_record(answer, tenant))
        builder.audit(
            "DOCTOR_ANSWER_REUSED",
            keys.digest(builder.command.command_id),
            (source_mission.ref, to_record(answer, tenant).ref),
        )
        return "doctor_answer_reused"
    row = builder.store.get(builder.scope, "mission", str(payload.get("mission_id", "")))
    if row is None:
        raise EffectsRejected("question_missing")
    mission = from_record(row, Mission)
    if mission.kind != "QUESTION" or mission.state in TERMINAL_STATES:
        raise EffectsRejected("question_not_open")
    if kind == "DeferQuestion":
        if (
            set(payload) != {"type", "mission_id", "expected_version"}
            or payload["expected_version"] != mission.version
        ):
            raise EffectsRejected("question_stale")
        result = transition_mission(
            mission,
            ev.DoctorExtend(
                event_id=builder.command.command_id,
                actor_id=builder.command.principal.subject,
                timing=ExplicitTiming(
                    instant=max(mission.due_at, builder.now + timedelta(hours=24)),
                    timezone=mission.timezone,
                    original_expression="defer by 24 hours",
                ),
                reason="doctor_deferred_question",
                consent_active=profile.binding_active and profile.consent_active,
            ),
            builder.now,
            builder.policy.timing,
        )
        builder.add(result)
        if not isinstance(result, ev.TransitionResult) or not isinstance(result.aggregate, Mission):
            raise EffectsRejected("question_defer_refused")
        maintain_question_review(builder, mission, result.aggregate)
        return "doctor_question_deferred"
    if set(payload) != {
        "type",
        "mission_id",
        "listing_token",
        "n",
        "mission_version",
        "reusable_id",
        "reusable_version",
    }:
        raise EffectsRejected("invalid_send_payload")
    latest = max(
        listings(builder.store, tenant.doctor_id),
        key=lambda i: (i.created_at, i.conversation_sequence, i.id),
        default=None,
    )
    n = payload["n"]
    if (
        latest is None
        or latest.question_listing_token != payload["listing_token"]
        or not latest.question_listing_expires_at
        or latest.question_listing_expires_at <= builder.now
        or latest.recipient_auth_epoch_seen != builder.command.principal.auth_epoch
        or type(n) is not int
        or n < 1
        or n > len(latest.question_bindings)
    ):
        raise EffectsRejected("question_listing_stale")
    bound = latest.question_bindings[n - 1]
    if (
        bound.patient_id != builder.scope.patient_id
        or bound.mission_id != mission.id
        or bound.mission_version != mission.version
        or payload["mission_version"] != mission.version
        or not bound.reusable_id
        or bound.reusable_id != payload["reusable_id"]
        or bound.reusable_version != payload["reusable_version"]
    ):
        raise EffectsRejected("question_proposal_stale")
    reuse_row = builder.store.get(tenant, "reusable_answer", bound.reusable_id)
    if reuse_row is None or reuse_row.version != bound.reusable_version:
        raise EffectsRejected("question_proposal_stale")
    answer = from_record(reuse_row, ReusableAnswer)
    builder.command = builder.command.model_copy(
        update={
            "expected_versions": tuple(
                dict.fromkeys((*builder.command.expected_versions, row.ref, reuse_row.ref))
            )
        }
    )
    reason = prepare_answer(
        builder,
        profile,
        args=AnswerQuestion(
            mission_id=mission.id,
            answer_text=answer.answer_text,
            listing_token=latest.question_listing_token or "",
        ),
    )
    builder.audit(
        "QUESTION_PROPOSED_REPLY_SENT",
        keys.digest(builder.command.command_id + ":reuse"),
        (row.ref, reuse_row.ref),
    )
    return reason


def guards(store: "StoreBase", request: CommitRequest, now: datetime) -> list["Check"] | None:
    from sanad.concierge.answer_command import prepare_answer
    from sanad.store._base import Check
    from sanad.store.corrections import doctor_checks

    command = request.command
    library = [
        r
        for r in (*request.puts, *request.events, *request.intents)
        if r.entity_type in {"reuse_offer", "reusable_answer"}
    ]
    kind = command.payload.get("type")
    if not library and kind not in COMMANDS | {"AnswerQuestion"}:
        return []
    if (
        kind not in COMMANDS | {"AnswerQuestion"}
        or not isinstance(command.scope, PatientScope)
        or not command.fence
        or command.worker
    ):
        return None
    checks = doctor_checks(store, command.principal, command.scope, now)
    if checks is None or not request.events:
        return None
    if store.lookup_command(command) is not None:
        return checks
    at = from_record(request.events[0], AuditEvent).accepted_at
    if not command.requested_at <= at <= now:
        return None
    profile = store.get_patient_profile(command.scope)
    if profile is None:
        return None
    builder = CommitBuilder(command.scope, command, at, StewardPolicy(DRAFT_POLICY_2026_09), store)
    try:
        reason = (
            prepare_answer(builder, profile)
            if kind == "AnswerQuestion"
            else prepare(builder, profile)
        )
        remember(builder)
        rebuilt = builder.finish().model_copy(update={"reason_code": reason})
    except ValueError:
        return None
    if rebuilt != request:
        return None
    if kind == "SendQuestion":
        latest = max(
            listings(store, command.scope.doctor_id),
            key=lambda i: (i.created_at, i.conversation_sequence, i.id),
        )
        row = to_record(latest, latest.scope)
        checks.append(Check(row.key, row.version))
    return checks


def reuse_callback(
    turn: "ScribeTurn", receipt: "InboundReceipt", auth: "Authorization", token: str
) -> "RouteResult | None":
    from sanad.channels.telegram.router import RouteResult
    from sanad.concierge.answer_command import reuse_reply
    from sanad.store.records import CommandEnvelope

    tenant = TenantScope(doctor_id=auth.principal.doctor_id or "")
    offer = next(
        (
            from_record(r, ReuseOffer)
            for r in records(turn.repo.store, tenant, "reuse_offer")
            if keys.digest(r.id) == token
        ),
        None,
    )
    if offer is None:
        return None
    claimed = turn._claim(receipt)
    if claimed is None:
        return RouteResult(route="busy", status="processing")
    receipt, claim = claimed
    result = turn.runtime.steward.handle(
        CommandEnvelope(
            command_id="question-reuse:" + receipt.id,
            scope=PatientScope(doctor_id=tenant.doctor_id, patient_id=offer.patient_id),
            principal=auth.principal,
            requested_at=turn.repo.clock(),
            payload={"type": "ReuseAnswer", "offer_id": offer.id},
        )
    )
    turn.checkpoint("doctor_clinical_committed")
    return reuse_reply(turn, receipt, auth.principal, claim, offer, result.status)


def remember(builder: CommitBuilder) -> None:
    """Retain the committed payload for doctor receipt recovery across newer listings."""
    id = keys.digest(builder.command.command_id + ":question-command")
    refs = tuple(r.ref for r in builder.puts.values() if r.entity_type == "mission")
    builder.audit("QUESTION_COMMAND_COMMITTED", id, refs)
    row = builder.events[id]
    event = from_record(row, AuditEvent).model_copy(
        update={"question_command": builder.command.payload}
    )
    builder.events[id] = to_record(event, builder.scope)
