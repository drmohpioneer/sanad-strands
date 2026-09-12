"""Missing matrix outcomes at mounted HTTP and captured delivery boundaries."""

from datetime import timedelta

import pytest
from harness import FakeClock
from store.account_fixtures import update
from store.login_fixtures import browser_login
from store.test_admin_boundary import headers

from sanad.store._base import StoreBase
from system.subjects import DOCTOR_A, DOCTOR_B, PATIENT_A, SubjectWorld


def test_t01_unapproved_attacker_cannot_read_or_write_chart(
    store: StoreBase, clock: FakeClock
) -> None:
    w = SubjectWorld.for_subjects(store, clock, doctor=DOCTOR_A, patient=PATIENT_A)
    w.approve(DOCTOR_A, language="en")
    w.enroll_named("Synthetic Protected Patient", id=100)
    w.send("talk to the doctor about my diary", id=1000)
    question = w.rows("mission")[0]
    before = {kind: w.rows(kind) for kind in ("patient", "mission", "review", "clinical_fact")}
    w.apply(DOCTOR_B)
    assert not w.actor(DOCTOR_B).verified_roles
    assert w.post(update(DOCTOR_B, "/login", 1001)).status_code == 200
    assert not [
        i
        for i in w.intents()
        if i.recipient_ref == DOCTOR_B and i.template_id == "doctor_login_link"
    ]
    with w.client() as attacker:
        attacker.cookies.set("sanad_session", "unapproved-forged-session")
        attacker.cookies.set("sanad_csrf", "synthetic-csrf")
        for path in (
            "/a",
            f"/a/patients/{w.patient_scope.patient_id}",
            "/api/patients",
            f"/api/patients/{w.patient_scope.patient_id}",
        ):
            result = attacker.get(path)
            assert result.status_code == 401
            assert "Synthetic Protected Patient" not in result.text
            assert question.id not in result.text
        result = attacker.post(
            f"/api/questions/{question.id}/close",
            headers=headers(attacker),
            json={"command_id": "attacker-close", "expected_version": question.version},
        )
        assert result.status_code == 401
    assert before == {kind: w.rows(kind) for kind in before}


@pytest.mark.parametrize("kind", ["guessed", "expired"])
def test_t03_guessed_and_expired_exchange_post_refused(
    store: StoreBase, clock: FakeClock, kind: str
) -> None:
    w = SubjectWorld.for_subjects(store, clock, doctor=DOCTOR_A, patient=PATIENT_A)
    w.approve(DOCTOR_A, language="en")
    w.enroll_named("Synthetic Protected Patient", id=100)
    path = w.login_path(DOCTOR_A, id=1000)
    if kind == "expired":
        clock.advance(timedelta(minutes=10))
    else:
        path = "/d/guessed-synthetic-token"
    with w.client() as client:
        # browser_login obtains and posts the genuine pre-session CSRF from GET.
        refused = browser_login(client, path)
        assert refused.status_code == 403
        assert "Synthetic Protected Patient" not in refused.text
        assert "sanad_session" not in client.cookies
        assert client.get("/api/patients").status_code == 401
        assert browser_login(client, w.login_path(DOCTOR_A, id=1001)).status_code == 303
        assert "Synthetic Protected Patient" in client.get("/api/patients").text


def test_t05_pre_activation_replies_disclose_no_chart(store: StoreBase, clock: FakeClock) -> None:
    w = SubjectWorld.for_subjects(store, clock, doctor=DOCTOR_A, patient=PATIENT_A)
    w.approve(DOCTOR_A, language="en")
    patient = w.named_stub("Synthetic Secret Patient", subject=DOCTOR_A)
    w.patient_scope = patient.scope
    w.dictate(
        "Synthetic Secret Patient. Let's give her Forxiga 10 mg.",
        {
            "patient": {"name_as_spoken": "Synthetic Secret Patient"},
            "orders": [
                {
                    "action": "start",
                    "drug": "Forxiga",
                    "dose": "10 mg",
                    "action_quote": "Let's give her",
                }
            ],
        },
        id=10,
    )
    w.tap(id=11)
    assert w.rows("care_order_head")
    start = len(w.transport.calls)
    pending = w.claim(w.invite(patient), subject=PATIENT_A, id=100)
    for id, text in ((110, "plan"), (111, "/login")):
        assert w.post(update(PATIENT_A, text, id)).status_code == 200
    pending = w.consent(pending, id=112)
    assert pending.state == "pending"
    for id, text in ((113, "plan"), (114, "/login")):
        assert w.post(update(PATIENT_A, text, id)).status_code == 200
    patient_calls = [call for call in w.transport.calls[start:] if call.recipient_ref == PATIENT_A]
    assert patient_calls
    for call in patient_calls:
        assert all(
            secret not in str(call.payload)
            for secret in ("Synthetic Secret Patient", "Forxiga", "10 mg", patient.id)
        )
    assert not w.actor(PATIENT_A).verified_roles
    assert w.claims.confirm(w.confirm_command(pending)).status == "accepted"
    with w.client() as client:
        assert browser_login(client, w.login_path(PATIENT_A, id=115)).status_code == 303
        assert "Forxiga" in client.get("/api/patient/plan").text


def test_t41_urgent_delivery_uses_changed_authorized_recipient(
    store: StoreBase, clock: FakeClock
) -> None:
    from store.fixtures import SCOPE
    from store.processing_fixtures import World

    w = World.create(store, clock)
    w.urgent.raise_incident(SCOPE, "before-chat-change", {}, "urgent", clock())
    old = w.queued("DANGER")[0]
    old_chat = old.recipient_ref
    new_chat = "synthetic-new-doctor-chat"
    w.put(w.doctor.model_copy(update={"version": w.doctor.version + 1, "recipient_ref": new_chat}))
    assert w.dispatch(old).status == "suppressed"
    assert not w.transport.calls
    w.urgent.raise_incident(SCOPE, "after-chat-change", {}, "urgent", clock())
    current = next(i for i in w.queued("DANGER") if i.id != old.id)
    assert w.dispatch(current).status == "provider_accepted"
    assert [call.recipient_ref for call in w.transport.calls] == [new_chat]
    assert all(call.recipient_ref != old_chat for call in w.transport.calls)
