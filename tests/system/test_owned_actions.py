"""Each foreign mutation has a populated target and an identical successful owner request."""

from typing import Any

import pytest
from harness import FakeClock
from providers.fixtures import document
from store import evidence_fixtures as f
from store.contact_fixtures import required
from store.login_fixtures import browser_login
from store.test_admin_boundary import headers

from sanad.domain import EvidencePredicate, TaskDetails
from sanad.steward.types import records
from sanad.store._base import StoreBase
from system.subjects import DOCTOR_A, DOCTOR_B, PATIENT_A, SubjectWorld
from system.test_journey import mission_kind


def pair(store: StoreBase, clock: FakeClock) -> tuple[SubjectWorld, SubjectWorld]:
    a = SubjectWorld.for_subjects(store, clock, doctor=DOCTOR_A, patient=PATIENT_A)
    a.approve(DOCTOR_A, language="en")
    a.enroll_named("Synthetic Patient", id=100)
    b = SubjectWorld.for_subjects(store, clock, doctor=DOCTOR_B, patient=PATIENT_A)
    b.approve(DOCTOR_B, language="en")
    return a, b


def denied_then_owner(
    a: SubjectWorld, b: SubjectWorld, path: str, absent: str, body: dict[str, Any]
) -> None:
    kinds = ("mission", "evidence", "evidence_head", "review", "correction", "patient")
    before = {kind: a.rows(kind) for kind in kinds}
    offers = {
        kind: list(records(a.store, a.doctor.scope, kind))
        for kind in ("reuse_offer", "reusable_answer")
    }
    with b.client() as attacker:
        assert browser_login(attacker, b.login_path(DOCTOR_B, id=5000)).status_code == 303
        response = attacker.post(path, json=body, headers=headers(attacker))
        missing = attacker.post(absent, json=body, headers=headers(attacker))
        assert response.status_code == (403 if path.startswith("/api/claims/") else 404), (
            response.text
        )
        assert (response.status_code, response.content) == (missing.status_code, missing.content)
    assert before == {kind: a.rows(kind) for kind in kinds}
    assert offers == {kind: list(records(a.store, a.doctor.scope, kind)) for kind in offers}
    with a.client() as owner:
        assert browser_login(owner, a.login_path(DOCTOR_A, id=5001)).status_code == 303
        result = owner.post(path, json=body, headers=headers(owner))
        assert result.status_code == 200, (path, result.text)
        assert result.json()["status"] == "accepted"


@pytest.mark.parametrize("action", ["answer", "defer", "close", "reuse", "send"])
def test_foreign_question_action_has_valid_owner_control(
    store: StoreBase, clock: FakeClock, action: str
) -> None:
    a, b = pair(store, clock)
    a.send("Where should I bring my appointment diary?", id=1000)
    q = mission_kind(a, "QUESTION")
    body: dict[str, Any] = {"command_id": "owned-action", "expected_version": q.version}
    if action == "answer":
        body["text"] = "Please bring your diary to the clinic."
    if action in {"reuse", "send"}:
        a.doctor_says("/questions", id=3000)
        a.doctor_says("/answer 1 Please bring your diary to the clinic.", id=3001)
        offer = list(records(store, a.doctor.scope, "reuse_offer"))[0]
        body = {"command_id": "owned-action", "offer_id": offer.id}
        if action == "send":
            a.doctor_says("/reuse", id=3002)
            a.send("Where should I bring my appointment diary tomorrow?", id=1001)
            with a.client() as client:
                assert browser_login(client, a.login_path(DOCTOR_A, id=3003)).status_code == 303
                listing = client.get("/api/questions").json()
                item = listing["questions"][0]
                q = required(store.get_mission(a.patient_scope, item["id"]))
                source = item["proposed_reply"]
                assert source and source["text"] == "Please bring your diary to the clinic."
                body = {
                    "command_id": "owned-action",
                    "listing_token": listing["listing_token"],
                    "n": item["n"],
                    "mission_version": item["version"],
                    "reusable_id": source["reusable_id"],
                    "reusable_version": source["version"],
                }
    denied_then_owner(
        a, b, f"/api/questions/{q.id}/{action}", f"/api/questions/absent/{action}", body
    )


@pytest.mark.parametrize(
    "action", ["associate", "accept", "reject", "confirm-identity", "correction"]
)
def test_foreign_evidence_action_has_valid_owner_control(
    store: StoreBase, clock: FakeClock, action: str
) -> None:
    a, b = pair(store, clock)
    if action in {"accept", "reject"}:
        f.mission(
            a,
            kind="TASK",
            order_refs=(),
            details=TaskDetails(
                category="proof", instruction="Send a document", completion_rule="doctor_acceptance"
            ),
            objective_predicate=EvidencePredicate(evaluator="task_evidence"),
        )
        reading = document(
            document_type="other",
            printed_name="Synthetic Patient",
            items=[{"name": "Requested proof"}],
        )
    else:
        f.mission(a, order_refs=())
        reading = f.lab(
            printed_name="Foreign Person"
            if action == "associate"
            else None
            if action == "confirm-identity"
            else "Synthetic Patient"
        )
    _, _, storage = f.providers(a, reading, reading)
    a.app.state.media_store = storage
    assert f.upload(a) == "accepted"
    e = f.current(a)
    body: dict[str, Any] = (
        {"mission_id": "potassium-test"}
        if action == "associate"
        else {"reason": "Wrong paper"}
        if action == "reject"
        else {}
    )
    path, absent = f"/api/evidence/{e.evidence_id}/{action}", f"/api/evidence/absent/{action}"
    if action == "correction":
        path = f"/api/patients/{a.patient_scope.patient_id}/corrections"
        absent = "/api/patients/absent/corrections"
        body = {
            "command_id": "owned-correction",
            "expected_binding_epoch": a.profile.binding_epoch,
            "expected_delivery_epoch": a.profile.delivery_epoch,
            "action": {
                "type": "CorrectRecord",
                "operation": "detach",
                "reason": "Wrong paper",
                "predecessor": {"entity_type": "evidence", "id": e.id, "version": e.version},
            },
        }
    denied_then_owner(a, b, path, absent, body)


def test_foreign_pending_claim_has_valid_owner_control(store: StoreBase, clock: FakeClock) -> None:
    a, b = pair(store, clock)
    patient = a.named_stub("Synthetic Pending Patient", subject=DOCTOR_A)
    pending = a.consent(a.claim(a.invite(patient), subject="30009", id=1000), id=1001)
    assert pending.state == "pending"
    versions = a.claims.confirmation_versions(a.owner, pending.id)
    assert versions
    body = {
        "command_id": "owned-confirm",
        "expected_versions": [v.model_dump(mode="json") for v in versions],
    }
    denied_then_owner(a, b, f"/api/claims/{pending.id}/confirm", "/api/claims/absent/confirm", body)
    assert required(a.claims.patient_claim(pending.id)).state == "approved"
    assert a.actor("30009").patient_id == patient.id
