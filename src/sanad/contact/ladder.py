"""Pure ladder. Clinical instants are inputs and are never rounded or changed."""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from sanad.contact.policy import ContactPolicy
from sanad.domain.deadlines import AmbiguousLocalTime, local_to_utc
from sanad.domain.entities import TERMINAL_STATES, DoctorTimingPolicy, Mission
from sanad.store.records import Consent, Patient, PatientProfile

type ContactReason = Literal[
    "no_recipient",
    "terminal",
    "proposed",
    "awaiting_link",
    "blocked",
    "unreachable",
    "overdue",
    "limit_reached",
    "opted_out",
    "frozen",
    "after_escalation",
    "question_kind",
]


@dataclass(frozen=True)
class ContactPlan:
    at: datetime
    slot_id: str
    window_end: datetime


def computed_local(local: datetime, zone: str) -> datetime:
    """Only computed contact times choose the later fold/gap; explicit timing still asks."""
    try:
        return local_to_utc(local, zone)
    except AmbiguousLocalTime:
        tz = ZoneInfo(zone)
        return max(local.replace(tzinfo=tz, fold=f).astimezone(UTC) for f in (0, 1))


def quiet_at(at: datetime, timezone: str, hours: tuple[str, str]) -> bool:
    clock = at.astimezone(ZoneInfo(timezone)).strftime("%H:%M")
    start, end = hours
    return start <= clock < end if start < end else clock >= start or clock < end


def after_quiet(at: datetime, patient: Patient, consent: Consent) -> datetime:
    if not quiet_at(at, patient.timezone, consent.quiet_hours):
        return at
    local = at.astimezone(ZoneInfo(patient.timezone))
    end = datetime.combine(local.date(), time.fromisoformat(consent.quiet_hours[1]))
    if end <= local.replace(tzinfo=None):
        end += timedelta(days=1)
    return computed_local(end, patient.timezone)


def reason_for(
    mission: Mission,
    patient: Patient,
    consent: Consent | None,
    profile: PatientProfile,
    policy: ContactPolicy,
) -> ContactReason | None:
    if mission.state in TERMINAL_STATES:
        return "terminal"
    if mission.state == "proposed":
        return "proposed"
    if mission.state == "awaiting_link" or patient.contact_status == "awaiting_link":
        return "awaiting_link"
    if (
        consent is None
        or not profile.binding_active
        or not profile.consent_active
        or not profile.recipient_ref
    ):
        return "no_recipient"
    if patient.contact_status == "frozen":
        return "frozen"
    if (
        consent.withdrawn_at
        or not consent.routine_contact_enabled
        or not profile.routine_contact_enabled
    ):
        return "opted_out"
    if patient.contact_status == "opted_out":
        return "opted_out"
    if mission.kind == "QUESTION":
        return "question_kind"
    # MONITOR uses only confirmed slots. STOP/CHANGE require doctor acknowledgment.
    if mission.kind == "MONITOR" or (
        mission.details.kind == "MEDICATION" and mission.details.action != "START"
    ):
        return "blocked"
    if mission.state == "blocked":
        return "blocked"
    if mission.state == "unreachable" or patient.contact_status == "unreachable":
        return "unreachable"
    if mission.state == "overdue":
        return "overdue"
    if mission.contact_count >= policy.per_mission_chase_limit:
        return "limit_reached"
    if _candidate_at(mission, patient, consent, profile, policy) >= mission.escalation_at:
        return "after_escalation"
    return None


def plan_next_contact(
    mission: Mission,
    patient: Patient,
    consent: Consent,
    profile: PatientProfile,
    policy: ContactPolicy,
    timing: DoctorTimingPolicy,
    now: datetime,
) -> ContactPlan | None:
    """An absent plan has a typed reason from reason_for (or after_escalation).

    A persisted deferred candidate is a floor, not a new clinical anchor.
    """
    if reason_for(mission, patient, consent, profile, policy):
        return None
    at = _candidate_at(mission, patient, consent, profile, policy)
    if at >= mission.escalation_at:
        return None
    day = at.astimezone(ZoneInfo(patient.timezone)).date().isoformat()
    return ContactPlan(
        at, "chase:" + day, min(at + policy.scheduled_prompt_window, mission.escalation_at)
    )


def _candidate_at(
    mission: Mission,
    patient: Patient,
    consent: Consent,
    profile: PatientProfile,
    policy: ContactPolicy,
) -> datetime:
    first = mission.first_chase_accepted_at
    if mission.contact_count == 0:
        base = max(mission.confirmed_at or mission.created_at, consent.accepted_at)
    elif mission.contact_count == 1 and first and mission.due_at - first >= timedelta(days=3):
        base = first + (mission.due_at - first) / 2
    else:
        base = mission.due_at - timedelta(days=1)
    local = base.astimezone(ZoneInfo(patient.timezone))
    at = computed_local(
        datetime.combine(local.date(), time.fromisoformat(policy.chase_local_hour)),
        patient.timezone,
    )
    floors = [at]
    for floor in (profile.routine_paused_until, mission.resume_at, mission.next_contact_at):
        if floor:
            floors.append(floor)
    if profile.last_chase_accepted_at:
        floors.append(profile.last_chase_accepted_at + policy.chase_min_gap)
    at = after_quiet(max(floors), patient, consent)
    return at


def tomorrow(now: datetime, patient: Patient, consent: Consent, policy: ContactPolicy) -> datetime:
    day = now.astimezone(ZoneInfo(patient.timezone)).date() + timedelta(days=1)
    return after_quiet(
        computed_local(
            datetime.combine(day, time.fromisoformat(policy.chase_local_hour)), patient.timezone
        ),
        patient,
        consent,
    )
