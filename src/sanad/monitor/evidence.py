"""The released monitor-screen delegation; the evidence controller owns acceptance."""

from collections.abc import Sequence
from datetime import datetime

from pydantic import JsonValue

from sanad.domain import Mission
from sanad.domain.entities import MonitorDetails
from sanad.domain.predicates import PredicateResult
from sanad.evidence.evaluate import material_disagreement
from sanad.monitor.executor import current_details, photo_readings, project
from sanad.monitor.slots import attach, coverage, metric, value_for
from sanad.steward.apply import CommitBuilder, EffectsRejected
from sanad.store.records import Evidence, from_record, to_record


def eligible(evidence: Evidence) -> bool:
    return (
        evidence.category == "monitor_screen"
        and not evidence.identity_pending
        and not evidence.shift_guard_fired
        and not material_disagreement(evidence)
        and not any(r.unreadable for r in evidence.readers)
        and not {"one_document_per_photo", "document_instructions"}.intersection(evidence.flags)
        and (
            "identity_mismatch" not in evidence.flags
            or evidence.patient_match_provenance == "doctor_choice"
        )
    )


def evaluate(
    mission: Mission, candidate: Evidence, prior: Sequence[Evidence], now: datetime
) -> PredicateResult:
    if not isinstance(mission.details, MonitorDetails):
        return PredicateResult(
            satisfied=False,
            missing=("supported_predicate",),
            detail="MONITOR details required.",
            evaluated_at=now,
        )
    details = mission.details
    for e in sorted((*prior, candidate), key=lambda e: (e.provenance.received_at, e.id)):
        if (
            e.scope != candidate.scope
            or e.mission_id not in {None, mission.id}
            or not eligible(e)
            or e.provenance.received_at > now
            or (e.provenance.observed_at or e.provenance.received_at) > e.provenance.received_at
            or e.evidence_id != candidate.evidence_id
            and (e.mission_id != mission.id or e.association_state != "accepted")
        ):
            continue
        details = attach(
            details,
            photo_readings(e),
            to_record(e, e.scope).ref,
            e.provenance.observed_at or e.provenance.received_at,
            e.provenance.received_at,
        )
    return coverage(details, now)


def prepare(
    builder: CommitBuilder,
    evidence: Evidence,
    chosen: Mission | None,
    plausible: tuple[Mission, ...],
) -> tuple[Evidence, str, dict[str, str], list[JsonValue]]:
    from sanad.evidence.commit import (
        accepted,
        patient_buttons,
        record_monitor,
        resolve_reviews,
        review,
    )
    from sanad.evidence.evaluate import evaluate as evaluate_evidence

    previous_row = builder.store.get(
        builder.scope, "evidence", f"{evidence.evidence_id}:{evidence.version - 1}"
    )
    if previous_row:
        previous = from_record(previous_row, Evidence)
        old_mission = (
            builder.store.get_mission(builder.scope, previous.mission_id)
            if previous.mission_id
            else None
        )
        if (
            previous.association_state == "accepted"
            and old_mission
            and old_mission.kind == "MONITOR"
            and old_mission.state == "fulfilled"
        ):
            raise EffectsRejected("correction_requires_slice19")
    values = photo_readings(evidence)
    names = {metric(r.analyte) for r in values} - {None}
    matching = tuple(
        m
        for m in plausible
        if isinstance(m.details, MonitorDetails) and metric(m.details.metric) in names
    )
    if (
        chosen is not None
        and chosen not in matching
        and evidence.patient_match_provenance in {"doctor_choice", "patient_choice"}
    ):
        evidence = evidence.model_copy(
            update={
                "required_predicate_results": (
                    PredicateResult(
                        satisfied=False,
                        missing=("monitor_metric",),
                        detail="The reading does not match the selected monitoring metric.",
                        evaluated_at=builder.now,
                    ),
                )
            }
        )
        review(builder, evidence)
        return evidence, "patient_evidence_kept", {}, []
    if chosen not in matching:
        chosen = matching[0] if len(matching) == 1 else None
    if chosen is None:
        if len(matching) > 1:
            review(builder, evidence)
            return (
                evidence.model_copy(update={"mission_id": None}),
                "patient_evidence_which",
                {},
                patient_buttons(builder, evidence, matching),
            )
        record_monitor(builder, evidence)
        predicate = PredicateResult(
            satisfied=False,
            missing=("monitor_mission",),
            detail="No matching monitoring mission.",
            evaluated_at=builder.now,
        )
        return (
            evidence.model_copy(
                update={"mission_id": None, "required_predicate_results": (predicate,)}
            ),
            "patient_evidence_kept",
            {},
            [],
        )
    evidence = evidence.model_copy(update={"mission_id": chosen.id})
    if (
        evidence.provenance.observed_at or evidence.provenance.received_at
    ) > evidence.provenance.received_at:
        review(builder, evidence)
        return evidence, "patient_evidence_kept", {}, []
    assert isinstance(chosen.details, MonitorDetails)
    if not any(value_for(chosen.details, value) is not None for value in values):
        evidence = evidence.model_copy(
            update={
                "required_predicate_results": (
                    PredicateResult(
                        satisfied=False,
                        missing=("verification",),
                        detail="Confirm the measurement value and unit.",
                        evaluated_at=builder.now,
                    ),
                )
            }
        )
        review(builder, evidence)
        return evidence, "patient_evidence_kept", {}, []
    from sanad.evidence.deadline import before_fulfillment

    chosen = before_fulfillment(builder, chosen)
    prior = accepted(builder, chosen.id)
    details = current_details(builder.store, builder.scope, chosen)
    checked = chosen.model_copy(update={"details": details})
    predicate = evaluate_evidence(checked, evidence, prior, builder.now)
    evidence = evidence.model_copy(update={"required_predicate_results": (predicate,)})
    if any(
        m in {"verification", "readable_document", "identity", "one_document_per_photo"}
        for m in predicate.missing
    ):
        review(builder, evidence)
        return evidence, "patient_evidence_kept", {}, []
    evidence = evidence.model_copy(
        update={
            "association_state": "accepted_pending_identity"
            if evidence.identity_pending
            else "accepted",
            "accepted_by": builder.command.principal.subject,
            "accepted_at": builder.now,
        }
    )
    if evidence.identity_pending:
        review(builder, evidence)
        return evidence, "patient_evidence_kept", {}, []
    # Do not rewrite the fact on identity confirmation or association replay.
    from sanad.store import keys

    if not builder.store.get(
        builder.scope, "clinical_fact", keys.digest(evidence.evidence_id + ":readings")
    ):
        record_monitor(builder, evidence)
        builder.audit(
            "MONITOR_READING_RECORDED",
            keys.digest(builder.command.command_id + ":monitor"),
            (to_record(evidence, builder.scope).ref,),
        )
    details = attach(
        details,
        values,
        to_record(evidence, builder.scope).ref,
        evidence.provenance.observed_at or evidence.provenance.received_at,
        evidence.provenance.received_at,
    )
    predicate = coverage(details, builder.now)
    evidence = evidence.model_copy(update={"required_predicate_results": (predicate,)})
    if details != chosen.details:
        builder.add(
            project(
                chosen,
                details,
                builder.command.command_id,
                builder.now,
                builder.policy.timing,
                danger=bool(evidence.incident_ids),
            )
        )
    if builder.command.principal.actor_kind == "doctor":
        resolve_reviews(builder, evidence)
    if predicate.satisfied:
        return evidence, "patient_evidence_accepted", {"title": chosen.title}, []
    return evidence, "patient_evidence_kept", {}, []
