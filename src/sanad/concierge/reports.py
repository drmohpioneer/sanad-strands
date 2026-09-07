"""Explicit reports are observations, never diagnoses or inferred adherence."""

import re
from dataclasses import dataclass
from datetime import timedelta
from zoneinfo import ZoneInfo

from sanad.concierge.plan import Snapshot
from sanad.concierge.records import Reading, ReportFactPayload
from sanad.concierge.text import contains, is_question, normalized
from sanad.domain import Mission, Provenance, transition_followup, transition_mission
from sanad.domain import events as ev
from sanad.domain.entities import MedicationDetails
from sanad.domain.predicates import PredicateResult
from sanad.safety import find_bp, grade_bp, grade_lab
from sanad.safety.models import LabCandidate, LabVerdict, Quantity, ScreenVerdict, VitalVerdict
from sanad.safety.policy import SafetyPolicy
from sanad.scribe.extract import OrderCandidate
from sanad.scribe.names import entry_for
from sanad.scribe.records import ClinicalFact
from sanad.steward.patient import PatientTurnCommit
from sanad.store import keys
from sanad.store.records import to_record


@dataclass(frozen=True)
class ReadingResult:
    values: tuple[Reading, ...] = ()
    danger: VitalVerdict | LabVerdict | None = None
    incomplete_bp: bool = False


def reading(text: str, safety: SafetyPolicy) -> ReadingResult:
    body = re.sub(r"(\d{2,3})\s*على\s*(\d{2,3})", r"\1/\2", text)
    pairs = find_bp(body, policy=safety)
    if pairs:
        values = []
        danger = None
        spans = list(re.finditer(r"\d{2,3}\s*(?:/|على)\s*\d{2,3}", text))
        for index, (systolic, diastolic, _) in enumerate(pairs):
            verdict = grade_bp(systolic, diastolic, policy=safety)
            values.append(
                Reading(
                    analyte="BP",
                    quoted=spans[index][0] if index < len(spans) else f"{systolic}/{diastolic}",
                    raw_value=f"{systolic}/{diastolic}",
                    raw_unit="mmHg",
                    judgment="implausible"
                    if verdict.level == "implausible"
                    else "critical"
                    if verdict.level in {"crisis", "low"}
                    else "reported",
                    rule_id=verdict.rule_id,
                )
            )
            if verdict.level in {"crisis", "low"}:
                danger = verdict
        return ReadingResult(tuple(values), danger)
    if re.search(r"(?:ضغط\w*|\bbp|blood pressure)\s*[:=]?\s*\d+", text, re.I):
        return ReadingResult(incomplete_bp=True)
    names = (
        r"سكر(?:ي)?|السكر|جلوكوز|بوتاسيوم|البوتاسيوم|كرياتينين|الكرياتينين|وزني|وزن|نبضي|نبض|"
        r"glucose|potassium|creatinine|INR|LDL|HbA1c|sodium|K|Na|weight|pulse"
    )
    pattern = (
        rf"(?<!\w)({names})\s*[:=]?\s*([<>]?\d+(?:[.,٫]\d+)?)"
        r"(?:\s*(mmol/L|mg/dL|mg/L|g/dL|kg|bpm|%|كيلو|ملجم/دل))?"
    )
    values = []
    danger_lab = None
    for match in re.finditer(pattern, text, re.I):
        name, value, unit = match.groups()
        canonical = {
            "سكر": "glucose",
            "سكري": "glucose",
            "السكر": "glucose",
            "جلوكوز": "glucose",
            "بوتاسيوم": "K",
            "البوتاسيوم": "K",
            "كرياتينين": "creatinine",
            "الكرياتينين": "creatinine",
        }.get(name, name)
        verdict_lab = grade_lab(
            LabCandidate(analyte_raw=canonical, value=Quantity(raw_value=value, raw_unit=unit)),
            policy=safety,
        )
        values.append(
            Reading(
                analyte=canonical,
                quoted=text[match.start(2) : match.end()].strip(),
                raw_value=value,
                raw_unit=unit,
                judgment="cannot_judge"
                if verdict_lab.level == "not_in_table"
                else verdict_lab.level,
                rule_id=verdict_lab.rule_id,
            )
        )
        if verdict_lab.level == "critical":
            danger_lab = verdict_lab
    # Unknown named measurements are retained with their actual unit and no inferred protocol.
    if not values:
        extra_match = re.search(
            r"(?<!\d)(\d+(?:[.,٫]\d+)?)\s*(mmol/L|mg/dL|kg|bpm|%)(?!\w)", text, re.I
        )
        if extra_match:
            values.append(
                Reading(
                    analyte="unspecified",
                    quoted=extra_match[0],
                    raw_value=extra_match[1],
                    raw_unit=extra_match[2],
                    judgment="cannot_judge",
                )
            )
    return ReadingResult(tuple(values), danger_lab)


def is_start(text: str) -> bool:
    return (
        not is_question(text)
        and contains(text, "بدأت", "بديت", "خدت", "started", "took")
        and not contains(text, "مش", "لسه", "لم", "ما", "not", "never")
        and not any(word in normalized(text) for word in ("مبدأتش", "مبداتش", "ماخدتش"))
    )


def ambiguous_start_time(text: str) -> bool:
    return contains(
        text,
        "من فترة",
        "من زمان",
        "من اسبوع",
        "من شهر",
        "مش فاكر",
        "قبل",
        "يوم السبت",
        "يوم الاحد",
        "يوم الاثنين",
        "يوم الثلاثاء",
        "يوم الاربعاء",
        "يوم الخميس",
        "يوم الجمعة",
    ) or bool(re.search(r"\d{4}-\d{2}-\d{2}", text))


def start_missions(snapshot: Snapshot, text: str) -> tuple[Mission, ...]:
    eligible = tuple(
        m
        for m in snapshot.missions
        if isinstance(m.details, MedicationDetails)
        and m.details.action == "START"
        and m.details.order_ref in {to_record(o, snapshot.scope).ref for o in snapshot.orders}
        and all(r in snapshot.order_refs for r in m.order_refs)
    )
    names = {
        o.order_id: o.structured_instruction.drug
        for o in snapshot.orders
        if isinstance(o.structured_instruction, OrderCandidate)
    }

    def mentions(name: str) -> bool:
        entry = entry_for(name)
        aliases = (name, *entry.arabic_spellings) if entry else (name,)
        return any(contains(text, alias) for alias in aliases)

    named = tuple(
        m
        for m in eligible
        if any(mentions(name) for id, name in names.items() if id in {r.id for r in m.order_refs})
    )
    if named:
        return named
    if normalized(text) in {
        normalized(v)
        for v in (
            "بدأت",
            "بدأت الدوا",
            "خدت الدوا",
            "بدأت الدواء",
            "بدأت الدوا النهارده",
            "بدأت الدوا امبارح",
            "بدأت الحباية",
            "بدأت العلاج",
            "بدأت واحد من الادوية",
            "started it",
            "I started it",
        )
    }:
        return eligible
    # A named medicine with no current START cannot fulfill another medicine's mission.
    return ()


def fact(
    tx: PatientTurnCommit,
    text: str,
    payload: ReportFactPayload,
    *,
    source: Provenance | None = None,
) -> ClinicalFact:
    value = ClinicalFact(
        id=keys.digest(tx.id + ":" + payload.report_kind),
        scope=tx.snapshot.scope,
        created_at=tx.now,
        updated_at=tx.now,
        category="patient_report",
        visibility="patient_released",
        payload=payload,
        provenance=source
        or Provenance(
            source_observation_id=tx.receipt.id,
            actor_kind="patient",
            actor_id=tx.principal.subject,
            source_kind="patient_report",
            received_at=tx.receipt.received_at,
        ),
    )
    tx.put(value)
    return value


def record_start(
    tx: PatientTurnCommit, mission: Mission, text: str, *, source: Provenance | None = None
) -> None:
    predicate = PredicateResult(
        satisfied=True,
        detail="Explicit patient report of the current START instruction; not verified ingestion.",
        evaluated_at=tx.now,
    )
    fact(
        tx,
        text,
        ReportFactPayload(
            report_kind="medication_start",
            text=text,
            target_ref=to_record(mission, tx.snapshot.scope).ref,
        ),
        source=source,
    )
    event = ev.ObjectiveFulfilled(
        event_id=tx.id,
        predicate_result=predicate,
        objective_received_at=tx.receipt.received_at,
        fulfillment_event_id=tx.id,
        danger_flag=False,
        actor_kind="patient",
    )
    result = transition_mission(mission, event, tx.now, tx.builder.policy.timing)
    if isinstance(result, ev.TransitionResult):
        effective = tx.receipt.received_at
        if contains(text, "امبارح", "yesterday"):
            effective = effective.astimezone(ZoneInfo(tx.snapshot.patient.timezone)) - timedelta(
                days=1
            )
        effects = tuple(
            e.model_copy(update={"anchor_time": effective})
            if isinstance(e, ev.AnchorFollowUp)
            else e
            for e in result.effects
        )
        result = result.model_copy(update={"effects": effects})
    tx.builder.add(result)
    # The accepted transition creates/anchors the independent day-three task and DONE.
    tx.kind("RecordPatientReply")


def record_day3(
    tx: PatientTurnCommit,
    text: str,
    *,
    source: Provenance | None = None,
    verdict: ScreenVerdict | None = None,
) -> bool:
    candidates = [
        f
        for f in tx.snapshot.followups
        if f.kind == "MEDICATION_DAY3"
        and f.state == "waiting_response"
        and all(ref in tx.snapshot.order_refs for ref in f.order_refs)
    ]
    if len(candidates) != 1 or is_question(text):
        return False
    task = candidates[0]
    value = fact(
        tx,
        text,
        ReportFactPayload(
            report_kind="day3", text=text, target_ref=to_record(task, tx.snapshot.scope).ref
        ),
        source=source,
    )
    danger = verdict is not None and verdict.level != "none"
    result = transition_followup(
        task,
        ev.ResponseReceived(
            event_id=tx.id,
            predicate_result=PredicateResult(
                satisfied=True,
                detail="Patient answered the waiting day-three check-in.",
                evaluated_at=tx.now,
            ),
            objective_received_at=tx.receipt.received_at,
            source_report_ids=(value.id,),
            danger_flag=danger,
        ),
        tx.now,
        tx.builder.policy.timing,
    )
    if danger and isinstance(result, ev.TransitionResult):
        # The independent incident already owns the DANGER through the unchanged gateway.
        result = result.model_copy(
            update={"effects": tuple(e for e in result.effects if not isinstance(e, ev.EmitIntent))}
        )
    tx.builder.add(result)
    tx.kind("RecordPatientReply")
    return True


def record_reading(
    tx: PatientTurnCommit, text: str, result: ReadingResult, *, source: Provenance | None = None
) -> None:
    fact(
        tx,
        text,
        ReportFactPayload(report_kind="reading", text=text, readings=result.values),
        source=source,
    )
    tx.kind("RecordPatientReply")
