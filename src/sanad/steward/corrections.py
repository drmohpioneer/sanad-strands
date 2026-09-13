"""Patient-scoped, replayable corrections with server-computed dependencies."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from sanad.concierge.records import ReportFactPayload
from sanad.corrections import Correction, FactHead
from sanad.domain import (
    AcceptedFactRef,
    FollowUpTask,
    FulfillmentValidity,
    Mission,
    PatientScope,
    ReviewObligation,
    TenantScope,
    VersionRef,
)
from sanad.domain import events as ev
from sanad.domain.boundaries import NonblankStr
from sanad.domain.entities import (
    MedicationDetails,
    MonitorDetails,
    MonitorReading,
    ReviewAction,
    ReviewKind,
)
from sanad.domain.predicates import PredicateResult
from sanad.domain.transitions import transition_followup, transition_mission, transition_review
from sanad.evidence.commit import accepted, load
from sanad.evidence.evaluate import evaluate
from sanad.monitor.executor import photo_readings
from sanad.monitor.slots import attach, coverage
from sanad.scribe.extract import LabRowCandidate
from sanad.scribe.records import ClinicalFact, FactPayload, LabFactPayload
from sanad.steward.apply import CommitBuilder, EffectsRejected, is_routine, make_intent
from sanad.steward.types import records
from sanad.store import keys
from sanad.store.protocol import Store
from sanad.store.records import (
    CommitRequest,
    Doctor,
    DoctorAuthority,
    Evidence,
    OutboundIntent,
    StoredRecord,
    canonical_json,
    from_record,
    to_record,
)

COMMANDS = {
    "CorrectRecord",
    "ValidateCorrection",
    "PreviewReopen",
    "ConfirmReopen",
    "CorrectionResponse",
    "AmendOrder",
}
MAX_WRITES = 24  # plus uniqueness markers, command, fence, authority and conditional reads


class CorrectBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["CorrectRecord"] = "CorrectRecord"
    predecessor: VersionRef
    operation: Literal["replace", "detach"]
    reason: NonblankStr = Field(max_length=1000)
    changes: dict[str, str | None] = Field(default_factory=dict, max_length=4)
    row_index: int | None = Field(default=None, ge=0)


class ValidateBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["ValidateCorrection"] = "ValidateCorrection"
    correction_id: NonblankStr
    mission_ref: VersionRef
    review_ref: VersionRef
    reason: NonblankStr = Field(max_length=1000)


def current_facts(
    store: Store, scope: PatientScope, *, include_detached: bool = False, bounded: bool = False
) -> tuple[StoredRecord, ...]:
    from sanad.steward.types import bounded_records

    reader = bounded_records if bounded else records
    heads = (
        {}
        if bounded
        else {r.id: from_record(r, FactHead) for r in reader(store, scope, "fact_head")}
    )
    result = []
    for row in reader(store, scope, "clinical_fact"):
        fact = from_record(row, ClinicalFact)
        root = fact.root_fact_id or fact.id
        if bounded and root not in heads:
            current = store.get(scope, "fact_head", root)
            if current:
                heads[root] = from_record(current, FactHead)
        head = heads.get(root)
        if (
            head is None
            or (include_detached or head.status == "accepted")
            and head.current_ref == row.ref
        ):
            result.append(row)
    return tuple(result)


def read(builder: CommitBuilder, ref: VersionRef) -> StoredRecord:
    row = builder.store.get(builder.scope, ref.entity_type, ref.id)
    if row is None or row.ref != ref:
        raise EffectsRejected("predecessor_not_current_or_owned")
    builder.command = builder.command.model_copy(
        update={
            "expected_versions": tuple(dict.fromkeys((*builder.command.expected_versions, ref)))
        }
    )
    return row


def result(missing: str, now: datetime) -> PredicateResult:
    return PredicateResult(satisfied=False, missing=(missing,), detail=missing, evaluated_at=now)


def predicate(
    builder: CommitBuilder,
    mission: Mission,
    replacement: Evidence | ClinicalFact | None = None,
    detached: bool = False,
) -> PredicateResult:
    if isinstance(mission.details, MonitorDetails):
        return coverage(mission.details, builder.now)
    if mission.objective_predicate.kind == "patient_report":
        from sanad.concierge.reports import is_change, is_start, is_stop
        from sanad.concierge.tasks import is_task_done
        from sanad.concierge.visits import is_attended, is_booked

        predicates = {
            "medication_start": is_start,
            "medication_stop": is_stop,
            "medication_change": is_change,
            "doctor_task": is_task_done,
            "visit_attendance_reported": is_attended,
            "visit_booking_reported": is_booked,
        }
        check = predicates.get(mission.objective_predicate.report_kind)
        facts = [from_record(r, ClinicalFact) for r in current_facts(builder.store, builder.scope)]
        if isinstance(replacement, ClinicalFact):
            facts = [f for f in facts if (f.root_fact_id or f.id) != replacement.root_fact_id]
            if not detached:
                facts.append(replacement)
        holds = bool(
            check
            and any(
                isinstance(f.payload, ReportFactPayload)
                and f.payload.target_ref
                and f.payload.target_ref.entity_type == "mission"
                and f.payload.target_ref.id == mission.id
                and check(f.payload.text)
                for f in facts
            )
        )
        return PredicateResult(
            satisfied=holds,
            missing=() if holds else ("current_report",),
            detail="Current source-linked report predicate; not proof of adherence.",
            evaluated_at=builder.now,
        )
    values = list(accepted(builder, mission.id))
    if isinstance(replacement, Evidence):
        values = [e for e in values if e.evidence_id != replacement.evidence_id]
        if replacement.association_state == "accepted":
            values.append(replacement)
    values = [e for e in values if e.association_state == "accepted" and not e.identity_pending]
    if not values:
        return result("accepted_evidence_missing", builder.now)
    return evaluate(mission, values[-1], values[:-1], builder.now)


def monitor_revision(
    mission: Mission,
    old: StoredRecord,
    new: Evidence | ClinicalFact,
    detached: bool,
    prior_links: tuple[MonitorReading, ...] = (),
) -> Mission:
    if not isinstance(mission.details, MonitorDetails):
        return mission
    details = mission.details
    values = (
        photo_readings(new)
        if isinstance(new, Evidence)
        else (new.payload.readings if isinstance(new.payload, ReportFactPayload) else ())
    )
    # Retained links recover a removed slot without making the old observation new.
    originals = tuple(r for r in details.readings if r.source_ref == old.ref) or prior_links
    kept = [r for r in details.readings if r.source_ref != old.ref]
    replacements: list[MonitorReading] = []
    if not detached:
        for entry in originals:
            rebuilt = attach(
                details.model_copy(update={"readings": ()}),
                values,
                to_record(new, new.scope).ref,
                entry.observed_at,
                entry.received_at,
            )
            replacements.extend(
                r for r in rebuilt.readings if r.reading_index == entry.reading_index
            )
    updated = sorted([*replacements, *kept], key=lambda r: (r.received_at, r.observed_at))
    return mission.model_copy(
        update={"details": details.model_copy(update={"readings": tuple(updated)})}
    )


def review(
    builder: CommitBuilder, correction: Correction, mission_id: str | None = None
) -> ReviewObligation:
    from sanad.domain import create_review

    outcome = create_review(
        ev.CreateReview(
            event_id=builder.command.command_id + ":review:" + (mission_id or "record"),
            source_type="correction",
            source_id=correction.id,
            source_version=1,
            owner_doctor_id=builder.scope.doctor_id,
            patient_id=builder.scope.patient_id,
            source_mission_id=mission_id,
            review_kind=ReviewKind.correction_disposition,
            review_at=builder.now + builder.policy.timing.result_review_interval,
        ),
        builder.now,
        builder.policy.timing,
    )
    builder.add(outcome, child=True)
    assert isinstance(outcome.aggregate, ReviewObligation)
    return outcome.aggregate


def exposure(
    builder: CommitBuilder, refs: set[VersionRef], mission_ids: set[str]
) -> tuple[dict[str, str], tuple[str, ...]]:
    exposed: dict[str, str] = {}
    reports = []
    for row in records(builder.store, builder.scope, "outbound_intent"):
        intent = from_record(row, OutboundIntent)
        related = bool(set((*intent.order_refs, *intent.source_versions)) & refs) or any(
            r.entity_type == "mission" and r.id in mission_ids for r in intent.source_versions
        )
        if not related:
            continue
        if intent.audience == "doctor":
            if intent.status == "provider_accepted" and intent.notification_purpose in {
                "DONE:FULFILLMENT",
                "DONE:CORRECTION",
                "DEADLINE",
                "DANGER",
            }:
                reports.append(intent.id)
            continue
        if not is_routine(intent):
            continue
        if intent.status == "queued":
            # Same accepted suppression shape, narrowed to the actual source dependency.
            builder.put(
                to_record(
                    intent.model_copy(
                        update={
                            "version": intent.version + 1,
                            "updated_at": builder.now,
                            "status": "suppressed",
                            "suppression_reason": "accepted_record_corrected",
                            "work_clock": None,
                        }
                    ),
                    builder.scope,
                )
            )
        elif intent.status in {"sending", "uncertain", "provider_accepted"}:
            read(builder, row.ref)
            exposed[intent.id] = intent.status
    return exposed, tuple(reports)


def describe(value: dict[str, JsonValue]) -> str:
    payload = value.get("payload")
    rows = value.get("values")
    if isinstance(rows, list):
        text = "; ".join(
            " ".join(str(row[k]) for k in ("analyte", "name", "value", "unit") if row.get(k))
            for row in rows
            if isinstance(row, dict)
        )
    elif isinstance(payload, dict):
        text = (
            " ".join(str(payload[k]) for k in ("analyte", "value", "unit") if payload.get(k))
            if payload.get("analyte")
            else str(payload.get("text") or "Retained record")
        )
    else:
        text = " · ".join(
            f"{k}: {value[k]}"
            for k in ("drug", "dose", "frequency", "route", "timing", "duration")
            if value.get(k)
        )
    return text + (" [detached from current chart]" if value.get("state") == "detached" else "")


def notice_text(
    correction: Correction, *, recipient: Doctor | None = None, actor: Doctor | None = None
) -> str:
    changed_by = (
        "you"
        if recipient and correction.actor.subject == recipient.telegram_user_id
        else actor.name
        if actor and actor.telegram_user_id == correction.actor.subject and actor.name.strip()
        else "the doctor"
    )
    before = describe(correction.before)
    after = describe(correction.after)
    lines = [
        "Correction to the accepted record.",
        f"From: {before}",
        f"To: {after}",
        f"Changed by {changed_by}: {correction.reason}",
    ]
    if correction.predicates:
        lines.append(
            "Current predicate: "
            + "; ".join(
                f"{id}: {'satisfied' if p.satisfied else 'incomplete'}"
                for id, p in correction.predicates.items()
            )
        )
        lines.append("Fulfilment history and its times are retained. Care has not restarted.")
    if "provider_accepted" in correction.exposures.values():
        lines.append("The provider accepted an earlier patient instruction. It cannot be unsent.")
    if {"sending", "uncertain"} & set(correction.exposures.values()):
        lines.append(
            "An earlier patient instruction may have been exposed. Delivery remains uncertain."
        )
    if correction.exposures:
        lines.append(
            "Doctor decision required: record the follow-up response separately. "
            "No automatic patient message."
        )
    return "\n".join(lines)


def rendered_notice(store: Store, correction: Correction, recipient_id: str) -> str:
    def doctor(id: str | None) -> Doctor | None:
        row = store.get(TenantScope(doctor_id=id), "doctor", id) if id else None
        return from_record(row, Doctor) if row else None

    return notice_text(
        correction, recipient=doctor(recipient_id), actor=doctor(correction.actor.doctor_id)
    )


def notice(builder: CommitBuilder, correction: Correction, obligation: ReviewObligation) -> None:
    doctor_row = builder.store.get(builder.scope, "doctor_authority", builder.scope.doctor_id)
    profile = builder.store.get_patient_profile(builder.scope)
    if not doctor_row or not profile:
        raise EffectsRejected("authority_missing")
    intent = make_intent(
        builder.scope,
        builder.command.command_id,
        (to_record(correction, builder.scope).ref,),
        "DONE:CORRECTION" if correction.prior_report_ids else "solicited_reply",
        correction.id,
        builder.now,
        builder.policy,
        from_record(doctor_row, DoctorAuthority),
        profile,
        template_id="accepted_correction",
    )
    payload: dict[str, JsonValue] = {
        "text": rendered_notice(builder.store, correction, builder.scope.doctor_id)
    }
    intent = intent.model_copy(
        update={
            "payload": payload,
            "payload_digest": keys.digest(canonical_json(payload).decode()),
            "review_obligation_id": obligation.id,
        }
    )
    builder.intents[intent.id] = to_record(intent, builder.scope)


def correct(builder: CommitBuilder) -> None:
    body = CorrectBody.model_validate(builder.command.payload)
    old_row = read(builder, body.predecessor)
    cid = keys.digest(builder.command.command_id)
    detached = body.operation == "detach"
    if detached and (body.changes or body.row_index is not None):
        raise EffectsRejected("detachment_has_no_replacement")
    if not detached and not body.changes:
        raise EffectsRejected("replacement_required")
    new: Evidence | ClinicalFact
    if old_row.entity_type == "evidence":
        old = from_record(old_row, Evidence)
        head, current = load(builder, old.evidence_id)
        if (
            current != old
            or old.association_state not in {"accepted", "detached"}
            or old.identity_pending
        ):
            raise EffectsRejected("predecessor_not_current_accepted")
        linked = builder.store.get_mission(builder.scope, old.mission_id or "")
        source_member = linked and (
            AcceptedFactRef(fact_kind="evidence", fact_id=old.evidence_id, version=old.version)
            in linked.evidence_refs
            or isinstance(linked.details, MonitorDetails)
            and any(r.source_ref == old_row.ref for r in linked.details.readings)
        )
        if (
            head.mission_id != old.mission_id
            or linked is None
            or not source_member
            and old.correction_id is None
            and linked.state == "fulfilled"
        ):
            raise EffectsRejected("predecessor_not_member")
        read(builder, to_record(head, builder.scope).ref)
        fields = old.model_dump()
        if not detached:
            if (
                body.row_index is None
                or body.row_index >= len(old.extracted_values)
                or set(body.changes) - {"value", "unit"}
            ):
                raise EffectsRejected("correct_read_value_or_unit")
            items = list(old.extracted_values)
            item = items[body.row_index]
            changed = type(item).model_validate(item.model_dump() | body.changes)
            if isinstance(changed, LabRowCandidate):
                from sanad.evidence.grading import grade
                from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT

                changed = grade(changed, SAFETY_POLICY_V1_CARDIOLOGY_DRAFT)
            items[body.row_index] = changed
            fields["extracted_values"] = items
        # A detached version retains the original mission as provenance, but contributes nowhere.
        fields.update(
            id=f"{old.evidence_id}:{old.version + 1}",
            version=old.version + 1,
            updated_at=builder.now,
            association_state="detached" if detached else "accepted",
            accepted_by=None if detached else builder.command.principal.subject,
            accepted_at=None if detached else builder.now,
            correction_id=cid,
            supersedes_evidence_id=old.evidence_id,
            supersedes_evidence_version=old.version,
            rejection_reason=body.reason if detached else None,
        )
        new = Evidence.model_validate(fields)
        builder.put(
            to_record(
                head.model_copy(
                    update={
                        "version": head.version + 1,
                        "updated_at": builder.now,
                        "current_version": new.version,
                        "status": new.association_state,
                    }
                ),
                builder.scope,
            )
        )
        before = {
            "values": old.model_dump(mode="json")["extracted_values"],
            "state": old.association_state,
        }
        after = {
            "values": new.model_dump(mode="json")["extracted_values"],
            "state": new.association_state,
        }
    elif old_row.entity_type == "clinical_fact":
        fact = from_record(old_row, ClinicalFact)
        root = fact.root_fact_id or fact.id
        if any(
            keys.digest(r.id + ":readings") == root
            for r in records(builder.store, builder.scope, "evidence_head")
        ):
            raise EffectsRejected("correct_linked_evidence_source")
        hrow = builder.store.get(builder.scope, "fact_head", root)
        if hrow and from_record(hrow, FactHead).current_ref != old_row.ref:
            raise EffectsRejected("predecessor_not_current_accepted")
        if hrow:
            read(builder, hrow.ref)
        if set(body.changes) - (
            {"value", "unit", "text"} if isinstance(fact.payload, LabFactPayload) else {"text"}
        ):
            raise EffectsRejected("unsupported_fact_field")
        payload = type(fact.payload).model_validate(fact.payload.model_dump() | body.changes)
        if isinstance(payload, LabFactPayload):
            from sanad.evidence.grading import grade
            from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT

            graded = grade(payload, SAFETY_POLICY_V1_CARDIOLOGY_DRAFT)
            canonical_text = " ".join(
                v for v in (payload.analyte, payload.value, payload.unit) if v
            )
            payload = LabFactPayload.model_validate(graded.model_dump() | {"text": canonical_text})
        if isinstance(payload, FactPayload) and "text" in body.changes:
            payload = payload.model_copy(update={"clinical_en": None, "terms": ()})
        if isinstance(payload, ReportFactPayload) and "text" in body.changes:
            from sanad.monitor.executor import parse_reading
            from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT

            payload = payload.model_copy(
                update={
                    "readings": parse_reading(
                        payload.text, SAFETY_POLICY_V1_CARDIOLOGY_DRAFT
                    ).values
                }
            )
        if isinstance(payload, ReportFactPayload) and payload.report_kind in {
            "medication_start",
            "start_date",
        }:
            from sanad.concierge.reports import reported_date

            patient = builder.store.get(builder.scope, "patient", builder.scope.patient_id)
            if patient is None:
                raise EffectsRejected("patient_missing")
            dated, effective = reported_date(
                payload.text, fact.provenance.received_at, str(patient.body["timezone"])
            )
            payload = payload.model_copy(
                update={
                    "effective_start": effective,
                    "anchor_unknown": dated and effective is None,
                }
            )
        new = ClinicalFact.model_validate(
            fact.model_dump()
            | {
                "id": cid + ":fact",
                "version": 1,
                "updated_at": builder.now,
                "payload": payload,
                "supersedes_fact_id": fact.id,
                "root_fact_id": root,
                "correction_id": cid,
            }
        )
        builder.put(
            to_record(
                FactHead(
                    id=root,
                    scope=builder.scope,
                    version=hrow.version + 1 if hrow else 1,
                    created_at=from_record(hrow, FactHead).created_at if hrow else builder.now,
                    updated_at=builder.now,
                    current_ref=to_record(new, builder.scope).ref,
                    status="detached" if detached else "accepted",
                ),
                builder.scope,
            )
        )
        before = {
            "payload": fact.payload.model_dump(mode="json"),
            "state": from_record(hrow, FactHead).status if hrow else "accepted",
        }
        after = {
            "payload": new.payload.model_dump(mode="json"),
            "state": "detached" if detached else "accepted",
        }
    else:
        raise EffectsRejected("correctable_record_required")
    inherited_links: dict[str, tuple[MonitorReading, ...]] = {}
    predecessor_correction = old_row.body.get("correction_id")
    if isinstance(predecessor_correction, str):
        prior_correction = from_record(
            read(
                builder, VersionRef(entity_type="correction", id=predecessor_correction, version=1)
            ),
            Correction,
        )
        inherited_links = prior_correction.monitor_links
    monitor_links = dict(inherited_links)
    predicates: dict[str, PredicateResult] = {}
    mission_ids = set()
    for row in records(builder.store, builder.scope, "mission"):
        mission = from_record(row, Mission)
        evidence_ref = (
            AcceptedFactRef(fact_kind="evidence", fact_id=new.evidence_id, version=old_row.version)
            if isinstance(new, Evidence)
            else None
        )
        member = evidence_ref in mission.evidence_refs if evidence_ref else False
        monitored = isinstance(mission.details, MonitorDetails) and any(
            r.source_ref == old_row.ref for r in mission.details.readings
        )
        associated = isinstance(new, Evidence) and new.mission_id == mission.id
        reported = (
            isinstance(new, ClinicalFact)
            and isinstance(new.payload, ReportFactPayload)
            and new.payload.target_ref is not None
            and (new.payload.target_ref.entity_type, new.payload.target_ref.id)
            == ("mission", mission.id)
        )
        if (
            not member
            and not monitored
            and not associated
            and not reported
            and mission.id not in inherited_links
        ):
            continue
        read(builder, row.ref)
        mission_ids.add(mission.id)
        if isinstance(mission.details, MonitorDetails):
            links = tuple(r for r in mission.details.readings if r.source_ref == old_row.ref)
            if links:
                monitor_links[mission.id] = links
        updated = monitor_revision(
            mission, old_row, new, detached, inherited_links.get(mission.id, ())
        )
        checked = predicate(builder, updated, new, detached)
        predicates[mission.id] = checked
        if evidence_ref and member:
            outcome = transition_mission(
                updated,
                ev.CorrectAcceptedEvidence(
                    event_id=builder.command.command_id + ":" + mission.id,
                    superseded_evidence_ref=evidence_ref,
                    correcting_evidence_ref=evidence_ref.model_copy(
                        update={"version": new.version}
                    ),
                    predicate_still_holds=checked.satisfied,
                ),
                builder.now,
                builder.policy.timing,
            )
            if isinstance(outcome, ev.TransitionRejected):
                raise EffectsRejected(outcome.reason_code)
            builder.add(
                outcome.model_copy(
                    update={
                        "effects": tuple(
                            e
                            for e in outcome.effects
                            if not isinstance(e, (ev.SuppressRoutineIntents, ev.CreateReview))
                        )
                    }
                )
            )
        else:
            # Source-linked projection only; no new transition or execution state.
            builder.put(
                to_record(
                    updated.model_copy(
                        update={
                            "version": mission.version + 1,
                            "updated_at": builder.now,
                            "fulfillment_validity": FulfillmentValidity.invalidated_pending_review
                            if mission.state == "fulfilled" and not checked.satisfied
                            else mission.fulfillment_validity,
                        }
                    ),
                    builder.scope,
                )
            )
    if isinstance(new, Evidence):
        new = new.model_copy(update={"required_predicate_results": tuple(predicates.values())})
    builder.put(to_record(new, builder.scope))
    dependencies = {old_row.ref}
    for row in records(builder.store, builder.scope, "followup"):
        task = from_record(row, FollowUpTask)
        parent = builder.store.get_mission(builder.scope, task.parent_mission_id)
        if (
            parent
            and parent.id in predicates
            and (
                not predicates[parent.id].satisfied
                or isinstance(new, ClinicalFact)
                and isinstance(new.payload, ReportFactPayload)
                and new.payload.report_kind in {"medication_start", "start_date"}
            )
            and parent.state == "fulfilled"
            and isinstance(parent.details, MedicationDetails)
            and parent.details.action == "START"
            and task.kind == "MEDICATION_DAY3"
            and task.anchor_kind == "reported_effective_start"
        ):
            read(builder, row.ref)
            dependencies.add(row.ref)
            if task.state in {"fulfilled", "cancelled", "contact_suppressed"}:
                continue
            builder.add(
                transition_followup(
                    task,
                    ev.SuppressFollowUpContact(
                        event_id=builder.command.command_id + ":anchor:" + task.id,
                        reason="Start-report source corrected; "
                        "anchor reliance needs doctor disposition.",
                    ),
                    builder.now,
                    builder.policy.timing,
                )
            )
    if isinstance(new, Evidence):
        root = keys.digest(new.evidence_id + ":readings")
        head_row = builder.store.get(builder.scope, "fact_head", root)
        mirror_ref = (
            from_record(head_row, FactHead).current_ref
            if head_row
            else VersionRef(entity_type="clinical_fact", id=root, version=1)
        )
        mirror_row = builder.store.get(builder.scope, "clinical_fact", mirror_ref.id)
        if mirror_row:
            mirror = from_record(read(builder, mirror_ref), ClinicalFact)
            if head_row:
                read(builder, head_row.ref)
            text = "\n".join(
                " ".join(
                    v
                    for v in (
                        (r.analyte if isinstance(r, LabRowCandidate) else r.name),
                        r.value,
                        r.unit,
                    )
                    if v
                )
                for r in new.extracted_values
            )
            assert isinstance(mirror.payload, ReportFactPayload)
            mirror_new = mirror.model_copy(
                update={
                    "id": cid + ":mirror",
                    "version": 1,
                    "updated_at": builder.now,
                    "root_fact_id": root,
                    "supersedes_fact_id": mirror.id,
                    "correction_id": cid,
                    "payload": mirror.payload.model_copy(
                        update={
                            "text": text or mirror.payload.text,
                            "readings": photo_readings(new),
                        }
                    ),
                }
            )
            builder.put(to_record(mirror_new, builder.scope))
            builder.put(
                to_record(
                    FactHead(
                        id=root,
                        scope=builder.scope,
                        version=head_row.version + 1 if head_row else 1,
                        created_at=from_record(head_row, FactHead).created_at
                        if head_row
                        else builder.now,
                        updated_at=builder.now,
                        current_ref=to_record(mirror_new, builder.scope).ref,
                        status="detached" if detached else "accepted",
                    ),
                    builder.scope,
                )
            )
            dependencies.add(mirror_row.ref)
    exposures, prior = exposure(builder, dependencies, mission_ids)
    correction = Correction(
        id=cid,
        scope=builder.scope,
        created_at=builder.now,
        updated_at=builder.now,
        predecessor=old_row.ref,
        successor=to_record(new, builder.scope).ref,
        operation=body.operation,
        actor=builder.command.principal,
        reason=body.reason,
        before=before,
        after=after,
        predicates=predicates,
        monitor_links=monitor_links,
        exposures=exposures,
        prior_report_ids=prior,
        affected_mission_ids=tuple(sorted(mission_ids)),
    )
    builder.put(to_record(correction, builder.scope))
    obligation = review(builder, correction, next(iter(sorted(mission_ids)), None))
    notice(builder, correction, obligation)
    builder.audit(
        "ACCEPTED_RECORD_CORRECTED",
        builder.command.command_id,
        (to_record(correction, builder.scope).ref,),
        (old_row.ref,),
    )


def validate(builder: CommitBuilder) -> None:
    body = ValidateBody.model_validate(builder.command.payload)
    c = from_record(
        read(builder, VersionRef(entity_type="correction", id=body.correction_id, version=1)),
        Correction,
    )
    m = from_record(read(builder, body.mission_ref), Mission)
    r = from_record(read(builder, body.review_ref), ReviewObligation)
    if m.id not in c.affected_mission_ids or r.source_type != "correction" or r.source_id != c.id:
        raise EffectsRejected("current_correction_review_required")
    if not correction_current(builder.store, c):
        raise EffectsRejected("correction_superseded")
    # One correction review covers its entire bounded dependency inventory.
    for mission_id in c.affected_mission_ids:
        row = builder.store.get(builder.scope, "mission", mission_id)
        if row is None:
            raise EffectsRejected("correction_dependency_missing")
        current = from_record(read(builder, row.ref), Mission)
        if current.fulfillment_validity != "invalidated_pending_review":
            continue
        checked = predicate(builder, current)
        if not checked.satisfied:
            raise EffectsRejected("predicate_incomplete")
        builder.add(
            transition_mission(
                current,
                ev.ValidateCorrection(
                    event_id=builder.command.command_id + ":" + mission_id,
                    predicate_result=checked,
                ),
                builder.now,
                builder.policy.timing,
            )
        )
    if r.state != "resolved":
        builder.add(
            transition_review(
                r,
                ev.ResolveReview(
                    event_id=builder.command.command_id + ":review",
                    action=ReviewAction.review,
                    expected_source_version=r.source_version,
                    actor_id=builder.command.principal.subject,
                    reason=body.reason,
                ),
                builder.now,
                builder.policy.timing,
            )
        )
    builder.audit(
        "CORRECTION_VALIDATED",
        builder.command.command_id + ":validated",
        (to_record(c, builder.scope).ref,),
        (body.review_ref,),
    )


def correction_current(store: Store, c: Correction) -> bool:
    row = store.get(c.scope, c.successor.entity_type, c.successor.id)
    if not row or row.ref != c.successor:
        return False
    if row.entity_type == "evidence":
        e = from_record(row, Evidence)
        h = store.get(c.scope, "evidence_head", e.evidence_id)
        return bool(h and h.body.get("current_version") == e.version)
    if row.entity_type == "clinical_fact":
        f = from_record(row, ClinicalFact)
        h = store.get(c.scope, "fact_head", f.root_fact_id or f.id)
        return bool(h and from_record(h, FactHead).current_ref == row.ref)
    if row.entity_type == "care_order_version":
        from sanad.scribe.order_changes import active_reference

        return active_reference(store, c.scope, c.successor)
    return False


def prepare(builder: CommitBuilder, *, enforce_limit: bool = True) -> CommitRequest:
    kind = builder.command.payload.get("type")
    if kind == "CorrectRecord":
        correct(builder)
    elif kind == "ValidateCorrection":
        validate(builder)
    else:
        from sanad.steward.correction_actions import prepare_action

        prepare_action(builder)
    request = builder.finish()
    if not enforce_limit:
        return request  # Read-only safety preflight; commit guards always enforce limits.
    if (
        len((*request.puts, *request.events, *request.intents, *request.command.expected_versions))
        > MAX_WRITES
    ):
        raise EffectsRejected("correction_transaction_limit")
    if (
        sum(
            len(canonical_json(r.model_dump(mode="json")))
            for r in (*request.puts, *request.events, *request.intents)
        )
        > 512 * 1024
    ):
        raise EffectsRejected("correction_transaction_size")
    return request
