"""Identity confirmation, timing, replay and language at the real local boundaries."""

import json
import re
from datetime import timedelta
from typing import cast

import pytest
from harness import FakeClock
from providers.fixtures import png

from sanad.contact.delivery import doctor_payload
from sanad.domain import Mission, WorkClock
from sanad.domain import TestDetails as LabDetails
from sanad.evidence.doctor import decide, owned
from sanad.steward.service import system_command
from sanad.steward.types import CommandResult
from sanad.store._base import StoreBase
from sanad.store.records import (
    Evidence,
    OutboundIntent,
    Patient,
    canonical_json,
    from_record,
    to_record,
)
from sanad.web.routes import CSRF_COOKIE
from store import evidence_fixtures as f
from store.account_fixtures import APPLICANT, update
from store.concierge_fixtures import PatientWorld
from store.login_fixtures import ORIGIN, browser_login


@pytest.fixture
def world(store: StoreBase, clock: FakeClock) -> PatientWorld:
    value = f.world(store, clock)
    language(value, "en")
    return value


def language(world: PatientWorld, value: str) -> None:
    doctor = world.doctor
    world.seed(doctor.model_copy(update={"language": value, "version": doctor.version + 1}))
    patient = from_record(world.rows("patient")[0], Patient)
    world.seed(
        patient.model_copy(
            update={
                "language": value,
                "version": patient.version + 1,
                "record_version": patient.record_version + 1,
            }
        )
    )


def card(world: PatientWorld, evidence: Evidence) -> OutboundIntent:
    return next(
        from_record(r, OutboundIntent)
        for r in world.rows("outbound_intent")
        if r.body.get("template_id") == "doctor_evidence_card"
        and any(
            ref.id == evidence.evidence_id and ref.version == evidence.version
            for ref in from_record(r, OutboundIntent).source_versions
        )
    )


def buttons(value: OutboundIntent) -> list[list[dict[str, str]]]:
    assert value.payload
    return cast(
        list[list[dict[str, str]]],
        json.loads(json.dumps(value.payload))["reply_markup"]["inline_keyboard"],
    )


def current(world: PatientWorld, id: str) -> Evidence:
    return next(e for e in owned(world.store, world.patient_scope.doctor_id) if e.evidence_id == id)


def confirm(world: PatientWorld, evidence: Evidence, id: str = "confirm-paper") -> CommandResult:
    return decide(
        world.runtime.steward,
        world.owner,
        evidence,
        "confirm_identity",
        id,
        mission_id=evidence.mission_id,
    )


@pytest.mark.parametrize("method", ["api", "telegram"])
@pytest.mark.parametrize("action", ["confirm", "reject"])
@pytest.mark.parametrize("lang", ["en", "ar"])
def test_identity_doctor_actions(world: PatientWorld, method: str, action: str, lang: str) -> None:
    language(world, lang)
    f.stopped_medication(world)
    before_orders = canonical_json([r.body for r in world.rows("care_order_head")])
    f.mission(world, title="Potassium test")
    vision, _, _ = f.providers(
        world, f.lab("6.3", printed_name=None), f.lab("6.3", printed_name="أحمد")
    )
    assert f.upload(world) == "accepted"
    e = f.current(world)
    assert e.association_state == "accepted_pending_identity"
    assert e.flags == ("identity_unverifiable",) and e.required_predicate_results[0].satisfied
    assert world.rows("mission")[0].body["state"] == "open"
    assert not any(r.body["review_kind"] == "result_review" for r in world.rows("review"))
    assert len(world.rows("incident")) == 1
    intent = card(world, e)
    markup = buttons(intent)
    assert len(markup) == 2
    assert [row[0]["text"] for row in markup] == (
        ["This patient's paper ✅", "Not this patient ❌"]
        if lang == "en"
        else ["ورقة المريض ده ✅", "مش ورقته ❌"]
    )
    assert intent.payload
    if lang == "en":
        assert (
            "The name on this paper could not be read. Is this Synthetic Patient's paper?"
            in str(intent.payload["text"])
        )
        assert not re.search(r"[\u0600-\u06ff]", json.dumps(intent.payload, ensure_ascii=False))
    else:
        assert "الاسم على الورقة مقدرتش أقراه، دي ورقة Synthetic Patient؟" in str(
            intent.payload["text"]
        )
    assert not any(i.template_id == "patient_evidence_name_check" for i in world.patient_intents())
    if lang == "en":
        emergency = next(i for i in world.patient_intents() if i.template_id == "patient_emergency")
        assert not re.search(
            r"[\u0600-\u06ff]", str(world.runtime.patient_payload(emergency)["text"])
        )
    if method == "api":
        with world.client() as client:
            assert browser_login(client, world.login_path()).status_code == 303
            headers = {"Origin": ORIGIN, "X-CSRF-Token": client.cookies[CSRF_COOKIE]}
            endpoint = "confirm-identity" if action == "confirm" else "reject"
            body = {} if action == "confirm" else {"reason": "Not this patient"}
            url = f"/api/evidence/{e.evidence_id}/{endpoint}"
            assert client.post(url, json=body, headers=headers).status_code == 200
            assert client.post(url, json=body, headers=headers).status_code == 409
    else:
        raw = markup[0 if action == "confirm" else 1][0]["callback_data"]
        world.tap(raw=raw, id=2200)
        version = f.current(world).version
        world.tap(raw=raw, id=2201)
        assert f.current(world).version == version
    after = f.current(world)
    assert after.version == e.version + 1
    assert after.association_state == ("accepted" if action == "confirm" else "rejected")
    m = from_record(world.rows("mission")[0], Mission)
    if action == "confirm":
        assert m.state == "fulfilled" and m.objective_received_at == world.receipt(1100).received_at
        assert "identity_confirmed" in after.flags
        assert sum(r.body["review_kind"] == "result_review" for r in world.rows("review")) == 1
        refused = decide(
            world.runtime.steward, world.owner, after, "reject", "late-reject", reason="Wrong paper"
        )
        assert refused.reason_code == "correction_requires_slice19"
    else:
        assert m.state == "open" and not m.evidence_refs and after.mission_id is None
        question = next(
            i for i in world.patient_intents() if i.template_id == "patient_evidence_name_check"
        )
        assert [row[0]["text"] for row in buttons(question)] == (
            ["Yes", "No"] if lang == "en" else ["نعم", "لا"]
        )
        world.press(question, id=2400)
        assert f.current(world).association_state == "candidate"
        assert world.rows("mission")[0].body["state"] == "open"
    assert canonical_json([r.body for r in world.rows("care_order_head")]) == before_orders
    assert len(vision.calls) == 2 and len(world.rows("incident")) == 1
    danger = [
        from_record(r, OutboundIntent)
        for r in world.rows("outbound_intent")
        if r.body.get("notification_purpose") == "DANGER"
    ]
    assert len(danger) == 1
    text = str(world.runtime.patient_payload(danger[0])["text"])
    assert (
        "The requested evidence was received" in text
        if lang == "en"
        else "وصل المستند المطلوب" in text
    ) == (action == "confirm")
    if lang == "en":
        assert not re.search(r"[\u0600-\u06ff]", text)
    assert not any(
        r.body.get("notification_purpose") == "DONE:FULFILLMENT"
        for r in world.rows("outbound_intent")
    )


@pytest.mark.parametrize("late", [False, True])
def test_t17_identity_pending_after_completed_extraction(world: PatientWorld, late: bool) -> None:
    due = world.clock() + timedelta(hours=1)
    f.mission(
        world,
        title="Potassium test",
        due_at=due,
        escalation_at=due,
        work_clock=WorkClock(work_lane="mission", next_action_at=due),
    )
    if late:
        world.clock.advance(timedelta(minutes=90))
    f.providers(world, f.lab(printed_name=None), f.lab(printed_name=None))
    assert f.upload(world) == "accepted"
    e = f.current(world)
    assert f.work(world).state == "completed" and e.required_predicate_results[0].satisfied
    assert e.association_state == "accepted_pending_identity"
    world.clock.advance(timedelta(hours=2))
    result = world.runtime.steward.handle(
        system_command(
            world.patient_scope,
            "identity-deadline",
            {"type": "_Deadline", "mission_id": e.mission_id},
            world.clock(),
            lane="mission",
        )
    )
    assert result.status == "accepted"
    deadline = next(
        from_record(r, OutboundIntent)
        for r in world.rows("outbound_intent")
        if r.body.get("notification_purpose") == "DEADLINE"
    )
    wording = str(doctor_payload(world.store, deadline)["text"])
    assert ("a file arrived before the deadline and verification is pending" in wording) is not late
    assert not re.search(r"[\u0600-\u06ff]", wording)
    assert confirm(world, e).status == "accepted"
    m = from_record(world.rows("mission")[0], Mission)
    assert m.timeliness == ("late" if late else "on_time")
    assert m.due_at == due and m.objective_received_at == world.receipt(1100).received_at
    assert m.fulfilled_at == world.clock()
    assert (
        sum(
            r.body.get("notification_purpose") == "DONE:FULFILLMENT"
            for r in world.rows("outbound_intent")
        )
        == 1
    )
    audit = next(
        r
        for r in world.rows("audit_event")
        if r.body["event_type"] == "DEADLINE_DISPOSITION_UPDATED"
    )
    assert json.loads(audit.model_dump_json())["body"]["before_versions"][0]["id"] == (
        m.latest_deadline_notice_event_id
    )


@pytest.mark.parametrize("both_unverifiable", [False, True])
def test_joint_content_needs_each_papers_identity(
    world: PatientWorld, both_unverifiable: bool
) -> None:
    f.mission(
        world, title="Panel", details=LabDetails(analytes=("K", "Creatinine"), completeness="all")
    )
    cr = f.lab(
        printed_name=None if both_unverifiable else "Synthetic Patient",
        items=[{"name": "Creatinine", "value": "1.0", "unit": "mg/dL"}],
    )
    vision, files, _ = f.providers(
        world, f.lab(printed_name=None), f.lab(printed_name=None), cr, cr
    )
    assert f.upload(world) == "accepted"
    first = f.current(world)
    assert (
        first.association_state == "accepted_pending_identity"
        and not first.required_predicate_results[0].satisfied
    )
    world.clock.advance(timedelta(minutes=30))
    files.result = png(3)
    assert f.upload(world, 1101) == "accepted"
    second = next(
        e
        for e in owned(world.store, world.patient_scope.doctor_id)
        if e.evidence_id != first.evidence_id
    )
    assert second.required_predicate_results[0].satisfied
    assert world.rows("mission")[0].body["state"] != "fulfilled"
    if both_unverifiable:
        assert confirm(world, second, "confirm-second").status == "accepted"
        assert world.rows("mission")[0].body["state"] != "fulfilled"
    assert confirm(world, first).status == "accepted"
    m = from_record(world.rows("mission")[0], Mission)
    assert m.state == "fulfilled" and m.objective_received_at == world.receipt(1101).received_at
    assert len(m.evidence_refs) == 2 and len(vision.calls) == 4
    assert all(not current(world, ref.fact_id).identity_pending for ref in m.evidence_refs)


@pytest.mark.parametrize("case", ["expiry", "epoch", "patient", "mission", "replay"])
def test_identity_confirmation_guards(world: PatientWorld, case: str) -> None:
    f.mission(world, title="Potassium test")
    f.providers(world, f.lab(printed_name=None), f.lab(printed_name=None))
    assert f.upload(world) == "accepted"
    e = f.current(world)
    raw = buttons(card(world, e))[0][0]["callback_data"]
    if case == "expiry":
        world.clock.advance(timedelta(hours=25))
        world.tap(raw=raw, id=2200)
    elif case == "epoch":
        doctor = world.doctor
        world.seed(
            doctor.model_copy(
                update={"version": doctor.version + 1, "auth_epoch": doctor.auth_epoch + 1}
            )
        )
        world.tap(raw=raw, id=2200)
    elif case == "patient":
        receipt = world.receipt(1100)
        actor = world.store.authorize(
            world.runtime.settings.bot_id, receipt.source_subject
        ).principal
        result = decide(world.runtime.steward, actor, e, "confirm_identity", "patient-forgery")
        assert result.status == "forbidden"
    elif case == "mission":
        f.mission(world, "second-mission", title="Another test")
        assert (
            decide(
                world.runtime.steward,
                world.owner,
                e,
                "confirm_identity",
                "wrong-target",
                mission_id="second-mission",
            ).status
            == "invalid_input"
        )
    else:
        assert confirm(world, e).status == "accepted"
        assert confirm(world, e).status == "accepted"
        assert f.current(world).version == e.version + 1
        assert (
            sum(r.body["event_type"] == "OBJECTIVE_FULFILLED" for r in world.rows("audit_event"))
            == 1
        )
        return
    assert f.current(world).version == e.version
    assert world.rows("mission")[0].body["state"] != "fulfilled"


def test_pending_identity_recovers_without_another_read(world: PatientWorld) -> None:
    f.mission(world, title="Potassium test")
    vision, _, _ = f.providers(world, f.lab(printed_name=None), f.lab(printed_name=None))

    def crash(stage: str) -> None:
        if stage == "evidence_acceptance_persisted":
            raise RuntimeError("synthetic crash after pending identity")

    world.concierge.checkpoint = crash
    with pytest.raises(RuntimeError, match="synthetic crash"):
        f.upload(world)
    e = f.current(world)
    assert e.association_state == "accepted_pending_identity"
    world.concierge.checkpoint = lambda stage: None
    world.clock.advance(timedelta(minutes=11))
    world.concierge.sweep(to_record(f.work(world), world.patient_scope))
    assert f.work(world).state == "completed" and len(vision.calls) == 2
    assert (
        len(
            [
                r
                for r in world.rows("outbound_intent")
                if r.body.get("template_id") == "doctor_evidence_card"
            ]
        )
        == 1
    )
    assert confirm(world, e).status == "accepted"


def test_missing_unit_equivalence_on_a_clear_panel(world: PatientWorld) -> None:
    f.mission(world, title="Potassium test")
    a = f.lab(
        items=[
            {"name": "K", "value": "5.0", "unit": "mmol/L"},
            {"name": "INR", "value": "2.6", "unit": ""},
        ]
    )
    b = f.lab(
        items=[
            {"name": "K", "value": "5.0", "unit": "mmol/L"},
            {"name": "INR", "value": "2.6", "unit": None},
        ]
    )
    f.providers(world, a, b)
    assert f.upload(world) == "accepted"
    e = f.current(world)
    assert not e.disagreements and world.rows("mission")[0].body["state"] == "fulfilled"
    assert e.readers[0].items[1].item.unit == "" and e.readers[1].items[1].item.unit is None


def test_english_listing_and_patient_choices(world: PatientWorld) -> None:
    f.mission(world, title="First test")
    f.mission(world, "second", title="Second test")
    f.providers(world)
    assert f.upload(world) == "accepted"
    choice = next(i for i in world.patient_intents() if i.template_id == "patient_evidence_which")
    assert buttons(choice)[-1][0]["text"] == "Something else"
    assert world.post(update(APPLICANT, "/evidence", 2200)).status_code == 200
    e = f.current(world)
    labels = [row[0]["text"] for row in buttons(card(world, e))]
    assert "Associate: First test" in labels and "Reject" in labels
    world.press(choice, id=2300)
    world.press(choice, id=2301)
    assert any(
        i.payload
        and i.payload["text"]
        == "This choice expired or changed. Send the document or ask for clarification again."
        for i in world.patient_intents()
    )


@pytest.mark.parametrize("action", ["confirm-identity", "reject"])
def test_identity_session_api_rechecks_csrf_scope_and_epoch(
    world: PatientWorld, action: str
) -> None:
    f.mission(world, title="Potassium test")
    f.providers(world, f.lab(printed_name=None), f.lab(printed_name=None))
    assert f.upload(world) == "accepted"
    e = f.current(world)
    url = f"/api/evidence/{e.evidence_id}/{action}"
    body = {} if action == "confirm-identity" else {"reason": "Not this patient"}
    with world.client() as client:
        assert client.post(url, json=body).status_code == 401
        assert browser_login(client, world.login_path()).status_code == 303
        headers = {"Origin": ORIGIN, "X-CSRF-Token": client.cookies[CSRF_COOKIE]}
        assert client.post(url, json=body).status_code == 403
        assert client.post(url, json=body, headers=headers).status_code == 401
        assert browser_login(client, world.login_path(id=201)).status_code == 303
        headers = {"Origin": ORIGIN, "X-CSRF-Token": client.cookies[CSRF_COOKIE]}
        assert (
            client.post(
                url, json=body, headers=headers | {"Origin": "https://foreign.invalid"}
            ).status_code
            == 403
        )
        assert browser_login(client, world.login_path(id=202)).status_code == 303
        headers = {"Origin": ORIGIN, "X-CSRF-Token": client.cookies[CSRF_COOKIE]}
        assert (
            client.post(
                f"/api/evidence/foreign-evidence/{action}", json=body, headers=headers
            ).status_code
            == 404
        )
        doctor = world.doctor
        world.seed(
            doctor.model_copy(
                update={"version": doctor.version + 1, "auth_epoch": doctor.auth_epoch + 1}
            )
        )
        assert client.post(url, json=body, headers=headers).status_code == 401
    assert f.current(world) == e and world.rows("mission")[0].body["state"] == "open"


def test_identity_confirmation_does_not_approve_disputed_values(world: PatientWorld) -> None:
    f.mission(world, title="Potassium test")
    f.providers(world, f.lab("5.0", printed_name=None), f.lab("4.0", printed_name=None))
    assert f.upload(world) == "accepted"
    e = f.current(world)
    assert e.required_predicate_results[0].missing == ("verification",)
    assert confirm(world, e).status == "accepted"
    after = f.current(world)
    assert "identity_confirmed" in after.flags
    assert after.required_predicate_results[0].missing == ("verification",)
    assert world.rows("mission")[0].body["state"] == "open"
    assert any(
        r.body["review_kind"] == "evidence_association" and r.body["state"] != "resolved"
        for r in world.rows("review")
    )
