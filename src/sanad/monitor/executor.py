"""One projection used by the executor and the patient store guard."""

import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sanad.concierge.records import Reading, ReportFactPayload
from sanad.domain import AcceptedFactRef, Mission, PatientScope, transition_mission
from sanad.domain import events as ev
from sanad.domain.entities import DoctorTimingPolicy, MonitorDetails
from sanad.monitor.slots import attach, coverage, filled, slot_for, value_for
from sanad.scribe.records import ClinicalFact
from sanad.store.protocol import Store
from sanad.store.records import Evidence, EvidenceHead, from_record, to_record

if TYPE_CHECKING:
    from sanad.concierge.reports import ReadingResult
    from sanad.safety.policy import SafetyPolicy

ACTIVE = {"open", "waiting_patient", "blocked", "unreachable", "overdue"}
_ISO_TIME = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})", re.I)
_LOCAL_TIME = re.compile(r"(?:\bat|الساعة|الساعه)\s+(\d{1,2}):(\d{2})\b", re.I)


def parse_reading(text: str, safety: "SafetyPolicy") -> "ReadingResult":
    from dataclasses import replace

    from sanad.concierge.reports import reading

    # An explicit timestamp is metadata, not a third number of a BP pair.
    # The unchanged parser and kernel still own the measurement and its grading.
    body = _LOCAL_TIME.sub(" ", _ISO_TIME.sub(" ", text))
    parsed = reading(body, safety)
    # BP's accepted text parser supplies mmHg implicitly. Preserve an explicitly
    # contradictory unit as an observation, so it cannot be filed as mmHg.
    wrong_unit = re.search(
        r"\d{2,3}\s*(?:/|على)\s*\d{2,3}\s*(kg|bpm|mmol/L|mg/dL|mg/L|g/dL|kPa|%)(?!\w)",
        body,
        re.I,
    )
    if wrong_unit:
        parsed = replace(
            parsed,
            values=tuple(
                value.model_copy(update={"raw_unit": wrong_unit[1]})
                if value.analyte == "BP"
                else value
                for value in parsed.values
            ),
        )
    return parsed


def reading_time(text: str, received_at: datetime, timezone: str) -> datetime | None:
    from sanad.domain.deadlines import local_to_utc, utc_instant

    iso = _ISO_TIME.search(text)
    clock = _LOCAL_TIME.search(text)
    if len(_ISO_TIME.findall(text)) + len(_LOCAL_TIME.findall(text)) > 1:
        return None
    try:
        if iso:
            observed = utc_instant(datetime.fromisoformat(iso[0]))
        elif clock:
            local = received_at.astimezone(ZoneInfo(timezone))
            if re.search(r"\byesterday\b|امبارح", text, re.I):
                local -= timedelta(days=1)
            observed = local_to_utc(
                local.replace(
                    hour=int(clock[1]), minute=int(clock[2]), second=0, microsecond=0, tzinfo=None
                ),
                timezone,
            )
        else:
            if re.search(
                r"\byesterday\b|\b(?:morning|evening)\b|امبارح|الصبح|بالليل|\d{4}-\d{2}-\d{2}|\bat\s+\d",
                text,
                re.I,
            ):
                return None
            return received_at
    except ValueError:
        return None
    return observed if observed <= received_at else None


def current_details(store: Store, scope: PatientScope, mission: Mission) -> MonitorDetails:
    """Evidence rejection cannot leave a contributing slot in the next projection."""
    assert isinstance(mission.details, MonitorDetails)
    kept = []
    for entry in mission.details.readings:
        row = store.get(scope, entry.source_ref.entity_type, entry.source_ref.id)
        if row is None or row.ref != entry.source_ref:
            continue
        values: tuple[Reading, ...] = ()
        if row.entity_type == "evidence":
            evidence = from_record(row, Evidence)
            head_row = store.get(scope, "evidence_head", evidence.evidence_id)
            if head_row is None:
                continue
            head = from_record(head_row, EvidenceHead)
            if (
                head.current_version != evidence.version
                or head.status != "accepted"
                or head.mission_id != mission.id
                or evidence.identity_pending
            ):
                continue
            values = photo_readings(evidence)
        elif row.entity_type == "clinical_fact":
            fact = from_record(row, ClinicalFact)
            from sanad.corrections import FactHead

            fact_head = store.get(scope, "fact_head", fact.root_fact_id or fact.id)
            if fact_head and (
                from_record(fact_head, FactHead).status != "accepted"
                or from_record(fact_head, FactHead).current_ref != row.ref
            ):
                continue
            if (
                isinstance(fact.payload, ReportFactPayload)
                and fact.payload.report_kind == "reading"
            ):
                values = fact.payload.readings
        if (
            entry.reading_index >= len(values)
            or entry.value != value_for(mission.details, values[entry.reading_index])
            or entry.slot != slot_for(mission.details, entry.observed_at)
        ):
            continue
        kept.append(entry)
    return mission.details.model_copy(update={"readings": tuple(kept)})


def project(
    mission: Mission,
    details: MonitorDetails,
    command_id: str,
    now: datetime,
    timing: DoctorTimingPolicy,
    *,
    danger: bool = False,
) -> ev.TransitionResult:
    from sanad.contact.scheduler import prime

    predicate = coverage(details, now)
    updated = mission.model_copy(
        update={"details": details, "danger_history": mission.danger_history or danger}
    )
    event_id = command_id + ":monitor:" + mission.id
    event: ev.ObjectiveFulfilled | ev.PatientReplied
    if predicate.satisfied:
        event = ev.ObjectiveFulfilled(
            event_id=event_id,
            predicate_result=predicate,
            objective_received_at=max(r.received_at for r in filled(details).values()),
            fulfillment_event_id=event_id,
            evidence_refs=tuple(
                dict.fromkeys(
                    AcceptedFactRef(
                        fact_kind="evidence",
                        fact_id=r.source_ref.id.rsplit(":", 1)[0],
                        version=r.source_ref.version,
                    )
                    for r in filled(details).values()
                    if r.source_ref.entity_type == "evidence"
                )
            ),
            danger_flag=danger,
            actor_kind="system",
        )
    else:
        event = ev.PatientReplied(event_id=event_id)
    result = transition_mission(updated, event, now, timing)
    if isinstance(result, ev.TransitionRejected):
        from sanad.steward.apply import EffectsRejected

        raise EffectsRejected(result.reason_code)
    assert isinstance(result.aggregate, Mission)
    if not predicate.satisfied:
        result = result.model_copy(update={"aggregate": prime(result.aggregate, now)})
    if danger:
        # The already committed urgent incident owns the existing DANGER gateway.
        result = result.model_copy(
            update={"effects": tuple(e for e in result.effects if not isinstance(e, ev.EmitIntent))}
        )
    return result


def text_details(
    store: Store,
    scope: PatientScope,
    mission: Mission,
    fact: ClinicalFact,
    observed_at: datetime,
    received_at: datetime,
) -> MonitorDetails:
    assert isinstance(fact.payload, ReportFactPayload)
    return attach(
        current_details(store, scope, mission),
        fact.payload.readings,
        to_record(fact, scope).ref,
        observed_at,
        received_at,
    )


def photo_readings(evidence: Evidence) -> tuple[Reading, ...]:
    from sanad.concierge.reports import reading
    from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT
    from sanad.scribe.extract import LabRowCandidate

    return tuple(
        value.model_copy(update={"raw_unit": row.unit})
        for row in evidence.extracted_values
        for text in (
            " ".join(
                v
                for v in (
                    row.analyte if isinstance(row, LabRowCandidate) else row.name,
                    row.value,
                    row.unit,
                )
                if v
            ),
        )
        for value in reading(text, SAFETY_POLICY_V1_CARDIOLOGY_DRAFT).values
    )
