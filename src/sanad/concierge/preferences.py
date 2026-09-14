"""Routine preferences do not edit orders, clinical slots or clinical consent."""

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from sanad.auth.service import revise
from sanad.concierge.policy import DRAFT_CONCIERGE_POLICY as POLICY
from sanad.concierge.text import contains, normalized
from sanad.domain import (
    CreateReview,
    ReviewKind,
    SuppressFollowUpContact,
    create_review,
    transition_followup,
)
from sanad.domain.entities import MonitorDetails
from sanad.steward.patient import PatientTurnCommit
from sanad.store.records import PatientProfile

if TYPE_CHECKING:
    from sanad.concierge.records import PatientAction
    from sanad.concierge.reports import ReadingResult
    from sanad.concierge.turn import ConciergeTurn
    from sanad.domain import Mission, Provenance


@dataclass(frozen=True)
class Preference:
    action: Literal["stop", "snooze", "resume", "quiet", "invalid_snooze", "invalid_quiet"]
    interval: timedelta | None = None
    quiet: tuple[str, str] | None = None


def parse(text: str) -> Preference | None:
    clean = normalized(text)
    if contains(
        text,
        "وقف الرسايل",
        "وقف الرسائل",
        "بطل رسايل",
        "وقف التذكيرات",
        "stop reminders",
        "stop messages",
    ):
        return Preference("stop")
    if clean in {
        normalized(v) for v in ("كمّل", "كمل", "رجع التذكيرات", "resume reminders", "resume")
    }:
        return Preference("resume")
    if contains(text, "اجل", "أجّل", "snooze"):
        match = re.search(r"(\d+)\s*(ساع\w*|يوم|ايام|أيام|hours?|days?)", text, re.I)
        if not match or len(match[1]) > 4:
            return Preference("invalid_snooze")
        interval = (
            timedelta(hours=int(match[1]))
            if match[2].lower().startswith(("ساع", "hour"))
            else timedelta(days=int(match[1]))
        )
        return (
            Preference("snooze", interval)
            if timedelta() < interval <= POLICY.snooze_max
            else Preference("invalid_snooze")
        )
    if contains(text, "ساعات الهدوء", "وقت الهدوء", "quiet hours"):
        times = re.findall(r"(?<!\d)([0-2]?\d:[0-5]\d)(?!\d)", text)
        if (
            len(times) == 2
            and all(int(t.split(":")[0]) < 24 for t in times)
            and times[0] != times[1]
        ):
            converted = [f"{int(t.split(':')[0]):02d}:{t.split(':')[1]}" for t in times]
            return Preference("quiet", quiet=(converted[0], converted[1]))
        return Preference("invalid_quiet")
    return None


def scheduled_slots(tx: PatientTurnCommit) -> tuple[tuple[str, str, str], ...]:
    from sanad.monitor.reschedule import slot_id

    result = []
    for m in tx.snapshot.missions:
        if isinstance(m.details, MonitorDetails):
            for i, instant in enumerate(m.details.slots):
                if instant >= tx.now:
                    result.append(
                        (
                            slot_id(m, i),
                            m.title,
                            instant.astimezone(ZoneInfo(tx.snapshot.patient.timezone)).isoformat(),
                        )
                    )
    for f in tx.snapshot.followups:
        if f.prompt_at and f.prompt_at >= tx.now and f.state not in {"fulfilled", "cancelled"}:
            result.append(
                (
                    f.consent_slot_id or f.id,
                    "متابعة اليوم الثالث" if f.kind == "MEDICATION_DAY3" else "متابعة الدكتور",
                    f.prompt_at.astimezone(ZoneInfo(tx.snapshot.patient.timezone)).isoformat(),
                )
            )
    return tuple(result)


def review(tx: PatientTurnCommit, suffix: str) -> None:
    tx.builder.add(
        create_review(
            CreateReview(
                event_id=tx.id + suffix,
                review_kind=ReviewKind.binding_review,
                source_type="patient",
                source_id=tx.snapshot.patient.id + suffix,
                source_version=tx.snapshot.patient.version + 1,
                owner_doctor_id=tx.snapshot.scope.doctor_id,
                patient_id=tx.snapshot.scope.patient_id,
                review_at=tx.now + POLICY.question_due,
            ),
            tx.now,
            tx.builder.policy.timing,
        )
    )


def apply(
    tx: PatientTurnCommit,
    preference: Preference,
    *,
    confirmed: bool = False,
    slot_id: str | None = None,
) -> str:
    if preference.action == "resume" and not confirmed:
        from sanad.domain.language import effective
        from sanad.presentation.patient_browser import CATALOG

        tx.button(
            "resume",
            CATALOG["patient_browser.confirm"][
                effective(tx.snapshot.patient.language, audience="patient")
            ],
        )
        return "patient_resume_ask"
    patient, consent, binding, profile = (
        tx.snapshot.patient,
        tx.snapshot.consent,
        tx.snapshot.binding,
        tx.snapshot.profile,
    )
    active = consent.routine_contact_enabled
    status = patient.contact_status
    pause = patient.resume_at
    quiet = consent.quiet_hours
    slots = consent.scheduled_slot_consents
    if preference.action == "stop":
        active, status, pause = False, "opted_out", None
        review(tx, ":stop")
        for task in tx.snapshot.followups:
            if task.state not in {"fulfilled", "cancelled", "contact_suppressed"}:
                tx.builder.add(
                    transition_followup(
                        task,
                        SuppressFollowUpContact(
                            event_id=tx.id + ":" + task.id, reason="patient_stop"
                        ),
                        tx.now,
                        tx.builder.policy.timing,
                    )
                )
    elif preference.action == "snooze":
        assert preference.interval
        status, pause = "paused", tx.now + preference.interval
    elif preference.action == "resume":
        active, status, pause = True, "active", None
    elif preference.action == "quiet":
        if slot_id:
            slots = tuple(dict.fromkeys((*slots, slot_id)))
        else:
            assert preference.quiet
            quiet = preference.quiet
            conflicting = []
            for slot, _, instant in scheduled_slots(tx):
                time = instant[11:16]
                inside = (
                    quiet[0] <= time < quiet[1]
                    if quiet[0] < quiet[1]
                    else time >= quiet[0] or time < quiet[1]
                )
                if inside:
                    conflicting.append(slot)
                    review(tx, ":quiet:" + slot)
            slots = tuple(s for s in slots if s not in conflicting)
    else:
        return (
            "patient_snooze_clarify"
            if preference.action == "invalid_snooze"
            else "patient_quiet_clarify"
        )
    changed = revise(
        consent,
        tx.now,
        routine_contact_enabled=active,
        quiet_hours=quiet,
        scheduled_slot_consents=slots,
    )
    tx.profile = PatientProfile.model_validate(
        profile.model_dump()
        | {
            "version": profile.version + 1,
            "updated_at": tx.now,
            "delivery_epoch": profile.delivery_epoch + 1,
            "consent_version": changed.version,
            "routine_contact_enabled": active,
            "routine_paused_until": pause,
        }
    )
    tx.put(tx.profile)
    tx.put(
        revise(
            patient,
            tx.now,
            contact_status=status,
            resume_at=pause,
            delivery_epoch=tx.profile.delivery_epoch,
            consent_version=changed.version,
        )
    )
    tx.put(changed)
    tx.put(revise(binding, tx.now, consent_version=changed.version))
    tx.kind("SetContactPreference")
    return {
        "stop": "patient_stop_ack",
        "snooze": "patient_snooze_ack",
        "resume": "patient_resume_ack",
        "quiet": "patient_quiet_ack",
    }[preference.action]


def schedule_reply(tx: PatientTurnCommit, reason: str) -> tuple[str, str, str]:
    from sanad.concierge.templates import render
    from sanad.domain.language import effective

    key = "patient_schedule_" + reason.split(":")[0]
    fields = {"hour": reason.split(":")[1]} if reason.startswith("which_half:") else {}
    return (
        key,
        render(key, effective(tx.snapshot.patient.language, audience="patient"), **fields),
        key,
    )


def schedule_button(
    tx: PatientTurnCommit,
    mission: "Mission",
    times: tuple[str, ...],
    action: Literal["schedule_yes", "schedule_no", "schedule_choose"],
    label: str,
) -> None:
    from secrets import token_urlsafe

    from sanad.concierge.records import PatientAction, ScheduleOffer
    from sanad.monitor.reschedule import tomorrow
    from sanad.store import keys
    from sanad.store.records import to_record

    assert isinstance(mission.details, MonitorDetails)
    raw = token_urlsafe(32)
    tx.put(
        PatientAction(
            id=keys.digest(raw),
            scope=tx.snapshot.scope,
            created_at=tx.now,
            updated_at=tx.now,
            expires_at=tx.now + timedelta(minutes=30),
            actor_subject=tx.principal.subject,
            action=action,
            target_ref=to_record(mission, tx.snapshot.scope).ref,
            source_receipt_id=tx.receipt.id,
            delivery_epoch=tx.profile.delivery_epoch,
            binding_epoch=tx.profile.binding_epoch,
            consent_version=tx.snapshot.consent.version,
            schedule=ScheduleOffer(times=times, effective_date=tomorrow(mission.details, tx.now)),
        )
    )
    tx.buttons.append([{"text": label, "callback_data": raw}])


def schedule_card(
    tx: PatientTurnCommit, mission: "Mission", times: tuple[str, ...], *, confirmed: bool = False
) -> tuple[str, str, str]:
    from html import escape

    from sanad.concierge.templates import render
    from sanad.domain.language import effective
    from sanad.monitor.reschedule import Refused, project, times_on, tomorrow

    language = effective(tx.snapshot.patient.language, audience="patient")
    assert isinstance(mission.details, MonitorDetails)
    day = tomorrow(mission.details, tx.now)
    if not confirmed:
        try:
            project(mission, times, day, tx.receipt.id, tx.now)
        except Refused as exc:
            return schedule_reply(tx, str(exc))
        for action in ("schedule_yes", "schedule_no"):
            schedule_button(tx, mission, times, action, render("patient_" + action, language))
    old = times_on(mission.details, day)
    text = (
        escape(mission.title)
        + ": "
        + ", ".join(old)
        + " → "
        + ", ".join(times)
        + "\n"
        + render("patient_schedule_start", language, date=day.isoformat())
    )
    return "patient_schedule_start", text, "patient_schedule_start"


def schedule_callback(tx: PatientTurnCommit, token: "PatientAction") -> tuple[str, str, str] | None:
    from sanad.concierge.records import PatientAction
    from sanad.concierge.templates import render
    from sanad.domain.language import effective
    from sanad.monitor.reschedule import Refused, eligible, tomorrow
    from sanad.steward.types import records
    from sanad.store.records import from_record, to_record

    if not token.action.startswith("schedule_"):
        return None
    language = effective(tx.snapshot.patient.language, audience="patient")
    stale = (
        "patient_callback_stale",
        render("patient_callback_stale", language),
        "patient_callback_stale",
    )
    if not token.schedule or not token.target_ref:
        return stale
    siblings = [
        from_record(r, PatientAction)
        for r in records(tx.store, tx.snapshot.scope, "patient_action")
        if r.body.get("source_receipt_id") == token.source_receipt_id
        and str(r.body.get("action", "")).startswith("schedule_")
    ]
    if any(s.consumed_at for s in siblings):
        return stale
    tx.consume(token)
    if token.action == "schedule_no":
        tx.schedule_cancelled = True
        return "patient_schedule_no", "", "patient_schedule_no"
    mission = next((m for m in eligible(tx.snapshot, tx.now) if m.id == token.target_ref.id), None)
    if mission is None:
        return schedule_reply(tx, "refused")
    assert isinstance(mission.details, MonitorDetails)
    if (
        token.action == "schedule_choose"
        or token.target_ref != to_record(mission, tx.snapshot.scope).ref
        or token.schedule.effective_date != tomorrow(mission.details, tx.now)
    ):
        return schedule_card(tx, mission, token.schedule.times)
    try:
        tx.reschedule(mission, token.schedule)
    except Refused as exc:
        return schedule_reply(tx, str(exc))
    response = schedule_card(tx, mission, token.schedule.times, confirmed=True)
    changed = from_record(tx.builder.puts[("mission", mission.id)], type(mission))
    assert isinstance(changed.details, MonitorDetails)
    line = schedule_quiet_offer(tx, changed)
    return response[0], "\n".join(filter(None, (response[1], line))), response[2]


def schedule_quiet_slots(
    tx: PatientTurnCommit, mission: "Mission", granted: tuple[str, ...]
) -> tuple[int, ...]:
    """Derive remaining reschedule reminders from accepted history and live state."""
    from sanad.contact.ladder import quiet_at
    from sanad.monitor.reschedule import slot_id
    from sanad.monitor.slots import filled

    details = mission.details
    if (
        not isinstance(details, MonitorDetails)
        or not details.time_history
        or mission.state not in {"open", "waiting_patient", "overdue"}
        or mission.due_at <= tx.now
        or tx.snapshot.patient.contact_status in {"frozen", "awaiting_link"}
        or any(ref not in tx.snapshot.order_refs for ref in mission.order_refs)
    ):
        return ()
    occupied = filled(details)
    moved = {index for entry in details.time_history for index in entry.moved}
    return tuple(
        index
        for index in sorted(moved, key=lambda index: (details.slots[index], index))
        if index not in occupied
        and details.slots[index] >= tx.now
        and slot_id(mission, index) not in granted
        and quiet_at(
            details.slots[index],
            tx.snapshot.patient.timezone,
            tx.snapshot.consent.quiet_hours,
        )
    )


def schedule_quiet_offer(tx: PatientTurnCommit, mission: "Mission") -> str:
    from html import escape

    from sanad.domain.language import effective
    from sanad.monitor.reschedule import slot_id
    from sanad.store.records import Consent, from_record, to_record

    consent_row = tx.builder.puts.get(("consent", tx.snapshot.consent.id))
    consent = from_record(consent_row, Consent) if consent_row else tx.snapshot.consent
    candidates = schedule_quiet_slots(tx, mission, consent.scheduled_slot_consents)
    if not candidates:
        return ""
    index = candidates[0]
    assert isinstance(mission.details, MonitorDetails)
    language = effective(tx.snapshot.patient.language, audience="patient")
    tx.button(
        "quiet_slot",
        ("Allow this reminder: " if language == "en" else "أوافق على التذكير: ") + mission.title,
        target=to_record(mission, tx.snapshot.scope).ref,
        slot=slot_id(mission, index),
        quiet_schedule_generation=mission.details.time_history[-1].generation,
    )
    return (
        escape(mission.title)
        + ": "
        + mission.details.slots[index]
        .astimezone(ZoneInfo(tx.snapshot.patient.timezone))
        .isoformat()
        + (
            "; this reminder needs separate consent."
            if language == "en"
            else "؛ التذكير ده محتاج موافقتك لوحده."
        )
    )


def schedule_quiet_callback(
    tx: PatientTurnCommit, token: "PatientAction"
) -> tuple[str, str, str] | None:
    from sanad.concierge.templates import render
    from sanad.domain.language import effective
    from sanad.monitor.reschedule import slot_id

    if token.action != "quiet_slot" or token.quiet_schedule_generation is None:
        return None
    mission = next(
        (m for m in tx.snapshot.missions if token.target_ref and m.id == token.target_ref.id),
        None,
    )
    language = effective(tx.snapshot.patient.language, audience="patient")
    if (
        mission is None
        or not isinstance(mission.details, MonitorDetails)
        or not mission.details.time_history
        or mission.details.time_history[-1].generation != token.quiet_schedule_generation
        or token.slot_id
        not in {
            slot_id(mission, i)
            for i in schedule_quiet_slots(tx, mission, tx.snapshot.consent.scheduled_slot_consents)
        }
    ):
        key = "patient_callback_stale"
        return key, render(key, language), key
    key = apply(tx, Preference("quiet"), slot_id=token.slot_id, confirmed=True)
    line = schedule_quiet_offer(tx, mission)
    return key, "\n".join(filter(None, (render(key, language), line))), key


def schedule_request(
    turn: "ConciergeTurn",
    tx: PatientTurnCommit,
    text: str,
    reading: "ReadingResult",
    source: "Provenance",
) -> tuple[str, str, str] | None:
    import asyncio

    from sanad.domain import Mission
    from sanad.monitor.reschedule import Refused, eligible, has_time, readers
    from sanad.monitor.slots import metric, requested_metric
    from sanad.store.records import from_record

    payload = tx.receipt.payload or {}
    typed = payload.get("schedule") if tx.receipt.transport == "web-preference" else None
    if isinstance(typed, dict):
        # The durable web receipt owns typed times; the browser supplies no scope.
        row = tx.store.get(tx.snapshot.scope, "mission", str(typed.get("mission_id", "")))
        if not row:
            return schedule_reply(tx, "refused")
        mission = from_record(row, Mission)
        if mission not in eligible(tx.snapshot, tx.now):
            return schedule_reply(tx, "refused")
        raw = typed.get("times")
        if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
            return schedule_reply(tx, "rules")
        return schedule_card(tx, mission, tuple(str(v) for v in raw))
    choices = eligible(tx.snapshot, tx.now)
    if not choices or reading.values or reading.incomplete_bp or not has_time(text):
        return None
    try:
        proposal = asyncio.run(readers(turn, tx, text, source))
    except Refused as exc:
        return schedule_reply(tx, str(exc))
    if proposal is None:
        return None
    target, times = proposal
    named = requested_metric(text)
    if named:
        choices = tuple(
            m
            for m in choices
            if isinstance(m.details, MonitorDetails) and metric(m.details.metric) == named
        )
    if target is not None and not any(m.id == target for m in choices):
        return schedule_reply(tx, "rules")
    if len(choices) != 1:
        if not choices:
            return schedule_reply(tx, "refused")
        from sanad.monitor.reschedule import validate_times

        try:
            validate_times(times, len(times))
        except Refused as exc:
            return schedule_reply(tx, str(exc))
        for mission in choices:
            schedule_button(tx, mission, times, "schedule_choose", mission.title)
        return schedule_reply(tx, "choose")
    return schedule_card(tx, choices[0], times)
