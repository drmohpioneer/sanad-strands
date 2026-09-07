import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from harness import FakeClock
from providers.fixtures import ScriptedModel, candidate

from sanad.concierge import answer, education, plan
from sanad.concierge.records import ReportFactPayload
from sanad.domain import FollowUpTask
from sanad.ops.sweep import sweep_due
from sanad.scribe.records import ClinicalFact
from sanad.store._base import StoreBase
from sanad.store.records import OutboundIntent, Patient, SessionSnapshot, from_record
from store.account_fixtures import PATIENT, update
from store.concierge_cases import ADVERSARIAL, PATIENT_CASES, PatientCase
from store.concierge_fixtures import PatientWorld
from store.login_fixtures import browser_login


@pytest.fixture
def world(store: StoreBase, clock: FakeClock) -> PatientWorld:
    world = PatientWorld.create(store, clock)
    assert isinstance(world, PatientWorld)
    world.enroll()
    return world


def test_plan_no_model(world: PatientWorld) -> None:
    model, intent = world.send("/plan")
    assert not model.script.calls
    assert intent.template_id == "patient_plan_summary"
    assert intent.payload and "40 مج" in str(intent.payload["text"])


def test_stop_then_question(world: PatientWorld) -> None:
    before = world.store.get_patient_profile(world.patient_scope)
    assert before
    _, intent = world.send("وقف الرسايل")
    assert intent.template_id == "patient_stop_ack"
    after = world.store.get_patient_profile(world.patient_scope)
    assert after
    assert after.delivery_epoch == before.delivery_epoch + 1
    assert after.consent_active and not after.routine_contact_enabled
    assert any(r.body["review_kind"] == "binding_review" for r in world.rows("review"))
    _, reply = world.send("عايز أكلم الدكتور")
    assert reply.template_id == "patient_question_forwarded"
    assert len([r for r in world.rows("mission") if r.body["kind"] == "QUESTION"]) == 1


def test_start_fulfills_anchors(world: PatientWorld) -> None:
    _, reply = world.send("بدأت الدوا")
    assert reply.template_id == "patient_start_recorded"
    mission = next(r for r in world.rows("mission") if r.body["kind"] == "MEDICATION")
    assert mission.body["state"] == "fulfilled"
    followup = world.rows("followup")[0]
    assert followup.body["state"] == "scheduled"
    assert followup.body["prompt_at"] == (world.clock() + timedelta(days=3)).isoformat().replace(
        "+00:00", "Z"
    )


@pytest.mark.parametrize("case", PATIENT_CASES, ids=lambda c: c.text)
def test_handwritten_patient_table(world: PatientWorld, case: PatientCase) -> None:
    _, reply = world.send(case.text)
    assert reply.template_id == case.template
    assert reply.payload and case.skeleton in str(reply.payload["text"])
    assert len([r for r in world.rows("mission") if r.body["kind"] == "QUESTION"]) == case.tickets
    facts = [
        from_record(r, ClinicalFact)
        for r in world.rows("clinical_fact")
        if r.body["category"] == "patient_report"
    ]
    assert len(facts) == int(case.fact is not None)
    if case.fact:
        assert isinstance(facts[0].payload, ReportFactPayload)
        assert facts[0].payload.report_kind == case.fact
        assert facts[0].provenance.source_kind == "patient_report"
        assert facts[0].provenance.source_observation_id == world.receipt(1000).id
    if case.preference:
        patient = world.store.get(world.patient_scope, "patient", world.patient_scope.patient_id)
        assert patient
        assert patient.body["contact_status"] == case.preference


@pytest.mark.parametrize("name,unsafe", ADVERSARIAL, ids=[r[0] for r in ADVERSARIAL])
def test_six_adversarial_model_replies(world: PatientWorld, name: str, unsafe: str) -> None:
    model, reply = world.send(
        "هو الدكتور قال 40 ولا 20؟",
        {
            "reply": unsafe,
            "kind": "plan",
            "needs_doctor": False,
        },
    )
    assert len(model.script.calls) == 1
    assert reply.template_id == "patient_safe_fallback", name
    assert reply.payload and unsafe not in str(reply.payload)
    assert any(r.body["kind"] == "QUESTION" for r in world.rows("mission"))


def test_question_due_and_dedupe_boundary(world: PatientWorld) -> None:
    world.send("عايز أكلم الدكتور")
    original = next(r for r in world.rows("mission") if r.body["kind"] == "QUESTION")
    assert original.body["due_at"] == (world.clock() + timedelta(hours=43)).isoformat().replace(
        "+00:00", "Z"
    )
    assert original.body["grace_seconds"] == 0
    world.clock.advance(timedelta(hours=23))
    world.send("عايز اكلم الدكتور")
    assert len([r for r in world.rows("mission") if r.body["kind"] == "QUESTION"]) == 1
    attachment = next(
        from_record(r, ClinicalFact)
        for r in world.rows("clinical_fact")
        if r.body["category"] == "patient_report"
    )
    assert (
        isinstance(attachment.payload, ReportFactPayload)
        and attachment.payload.report_kind == "question_attachment"
    )
    world.clock.advance(timedelta(hours=1))
    world.send("عايز أكلم الدكتور")
    assert len([r for r in world.rows("mission") if r.body["kind"] == "QUESTION"]) == 2


def test_resume_requires_single_use_tap(world: PatientWorld) -> None:
    world.send("وقف الرسايل")
    stopped = world.store.get_patient_profile(world.patient_scope)
    assert stopped
    _, ask = world.send("كمّل")
    assert world.profile.delivery_epoch == stopped.delivery_epoch
    world.press(ask)
    resumed = world.store.get_patient_profile(world.patient_scope)
    assert resumed
    assert resumed.routine_contact_enabled and resumed.delivery_epoch == stopped.delivery_epoch + 1
    world.press(ask, id=2001)
    assert world.profile.delivery_epoch == resumed.delivery_epoch
    assert len([r for r in world.rows("patient_action") if r.body["consumed_at"]]) == 1


def test_model_quotes_only_current_plan(world: PatientWorld) -> None:
    snapshot = plan.load(world.store, world.patient_scope, world.clock())
    assert snapshot
    line = plan.order_line(snapshot.orders[0])
    model, reply = world.send(
        "هو الدكتور قال 40 ولا 20؟",
        {
            "reply": line,
            "kind": "plan",
            "needs_doctor": False,
        },
    )
    assert len(model.script.calls) == 1 and reply.template_id == "patient_answer"
    assert reply.payload and reply.payload["text"] == line
    assert not any(r.body["kind"] == "QUESTION" for r in world.rows("mission"))


def test_education_is_labeled_and_gate_allows_it(world: PatientWorld) -> None:
    query = "يعني إيه ارتفاع ضغط الدم؟"
    entries = education.retrieve(query, synthetic=True)
    assert entries
    entry = entries[0]
    line = entry.lines("ar")[0]
    _, reply = world.send(
        query,
        {
            "reply": line,
            "kind": "education",
            "needs_doctor": False,
        },
    )
    assert reply.template_id == "patient_answer"
    assert reply.payload and "(مصدر:" in str(reply.payload["text"])


def waiting(world: PatientWorld) -> FollowUpTask:
    world.send("بدأت الدوا")
    task = from_record(world.rows("followup")[0], FollowUpTask)
    task = FollowUpTask.model_validate(
        task.model_dump()
        | {"version": task.version + 1, "state": "waiting_response", "updated_at": world.clock()}
    )
    world.seed(task)
    return task


def test_day3_response_done_and_preference_precedence(world: PatientWorld) -> None:
    waiting(world)
    world.send("أجّل 3 ساعات")
    assert world.rows("followup")[0].body["state"] == "waiting_response"
    world.send("عايز أكلم الدكتور")
    assert world.rows("followup")[0].body["state"] == "waiting_response"
    _, reply = world.send("الحمد لله كويس")
    assert reply.template_id == "patient_day3_recorded"
    assert world.rows("followup")[0].body["state"] == "fulfilled"
    assert any(
        r.body["notification_purpose"] == "DONE:FULFILLMENT"
        and any(v.entity_type == "followup" for v in from_record(r, OutboundIntent).source_versions)
        for r in world.rows("outbound_intent")
    )


def test_day3_concern_creates_independent_incident(world: PatientWorld) -> None:
    waiting(world)
    world.send("كان عندي ألم في صدري الأسبوع اللي فات")
    assert world.rows("incident")
    assert world.rows("followup")[0].body["state"] == "fulfilled"


def test_sources_ledger_and_synthetic_gate() -> None:
    path = Path("src/sanad/concierge/education/sources-fetch-2026-09-07.json")
    ledger = json.loads(path.read_text())
    fetched = ledger["sources"]
    assert len({r["requested_url"] for r in fetched}) == len(fetched)
    successes = {r["final_url"]: r for r in fetched if r["http_status"] == 200}
    entries = education.source_set()
    assert len(entries) == 17
    for e in entries:
        assert 60 <= len(e.text_ar.split()) <= 200
        assert e.source_url in successes
        assert successes[e.source_url]["title"] == e.source_title
        assert e.reviewed_by == "pending owner review"
        assert all("(مصدر: " in line for line in e.lines("ar"))
    assert not education.retrieve("ارتفاع ضغط الدم")
    assert len(education.retrieve("ضغط القلب السكر بوتاسيوم", synthetic=True)) == 2
    assert not education.retrieve("البركان حمم المريخ", synthetic=True)
    emergency = next(e for e in entries if e.content_kind == "safety")
    assert (
        "SAFETY_POLICY_V1_CARDIOLOGY_DRAFT" in emergency.source_basis
        and "999" not in emergency.text_ar
    )


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("number", "unsafe_output"),
        ("source", "source_not_retrieved"),
        ("language", "language_drift"),
        ("length", "reply_length"),
        ("missing_label", "sentence_not_grounded"),
    ],
)
def test_generated_reply_gates(world: PatientWorld, mutation: str, reason: str) -> None:
    entries = education.retrieve("يعني إيه ارتفاع ضغط الدم؟", synthetic=True)
    e = entries[0]
    line = e.lines("ar")[0]
    value: dict[str, object] = {
        "reply": line,
        "kind": "education",
        "needs_doctor": False,
    }
    if mutation == "number":
        value.update(reply=line + " 9999")
    if mutation == "source":
        value["reply"] = line.replace(e.source_label, "مصدر مختلق")
    if mutation == "language":
        value["reply"] = "Your doctor has everything under control"
    if mutation == "length":
        value["reply"] = " ".join([line] * 20)
    if mutation == "missing_label":
        value.update(reply=line.split(" (مصدر:")[0])
    _, reply = world.send("يعني إيه ارتفاع ضغط الدم؟", value)
    assert reply.template_id == "patient_safe_fallback"
    assert world.runtime.counters.get("concierge_" + reason) == 1, world.runtime.counters


def test_thinking_tags_are_stripped(world: PatientWorld) -> None:
    snap = plan.load(world.store, world.patient_scope, world.clock())
    assert snap
    line = plan.order_line(snap.orders[0])
    _, reply = world.send(
        "هو الدكتور قال 40 ولا 20؟",
        {
            "reply": "<thinking>hidden reasoning</thinking>" + line,
            "kind": "plan",
            "needs_doctor": False,
        },
    )
    assert reply.template_id == "patient_answer"
    assert reply.payload and reply.payload["text"] == line


@pytest.mark.parametrize("question,tickets", [("يعني إيه؟", 1), ("صباح الخير", 0)])
def test_model_unavailable(world: PatientWorld, question: str, tickets: int) -> None:
    model = ScriptedModel(TimeoutError())
    world.concierge.model_factory = lambda registry, role: model
    world.post(update(PATIENT, question, 1200))
    assert world.receipt(1200).state == "completed" and len(model.script.calls) == 1
    assert len([r for r in world.rows("mission") if r.body["kind"] == "QUESTION"]) == tickets
    reply = next(
        i
        for i in world.patient_intents()
        if "patient-turn:" + world.receipt(1200).id in i.source_event_ids
    )
    assert reply.template_id == "patient_safe_fallback"
    assert reply.payload and ("وصّلت" in str(reply.payload["text"])) == bool(tickets)


def test_crash_after_ticket_commit_sweep_sends_once(world: PatientWorld) -> None:
    def crash(stage: str) -> None:
        if stage == "turn_persisted":
            raise RuntimeError("synthetic crash")

    world.concierge.checkpoint = crash
    before = len(world.transport.calls)
    world.post(update(PATIENT, "عايز أكلم الدكتور", 1300))
    assert world.receipt(1300).state == "completed"
    assert len([r for r in world.rows("mission") if r.body["kind"] == "QUESTION"]) == 1
    assert len(world.transport.calls) == before
    world.concierge.checkpoint = lambda stage: None
    sweep_due(world.runtime, world.store)
    sent = len(world.transport.calls)
    assert sent > before
    world.post(update(PATIENT, "عايز أكلم الدكتور", 1300))
    sweep_due(world.runtime, world.store)
    assert len(world.transport.calls) == sent
    assert len([r for r in world.rows("mission") if r.body["kind"] == "QUESTION"]) == 1


def test_stale_worker_and_second_message_wait(world: PatientWorld) -> None:
    lease = world.store.acquire_patient(
        world.patient_scope, "synthetic-other-turn", world.clock(), timedelta(seconds=60)
    )
    assert lease
    model = ScriptedModel()
    world.concierge.model_factory = lambda registry, role: model
    world.post(update(PATIENT, "عايز أكلم الدكتور", 1400))
    assert world.receipt(1400).state == "pending" and not model.script.calls
    world.store.release_patient(lease)
    sweep_due(world.runtime, world.store)
    assert world.receipt(1400).state == "completed"
    assert len([r for r in world.rows("mission") if r.body["kind"] == "QUESTION"]) == 1


def test_changed_epoch_during_model_discards_reply(world: PatientWorld) -> None:
    snap = plan.load(world.store, world.patient_scope, world.clock())
    assert snap

    def change(kwargs: dict[str, Any]) -> dict[str, Any]:
        profile = world.store.get_patient_profile(world.patient_scope)
        assert profile
        world.seed(
            profile.model_copy(
                update={
                    "version": profile.version + 1,
                    "delivery_epoch": profile.delivery_epoch + 1,
                }
            )
        )
        return candidate(
            {
                "reply": plan.order_line(snap.orders[0]),
                "kind": "plan",
                "needs_doctor": False,
            }
        )

    model = ScriptedModel(change)
    world.concierge.model_factory = lambda registry, role: model
    world.post(update(PATIENT, "هو الدكتور قال 40 ولا 20؟", 1500))
    assert world.receipt(1500).state == "processing"
    assert not any(
        "patient-turn:" + world.receipt(1500).id in i.source_event_ids
        for i in world.patient_intents()
    )


def test_danger_bypasses_busy_patient_and_preferences(world: PatientWorld) -> None:
    lease = world.store.acquire_patient(
        world.patient_scope, "ordinary-busy", world.clock(), timedelta(seconds=60)
    )
    assert lease
    model = ScriptedModel()
    world.concierge.model_factory = lambda registry, role: model
    world.post(update(PATIENT, "وقف الرسايل وعندي ألم في صدري", 1600))
    assert world.rows("incident") and not model.script.calls
    assert world.profile.routine_contact_enabled
    assert any(i.template_id == "patient_emergency" for i in world.patient_intents())
    world.store.release_patient(lease)


def test_ambiguous_start_buttons_and_current_target(world: PatientWorld) -> None:
    world.dictate(
        "Synthetic Patient ميتوبرولول 25 مج مرة يوميا",
        {
            "patient": {"name_as_spoken": "Synthetic Patient"},
            "orders": [
                {"action": "start", "drug": "ميتوبرولول", "dose": "25 مج", "frequency": "مرة يوميا"}
            ],
        },
        id=450,
    )
    world.tap(id=451)
    _, reply = world.send("بدأت الدوا")
    assert reply.template_id == "patient_start_choose"
    assert len([r for r in world.rows("mission") if r.body["state"] == "fulfilled"]) == 0
    world.press(reply)
    assert len([r for r in world.rows("mission") if r.body["state"] == "fulfilled"]) == 1


def test_quiet_slot_needs_explicit_consent_without_moving_time(world: PatientWorld) -> None:
    world.send("بدأت الدوا")
    task = from_record(world.rows("followup")[0], FollowUpTask)
    _, reply = world.send("ساعات الهدوء من 00:00 لحد 23:59")
    unchanged = from_record(world.rows("followup")[0], FollowUpTask)
    assert unchanged.prompt_at == task.prompt_at and unchanged.due_at == task.due_at
    assert reply.payload and "محتاج موافقتك لوحده" in str(reply.payload["text"])
    assert any(r.body["review_kind"] == "binding_review" for r in world.rows("review"))
    before = plan.load(world.store, world.patient_scope, world.clock())
    assert before
    assert (task.consent_slot_id or task.id) not in before.consent.scheduled_slot_consents
    world.press(reply)
    after = plan.load(world.store, world.patient_scope, world.clock())
    assert after
    assert (task.consent_slot_id or task.id) in after.consent.scheduled_slot_consents
    assert after.profile.delivery_epoch == before.profile.delivery_epoch + 1


def test_english_patient_language_setting(world: PatientWorld) -> None:
    patient = from_record(world.rows("patient")[0], Patient)
    world.seed(
        patient.model_copy(
            update={
                "version": patient.version + 1,
                "record_version": patient.version + 1,
                "language": "en",
            }
        )
    )
    entries = education.retrieve("hypertension", synthetic=True)
    e = entries[0]
    line = e.lines("en")[0]
    _, reply = world.send(
        "What is hypertension?",
        {
            "reply": line,
            "kind": "education",
            "needs_doctor": False,
        },
    )
    assert reply.template_id == "patient_answer"
    assert reply.payload and reply.payload["text"] == line


def test_patient_web_shared_projection_and_scope(world: PatientWorld) -> None:
    world.send("سكر 240")
    world.send("عايز أكلم الدكتور")
    with world.client() as client:
        assert client.get("/api/patient/plan").status_code == 401
        assert browser_login(client, world.login_path(PATIENT)).status_code == 303
        response = client.get("/api/patient/plan")
        assert response.status_code == 200
        data = response.json()
        assert client.get("/api/patient/me").json()["plan"] == data
        snapshot = plan.load(world.store, world.patient_scope, world.clock())
        assert snapshot
        assert data == plan.projection(snapshot)
        assert data["orders"][0]["dose"] == "40 مج"
        assert data["last_reading"]["readings"][0]["quoted"] == "240"
        assert data["open_questions"][0]["status"] == "في انتظار الدكتور"
        page = client.get("/pp")
        assert page.status_code == 200 and 'dir="rtl"' in page.text
        assert "40 مج" in page.text and "مصدر:" not in page.text
        assert client.get("/api/patients/" + world.patient_scope.patient_id).status_code == 401
        world.send("وقف الرسايل")
        assert client.get("/api/patient/plan").status_code == 401
        assert browser_login(client, world.login_path(PATIENT, id=201)).status_code == 303
        assert (
            client.get("/api/patient/plan").json()["preferences"]["contact_status"] == "opted_out"
        )
    with world.client() as doctor_client:
        assert browser_login(doctor_client, world.login_path(id=202)).status_code == 303
        record = doctor_client.get("/api/patients/" + world.patient_scope.patient_id)
        assert record.status_code == 200 and "عايز أكلم الدكتور" in record.text
        assert doctor_client.get("/api/patient/plan").status_code == 401


def test_stop_suppresses_routine_outbox_keeps_order(world: PatientWorld) -> None:
    snapshot = plan.load(world.store, world.patient_scope, world.clock())
    assert snapshot
    routine = [
        r
        for r in world.rows("outbound_intent")
        if r.body["notification_purpose"] == "routine_prompt"
    ]
    world.send("وقف الرسايل")
    after = plan.load(world.store, world.patient_scope, world.clock())
    assert after
    assert after.orders == snapshot.orders
    for old in routine:
        row = world.store.get(world.patient_scope, "outbound_intent", old.id)
        assert row
        assert row.body["status"] == "suppressed"
    _, reply = world.send("/plan")
    assert reply.template_id == "patient_plan_summary"


def test_session_contains_only_six_fenced_turns(world: PatientWorld) -> None:
    for _ in range(5):
        world.send("/plan")
    row = world.store.get(
        world.patient_scope, "session_snapshot", "concierge:" + world.patient_scope.patient_id
    )
    assert row
    saved = from_record(row, SessionSnapshot)
    turns = saved.blob["turns"]
    assert isinstance(turns, list) and len(turns) == 6
    assert saved.source_order_versions


def test_model_context_excludes_private_facts_and_old_session_numbers(world: PatientWorld) -> None:
    seen: list[dict[str, Any]] = []
    snap = plan.load(world.store, world.patient_scope, world.clock())
    assert snap

    def inspect_call(kwargs: dict[str, Any]) -> dict[str, Any]:
        seen.append(kwargs)
        return candidate(
            {
                "reply": plan.order_line(snap.orders[0]),
                "kind": "plan",
                "needs_doctor": False,
            }
        )

    model = ScriptedModel(inspect_call)
    world.concierge.model_factory = lambda registry, role: model
    world.post(update(PATIENT, "هو الدكتور قال 40 ولا 20؟", 1700))
    assert world.receipt(1700).state == "completed"
    assert len(seen) == 1
    body = json.dumps(seen[0], ensure_ascii=False)
    assert answer.PROMPT_VERSION in body and "effective_from" in body
    assert "Answer the CURRENT question" in body
    assert "Extract a candidate from" not in body
    bundle = answer.build_bundle(
        snap, "هو الدكتور قال 40 ولا 20؟", (), ({"role": "user", "content": "98765"},)
    )
    assert "98765" not in bundle.numbers


def test_start_while_stopped_retains_anchor_without_restarting_contact(world: PatientWorld) -> None:
    world.send("وقف الرسايل")
    _, reply = world.send("بدأت الدوا")
    assert reply.template_id == "patient_start_recorded"
    assert reply.payload and "لسه موقوفة" in str(reply.payload["text"])
    task = from_record(world.rows("followup")[0], FollowUpTask)
    assert task.anchor_time == world.clock() and task.state == "contact_suppressed"
    assert any(r.body["state"] == "fulfilled" for r in world.rows("mission"))
    assert not world.profile.routine_contact_enabled


def test_ambiguous_effective_start_date_does_not_fulfill(world: PatientWorld) -> None:
    _, reply = world.send("بدأت أتورفاستاتين من فترة")
    assert reply.template_id == "patient_start_date"
    assert not any(r.body["state"] == "fulfilled" for r in world.rows("mission"))
    assert not any(r.body["category"] == "patient_report" for r in world.rows("clinical_fact"))
    _, reply = world.send("بدأت الدوا امبارح")
    task = from_record(world.rows("followup")[0], FollowUpTask)
    assert task.anchor_time == world.clock() - timedelta(days=1)
    assert task.prompt_at == world.clock() + timedelta(days=2)
    assert reply.payload and task.prompt_at.date().isoformat() in str(reply.payload["text"])


def test_third_party_report_does_not_become_patient_reading(world: PatientWorld) -> None:
    _, reply = world.send("ابني قاس سكر 240")
    assert reply.template_id == "patient_safe_fallback"
    assert not any(r.body["category"] == "patient_report" for r in world.rows("clinical_fact"))


def test_private_fact_and_other_patient_never_enter_context(world: PatientWorld) -> None:
    from sanad.domain import Provenance
    from sanad.scribe.records import FactPayload

    own = world.patient_scope
    fact = ClinicalFact(
        id="private-fixture",
        scope=own,
        created_at=world.clock(),
        updated_at=world.clock(),
        category="history",
        payload=FactPayload(text="PRIVATE_DOCTOR_NOTE_76543"),
        provenance=Provenance(
            source_observation_id="private-doctor-observation",
            actor_kind="doctor",
            actor_id=world.owner.subject,
            source_kind="doctor_statement",
            received_at=world.clock(),
        ),
    )
    world.seed(fact)
    another = world.named_stub("OTHER_PATIENT_SECRET")
    snapshot = plan.load(world.store, own, world.clock())
    assert snapshot
    data = answer.build_bundle(snapshot, "هو الدكتور قال 40 ولا 20؟", ()).json
    assert "PRIVATE_DOCTOR_NOTE" not in data and another.display_name not in data
    assert fact.id not in json.dumps(plan.projection(snapshot))


def test_reading_does_not_fulfill_monitor(world: PatientWorld) -> None:
    from sanad.domain import Mission
    from sanad.domain.entities import MonitorDetails
    from sanad.domain.predicates import EvidencePredicate

    template = from_record(
        next(r for r in world.rows("mission") if r.body["kind"] == "MEDICATION"), Mission
    )
    monitor = Mission.model_validate(
        template.model_dump()
        | {
            "id": "monitor-fixture",
            "version": 1,
            "kind": "MONITOR",
            "details": MonitorDetails(
                metric="BP",
                unit="mmHg",
                slots=(world.clock() + timedelta(hours=1),),
                required_coverage=1,
            ),
            "objective_predicate": EvidencePredicate(evaluator="monitor"),
            "order_refs": (),
        }
    )
    world.seed(monitor)
    world.send("ضغطي ١٥٠ على ٩٥")
    saved = world.store.get_mission(world.patient_scope, monitor.id)
    assert saved is not None and saved.state == monitor.state
    assert (saved.details, saved.due_at, saved.escalation_at, saved.evidence_refs) == (
        monitor.details,
        monitor.due_at,
        monitor.escalation_at,
        monitor.evidence_refs,
    )
    assert saved.fulfillment_validity == monitor.fulfillment_validity
    assert saved.last_patient_reply_at == world.clock()
    fact = next(
        from_record(r, ClinicalFact)
        for r in world.rows("clinical_fact")
        if r.body["category"] == "patient_report"
    )
    assert (
        isinstance(fact.payload, ReportFactPayload)
        and fact.payload.readings[0].quoted == "١٥٠ على ٩٥"
    )


def test_implausible_reading_is_retained_as_report_without_validation(world: PatientWorld) -> None:
    _, reply = world.send("ضغطي 300/200")
    assert reply.template_id == "patient_reading_recorded"
    assert reply.payload and "300/200" in str(reply.payload["text"])
    assert "محتاجة تأكيد" in str(reply.payload["text"])
    fact = next(
        from_record(r, ClinicalFact)
        for r in world.rows("clinical_fact")
        if r.body["category"] == "patient_report"
    )
    assert isinstance(fact.payload, ReportFactPayload)
    assert fact.payload.readings[0].judgment == "implausible"
    assert not any(r.body["state"] == "fulfilled" for r in world.rows("mission"))
