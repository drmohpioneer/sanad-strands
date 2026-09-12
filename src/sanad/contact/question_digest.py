"""Durable doctor-time question rings; current records own every rendered line."""

import re
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from pydantic import JsonValue

from sanad.auth.service import revise
from sanad.channels.telegram import wording
from sanad.concierge.answer_command import active_orders, owned_questions
from sanad.concierge.plan import order_line
from sanad.domain import (
    DRAFT_POLICY_2026_09,
    Mission,
    PatientScope,
    Principal,
    ReviewObligation,
    TenantScope,
    VersionRef,
)
from sanad.domain.entities import QuestionDetails
from sanad.steward.types import StewardPolicy, records
from sanad.store import keys
from sanad.store.protocol import Store
from sanad.store.records import (
    AuditEvent,
    BundleSchedule,
    CommandEnvelope,
    CommitRequest,
    Doctor,
    DoctorAuthority,
    OperationalClock,
    OutboundIntent,
    Patient,
    QuestionDigestSchedule,
    StoredRecord,
    WorkerCapability,
    from_record,
    to_record,
)

if TYPE_CHECKING:
    from sanad.steward.service import Steward

LIMIT = 20


def parse_setting(argument: str, doctor: Doctor) -> dict[str, str] | None:
    parts = argument.split()
    values = {"digest_time": doctor.digest_time, "digest_packing": doctor.digest_packing}
    if parts and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", parts[0]):
        values["digest_time"] = parts.pop(0)
    if parts and parts[0] in {"one", "each"}:
        values["digest_packing"] = parts.pop(0)
    return None if parts else values


def next_instant(doctor: Doctor, now: datetime) -> datetime:
    zone = ZoneInfo(doctor.timezone)
    date = now.astimezone(zone).date()
    while True:
        local = datetime.combine(date, time.fromisoformat(doctor.digest_time))
        while True:
            first = local.replace(tzinfo=zone, fold=0).astimezone(UTC)
            if first.astimezone(zone).replace(tzinfo=None) == local:
                break
            local += timedelta(minutes=1)
        if first > now:
            return first
        date += timedelta(days=1)


def load(store: Store, scope: TenantScope) -> QuestionDigestSchedule | None:
    row = store.get(scope, "question_digest_schedule", scope.doctor_id)
    return from_record(row, QuestionDigestSchedule) if row else None


def arm(
    store: Store, doctor: Doctor, now: datetime, *, setting: bool = False
) -> QuestionDigestSchedule | None:
    old = load(store, doctor.scope)
    if old and old.pending_intent_id:
        return None if setting else revise(old, now)
    if old and old.pending_each:
        for patient_id, intent_id in old.pending_each:
            pending_row = store.get(
                PatientScope(doctor_id=doctor.id, patient_id=patient_id),
                "outbound_intent",
                intent_id,
            )
            if pending_row:
                pending = from_record(pending_row, OutboundIntent)
                if (
                    pending.status in {"queued", "sending", "uncertain", "failed"}
                    or pending.notice_feedback_pending
                ):
                    return None if setting else revise(old, now)
    if old and old.next_action_at is not None and not setting:
        # Fence an empty-clear or acceptance racing a newly overdue question.
        return revise(old, now)
    if setting and (old is None or old.next_action_at is None):
        return None
    at = next_instant(doctor, now)
    clock = OperationalClock(
        work_lane="question_digest", next_action_at=at, work_generation=old.generation if old else 1
    )
    if old:
        return revise(old, now, next_action_at=at, work_clock=clock, pending_each=())
    return QuestionDigestSchedule(
        id=doctor.id,
        doctor_id=doctor.id,
        scope=doctor.scope,
        next_action_at=at,
        work_clock=clock,
        created_at=now,
        updated_at=now,
    )


def setting_schedule(store: Store, doctor: Doctor, now: datetime) -> QuestionDigestSchedule | None:
    return arm(store, doctor, now, setting=True)


def due(store: Store, scope: TenantScope) -> list[tuple[Patient, Mission, ReviewObligation]]:
    result = []
    for patient, mission in owned_questions(store, scope.doctor_id):
        if mission.state != "overdue":
            continue
        review = next(
            (
                from_record(r, ReviewObligation)
                for r in records(store, patient.scope, "review")
                if r.body.get("source_id") == mission.id
                and r.body.get("review_kind") == "question_answer"
                and r.body.get("state") in {"open", "acknowledged"}
            ),
            None,
        )
        if review:
            result.append((patient, mission, review))
    schedule = load(store, scope)
    shown = set(schedule.last_shown_ids if schedule else ())
    return sorted(
        result,
        key=lambda x: (identity(x[0].id, x[1].id) in shown, x[1].created_at, x[0].id, x[1].id),
    )


def identity(patient_id: str, mission_id: str) -> str:
    return keys.digest(patient_id + ":" + mission_id)


def capture(
    store: Store, intent: OutboundIntent
) -> tuple[
    Doctor,
    list[tuple[Patient, Mission, ReviewObligation]],
    tuple[str, ...],
    tuple[StoredRecord, ...],
    int,
]:
    assert isinstance(intent.scope, TenantScope)
    tenant = TenantScope(doctor_id=intent.scope.doctor_id)
    doctor_row = store.get(tenant, "doctor", tenant.doctor_id)
    schedule_row = store.get(tenant, "question_digest_schedule", tenant.doctor_id)
    assert doctor_row and schedule_row
    doctor = from_record(doctor_row, Doctor)
    rows = [doctor_row, schedule_row]
    values = due(store, tenant)
    selected = values[:LIMIT]
    if individual(intent):
        schedule = from_record(schedule_row, QuestionDigestSchedule)
        current = {(p.id, m.id): (p, m, r) for p, m, r in values}
        selected = []
        for patient_id, intent_id in schedule.pending_each:
            scope = PatientScope(doctor_id=doctor.id, patient_id=patient_id)
            pending = store.get(scope, "outbound_intent", intent_id)
            if pending is None:
                continue
            outgoing = from_record(pending, OutboundIntent)
            mission_id = next(
                (r.id for r in outgoing.source_versions if r.entity_type == "mission"), ""
            )
            value = current.get((patient_id, mission_id))
            if value:
                selected.append(value)
    from sanad.concierge.reuse import choose
    from sanad.liaison.records import ReusableAnswer

    answers = tuple(
        from_record(r, ReusableAnswer) for r in records(store, tenant, "reusable_answer")
    )
    contexts = []
    for patient, mission, review in selected:
        rows.extend(
            (
                to_record(patient, patient.scope),
                to_record(mission, patient.scope),
                to_record(review, patient.scope),
            )
        )
        orders = active_orders(store, patient.scope)
        context = "No active medication plan is recorded."
        if orders:
            order = orders[0]
            context = order_line(order, doctor.language)
            rows.append(to_record(order, patient.scope))
        # Heads and order authority determine which plan line is currently active.
        rows.extend(records(store, patient.scope, "care_order_head"))
        rows.extend(records(store, patient.scope, "care_order"))
        assert isinstance(mission.details, QuestionDetails)
        reusable = choose(answers, mission.details.question_text)
        if reusable:
            rows.append(to_record(reusable, tenant))
        contexts.append(context)
    return doctor, selected, tuple(contexts), tuple(rows), len(values) - len(selected)


def snapshot_rows(store: Store, intent: OutboundIntent) -> tuple[StoredRecord, ...]:
    return capture(store, intent)[3]


def payload_snapshot(
    store: Store, intent: OutboundIntent, now: datetime
) -> tuple[dict[str, JsonValue], tuple[VersionRef, ...]]:
    doctor, selected, contexts, rows, overflow = capture(store, intent)
    from sanad.liaison.records import ReusableAnswer

    answers = tuple(
        from_record(r, ReusableAnswer) for r in rows if r.entity_type == "reusable_answer"
    )
    lines = []
    shortened = False
    line_budget = 3500 // max(1, len(selected))

    def clip(value: str, limit: int) -> str:
        nonlocal shortened
        value = " ".join(value.split())
        if len(value) <= limit:
            return value
        shortened = True
        return value[: limit - 1] + "…"

    for n, ((patient, mission, _), context) in enumerate(zip(selected, contexts, strict=True), 1):
        if (
            isinstance(intent.scope, PatientScope)
            and individual(intent)
            and (
                patient.id != intent.scope.patient_id
                or mission.id
                not in {r.id for r in intent.source_versions if r.entity_type == "mission"}
            )
        ):
            continue
        assert isinstance(mission.details, QuestionDetails)
        hours = max(0, int((now - mission.created_at).total_seconds() // 3600))
        from sanad.concierge.reuse import proposal_line

        proposed = proposal_line(
            store, mission, n, limit=min(160, max(16, line_budget // 3)), answers=answers
        )
        actions = f"Waiting: {hours} hours.\n/answer {n} … · /close {n} · /defer {n}"
        actions = proposed + "\n" + actions
        available = line_budget - len(actions) - len(str(n)) - 7
        name = clip(patient.display_name, max(4, available // 4))
        plan = clip(context, max(4, available // 3))
        question = clip(mission.details.question_text, max(4, available - len(name) - len(plan)))
        lines.append(f"{n}. {name}\n{plan}\n{question}\n{actions}")
    if overflow:
        lines.append(f"… and {overflow} more, /questions")
    elif shortened:
        lines.append("Open /questions to read more.")
    return (
        {
            "text": wording.render(
                "doctor_question_digest", doctor.language, lines="\n\n".join(lines)
            )
        },
        tuple(r.ref for r in rows),
    )


def listing_targets(store: Store, intent: OutboundIntent) -> tuple[tuple[str, str], ...]:
    assert type(intent.scope) is TenantScope
    return tuple((p.id, m.id) for p, m, _ in due(store, intent.scope)[:LIMIT])


def prepare(
    store: Store, schedule: QuestionDigestSchedule, doctor: Doctor, now: datetime
) -> CommitRequest | None:
    if schedule.next_action_at is None or schedule.next_action_at > now:
        return None
    policy = StewardPolicy(DRAFT_POLICY_2026_09)
    pending = schedule.pending_intent_id
    pending_each = schedule.pending_each
    generation = schedule.generation
    shown = schedule.last_shown_ids
    outstanding = []
    if pending:
        outgoing = store.get(schedule.scope, "outbound_intent", pending)
        if outgoing:
            outstanding.append(from_record(outgoing, OutboundIntent))
    for patient_id, intent_id in pending_each:
        outgoing = store.get(
            PatientScope(doctor_id=doctor.id, patient_id=patient_id), "outbound_intent", intent_id
        )
        if outgoing:
            outstanding.append(from_record(outgoing, OutboundIntent))
    at: datetime | None
    if any(
        i.status in {"queued", "sending", "uncertain", "failed"} or i.notice_feedback_pending
        for i in outstanding
    ):
        at = now + policy.timing.result_review_interval
        selected = []
        blocked = True
    else:
        if pending:
            generation += 1  # Accepted feedback normally clears it; suppression retires it.
        pending, pending_each = None, ()
        selected = due(store, schedule.scope)[:LIMIT]
        at = next_instant(doctor, now) if selected else None
        blocked = False
    intents = []
    if selected and not blocked:
        logical = f"question_digest:{doctor.id}:{generation}"
        if doctor.digest_packing == "one":
            intent = OutboundIntent(
                id=keys.digest(logical),
                scope=schedule.scope,
                scope_kind="doctor",
                audience="doctor",
                logical_key=logical,
                source_event_ids=(logical,),
                source_versions=(),
                recipient_ref=doctor.private_chat_id,
                notification_purpose="DEADLINE",
                eligibility_class="bundle",
                payload_ref=schedule.id,
                payload_digest=keys.digest(schedule.id),
                conversation_sequence=0,
                expires_at=now + timedelta(days=7),
                status="queued",
                created_at=now,
                updated_at=now,
                recipient_auth_epoch_seen=doctor.auth_epoch,
                bot_id=doctor.telegram_bot_id,
                template_id="doctor_question_digest",
                work_clock=OperationalClock(work_lane="delivery", next_action_at=now),
                delivery_lease_seconds=int(policy.operations.lease_ttl.total_seconds()),
            )
            intents.append(to_record(intent, schedule.scope))
            pending = intent.id
        else:
            from sanad.steward.apply import make_intent

            for patient, mission, review in selected:
                authority_row = store.get(patient.scope, "doctor_authority", doctor.id)
                profile = store.get_patient_profile(patient.scope)
                if authority_row is None or profile is None:
                    return None
                intent = make_intent(
                    patient.scope,
                    logical + ":" + identity(patient.id, mission.id),
                    (to_record(mission, patient.scope).ref, to_record(review, patient.scope).ref),
                    "DEADLINE",
                    mission.id,
                    now,
                    policy,
                    from_record(authority_row, DoctorAuthority),
                    profile,
                    order_refs=mission.order_refs,
                )
                intent = intent.model_copy(update={"review_obligation_id": review.id})
                intents.append(to_record(intent, patient.scope))
            pending_each = tuple(
                (p.id, r.id) for (p, _, _), r in zip(selected, intents, strict=True)
            )
            shown = tuple(identity(p.id, m.id) for p, m, _ in selected)
            generation += 1
    changed = revise(
        schedule,
        now,
        pending_intent_id=pending,
        pending_each=pending_each,
        generation=generation,
        last_shown_ids=shown,
        next_action_at=at,
        work_clock=OperationalClock(
            work_lane="question_digest", next_action_at=at, work_generation=generation
        )
        if at
        else None,
    )
    command_id = f"sweep:question_digest:{schedule.id}:{schedule.version}"
    actor = Principal(subject="steward:question_digest", actor_kind="system", doctor_id=doctor.id)
    command = CommandEnvelope(
        command_id=command_id,
        principal=actor,
        scope=doctor.scope,
        requested_at=now,
        payload={"type": "_QuestionDigestWake"},
        worker=WorkerCapability(
            service_subject=actor.subject,
            resolved_scope=doctor.scope,
            permitted_lanes=frozenset({"question_digest"}),
            auth_expiry=now + policy.operations.claim_ttl,
            invocation_id=command_id,
        ),
    )
    # An empty due set only clears its durable clock.
    events = (
        ()
        if not selected
        else (
            to_record(
                AuditEvent(
                    id=command_id,
                    event_id=command_id,
                    command_id=command_id,
                    scope=doctor.scope,
                    event_type="QUESTION_DIGEST_WAKE",
                    actor=actor,
                    accepted_at=now,
                    created_at=now,
                    updated_at=now,
                ),
                doctor.scope,
            ),
        )
    )
    row = to_record(changed, doctor.scope)
    return CommitRequest(
        command=command,
        puts=(row,),
        events=events,
        intents=tuple(intents),
        expected=tuple(r.ref for r in (row, *events, *intents)),
    )


def wake(steward: "Steward", record: StoredRecord) -> None:
    initial = from_record(record, QuestionDigestSchedule)
    schedule = load(steward.store, initial.scope)
    row = steward.store.get(initial.scope, "doctor", initial.doctor_id)
    if schedule and row:
        request = prepare(steward.store, schedule, from_record(row, Doctor), steward.clock())
        if request:
            steward.store.commit(request)


def accepted(store: Store, intent: OutboundIntent, now: datetime) -> dict[str, object]:
    assert type(intent.scope) is TenantScope
    schedule = load(store, intent.scope)
    row = store.get(intent.scope, "doctor", intent.scope.doctor_id)
    if schedule is None or row is None or schedule.pending_intent_id != intent.id:
        return {}
    doctor = from_record(row, Doctor)
    at = next_instant(doctor, now) if due(store, intent.scope) else None
    changed = revise(
        schedule,
        now,
        pending_intent_id=None,
        generation=schedule.generation + 1,
        last_provider_accepted_at=intent.accepted_at,
        last_shown_ids=tuple(identity(p, m) for p, m in intent.question_listing_targets),
        next_action_at=at,
        work_clock=OperationalClock(
            work_lane="question_digest", next_action_at=at, work_generation=schedule.generation + 1
        )
        if at
        else None,
    )
    stamps = []
    for displayed in intent.review_listing:
        r = store.get(displayed.scope, "review", displayed.review_ref.id)
        if r:
            review = from_record(r, ReviewObligation)
            if review.review_kind == "question_answer" and review.first_notice_at is None:
                stamps.append(
                    to_record(
                        revise(review, now, first_notice_at=intent.accepted_at or now),
                        displayed.scope,
                    )
                )
    fields: dict[str, object] = {
        "question_schedule": to_record(changed, doctor.scope),
        "question_schedule_expected_version": schedule.version,
        "question_stamps": tuple(stamps),
    }
    if stamps:
        old_row = store.get(doctor.scope, "bundle_schedule", doctor.id)
        old = from_record(old_row, BundleSchedule) if old_row else None
        weekly_at = (intent.accepted_at or now) + timedelta(days=7)
        if old is None or (
            not old.pending_intent_id
            and (old.next_action_at is None or old.next_action_at > weekly_at)
        ):
            clock = OperationalClock(
                work_lane="bundle",
                next_action_at=weekly_at,
                work_generation=old.generation if old else 1,
            )
            weekly = (
                revise(old, now, next_action_at=weekly_at, work_clock=clock)
                if old
                else BundleSchedule(
                    id=doctor.id,
                    doctor_id=doctor.id,
                    scope=doctor.scope,
                    next_action_at=weekly_at,
                    work_clock=clock,
                    created_at=now,
                    updated_at=now,
                )
            )
            fields.update(
                bundle_schedule=to_record(weekly, doctor.scope),
                bundle_expected_version=old.version if old else None,
            )
    return fields


def individual(intent: OutboundIntent) -> bool:
    return (
        isinstance(intent.scope, PatientScope)
        and intent.audience == "doctor"
        and any(s.startswith("question_digest:") for s in intent.source_event_ids)
    )
