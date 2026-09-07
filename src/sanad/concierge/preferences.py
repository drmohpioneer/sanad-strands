"""Routine preferences do not edit orders, clinical slots or clinical consent."""

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal
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
    result = []
    for m in tx.snapshot.missions:
        if isinstance(m.details, MonitorDetails):
            for i, instant in enumerate(m.details.slots):
                if instant >= tx.now:
                    result.append(
                        (
                            f"{m.id}:{i}",
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
        tx.button("resume", "أيوه، رجّع التذكيرات")
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
