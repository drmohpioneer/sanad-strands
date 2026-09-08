"""Explicit reports are observations, never diagnoses or inferred adherence."""

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter

from sanad.concierge.plan import Snapshot
from sanad.concierge.policy import DRAFT_CONCIERGE_POLICY as POLICY
from sanad.concierge.records import PatientAction, Reading, ReportFactPayload
from sanad.concierge.text import contains, is_question, normalized
from sanad.domain import Mission, Provenance, transition_followup, transition_mission
from sanad.domain import events as ev
from sanad.domain.entities import MedicationDetails, ReviewKind
from sanad.domain.predicates import PredicateResult
from sanad.safety import find_bp, grade_bp, grade_lab
from sanad.safety.models import LabCandidate, LabVerdict, Quantity, ScreenVerdict, VitalVerdict
from sanad.safety.policy import SafetyPolicy
from sanad.scribe.extract import OrderCandidate
from sanad.scribe.names import entry_for
from sanad.scribe.records import ClinicalFact
from sanad.steward.apply import EffectsRejected
from sanad.steward.patient import PatientTurnCommit
from sanad.store import keys
from sanad.store.records import OutboundIntent, PendingStartClarification, from_record, to_record


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
        not _acknowledgment_question(text)
        and contains(text, "started", "took", "بدأت", "وبدأت", "بديت", "خدت")
        and not _negated(text, "start")
    )


def ambiguous_start_time(text: str) -> bool:
    return contains(
        text,
        "a while ago",
        "long ago",
        "last week",
        "last month",
        "don't remember",
        "cannot remember",
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


def medication_missions(
    snapshot: Snapshot,
    text: str,
    action: Literal["START", "STOP", "CHANGE"],
    *,
    barrier: bool = False,
) -> tuple[Mission, ...]:
    orders = (*snapshot.orders, *snapshot.stopped_orders)
    eligible = tuple(
        m
        for m in snapshot.missions
        if isinstance(m.details, MedicationDetails)
        and m.details.action == action
        and m.details.order_ref in {to_record(o, snapshot.scope).ref for o in orders}
        and all(r in snapshot.acknowledgment_refs for r in m.order_refs)
    )
    names = {
        o.order_id: o.structured_instruction.drug
        for o in orders
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
    if any(mentions(name) for name in names.values()):
        return ()
    if (
        barrier
        or _generic_report(text)
        or normalized(text)
        in {
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
        }
    ):
        return eligible
    # A named medicine with no current START cannot fulfill another medicine's mission.
    return ()


def start_missions(snapshot: Snapshot, text: str) -> tuple[Mission, ...]:
    return medication_missions(snapshot, text, "START")


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
    tx: PatientTurnCommit,
    mission: Mission,
    text: str,
    *,
    source: Provenance | None = None,
    effective_start: datetime | None = None,
    anchor_unknown: bool = False,
) -> None:
    original = mission
    if mission.state in {"blocked", "overdue"} and mission.barrier_reason:
        resolved = transition_mission(
            mission,
            ev.BarrierResolved(event_id=tx.id + ":barrier-resolved:" + mission.id),
            tx.now,
            tx.builder.policy.timing,
        )
        if isinstance(resolved, ev.TransitionRejected):
            raise EffectsRejected(resolved.reason_code)
        assert isinstance(resolved.aggregate, Mission)
        # Both domain events share one stored revision and one fenced transaction.
        mission = resolved.aggregate.model_copy(update={"version": original.version})
        tx.builder.audit("BARRIER_RESOLVED", tx.id + ":barrier-resolved:" + mission.id, ())
    predicate = PredicateResult(
        satisfied=True,
        detail="Explicit START self-reported; not verified.",
        evaluated_at=tx.now,
    )
    fact(
        tx,
        text,
        ReportFactPayload(
            report_kind="medication_start",
            text=text,
            target_ref=to_record(mission, tx.snapshot.scope).ref,
            effective_start=effective_start,
            anchor_unknown=anchor_unknown,
        ),
        source=source,
    )
    event = ev.ObjectiveFulfilled(
        event_id=tx.id + ":start:" + mission.id,
        predicate_result=predicate,
        objective_received_at=tx.receipt.received_at,
        fulfillment_event_id=tx.id + ":start:" + mission.id,
        danger_flag=False,
        actor_kind="patient",
    )
    result = transition_mission(mission, event, tx.now, tx.builder.policy.timing)
    if isinstance(result, ev.TransitionResult):
        effective = effective_start or tx.receipt.received_at
        if effective_start is None and contains(text, "امبارح", "yesterday"):
            effective = effective.astimezone(ZoneInfo(tx.snapshot.patient.timezone)) - timedelta(
                days=1
            )
        effects = tuple(
            e.model_copy(
                update={
                    "anchor_time": effective,
                    "allow_elapsed_deadline": effective_start is not None
                    and timedelta()
                    <= tx.receipt.received_at - effective_start
                    <= POLICY.start_anchor_max_age,
                }
            )
            if isinstance(e, ev.AnchorFollowUp)
            else e
            for e in result.effects
            if not (anchor_unknown and isinstance(e, ev.AnchorFollowUp))
        )
        result = result.model_copy(update={"effects": effects})
    tx.builder.add(result)
    # The accepted transition creates/anchors the independent day-three task and DONE.
    tx.kind("RecordPatientReply")
    if not anchor_unknown:
        tx.builder.effect(
            ev.ResolveReviewEffect(
                review_kind=ReviewKind.unmet_objective,
                source_type="mission",
                source_id=mission.id,
                reason="self-reported start",
            ),
            result.aggregate if isinstance(result, ev.TransitionResult) else None,
        )


def record_day3(
    tx: PatientTurnCommit,
    text: str,
    *,
    source: Provenance | None = None,
    verdict: ScreenVerdict | None = None,
    treatment_change: bool = False,
) -> bool:
    candidates = [
        f
        for f in tx.snapshot.followups
        if f.kind == "MEDICATION_DAY3"
        and f.state == "waiting_response"
        and all(ref in tx.snapshot.order_refs for ref in f.order_refs)
    ]
    if len(candidates) != 1 or (
        is_question(text) and not treatment_change and recognize_barrier(text) is None
    ):
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
    obstacle = recognize_barrier(text)
    report_ids: tuple[str, ...] = (value.id,)
    if obstacle:
        barrier_fact = fact(
            tx,
            text,
            ReportFactPayload(
                report_kind="barrier",
                text=text,
                barrier_type=obstacle,
                target_ref=to_record(task, tx.snapshot.scope).ref,
            ),
            source=source,
        )
        report_ids += (barrier_fact.id,)
    result = transition_followup(
        task,
        ev.ResponseReceived(
            event_id=tx.id,
            predicate_result=PredicateResult(
                satisfied=True,
                detail="Day-three experience self-reported; not verified.",
                evaluated_at=tx.now,
            ),
            objective_received_at=tx.receipt.received_at,
            source_report_ids=report_ids,
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


type BarrierType = Literal[
    "cost", "availability", "forgot", "confusion", "side_effect_experience", "other"
]
type MedicationReply = tuple[str, dict[str, str]]


def _acknowledgment_question(text: str) -> bool:
    # These explicit inability reports are not questions. Leave uncertain
    # wording such as "can't remember if I started" under the existing guard.
    body = re.sub(r"\bcan['’]t (?=afford\b|find\b|do it\b)", "cannot ", text, flags=re.I)
    return is_question(body)


def _negated(text: str, action: str) -> bool:
    low = normalized(text)
    negative = {
        "start": ("مبدأتش", "مبداتش", "ماخدتش", "didn't start", "did not start", "haven't started"),
        "stop": ("موقفتش", "مبطلتش", "didn't stop", "did not stop", "haven't stopped"),
        "change": ("مغيرتش", "didn't change", "did not change", "haven't changed", "haven't taken"),
    }
    return contains(
        text, "not", "never", "didn't", "haven't", "hasn't", "مش", "لسه", "لم", "ما"
    ) or any(normalized(word) in low for word in negative[action])


def is_stop(text: str) -> bool:
    return (
        not _acknowledgment_question(text)
        and contains(text, "stopped", "وقفت", "بطلت", "مبقتش اخد")
        and not _negated(text, "stop")
    )


def is_change(text: str) -> bool:
    return (
        not _acknowledgment_question(text)
        and contains(
            text, "changed", "new dose", "غيرت", "بدأت الجرعة الجديدة", "خدت الجرعة الجديدة"
        )
        and not _negated(text, "change")
    )


def stop_missions(snapshot: Snapshot, text: str) -> tuple[Mission, ...]:
    return medication_missions(snapshot, text, "STOP")


def change_missions(snapshot: Snapshot, text: str) -> tuple[Mission, ...]:
    return medication_missions(snapshot, text, "CHANGE")


_WEEKDAYS = (
    ("monday", "الاثنين", "الاتنين"),
    ("tuesday", "الثلاثاء", "التلات"),
    ("wednesday", "الاربعاء", "الاربع"),
    ("thursday", "الخميس"),
    ("friday", "الجمعة"),
    ("saturday", "السبت"),
    ("sunday", "الاحد", "الحد"),
)
_DATE_PATTERN = (
    r"\d{4}-\d{2}-\d{2}|(?:\d+ days? ago)|(?:من \d+ ايام)|"
    r"day before yesterday|today|yesterday|tomorrow|النهارده|امبارح|اول امبارح|بكره|بكرة|"
    + "|".join(normalized(v) for names in _WEEKDAYS for v in names)
)


def _date_text(text: str) -> str:
    without_iso = re.sub(r"\d{4}-\d{2}-\d{2}", "", text)
    return re.sub(r"(?<!\w)(?:" + _DATE_PATTERN + r")(?!\w)", "", normalized(without_iso))


def _generic_report(text: str) -> bool:
    remaining = _date_text(text)
    # Unrecognized drug names remain, so they cannot acknowledge another drug.
    fillers = (
        "i",
        "have",
        "it",
        "the",
        "medicine",
        "medication",
        "medicines",
        "my",
        "one",
        "of",
        "started",
        "took",
        "stopped",
        "changed",
        "new",
        "old",
        "dose",
        "and",
        "on",
        "done",
        "a",
        "while",
        "ago",
        "long",
        "last",
        "week",
        "month",
        "day",
        "finally",
        "بدأت",
        "وبدأت",
        "بديت",
        "خدت",
        "وقفت",
        "بطلت",
        "غيرت",
        "الدوا",
        "الدواء",
        "العلاج",
        "الحباية",
        "واحد",
        "من",
        "الادوية",
        "القديم",
        "الجديد",
        "الجرعة",
        "الجديدة",
        "و",
        "يوم",
        "فترة",
        "زمان",
        "اسبوع",
        "شهر",
        "انا",
        "لقيته",
        "اخيرا",
    )
    remaining = re.sub(
        r"(?<!\w)(?:" + "|".join(map(re.escape, map(normalized, fillers))) + r")(?!\w)",
        "",
        remaining,
    )
    return not re.search(r"\w", remaining)


def reported_date(text: str, now: datetime, timezone: str) -> tuple[bool, datetime | None]:
    """A date selects a local calendar day; it never invents a prescribed instant."""
    local = now.astimezone(ZoneInfo(timezone))
    low = normalized(text)
    selected: date | None = None
    iso = re.findall(r"\d{4}-\d{2}-\d{2}", text)
    if iso:
        try:
            if len(set(iso)) != 1:
                return True, None
            selected = date.fromisoformat(iso[0])
        except ValueError:
            return True, None
    elif contains(text, "day before yesterday", "أول امبارح"):
        selected = local.date() - timedelta(days=2)
    elif contains(text, "today", "النهارده"):
        selected = local.date()
    elif contains(text, "yesterday", "امبارح"):
        selected = local.date() - timedelta(days=1)
    elif contains(text, "tomorrow", "بكره", "بكرة"):
        return True, None
    else:
        relative = re.search(r"(?:(\d+) days? ago|من (\d+) ايام)", low)
        if relative:
            days = int(relative[1] or relative[2])
            if days > 36500:
                return True, None
            selected = local.date() - timedelta(days=days)
        else:
            weekdays = [i for i, names in enumerate(_WEEKDAYS) if contains(text, *names)]
            if len(weekdays) > 1:
                return True, None
            if weekdays:
                selected = local.date() - timedelta(days=(local.weekday() - weekdays[0]) % 7)
    if selected is None:
        return False, None
    if selected > local.date():
        return True, None
    from sanad.domain.deadlines import local_to_utc

    try:
        effective = local_to_utc(
            datetime.combine(selected, local.time().replace(tzinfo=None)), timezone
        )
    except ValueError:
        return True, None
    return True, effective


def date_only(text: str) -> bool:
    return not re.search(r"\w", re.sub(r"(?<!\w)(?:on|يوم)(?!\w)", "", _date_text(text)))


@lru_cache(maxsize=1)
def barrier_seeds() -> dict[BarrierType, tuple[str, ...]]:
    path = Path(__file__).parents[1] / POLICY.barrier_seed
    values = json.loads(path.read_text(encoding="utf-8"))
    values = TypeAdapter(dict[BarrierType, dict[Literal["en", "ar"], list[str]]]).validate_python(
        {k: v for k, v in values.items() if k != "OWNER_REVIEW_PENDING"}
    )
    return {kind: tuple((*phrases["en"], *phrases["ar"])) for kind, phrases in values.items()}


def recognize_barrier(text: str) -> BarrierType | None:
    # A personal confusion report is a barrier; a question quoting a barrier is not.
    question_text = re.sub(r"\bcan['’]t\b", "cannot", text, flags=re.I)
    if is_question(question_text) and normalized(text) not in {
        normalized("ازاي اخده"),
        "how to take it",
    }:
        return None
    return next(
        (kind for kind, phrases in barrier_seeds().items() if contains(text, *phrases)), None
    )


def pending_start(tx: PatientTurnCommit, mission: Mission | None) -> None:
    pending = (
        PendingStartClarification(
            mission_ref=to_record(mission, tx.snapshot.scope).ref,
            asked_at=tx.now,
            expires_at=tx.now + POLICY.start_clarification_window,
        )
        if mission
        else None
    )
    if pending == tx.profile.pending_start_clarification:
        return
    from sanad.auth.service import revise

    profile = revise(tx.snapshot.profile, tx.now, pending_start_clarification=pending)
    tx.profile = profile
    tx.builder.puts[(profile.entity_type, profile.id)] = to_record(profile, tx.snapshot.scope)
    tx.kind("RecordPatientReply")


def _record_ack(
    tx: PatientTurnCommit,
    mission: Mission,
    text: str,
    action: Literal["stop", "change"],
    source: Provenance | None,
) -> None:
    fact(
        tx,
        text,
        ReportFactPayload(
            report_kind="medication_stop" if action == "stop" else "medication_change",
            text=text,
            target_ref=to_record(mission, tx.snapshot.scope).ref,
        ),
        source=source,
    )
    result = transition_mission(
        mission,
        ev.ObjectiveFulfilled(
            event_id=tx.id + ":" + action + ":" + mission.id,
            predicate_result=PredicateResult(
                satisfied=True, detail="self-reported; not verified", evaluated_at=tx.now
            ),
            objective_received_at=tx.receipt.received_at,
            fulfillment_event_id=tx.id + ":" + action + ":" + mission.id,
            danger_flag=False,
            actor_kind="patient",
        ),
        tx.now,
        tx.builder.policy.timing,
    )
    tx.builder.add(result)
    if action == "stop":
        # This is a doctor report about a completed STOP, not active-order guidance.
        for id, row in tuple(tx.builder.intents.items()):
            if any(r.id == mission.id for r in from_record(row, OutboundIntent).source_versions):
                intent = from_record(row, OutboundIntent).model_copy(update={"order_refs": ()})
                tx.builder.intents[id] = to_record(intent, tx.snapshot.scope)
    tx.kind("RecordPatientReply")


def record_stop(
    tx: PatientTurnCommit, mission: Mission, text: str, *, source: Provenance | None = None
) -> None:
    _record_ack(tx, mission, text, "stop", source)


def record_change(
    tx: PatientTurnCommit, mission: Mission, text: str, *, source: Provenance | None = None
) -> None:
    _record_ack(tx, mission, text, "change", source)


def record_barrier(
    tx: PatientTurnCommit,
    mission: Mission,
    text: str,
    obstacle: BarrierType,
    *,
    source: Provenance | None = None,
) -> None:
    fact(
        tx,
        text,
        ReportFactPayload(
            report_kind="barrier",
            text=text,
            barrier_type=obstacle,
            target_ref=to_record(mission, tx.snapshot.scope).ref,
        ),
        source=source,
    )
    result = transition_mission(
        mission,
        ev.BarrierRecorded(
            event_id=tx.id + ":barrier:" + mission.id,
            barrier_type=obstacle,
            reason=text,
            resume_at=tx.now + POLICY.barrier_resume_after,
        ),
        tx.now,
        tx.builder.policy.timing,
    )
    if isinstance(result, ev.TransitionResult):
        result = result.model_copy(
            update={
                "effects": tuple(
                    e.model_copy(update={"order_refs": mission.order_refs})
                    if isinstance(e, ev.SuppressRoutineIntents)
                    else e
                    for e in result.effects
                )
            }
        )
    tx.builder.add(result)
    tx.kind("RecordPatientReply")


def _start(
    tx: PatientTurnCommit, mission: Mission, text: str, source: Provenance
) -> MedicationReply:
    dated, effective = reported_date(text, tx.receipt.received_at, tx.snapshot.patient.timezone)
    if (dated and effective is None) or (not dated and ambiguous_start_time(text)):
        pending_start(tx, mission)
        return "patient_start_date", {}
    unknown = (
        effective is not None and tx.receipt.received_at - effective > POLICY.start_anchor_max_age
    )
    if tx.profile.pending_start_clarification:
        pending_start(tx, None)
    if dated:
        fact(
            tx,
            text,
            ReportFactPayload(
                report_kind="start_date",
                text=text,
                target_ref=to_record(mission, tx.snapshot.scope).ref,
                effective_start=None if unknown else effective,
                anchor_unknown=unknown,
            ),
            source=source,
        )
    record_start(
        tx,
        mission,
        text,
        source=source,
        effective_start=None if unknown else effective,
        anchor_unknown=unknown,
    )
    return ("patient_start_unknown", {}) if unknown else ("_start", {})


def medication_reply(
    tx: PatientTurnCommit, text: str, *, source: Provenance
) -> MedicationReply | None:
    from sanad.concierge import question

    stop, change, start = is_stop(text), is_change(text), is_start(text)
    starts = start_missions(tx.snapshot, text) if start else ()
    changes = change_missions(tx.snapshot, text) if change or (start and not starts) else ()
    stops = stop_missions(tx.snapshot, text) if stop else ()
    if stop and start and len(stops) == len(starts) == 1:
        # A date question must not partially acknowledge a combined report.
        dated, effective = reported_date(text, tx.receipt.received_at, tx.snapshot.patient.timezone)
        if (dated and effective is None) or (not dated and ambiguous_start_time(text)):
            pending_start(tx, starts[0])
            return "patient_start_date", {}
        record_stop(tx, stops[0], text, source=source)
        _start(tx, starts[0], text, source)
        return "patient_stop_start_recorded", {}
    action = (
        "stop"
        if stop
        else "change"
        if change or (start and not starts and len(changes) == 1)
        else "start"
        if start
        else None
    )
    if action:
        choices = stops if action == "stop" else changes if action == "change" else starts
        if len(choices) == 1:
            if action == "start":
                return _start(tx, choices[0], text, source)
            (record_stop if action == "stop" else record_change)(
                tx, choices[0], text, source=source
            )
            return "patient_" + action + "_recorded", {}
        if choices:
            for mission in choices:
                medication_button(tx, mission, action, text)
            return "patient_" + action + "_choose", {}
        question.open_ticket(tx, text)
        return "patient_" + action + "_missing", {}
    pending = tx.profile.pending_start_clarification
    if pending and pending.expires_at <= tx.now:
        if date_only(text):
            pending_start(tx, None)
            return "patient_start_date_expired", {}
    if date_only(text) and text.strip():
        pending_mission = next(
            (
                m
                for m in medication_missions(tx.snapshot, "started it", "START")
                if pending and m.id == pending.mission_ref.id
            ),
            None,
        )
        if pending and pending.expires_at > tx.now and pending_mission:
            return _start(tx, pending_mission, text, source)
        if pending and tx.profile.pending_start_clarification:
            pending_start(tx, None)
        return "patient_start_date_expired", {}
    obstacle = recognize_barrier(text)
    if obstacle:
        from sanad.concierge.barriers import route

        return route(tx, text, source)
    return None


def medication_button(tx: PatientTurnCommit, mission: Mission, action: str, text: str) -> None:
    target = to_record(mission, tx.snapshot.scope).ref
    slot = "medication_" + action
    tx.button("start", mission.title, target=target, slot=slot)
    # Keep the screened words with the choice: a voice receipt has no text payload.
    for key, row in tuple(tx.builder.puts.items()):
        if row.entity_type == "patient_action" and row.version == 1:
            choice = from_record(row, PatientAction)
            if choice.target_ref == target and choice.slot_id == slot:
                tx.builder.puts[key] = to_record(
                    choice.model_copy(update={"medication_report_text": text}), tx.snapshot.scope
                )


def medication_callback(
    tx: PatientTurnCommit, token: object, source: Provenance
) -> MedicationReply | None:
    from sanad.store.records import InboundReceipt

    assert isinstance(token, PatientAction)
    if token.action != "start" or not (token.slot_id or "").startswith("medication_"):
        return None
    action = (token.slot_id or "").removeprefix("medication_")
    kind: Literal["START", "STOP", "CHANGE"] = (
        "STOP" if action == "stop" else "CHANGE" if action == "change" else "START"
    )
    mission = next(
        (
            m
            for m in medication_missions(tx.snapshot, "", kind, barrier=True)
            if to_record(m, tx.snapshot.scope).ref == token.target_ref
        ),
        None,
    )
    receipt_row = tx.store.get(tx.snapshot.scope, "inbound_receipt", token.source_receipt_id)
    if mission is None or receipt_row is None:
        return "patient_callback_stale", {}
    prior = from_record(receipt_row, InboundReceipt)
    text = token.medication_report_text or str((prior.payload or {}).get("text", ""))
    if not text.strip():
        return "patient_callback_stale", {}
    if action == "barrier":
        obstacle = recognize_barrier(text)
        if obstacle is None:
            return "patient_callback_stale", {}
        record_barrier(tx, mission, text, obstacle, source=source)
        return "patient_barrier_recorded", {"drug": mission.title}
    if action == "start":
        return _start(tx, mission, text, source)
    (record_stop if action == "stop" else record_change)(tx, mission, text, source=source)
    return "patient_" + action + "_recorded", {}
