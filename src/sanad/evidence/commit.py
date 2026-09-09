"""Steward-owned evidence commands: one acceptance transaction with objective effects."""

from secrets import token_urlsafe

from pydantic import JsonValue

from sanad.concierge.records import PatientAction, ReportFactPayload
from sanad.domain import (
    AcceptedFactRef,
    CreateReview,
    Mission,
    ObservationRef,
    ReviewAction,
    ReviewKind,
    ReviewObligation,
    VersionRef,
    create_review,
    transition_mission,
    transition_review,
)
from sanad.domain import events as ev
from sanad.domain.language import default_language
from sanad.domain.predicates import PredicateResult
from sanad.evidence import associate, templates
from sanad.evidence.evaluate import completing_set, evaluate
from sanad.evidence.policy import POLICY
from sanad.scribe.records import ClinicalFact
from sanad.steward.apply import CommitBuilder, EffectsRejected, make_intent
from sanad.steward.types import records
from sanad.store import keys
from sanad.store.records import (
    Evidence,
    EvidenceAction,
    EvidenceHash,
    EvidenceHead,
    InboundReceipt,
    MediaWork,
    Patient,
    PatientMedia,
    canonical_json,
    from_record,
    to_record,
)


def load(builder: CommitBuilder, id: str) -> tuple[EvidenceHead, Evidence]:
    head_row = builder.store.get(builder.scope, "evidence_head", id)
    if not head_row:
        raise EffectsRejected("evidence_missing")
    head = from_record(head_row, EvidenceHead)
    row = builder.store.get(builder.scope, "evidence", f"{id}:{head.current_version}")
    if not row:
        raise EffectsRejected("evidence_version_missing")
    return head, from_record(row, Evidence)


def accepted(builder: CommitBuilder, mission_id: str) -> tuple[Evidence, ...]:
    return tuple(
        load(builder, row.id)[1]
        for row in records(builder.store, builder.scope, "evidence_head")
        if row.body.get("status") in {"accepted", "accepted_pending_identity"}
        and row.body.get("mission_id") == mission_id
    )


def emit(
    builder: CommitBuilder,
    key: str,
    fields: dict[str, str] | None = None,
    buttons: list[JsonValue] | None = None,
    *,
    audience: str = "patient",
    source_versions: tuple[VersionRef, ...] = (),
) -> None:
    from sanad.store.records import DoctorAuthority

    profile = builder.store.get_patient_profile(builder.scope)
    doctor_row = builder.store.get(builder.scope, "doctor_authority", builder.scope.doctor_id)
    patient_row = builder.store.get(builder.scope, "patient", builder.scope.patient_id)
    if not profile or not doctor_row or not patient_row:
        raise EffectsRejected("recipient_authority_missing")
    if audience == "patient" and not profile.routine_contact_enabled:
        return
    patient = from_record(patient_row, Patient)
    language = patient.language
    if audience == "doctor":
        from sanad.store.records import Doctor

        row = builder.store.get(builder.scope, "doctor", builder.scope.doctor_id)
        language = from_record(row, Doctor).language if row else default_language
    text = templates.render(key, language, **(fields or {}))
    if audience == "patient":
        from sanad.safety import validate_patient_output
        from sanad.safety.models import OutputContext
        from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT

        checked = validate_patient_output(
            text,
            context=OutputContext(
                mode="plan_explanation",
                language=patient.language,
                allowed_numbers=tuple((fields or {}).values()),
            ),
            policy=SAFETY_POLICY_V1_CARDIOLOGY_DRAFT,
        )
        if not checked.ok:
            # Retain an explicit review outcome without unsafe dynamic wording.
            key, text = (
                "patient_evidence_kept",
                templates.render("patient_evidence_kept", patient.language),
            )
    outgoing = make_intent(
        builder.scope,
        builder.command.command_id,
        source_versions,
        "solicited_reply",
        builder.command.command_id,
        builder.now,
        builder.policy,
        from_record(doctor_row, DoctorAuthority),
        profile,
        audience=audience,
        template_id=key,
    )
    payload: dict[str, JsonValue] = {"text": text}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    outgoing = outgoing.model_copy(
        update={"payload": payload, "payload_digest": keys.digest(canonical_json(payload).decode())}
    )
    builder.intents[outgoing.id] = to_record(outgoing, builder.scope)


def review(builder: CommitBuilder, evidence: Evidence) -> None:
    if any(
        r.body.get("source_type") == "evidence"
        and r.body.get("source_id") == evidence.evidence_id
        and r.body.get("state") != "resolved"
        for r in records(builder.store, builder.scope, "review")
    ):
        return
    builder.add(
        create_review(
            CreateReview(
                event_id=builder.command.command_id + ":association",
                source_type="evidence",
                source_id=evidence.evidence_id,
                source_version=evidence.version,
                review_kind=ReviewKind.evidence_association,
                owner_doctor_id=builder.scope.doctor_id,
                patient_id=builder.scope.patient_id,
                source_mission_id=evidence.mission_id,
                review_at=builder.now + POLICY.association_clarification,
            ),
            builder.now,
            builder.policy.timing,
        ),
        child=True,
    )


def resolve_reviews(builder: CommitBuilder, evidence: Evidence, *, rejected: bool = False) -> None:
    for row in records(builder.store, builder.scope, "review"):
        value = from_record(row, ReviewObligation)
        if value.review_kind != "evidence_association" or value.state == "resolved":
            continue
        if value.source_type != "evidence" or value.source_id != evidence.evidence_id:
            continue
        builder.add(
            transition_review(
                value,
                ev.ResolveReview(
                    event_id=builder.command.command_id + ":resolve:" + value.id,
                    action=ReviewAction.dispose if rejected else ReviewAction.associate,
                    expected_source_version=value.source_version,
                    actor_id=builder.command.principal.subject,
                    reason="Evidence rejected" if rejected else "Evidence association decided",
                ),
                builder.now,
                builder.policy.timing,
            ),
            child=True,
        )


def patient_buttons(
    builder: CommitBuilder,
    evidence: Evidence,
    missions: tuple[Mission, ...],
    *,
    identity: bool = False,
) -> list[JsonValue]:
    profile = builder.store.get_patient_profile(builder.scope)
    assert profile and profile.recipient_subject
    patient_row = builder.store.get(builder.scope, "patient", builder.scope.patient_id)
    assert patient_row
    language = from_record(patient_row, Patient).language
    choices = (
        [
            ("evidence_yes", templates.button("yes", language), None),
            ("evidence_no", templates.button("no", language), None),
        ]
        if identity
        else [("evidence_choose", f"{m.title} · {m.due_at.isoformat()}", m) for m in missions]
        + [("evidence_other", templates.button("other", language), None)]
    )
    keyboard: list[JsonValue] = []
    for action, label, mission in choices:
        raw = token_urlsafe(32)
        token = PatientAction.model_validate(
            {
                "id": keys.digest(raw),
                "scope": builder.scope,
                "created_at": builder.now,
                "updated_at": builder.now,
                "expires_at": builder.now + POLICY.association_clarification,
                "actor_subject": profile.recipient_subject,
                "action": action,
                "target_ref": to_record(mission, builder.scope).ref if mission else None,
                "evidence_id": evidence.evidence_id,
                "evidence_version": evidence.version,
                "source_receipt_id": evidence.observation_id,
                "delivery_epoch": profile.delivery_epoch,
                "binding_epoch": profile.binding_epoch,
                "consent_version": profile.consent_version,
            }
        )
        builder.put(to_record(token, builder.scope))
        keyboard.append([{"text": label, "callback_data": raw}])
    return keyboard


def prepare(builder: CommitBuilder) -> None:
    """Only Steward.handle calls this after the shared authority/fence checks."""
    command, store, scope, now = builder.command, builder.store, builder.scope, builder.now
    action = command.payload.get("action")
    if action == "patient_stale":
        emit(builder, "patient_evidence_stale")
        return
    if action == "record":
        candidate = Evidence.model_validate(command.payload.get("candidate"))
        work_row = store.get(scope, "media_work", keys.digest(candidate.observation_id))
        if not work_row:
            raise EffectsRejected("media_missing")
        work = from_record(work_row, MediaWork)
        if (
            candidate.scope != scope
            or candidate.version != 1
            or not work.transcript_ref
            or candidate.content_hash != work.byte_hash
        ):
            raise EffectsRejected("evidence_source")
        if store.get(scope, "evidence_hash", candidate.content_hash):
            raise EffectsRejected("duplicate_content")
        builder.put(to_record(candidate, scope))
        builder.put(
            to_record(
                EvidenceHead(
                    id=candidate.evidence_id,
                    evidence_id=candidate.evidence_id,
                    scope=scope,
                    current_version=1,
                    status="candidate",
                    mission_id=candidate.mission_id,
                    created_at=now,
                    updated_at=now,
                ),
                scope,
            )
        )
        builder.put(
            to_record(
                EvidenceHash(
                    id=candidate.content_hash,
                    evidence_id=candidate.evidence_id,
                    scope=scope,
                    created_at=now,
                    updated_at=now,
                ),
                scope,
            )
        )
        builder.put(
            to_record(
                PatientMedia(
                    id=candidate.media_id,
                    scope=scope,
                    media_scope=scope,
                    media_work_id=work.id,
                    source_receipt_id=work.receipt_id,
                    kind="lab"
                    if candidate.category == "lab_result"
                    else "prescription"
                    if candidate.category in {"prescription", "medication_list"}
                    else "other",
                    mime=work.mime or "image/png",
                    created_at=now,
                    updated_at=now,
                ),
                scope,
            )
        )
        builder.audit(
            "EVIDENCE_RECEIVED", keys.digest(command.command_id), (to_record(candidate, scope).ref,)
        )
        return
    if action == "duplicate":
        emit(builder, "patient_evidence_duplicate")
        return
    patient_row = store.get(scope, "patient", scope.patient_id)
    assert patient_row
    head, previous = load(builder, str(command.payload.get("evidence_id", "")))
    if command.payload.get("evidence_version") != previous.version:
        raise EffectsRejected("evidence_stale")
    if action == "confirm_identity" and (
        command.principal.actor_kind != "doctor"
        or previous.association_state != "accepted_pending_identity"
        or not previous.identity_pending
        or command.payload.get("mission_id") not in {None, previous.mission_id}
    ):
        raise EffectsRejected("identity_confirmation_not_pending")
    if action == "doctor_card":
        doctor_card(builder, head, previous)
        return
    answering_rejection = (
        head.status == "rejected"
        and previous.identity_pending
        and action in {"patient_yes", "patient_no"}
    )
    if (head.status in {"rejected", "superseded"} and not answering_rejection) or (
        head.status in {"accepted", "accepted_pending_identity"}
        and action not in {"associate", "reject", "accept", "confirm_identity"}
    ):
        raise EffectsRejected("evidence_already_decided")
    if head.status == "accepted" and previous.mission_id and action == "reject":
        target = store.get(scope, "mission", previous.mission_id)
        if target and target.body.get("state") == "fulfilled":
            raise EffectsRejected("correction_requires_slice19")
    raw_token = command.payload.get("token_hash")
    if raw_token:
        consume(builder, previous, str(raw_token))
    missions = tuple(from_record(row, Mission) for row in records(store, scope, "mission"))
    receipt_row = store.get(scope, "inbound_receipt", previous.observation_id)
    if not receipt_row:
        raise EffectsRejected("receipt_missing")
    receipt = from_record(receipt_row, InboundReceipt)
    chosen, provenance, plausible = associate.choose(
        missions, previous, str((receipt.payload or {}).get("text", ""))
    )
    if action in {"associate", "accept", "patient_choose", "confirm_identity"}:
        mission_id = command.payload.get("mission_id") or previous.mission_id
        chosen = next((m for m in associate.open_missions(missions) if m.id == mission_id), None)
        if not chosen:
            raise EffectsRejected("mission_missing")
        provenance = "patient_choice" if action == "patient_choose" else "doctor_choice"
    changes: dict[str, object] = {
        "id": f"{head.id}:{previous.version + 1}",
        "version": previous.version + 1,
        "updated_at": now,
        "mission_id": chosen.id if chosen else None,
        "patient_match_provenance": provenance,
        "candidate_mission_ids": tuple(m.id for m in plausible),
        "association_state": "candidate",
        "accepted_by": None,
        "accepted_at": None,
    }
    evidence = Evidence.model_validate(previous.model_dump() | changes)
    if action == "confirm_identity":
        evidence = evidence.model_copy(update={"flags": (*evidence.flags, "identity_confirmed")})
    keyboard: list[JsonValue] = []
    key, fields = "patient_evidence_kept", {}
    if action in {"reject", "patient_no"}:
        evidence = Evidence.model_validate(
            evidence.model_dump()
            | {
                "association_state": "rejected",
                "mission_id": None,
                "candidate_mission_ids": (),
                "accepted_by": None,
                "accepted_at": None,
                "rejection_reason": command.payload.get("reason")
                or "Patient says this is not their document",
            }
        )
        resolve_reviews(builder, previous, rejected=True)
        if action == "reject" and previous.association_state == "accepted_pending_identity":
            key = "patient_evidence_name_check"
            keyboard = patient_buttons(builder, evidence, (), identity=True)
    elif action in {"patient_yes", "patient_other"}:
        evidence = Evidence.model_validate(
            evidence.model_dump()
            | {
                "association_state": "candidate",
                "patient_match_provenance": "patient_choice",
                "mission_id": None if answering_rejection else evidence.mission_id,
            }
        )
        review(builder, evidence)
    elif "one_document_per_photo" in evidence.flags:
        key = "patient_evidence_one_per_photo"
        evidence = evidence.model_copy(
            update={
                "required_predicate_results": (
                    PredicateResult(
                        satisfied=False,
                        missing=("one_document_per_photo",),
                        detail="One document per photo required.",
                        evaluated_at=now,
                    ),
                )
            }
        )
        review(builder, evidence)
    elif "identity_mismatch" in evidence.flags and provenance != "doctor_choice":
        key = "patient_evidence_name_check"
        keyboard = patient_buttons(builder, evidence, plausible, identity=True)
        review(builder, evidence)
    elif evidence.category == "monitor_screen":
        from sanad.monitor.evidence import prepare as prepare_monitor

        evidence, key, monitor_fields, keyboard = prepare_monitor(
            builder, evidence, chosen, plausible
        )
        fields.update(monitor_fields)
    elif chosen is None:
        if len(plausible) > 1 and evidence.category != "other":
            key = "patient_evidence_which"
            keyboard = patient_buttons(builder, evidence, plausible)
        else:
            closed = associate.closed_match(missions, evidence)
            if closed:
                late = transition_mission(
                    closed,
                    ev.LateInputRecorded(
                        event_id=command.command_id + ":late",
                        observation_ref=ObservationRef(observation_id=evidence.observation_id),
                    ),
                    now,
                    builder.policy.timing,
                )
                if isinstance(late, ev.TransitionResult):
                    # The observation is retained by the domain; the evidence-owned
                    # obligation supplies the single matching association action.
                    late = late.model_copy(
                        update={
                            "effects": tuple(
                                e for e in late.effects if not isinstance(e, ev.CreateReview)
                            )
                        }
                    )
                builder.add(late)
            evidence = evidence.model_copy(update={"association_state": "unmatched"})
        review(builder, evidence)
    else:
        if action == "accept":
            if (
                chosen.objective_predicate.kind != "evidence"
                or chosen.objective_predicate.evaluator != "task_evidence"
            ):
                raise EffectsRejected("task_acceptance_only")
            evidence = evidence.model_copy(update={"flags": (*evidence.flags, "doctor_accepted")})
        from sanad.evidence.deadline import before_fulfillment

        chosen = before_fulfillment(builder, chosen)
        prior = accepted(builder, chosen.id)
        predicate = evaluate(chosen, evidence, prior, now)
        evidence = evidence.model_copy(update={"required_predicate_results": (predicate,)})
        if (
            predicate.missing in {("doctor_acceptance",), ("readable_document",)}
            or "document_instructions" in evidence.flags
            or evidence.category == "other"
            and provenance != "doctor_choice"
        ):
            review(builder, evidence)
        else:
            evidence = Evidence.model_validate(
                evidence.model_dump()
                | {
                    "association_state": "accepted_pending_identity"
                    if evidence.identity_pending
                    else "accepted",
                    "accepted_by": command.principal.subject,
                    "accepted_at": now,
                }
            )
            if predicate.missing == ("verification",) or evidence.identity_pending:
                review(builder, evidence)
            group = completing_set(chosen, evidence, prior, now) if predicate.satisfied else ()
            ready = predicate.satisfied and not any(e.identity_pending for e in group)
            key = (
                "patient_evidence_accepted"
                if ready
                else "patient_evidence_kept"
                if predicate.satisfied
                else "patient_evidence_partial"
            )
            fields = (
                {"title": chosen.title}
                if ready
                else {}
                if predicate.satisfied
                else {
                    "missing": templates.missing_text(
                        predicate.missing,
                        from_record(patient_row, Patient).language,
                    )
                }
            )
            if ready:
                refs = tuple(
                    AcceptedFactRef(fact_kind="evidence", fact_id=e.evidence_id, version=e.version)
                    for e in group
                )
                danger = any(e.incident_ids for e in group)
                event = ev.ObjectiveFulfilled(
                    event_id=command.command_id,
                    predicate_result=predicate,
                    objective_received_at=max(e.provenance.received_at for e in group),
                    evidence_refs=refs,
                    fulfillment_event_id=command.command_id,
                    danger_flag=danger,
                    actor_kind="system",
                )
                result = transition_mission(chosen, event, now, builder.policy.timing)
                if danger and isinstance(result, ev.TransitionResult):
                    result = result.model_copy(
                        update={
                            "effects": tuple(
                                e for e in result.effects if not isinstance(e, ev.EmitIntent)
                            )
                        }
                    )
                builder.add(result)
                if chosen.latest_deadline_notice_event_id:
                    builder.audit(
                        "DEADLINE_DISPOSITION_UPDATED",
                        keys.digest(command.command_id + ":deadline_disposition"),
                        (to_record(evidence, scope).ref,),
                        (
                            VersionRef(
                                entity_type="audit_event",
                                id=chosen.latest_deadline_notice_event_id,
                                version=1,
                            ),
                        ),
                    )
        if (
            provenance == "doctor_choice"
            and not evidence.identity_pending
            and not (action == "confirm_identity" and predicate.missing == ("verification",))
        ):
            resolve_reviews(builder, previous)
    builder.put(to_record(evidence, scope))
    next_head = EvidenceHead.model_validate(
        head.model_dump()
        | {
            "version": head.version + 1,
            "updated_at": now,
            "current_version": evidence.version,
            "status": evidence.association_state
            if evidence.association_state in {"accepted", "accepted_pending_identity", "rejected"}
            else "candidate",
            "mission_id": evidence.mission_id,
        }
    )
    builder.put(to_record(next_head, scope))
    builder.audit(
        "EVIDENCE_" + evidence.association_state.upper(),
        keys.digest(command.command_id + ":evidence"),
        (to_record(evidence, scope).ref,),
    )
    emit(builder, key, fields, keyboard)
    if evidence.association_state == "accepted_pending_identity":
        doctor_card(builder, next_head, evidence)


def record_monitor(builder: CommitBuilder, evidence: Evidence) -> None:
    scope, now, command = builder.scope, builder.now, builder.command
    from sanad.concierge.reports import reading
    from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT

    text = "\n".join(
        " ".join(v for v in (r.item.name, r.item.value, r.item.unit) if v)
        for r in evidence.readers[0].items
    )
    values = reading(text, SAFETY_POLICY_V1_CARDIOLOGY_DRAFT).values
    if values:
        builder.put(
            to_record(
                ClinicalFact(
                    id=keys.digest(evidence.evidence_id + ":readings"),
                    scope=scope,
                    created_at=now,
                    updated_at=now,
                    category="patient_report",
                    visibility="patient_released",
                    payload=ReportFactPayload(report_kind="reading", text=text, readings=values),
                    provenance=evidence.provenance,
                ),
                scope,
            )
        )
    builder.audit("MONITOR_EVIDENCE_AWAITS_SLOTS", keys.digest(command.command_id + ":monitor"), ())


def consume(builder: CommitBuilder, evidence: Evidence, token_hash: str) -> None:
    actor = builder.command.principal
    kind = "patient_action" if actor.actor_kind == "patient" else "evidence_action"
    row = builder.store.get(builder.scope, kind, token_hash)
    if not row:
        raise EffectsRejected("evidence_token_missing")
    token = (
        from_record(row, PatientAction)
        if kind == "patient_action"
        else from_record(row, EvidenceAction)
    )
    if (
        token.consumed_at
        or token.expires_at <= builder.now
        or token.evidence_id != evidence.evidence_id
        or token.evidence_version != evidence.version
        or token.actor_subject != actor.subject
    ):
        raise EffectsRejected("evidence_token_stale")
    action = builder.command.payload.get("action")
    expected_action = (
        token.action.replace("evidence_", "patient_")
        if isinstance(token, PatientAction)
        else token.action
    )
    if action != expected_action:
        raise EffectsRejected("evidence_token_action")
    if isinstance(token, PatientAction) and token.target_ref:
        target = builder.store.get(builder.scope, "mission", token.target_ref.id)
        if (
            not target
            or target.ref != token.target_ref
            or builder.command.payload.get("mission_id") != token.target_ref.id
        ):
            raise EffectsRejected("evidence_token_target")
    if isinstance(token, EvidenceAction) and token.mission_id != builder.command.payload.get(
        "mission_id"
    ):
        raise EffectsRejected("evidence_token_target")
    builder.put(
        to_record(
            token.model_copy(
                update={
                    "version": token.version + 1,
                    "updated_at": builder.now,
                    "consumed_at": builder.now,
                }
            ),
            builder.scope,
        )
    )


def doctor_card(builder: CommitBuilder, head: EvidenceHead, evidence: Evidence) -> None:
    from sanad.store.records import Doctor, DoctorAuthority

    missions = tuple(
        from_record(row, Mission) for row in records(builder.store, builder.scope, "mission")
    )
    keyboard: list[JsonValue] = []
    authority_row = builder.store.get(builder.scope, "doctor_authority", builder.scope.doctor_id)
    doctor_row = builder.store.get(builder.scope, "doctor", builder.scope.doctor_id)
    patient_row = builder.store.get(builder.scope, "patient", builder.scope.patient_id)
    assert authority_row and doctor_row and patient_row
    authority = from_record(authority_row, DoctorAuthority)
    language = from_record(doctor_row, Doctor).language
    patient = from_record(patient_row, Patient)
    choices: list[tuple[str, str | None, str]] = [
        ("associate", m.id, templates.button("associate", language) + ": " + m.title)
        for m in associate.open_missions(missions)
    ]
    if any(
        m.id == evidence.mission_id
        and m.objective_predicate.kind == "evidence"
        and m.objective_predicate.evaluator == "task_evidence"
        for m in missions
    ):
        choices.append(("accept", evidence.mission_id, templates.button("accept", language)))
    choices.append(("reject", None, templates.button("reject", language)))
    if evidence.association_state == "accepted_pending_identity":
        choices = [
            (
                "confirm_identity",
                evidence.mission_id,
                templates.button("confirm_identity", language),
            ),
            ("reject", None, templates.button("reject_identity", language)),
        ]
    for action, mission_id, label in choices:
        raw = token_urlsafe(32)
        token = EvidenceAction.model_validate(
            {
                "id": keys.digest(raw),
                "scope": builder.scope,
                "created_at": builder.now,
                "updated_at": builder.now,
                "evidence_id": head.id,
                "evidence_version": evidence.version,
                "actor_subject": authority.subject,
                "action": action,
                "mission_id": mission_id,
                "expires_at": builder.now + POLICY.association_clarification,
                "auth_epoch": authority.auth_epoch,
            }
        )
        builder.put(to_record(token, builder.scope))
        keyboard.append([{"text": label, "callback_data": raw}])
    emit(
        builder,
        "doctor_evidence_card",
        {
            "title": evidence.category,
            "details": " · ".join(
                (
                    str(evidence.printed_date or "—"),
                    evidence.association_state,
                    *evidence.flags,
                    *(m for p in evidence.required_predicate_results for m in p.missing),
                )
            )
            + (
                "\n"
                + templates.render(
                    "doctor_evidence_identity",
                    language,
                    patient=patient.display_name.replace("{", "(").replace("}", ")"),
                )
                if evidence.association_state == "accepted_pending_identity"
                else ""
            ),
        },
        keyboard,
        audience="doctor",
        source_versions=(to_record(head, builder.scope).ref,),
    )
