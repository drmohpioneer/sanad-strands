"""Contract 29 through durable receipts, scoped web sessions and both stores."""

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from domain_fixtures import mission
from harness import FakeClock
from providers.fixtures import ScriptedModel, candidate

from sanad.domain import EvidencePredicate, Mission, MissionState
from sanad.domain.entities import MonitorDetails
from sanad.monitor.slots import generate
from sanad.store._base import StoreBase
from sanad.store.records import OutboundIntent, from_record
from store import evidence_fixtures as f
from store.account_fixtures import PATIENT
from store.concierge_fixtures import PatientWorld
from store.login_fixtures import ORIGIN, browser_login

ZONE = "Africa/Cairo"


def local(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(ZONE)).astimezone(UTC)


def scheduled(
    *, first: str = "2026-09-13T07:00", state: MissionState = MissionState.open
) -> Mission:
    slots = generate(local(first), ZONE, 2, 4)
    return mission(
        state=state,
        kind="MONITOR",
        order_refs=(),
        title="Blood pressure",
        timezone=ZONE,
        due_at=slots[-1] + timedelta(hours=1),
        escalation_at=slots[-1] + timedelta(hours=1),
        objective_predicate=EvidencePredicate(evaluator="monitor"),
        details=MonitorDetails(
            metric="blood pressure",
            unit="mmHg",
            slots=slots,
            required_coverage=len(slots),
            timezone=ZONE,
            times_per_day=2,
            slot_rule="window-next-v1",
        ),
    )


@pytest.fixture
def world(store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> PatientWorld:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "1")
    w = f.world(store, clock)
    clock.now = local("2026-09-14T12:00")
    seed_schedule(w)
    return w


def seed_schedule(w: PatientWorld, id: str = "schedule") -> Mission:
    m = scheduled().model_copy(
        update={
            "id": id,
            "doctor_id": w.patient_scope.doctor_id,
            "patient_id": w.patient_scope.patient_id,
        }
    )
    w.seed(m)
    return m


def current(w: PatientWorld, id: str = "schedule") -> Mission:
    result = w.store.get_mission(w.patient_scope, id)
    assert result
    return result


def details(w: PatientWorld, id: str = "schedule") -> MonitorDetails:
    result = current(w, id).details
    assert isinstance(result, MonitorDetails)
    return result


def scripts(
    w: PatientWorld,
    *,
    times: tuple[str, ...] = ("09:00", "21:00"),
    quotes: tuple[str, ...] = ("9am", "9pm"),
    other: dict[str, Any] | None = None,
    target: str | None = "schedule",
) -> ScriptedModel:
    value = {
        "asserted": True,
        "mission_id": target,
        "times": [{"value": v, "quote": q} for v, q in zip(times, quotes, strict=True)],
    }
    model = ScriptedModel(candidate(value), candidate(value if other is None else other))
    w.concierge.schedule_model_factory = lambda registry, role: model
    return model


def reply_to(w: PatientWorld, id: int) -> OutboundIntent:
    return next(
        i for i in w.patient_intents() if "patient-turn:" + w.receipt(id).id in i.source_event_ids
    )


def preview(
    w: PatientWorld, text: str = "change my pressure times to 9am and 9pm", *, id: int = 3000
) -> OutboundIntent:
    _, reply = w.send(text, id=id)
    assert reply.template_id == "patient_schedule_start", reply.payload
    return reply


def test_telegram_confirm_replay_and_no_doctor_notice(world: PatientWorld) -> None:
    model = scripts(world)
    before = current(world)
    reply = preview(world)
    assert current(world).details == before.details
    assert len(model.script.calls) == 2
    assert "10:00, 22:00 → 09:00, 21:00" in str(reply.payload)
    assert "2026-09-15" in str(reply.payload)
    world.press(reply, id=3001)
    after = current(world)
    assert isinstance(after.details, MonitorDetails)
    assert after.details.time_history[-1].source_receipt_id == world.receipt(3001).id
    assert after.details.slots[4] == local("2026-09-15T09:00")
    assert (after.due_at, after.escalation_at, after.review_at) == (
        before.due_at,
        before.escalation_at,
        before.review_at,
    )
    world.press(reply, id=3001)
    assert current(world) == after
    world.press(reply, id=3002)
    assert current(world).details == after.details
    assert not [r for r in world.rows("outbound_intent") if r.body.get("audience") == "doctor"]


@pytest.mark.parametrize("mode", ["no", "expired", "message", "midnight", "reading"])
def test_confirmation_lifecycle_and_unrelated_missions(world: PatientWorld, mode: str) -> None:
    other = f.mission(world, "unrelated", order_refs=())

    def snapshots() -> tuple[object, ...]:
        return (
            current(world).details,
            {
                k: v
                for k, v in current(world, "unrelated").model_dump().items()
                if k in {"details", "due_at", "escalation_at", "review_at"}
            },
            world.rows("review"),
        )

    scripts(world)
    before = snapshots()
    reply = preview(world)
    assert snapshots() == before
    if mode == "expired":
        world.clock.advance(timedelta(minutes=31))
    elif mode == "message":
        world.send("plan", id=3005)
    elif mode == "midnight":
        # Fresh preview at 23:50 makes crossing midnight distinct from expiry.
        world.clock.now = local("2026-09-14T23:50")
        scripts(world)
        reply = preview(world, id=3006)
        world.clock.advance(timedelta(minutes=11))
    elif mode == "reading":
        world.send("BP 120/80", id=3007)
        before = snapshots()
    before = snapshots()
    before_intents = len(world.patient_intents())
    world.press(reply, index=1 if mode == "no" else 0, id=3008)
    assert snapshots() == before
    if mode == "no":
        assert len(world.patient_intents()) == before_intents
        assert world.receipt(3008).state == "completed"
        world.press(reply, id=3009)
        assert current(world).details == before[0]
    elif mode in {"message", "midnight", "reading"}:
        assert reply_to(world, 3008).template_id == "patient_schedule_start"
        world.press(reply_to(world, 3008), id=3010)
        assert current(world).details != before[0]
    assert current(world, "unrelated").details == other.details


@pytest.mark.parametrize(
    "text", ["what is my plan", "BP 120/80", "BP 120", "don't change my times"]
)
def test_no_time_or_reading_makes_zero_schedule_calls(world: PatientWorld, text: str) -> None:
    model = scripts(world)
    world.send(text)
    assert len(model.script.calls) == 0


def test_schedule_bypasses_barrier_readers(world: PatientWorld) -> None:
    model = scripts(world)
    barrier = ScriptedModel()
    world.concierge.barrier_model_factory = lambda registry, role: barrier
    preview(world)
    assert len(model.script.calls) == 2 and not barrier.script.calls


@pytest.mark.parametrize("mode", ["disagree", "absent_time", "bare", "failure", "none"])
def test_reader_refusals(world: PatientWorld, mode: str) -> None:
    before = current(world).details
    if mode == "disagree":
        scripts(
            world,
            other={
                "asserted": True,
                "mission_id": "schedule",
                "times": [{"value": "09:00", "quote": "9am"}, {"value": "20:00", "quote": "8pm"}],
            },
        )
    elif mode == "bare":
        scripts(world, quotes=("9", "9pm"))
    elif mode == "failure":
        world.concierge.schedule_model_factory = lambda registry, role: ScriptedModel(
            RuntimeError("synthetic outage"), RuntimeError("synthetic outage")
        )
    elif mode == "none":
        world.concierge.schedule_model_factory = lambda registry, role: ScriptedModel(
            candidate({"asserted": False, "times": []})
        )
    else:
        scripts(world)
    _, reply = world.send(
        "change times to 9 and 9pm"
        if mode in {"bare", "absent_time"}
        else "change times to 9am and 9pm"
    )
    assert current(world).details == before
    assert reply.template_id == (
        "patient_schedule_which_half"
        if mode == "bare"
        else "patient_schedule_rules"
        if mode != "none"
        else "patient_safe_fallback"
    )


def test_multiple_matching_plans_choose_then_confirm(world: PatientWorld) -> None:
    seed_schedule(world, "second")
    scripts(world, target=None)
    _, reply = world.send("change pressure times to 9am and 9pm")
    assert reply.template_id == "patient_schedule_choose"
    world.press(reply, index=1, id=3020)
    card = reply_to(world, 3020)
    assert card.template_id == "patient_schedule_start"
    world.press(card, id=3021)
    assert isinstance(current(world, "second").details, MonitorDetails)
    assert details(world, "second").time_history
    assert not details(world).time_history


def test_web_csrf_idor_preview_confirm_session_and_record(world: PatientWorld) -> None:
    scripts(world)
    with world.client() as client:
        assert browser_login(client, world.login_path(PATIENT, id=8000)).status_code == 303
        headers = {"origin": ORIGIN, "x-csrf-token": client.cookies["sanad_csrf"]}
        plans = client.get("/api/patient/schedules").json()["plans"]
        assert plans[0]["times"] == ["10:00", "22:00"]
        body = {
            "command_id": "schedule-preview",
            "mission_id": "schedule",
            "version": current(world).version,
            "times": ["09:00", "21:00"],
        }
        assert (
            client.post(
                "/api/patient/preferences", json=body | {"mission_id": "foreign"}, headers=headers
            ).status_code
            == 404
        )
        result = client.post("/api/patient/preferences", json=body, headers=headers)
        assert result.status_code == 200, result.text
        data = result.json()["schedule_reply"]
        token = data["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        assert not details(world).time_history
        saved = client.post(
            "/api/patient/preferences/confirm",
            json={"command_id": "confirm-schedule", "token": token},
            headers=headers,
        )
        assert saved.status_code == 200, saved.text
        assert details(world).time_history
        assert client.get("/api/patient/me").status_code == 200
        assert client.get("/api/patient/schedules").json()["plans"][0]["times"] == [
            "09:00",
            "21:00",
        ]
        assert client.post("/api/patient/preferences", json=body).status_code == 403
    with world.client() as client:
        assert (
            browser_login(client, world.login_path(world.owner.subject, id=8001)).status_code == 303
        )
        record = client.get("/api/patients/" + world.patient_scope.patient_id).json()
        plan = next(m for m in record["missions"] if m["id"] == "schedule")
        assert plan["schedule_history"][0]["new"][0] == local("2026-09-15T09:00").isoformat()


def profile_preferences(w: PatientWorld) -> dict[str, Any]:
    return w.profile.model_dump(
        exclude={"version", "updated_at", "lease_owner", "lease_expires_at", "lease_generation"}
    )


def queued(w: PatientWorld, index: int, *, status: str = "queued") -> OutboundIntent:
    from sanad.monitor.reschedule import slot_id
    from sanad.steward.apply import make_intent
    from sanad.store.records import DoctorAuthority, to_record

    m = current(w)
    assert isinstance(m.details, MonitorDetails)
    authority = w.store.get(w.patient_scope, "doctor_authority", w.patient_scope.doctor_id)
    assert authority
    intent = make_intent(
        w.patient_scope,
        "synthetic-queue-" + str(index) + status,
        (to_record(m, w.patient_scope).ref,),
        "routine_prompt",
        "synthetic",
        w.clock(),
        w.runtime.steward.policy_provider(w.patient_scope),
        from_record(authority, DoctorAuthority),
        w.profile,
        audience="patient",
        slot=slot_id(m, index, prompt=True),
        contact_kind="scheduled",
        template_id="patient_monitor_prompt",
        expires_at=m.details.slots[index] + timedelta(hours=3),
    )
    assert intent.work_clock
    intent = intent.model_copy(
        update={
            "status": status,
            "work_clock": intent.work_clock.model_copy(
                update={"next_action_at": w.clock() + timedelta(minutes=1)}
            ),
        }
    )
    w.seed(intent)
    return intent


def test_queued_suppression_rebinding_and_delivery(world: PatientWorld) -> None:
    from store.contact_fixtures import dispatch

    world.clock.now = local("2026-09-14T10:00")
    original = queued(world, 2)
    moved = queued(world, 4)
    retained = [queued(world, 4, status=s) for s in ("sending", "uncertain", "failed")]
    scripts(world)
    reply = preview(world)
    world.press(reply, id=3040)
    rows = {r.id: from_record(r, OutboundIntent) for r in world.rows("outbound_intent")}
    assert rows[moved.id].status == "suppressed"
    assert rows[moved.id].suppression_reason == "patient_schedule_changed"
    changed = rows[original.id]
    assert changed.source_versions[0].version == current(world).version
    assert changed.model_dump(
        exclude={"source_versions", "version", "updated_at"}
    ) == original.model_dump(exclude={"source_versions", "version", "updated_at"})
    assert [rows[i.id] for i in retained] == retained
    world.clock.advance(timedelta(minutes=1))
    assert dispatch(world, changed).status == "provider_accepted"


def test_replacement_is_scheduled_and_delivered(world: PatientWorld) -> None:
    from sanad.contact.scheduler import schedule
    from sanad.store.records import to_record
    from store.contact_fixtures import dispatch, routine

    scripts(world)
    world.press(preview(world), id=3050)
    world.clock.now = local("2026-09-15T09:00")
    result = schedule(world.runtime.steward, to_record(current(world), world.patient_scope))
    assert result.status == "accepted", result
    prompt = next(i for i in routine(world) if i.status == "queued")
    assert prompt.slot_id == "monitor:schedule:r1-4"
    assert dispatch(world, prompt).status == "provider_accepted"


def test_quiet_slots_require_fresh_consent_and_old_buttons_stale(world: PatientWorld) -> None:
    from sanad.contact.scheduler import schedule
    from sanad.store.records import Consent, to_record
    from store.contact_fixtures import routine

    # Keep old grants and old buttons, then move their slots into a new quiet instant.
    _, quiet_reply = world.send("quiet hours 20:00 08:00", id=3060)
    patient = world.claims.patient(world.patient_scope.doctor_id, world.patient_scope.patient_id)
    assert patient and patient.consent_id
    row = world.store.get(world.patient_scope, "consent", patient.consent_id)
    assert row
    consent = from_record(row, Consent)
    world.seed(consent.model_copy(update={"scheduled_slot_consents": ("schedule:5",)}))
    before = (
        world.rows("consent"),
        world.rows("patient"),
        world.rows("patient_binding"),
        profile_preferences(world),
    )
    scripts(world)
    world.press(preview(world), id=3061)
    assert (
        world.rows("consent"),
        world.rows("patient"),
        world.rows("patient_binding"),
        profile_preferences(world),
    ) == before
    confirmation = reply_to(world, 3061)
    assert "separate consent" in str(confirmation.payload)
    world.clock.now = local("2026-09-15T21:00")
    result = schedule(world.runtime.steward, to_record(current(world), world.patient_scope))
    assert result.status == "accepted"
    assert not [i for i in routine(world) if i.slot_id == "monitor:schedule:r1-5"]
    # Grant a current replacement reminder through the existing command, then retry scheduling.
    world.press(confirmation, index=0, id=3062)
    fresh = world.store.get(world.patient_scope, "consent", patient.consent_id)
    assert fresh and "schedule:r1-5" in from_record(fresh, Consent).scheduled_slot_consents
    assert (
        schedule(world.runtime.steward, to_record(current(world), world.patient_scope)).status
        == "accepted"
    )
    assert any(i.slot_id == "monitor:schedule:r1-5" for i in routine(world))
    # The original quiet offer can never grant the replaced slot (select day three).
    world.press(quiet_reply, index=1, id=3063)
    final_consent = world.store.get(world.patient_scope, "consent", patient.consent_id)
    assert final_consent
    assert (
        from_record(final_consent, Consent).scheduled_slot_consents
        == from_record(fresh, Consent).scheduled_slot_consents
    )


@pytest.mark.parametrize(
    "tamper", ["slots", "history", "deadline", "missing_queue", "queue_payload", "extra_mission"]
)
def test_store_recomputes_schedule_write_set(
    world: PatientWorld, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    from sanad.store.records import CommitRequest, CommitResult, to_record

    moved = queued(world, 4)
    scripts(world)
    reply = preview(world)
    before = current(world)
    assert isinstance(before.details, MonitorDetails)
    assert isinstance(before.details, MonitorDetails)
    original_slots = before.details.slots
    original_commit = world.store.commit
    seen = []

    def commit(request: CommitRequest) -> CommitResult:
        if request.command.payload.get("type") != "RescheduleMonitorTimes":
            return original_commit(request)
        seen.append(True)
        rows = list(request.puts)
        for index, row in enumerate(rows):
            if row.entity_type == "mission" and row.id == "schedule":
                m = from_record(row, Mission)
                assert isinstance(m.details, MonitorDetails)
                if tamper == "slots":
                    rows[index] = to_record(
                        m.model_copy(
                            update={
                                "details": m.details.model_copy(update={"slots": original_slots})
                            }
                        ),
                        world.patient_scope,
                    )
                elif tamper == "history":
                    rows[index] = to_record(
                        m.model_copy(
                            update={"details": m.details.model_copy(update={"time_history": ()})}
                        ),
                        world.patient_scope,
                    )
                elif tamper == "deadline":
                    rows[index] = to_record(
                        m.model_copy(
                            update={
                                "due_at": m.due_at + timedelta(days=1),
                                "escalation_at": m.escalation_at + timedelta(days=1),
                            }
                        ),
                        world.patient_scope,
                    )
                elif tamper == "extra_mission":
                    rows.append(
                        to_record(
                            m.model_copy(update={"id": "foreign-mission"}), world.patient_scope
                        )
                    )
            if row.id == moved.id and tamper == "queue_payload":
                intent = from_record(row, OutboundIntent)
                rows[index] = to_record(
                    intent.model_copy(update={"payload": {"text": "altered"}}), world.patient_scope
                )
        if tamper == "missing_queue":
            rows = [r for r in rows if r.id != moved.id]
        return original_commit(request.model_copy(update={"puts": tuple(rows)}))

    monkeypatch.setattr(world.store, "commit", commit)
    world.press(reply, id=3070)
    assert seen
    assert current(world) == before
    final_queue = world.store.get(world.patient_scope, "outbound_intent", moved.id)
    assert final_queue and final_queue.body["status"] == "queued"


def test_correction_restore_and_delayed_reading_after_edit(world: PatientWorld) -> None:
    from sanad.steward.corrections import current_facts
    from store.test_corrections_19 import fact_change

    world.send("BP 120/80", id=3080)
    original = current_facts(world.store, world.patient_scope)[0]
    scripts(world)
    world.press(preview(world), id=3081)
    history = details(world).time_history
    assert fact_change(world, original.id, None).status == "accepted"
    detached = world.rows("fact_head")[0].body["current_ref"]
    assert isinstance(detached, dict)
    assert fact_change(world, str(detached["id"]), "BP 125/80").status == "accepted"
    assert details(world).time_history == history
    assert details(world).readings[0].slot == 2
    world.clock.now = local("2026-09-15T12:00")
    world.send("BP 130/80 2026-09-15T05:59:00Z", id=3082)
    assert details(world).readings[-1].slot == 3


@pytest.mark.parametrize("preference", ["stop reminders", "snooze 2 days"])
def test_stopped_or_paused_reminders_do_not_prevent_edit(
    world: PatientWorld, preference: str
) -> None:
    world.send(preference, id=3090)
    before = world.rows("consent"), world.rows("patient"), profile_preferences(world)
    scripts(world)
    world.press(preview(world), id=3091)
    assert details(world).time_history
    assert (world.rows("consent"), world.rows("patient"), profile_preferences(world)) == before


def test_stale_quiet_button_cannot_grant_replacement_id(world: PatientWorld) -> None:
    from sanad.store.records import Consent

    _, old = world.send("quiet hours 20:00 08:00", id=3200)
    scripts(world)
    world.press(preview(world), id=3201)
    before = world.rows("consent")
    # The second old button names tomorrow's 22:00; no other consent change intervened.
    world.press(old, index=1, id=3202)
    assert reply_to(world, 3202).template_id == "patient_callback_stale"
    assert world.rows("consent") == before
    assert not from_record(before[0], Consent).scheduled_slot_consents


def test_second_edit_preserves_unchanged_queued_generation(world: PatientWorld) -> None:
    scripts(world)
    world.press(preview(world), id=3210)
    old = queued(world, 4)
    scripts(world, times=("09:00", "20:00"), quotes=("9am", "8pm"))
    world.press(preview(world, "change my pressure times to 9am and 8pm", id=3211), id=3212)
    result = world.store.get(world.patient_scope, "outbound_intent", old.id)
    assert result
    kept = from_record(result, OutboundIntent)
    assert kept.status == "queued" and kept.slot_id == "monitor:schedule:r1-4"
    assert kept.source_versions[0].version == current(world).version
    assert details(world).time_history[-1].moved == (5, 7)


@pytest.mark.parametrize("first", [0, 1])
def test_either_confirmation_token_uses_the_whole_offer(world: PatientWorld, first: int) -> None:
    scripts(world)
    reply = preview(world)
    world.press(reply, id=3901, index=first)
    after = current(world).details
    world.press(reply, id=3902, index=1 - first)
    assert reply_to(world, 3902).template_id == "patient_callback_stale"
    assert current(world).details == after


def test_quiet_confirmation_escapes_the_plan_title(world: PatientWorld) -> None:
    world.seed(current(world).model_copy(update={"title": "Pressure <b>plan</b>"}))
    scripts(world, times=("09:00", "23:00"), quotes=("9am", "11pm"))
    reply = preview(world, "change my pressure times to 9am and 11pm")
    world.press(reply, id=3910)
    text = str((reply_to(world, 3910).payload or {})["text"])
    assert "<b>plan</b>" not in text
    assert "Pressure &lt;b&gt;plan&lt;/b&gt;" in text


@pytest.mark.parametrize("selected", [0, 1])
def test_different_cadences_choose_before_plan_specific_validation(
    world: PatientWorld, selected: int
) -> None:
    second = seed_schedule(world, "second")
    slots = generate(local("2026-09-13T07:00"), ZONE, 3, 4)
    revised = MonitorDetails.model_validate(
        details(world, "second").model_dump()
        | {"times_per_day": 3, "slots": slots, "required_coverage": len(slots)}
    )
    world.seed(
        second.model_copy(
            update={
                "details": revised,
                "due_at": slots[-1] + timedelta(hours=1),
                "escalation_at": slots[-1] + timedelta(hours=1),
            }
        )
    )
    scripts(world, target=None)
    _, reply = world.send("change pressure times to 9am and 9pm")
    assert reply.template_id == "patient_schedule_choose"
    assert not details(world).time_history and not details(world, "second").time_history
    world.press(reply, index=selected, id=3920)
    response = reply_to(world, 3920)
    if selected == 0:
        assert response.template_id == "patient_schedule_start"
        world.press(response, id=3921)
        assert details(world).time_history[-1].new_times == ("09:00", "21:00")
    else:
        assert response.template_id == "patient_schedule_rules"
        assert not details(world).time_history
    assert not details(world, "second").time_history


def quiet_consent(w: PatientWorld) -> Any:
    from sanad.store.records import Consent

    return from_record(w.rows("consent")[0], Consent)


def seed_quiet(w: PatientWorld, hours: tuple[str, str] = ("20:00", "08:00")) -> None:
    w.seed(quiet_consent(w).model_copy(update={"quiet_hours": hours}))


def quiet_offer(w: PatientWorld, reply: OutboundIntent) -> Any:
    from sanad.concierge.records import PatientAction
    from sanad.store import keys

    assert reply.payload
    keyboard = reply.payload["reply_markup"]
    assert isinstance(keyboard, dict)
    rows = keyboard["inline_keyboard"]
    assert isinstance(rows, list) and len(rows) == 1
    row = rows[0]
    assert isinstance(row, list) and len(row) == 1 and isinstance(row[0], dict)
    action = w.store.get(
        w.patient_scope, "patient_action", keys.digest(str(row[0]["callback_data"]))
    )
    assert action
    return from_record(action, PatientAction)


def begin_quiet(w: PatientWorld) -> OutboundIntent:
    seed_quiet(w)
    scripts(w)
    w.press(preview(w), id=4100)
    return reply_to(w, 4100)


def occupy_quiet(w: PatientWorld, index: int) -> None:
    from sanad.domain import VersionRef
    from sanad.domain.entities import MonitorReading

    m = current(w)
    d = details(w)
    reading = MonitorReading(
        source_ref=VersionRef(entity_type="clinical_fact", id="synthetic-occupied", version=1),
        reading_index=0,
        observed_at=d.slots[index],
        received_at=w.clock(),
        slot=index,
        value="120/80",
    )
    w.seed(
        m.model_copy(
            update={
                "version": m.version + 1,
                "details": d.model_copy(update={"readings": (*d.readings, reading)}),
            }
        )
    )


def test_r6_maximum_plan_atomic_commit(
    world: PatientWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sanad.store._base import Check, Write

    slots = generate(local("2026-09-15T06:00"), ZONE, 4, 30)
    m = current(world)
    world.seed(
        m.model_copy(
            update={
                "due_at": slots[-1] + timedelta(hours=1),
                "escalation_at": slots[-1] + timedelta(hours=1),
                "details": MonitorDetails(
                    metric="blood pressure",
                    unit="mmHg",
                    slots=slots,
                    required_coverage=120,
                    timezone=ZONE,
                    times_per_day=4,
                    slot_rule="window-next-v1",
                ),
            }
        )
    )
    seed_quiet(world, ("06:00", "23:00"))
    old_queue = queued(world, 0)
    scripts(
        world,
        times=("07:00", "11:00", "15:00", "19:00"),
        quotes=("07:00", "11:00", "15:00", "19:00"),
    )
    reply = preview(world, "change times to 07:00 11:00 15:00 19:00")
    original = world.store._atomic
    counts = []

    def atomic(writes: list[Write], checks: list[Check]) -> bool:
        if any(write.item.get("entity_type") == "mission" for write in writes):
            counts.append(len(writes) + len(checks))
        return original(writes, checks)

    monkeypatch.setattr(world.store, "_atomic", atomic)
    world.press(reply, id=4110)
    changed = details(world)
    assert len(changed.slots) == len(changed.time_history[-1].moved) == 120
    assert changed.required_coverage == 120
    assert counts and max(counts) <= 100
    assert quiet_offer(world, reply_to(world, 4110)).slot_id == "schedule:r1-0"
    actions = [r for r in world.rows("patient_action") if r.body.get("quiet_schedule_generation")]
    assert len(actions) == 1
    saved = world.store.get(world.patient_scope, "outbound_intent", old_queue.id)
    assert saved and saved.body["status"] == "suppressed"
    assert not quiet_consent(world).scheduled_slot_consents
    print("R6 maximum plan operations:", counts)


@pytest.mark.parametrize("crash", [False, True])
def test_r6_successive_grants_and_crash_replay(world: PatientWorld, crash: bool) -> None:
    first = begin_quiet(world)
    offer = quiet_offer(world, first)
    before = quiet_consent(world)

    def checkpoint(stage: str) -> None:
        if crash and stage == "turn_persisted":
            raise RuntimeError("synthetic crash after quiet grant commit")

    world.concierge.checkpoint = checkpoint
    world.press(first, id=4120)
    assert world.receipt(4120).state == "completed"
    world.concierge.checkpoint = lambda stage: None
    after = quiet_consent(world)
    assert after.scheduled_slot_consents == (*before.scheduled_slot_consents, offer.slot_id)
    second = reply_to(world, 4120)
    renewed = quiet_offer(world, second)
    assert renewed.slot_id == "schedule:r1-7"
    assert renewed.consent_version == after.version == offer.consent_version + 1
    assert renewed.delivery_epoch == world.profile.delivery_epoch == offer.delivery_epoch + 1
    actions = world.rows("patient_action")
    world.press(first, id=4120)
    assert quiet_consent(world) == after
    assert world.rows("patient_action") == actions
    world.press(first, id=4121)
    assert reply_to(world, 4121).template_id == "patient_callback_stale"
    assert quiet_consent(world) == after
    world.press(second, id=4122)
    assert quiet_consent(world).scheduled_slot_consents == (
        *after.scheduled_slot_consents,
        renewed.slot_id,
    )
    assert "reply_markup" not in (reply_to(world, 4122).payload or {})
    world.press(second, id=4122)
    assert quiet_consent(world).version == after.version + 1


@pytest.mark.parametrize("change", ["reschedule", "occupied", "quiet", "past", "terminal", "order"])
def test_r6_offer_revalidates_current_slot(world: PatientWorld, change: str) -> None:
    from sanad.domain import VersionRef

    first = begin_quiet(world)
    if change == "reschedule":
        scripts(world, times=("08:00", "20:00"), quotes=("8am", "8pm"))
        world.press(preview(world, "change times to 8am and 8pm", id=4130), id=4131)
    elif change == "occupied":
        occupy_quiet(world, 5)
    elif change == "quiet":
        seed_quiet(world, ("23:00", "06:00"))
    elif change == "past":
        world.clock.now = local("2026-09-15T21:01")
    elif change == "terminal":
        world.seed(
            current(world).model_copy(update={"state": MissionState.cancelled, "work_clock": None})
        )
    else:
        world.seed(
            current(world).model_copy(
                update={
                    "order_refs": (
                        VersionRef(entity_type="order", id="superseded-order", version=1),
                    )
                }
            )
        )
    before = quiet_consent(world)
    world.press(first, id=4132)
    assert quiet_consent(world) == before
    assert reply_to(world, 4132).template_id == "patient_callback_stale"


def test_r6_continuation_skips_newly_filled_slot(world: PatientWorld) -> None:
    first = begin_quiet(world)
    occupy_quiet(world, 7)
    world.press(first, id=4140)
    assert quiet_consent(world).scheduled_slot_consents == ("schedule:r1-5",)
    assert "reply_markup" not in (reply_to(world, 4140).payload or {})
