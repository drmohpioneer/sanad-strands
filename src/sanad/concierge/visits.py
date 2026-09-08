"""Patient-reported visits: booking, attendance and documents stay distinct."""

import re
from datetime import datetime, time, timedelta
from secrets import token_urlsafe
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from sanad.concierge.plan import Snapshot
from sanad.concierge.records import PatientAction, ReportFactPayload
from sanad.concierge.reports import fact
from sanad.concierge.text import contains, is_question, normalized
from sanad.domain import Mission, Provenance, ReviewKind, transition_mission
from sanad.domain import events as ev
from sanad.domain.deadlines import local_to_utc
from sanad.domain.entities import TERMINAL_STATES, VisitDetails
from sanad.domain.predicates import PredicateResult
from sanad.steward.patient import PatientTurnCommit
from sanad.steward.types import records
from sanad.store import keys
from sanad.store.records import InboundReceipt, from_record, to_record

if TYPE_CHECKING:
    from sanad.store.protocol import Store
    from sanad.store.records import Consent, Patient

_NEGATIVE = (
    "not",
    "never",
    "didn't",
    "did not",
    "haven't",
    "have not",
    "couldn't",
    "could not",
    "مش",
    "لسه",
    "لم",
    "ما",
    "مرحتش",
    "مارحتش",
    "محجزتش",
    "محضرتش",
)
_DAYS = (
    ("monday", "الاثنين", "الاتنين"),
    ("tuesday", "الثلاثاء", "التلات"),
    ("wednesday", "الاربعاء", "الاربع"),
    ("thursday", "الخميس"),
    ("friday", "الجمعة"),
    ("saturday", "السبت"),
    ("sunday", "الاحد", "الحد"),
)


def is_booked(text: str) -> bool:
    return (
        not is_question(text)
        and contains(text, "booked", "appointment on", "حجزت", "الميعاد يوم")
        and not contains(text, *_NEGATIVE)
    )


def is_attended(text: str) -> bool:
    return (
        not is_question(text)
        and contains(text, "attended", "went", "went to", "رحت", "ورحت", "كشفت", "قابلت الدكتور")
        and not contains(text, *_NEGATIVE)
        and not not_attended(text)
    )


def not_attended(text: str) -> bool:
    return not is_question(text) and contains(
        text,
        "couldn't go",
        "could not go",
        "didn't go",
        "did not attend",
        "postponed",
        "مرحتش",
        "مارحتش",
        "اتأجل",
        "ما رحتش",
    )


def words(text: str) -> set[str]:
    return set(re.findall(r"[^\W\d_]+", normalized(text)))


_FILLER = words(
    "I have it the a an my was to for on at this that visit appointment doctor with "
    "book booked booking arrange arranged went attended done finished completed task "
    "requested request please couldn't could not didn't did go postponed and then "
    "today tomorrow day after yesterday next "
    "انا حجزت الحجز الميعاد يوم الزيارة زيارة رحت كشفت قابلت الدكتور عند للدكتور "
    "عملت خلصت نفذت المطلوب طلب الطلب ده دي و ورحت بعد بكرة بكره النهارده امبارح مرحتش اتأجل"
) | {normalized(day) for group in _DAYS for day in group}


def matches(snapshot: Snapshot, text: str, kind: Literal["VISIT", "TASK"]) -> tuple[Mission, ...]:
    """Only this patient's active titles may resolve a named report."""
    if re.search(r"\b(?:another patient|someone else|for him|for her)\b|\w['’]s\b", text, re.I):
        return ()
    eligible = tuple(
        m
        for m in snapshot.missions
        if m.kind == kind
        and m.state not in TERMINAL_STATES | {"proposed", "awaiting_link"}
        and all(r in snapshot.order_refs for r in m.order_refs)
    )
    named_words = words(text) - _FILLER
    named = tuple(m for m in eligible if (words(m.title) - _FILLER) & named_words)
    if named:
        return named
    return eligible if not named_words else ()


def visit_missions(snapshot: Snapshot, text: str) -> tuple[Mission, ...]:
    return matches(snapshot, text, "VISIT")


def booking_window(text: str, at: datetime, zone: str) -> tuple[datetime, datetime] | None:
    """A single explicit local day, without changing the doctor's deadline."""
    today = at.astimezone(ZoneInfo(zone)).date()
    days = set()
    for value in re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text):
        days.add(datetime.strptime(value, "%Y-%m-%d").date())
    if contains(text, "day after tomorrow", "بعد بكرة", "بعد بكره"):
        days.add(today + timedelta(days=2))
    elif contains(text, "tomorrow", "بكرة", "بكره"):
        days.add(today + timedelta(days=1))
    elif contains(text, "today", "النهارده"):
        days.add(today)
    for index, names in enumerate(_DAYS):
        if contains(text, *names):
            days.add(today + timedelta(days=(index - today.weekday()) % 7))
    if len(days) > 1:
        raise ValueError("ambiguous_booking_date")
    if not days:
        return None
    day = days.pop()
    # Explicit local dates use the existing DST ambiguity gate, never a guessed instant.
    return (
        local_to_utc(datetime.combine(day, time.min), zone),
        local_to_utc(datetime.combine(day + timedelta(days=1), time.min), zone)
        - timedelta(microseconds=1),
    )


def choose(
    tx: PatientTurnCommit,
    missions: tuple[Mission, ...],
    text: str,
    action: Literal["visit_report", "task_report"],
) -> None:
    for mission in missions:
        raw = token_urlsafe(32)
        tx.put(
            PatientAction(
                id=keys.digest(raw),
                scope=tx.snapshot.scope,
                created_at=tx.now,
                updated_at=tx.now,
                expires_at=tx.now + tx.builder.policy.timing.overdue_review_interval,
                actor_subject=tx.principal.subject,
                action=action,
                target_ref=to_record(mission, tx.snapshot.scope).ref,
                source_receipt_id=tx.receipt.id,
                report_text=text,
                delivery_epoch=tx.profile.delivery_epoch,
                binding_epoch=tx.profile.binding_epoch,
                consent_version=tx.profile.consent_version or 1,
            )
        )
        tx.buttons.append([{"text": mission.title, "callback_data": raw}])


def original_time(tx: PatientTurnCommit, original_receipt_id: str | None) -> datetime:
    if original_receipt_id:
        row = tx.store.get(tx.snapshot.scope, "inbound_receipt", original_receipt_id)
        if row:
            receipt = from_record(row, InboundReceipt)
            if receipt.source_subject == tx.principal.subject:
                return receipt.received_at
    return tx.receipt.received_at


def record_visit(
    tx: PatientTurnCommit,
    mission: Mission,
    text: str,
    *,
    source: Provenance | None = None,
    original_receipt_id: str | None = None,
) -> str:
    assert isinstance(mission.details, VisitDetails)
    details = mission.details
    booked, attended, absent = is_booked(text), is_attended(text), not_attended(text)
    at = original_time(tx, original_receipt_id)
    window = None
    if booked:
        try:
            window = booking_window(text, at, tx.snapshot.patient.timezone)
        except ValueError:
            return "patient_visit_date"
    late = bool(window and window[0] > mission.escalation_at)
    if window and not late:
        details = VisitDetails.model_validate(
            details.model_dump()
            | {
                "window_start": window[0],
                "window_end": window[1],
            }
        )
    satisfies = (
        not absent
        and not late
        and (
            (details.objective in {"arrange", "booking_reported"} and booked)
            or (details.objective == "attendance_reported" and attended)
        )
    )
    report: Literal["visit_booking", "visit_attendance", "visit_report_pending"] = (
        "visit_booking"
        if booked and details.objective in {"arrange", "booking_reported"}
        else "visit_report_pending"
        if attended and details.objective == "report_received"
        else "visit_attendance"
        if attended or absent
        else "visit_booking"
    )
    note = "Patient-reported; not verified attendance or a received document."
    if absent:
        note = "Patient reported nonattendance; objective remains unfinished."
    elif late:
        note = "Booking date exceeds the unchanged deadline; doctor decision required."
    elif attended and details.window_start and at < details.window_start:
        note += " Attendance reported before the recorded visit window."
    fact(
        tx,
        text,
        ReportFactPayload(
            report_kind=report,
            text=text,
            target_ref=to_record(mission, tx.snapshot.scope).ref,
            detail=note,
            original_receipt_id=original_receipt_id,
        ),
        source=source,
    )
    base = mission.model_copy(update={"details": details})
    if satisfies:
        tx.builder.add(
            transition_mission(
                base,
                ev.ObjectiveFulfilled(
                    event_id=tx.id,
                    fulfillment_event_id=tx.id,
                    actor_kind="patient",
                    predicate_result=PredicateResult(
                        satisfied=True, detail=note, evaluated_at=tx.now
                    ),
                    objective_received_at=at,
                    danger_flag=False,
                ),
                tx.now,
                tx.builder.policy.timing,
            )
        )
    else:
        # A fact-only report must also prevent the ordinary PatientReplied projection
        # from opening/changing this visit's execution state.
        tx.put(
            Mission.model_validate(
                base.model_dump()
                | {
                    "version": mission.version + 1,
                    "updated_at": tx.now,
                }
            )
        )
    if late and not any(
        r.body.get("source_id") == mission.id
        and r.body.get("review_kind") == "unmet_objective"
        and r.body.get("state") != "resolved"
        for r in records(tx.store, tx.snapshot.scope, "review")
    ):
        tx.builder.effect(
            ev.CreateReview(
                event_id=tx.id + ":late-booking",
                review_kind=ReviewKind.unmet_objective,
                source_type="mission",
                source_id=mission.id,
                source_version=mission.version + 1,
                owner_doctor_id=mission.doctor_id,
                patient_id=mission.patient_id,
                source_mission_id=mission.id,
                review_at=max(tx.now, mission.escalation_at),
            ),
            mission,
        )
    tx.kind("RecordPatientReply")
    if absent:
        return "patient_visit_not_attended"
    if late:
        return "patient_visit_late_booking"
    if attended and details.objective == "report_received":
        return "patient_visit_report_needed"
    if satisfies:
        return (
            "patient_visit_attended"
            if details.objective == "attendance_reported"
            else "patient_visit_booked"
        )
    return "patient_visit_booked_wait" if booked else "patient_visit_booking_needed"


def brief_at(mission: Mission, patient: "Patient", consent: "Consent") -> datetime | None:
    from sanad.concierge.policy import DRAFT_CONCIERGE_POLICY
    from sanad.contact.ladder import after_quiet, computed_local
    from sanad.contact.policy import DRAFT_CONTACT_POLICY

    if (
        not isinstance(mission.details, VisitDetails)
        or mission.details.objective
        not in {
            "attendance_reported",
            "report_received",
        }
        or mission.state not in {"open", "waiting_patient"}
    ):
        return None
    day = (
        (mission.due_at - DRAFT_CONCIERGE_POLICY.visit_brief_offset)
        .astimezone(ZoneInfo(patient.timezone))
        .date()
    )
    return after_quiet(
        computed_local(
            datetime.combine(day, time.fromisoformat(DRAFT_CONTACT_POLICY.chase_local_hour)),
            patient.timezone,
        ),
        patient,
        consent,
    )


def brief_text(store: "Store", mission: Mission, patient: "Patient") -> str:
    from sanad.contact.templates import render
    from sanad.domain.deadlines import format_local

    lines = []
    for row in records(store, patient.scope, "mission"):
        other = from_record(row, Mission)
        if (
            other.kind in {"TEST", "SEND_RECORDS"}
            and other.state
            not in TERMINAL_STATES
            | {
                "proposed",
                "awaiting_link",
            }
            and other.due_at <= mission.due_at
        ):
            lines.append(
                render(
                    "patient_visit_bring_test"
                    if other.kind == "TEST"
                    else "patient_visit_bring_records",
                    patient.language,
                    title=other.title,
                )
            )
    return render(
        "patient_visit_brief",
        patient.language,
        title=mission.title,
        due_local=format_local(mission.due_at, patient.timezone),
        lines="\n".join(lines),
    ).strip()
