"""Persisted administrative details, with the memory/DynamoDB parity fixture."""

import json
from uuid import uuid4

import pytest

from sanad.accounts.commands import RejectDoctor
from sanad.store._base import Write
from sanad.store.records import Application
from store.account_fixtures import ADMIN, callback
from store.login_fixtures import LoginWorld, browser_login
from store.test_admin_boundary import admin_path, headers
from store.test_login_claim import enrollment as enrollment


@pytest.mark.parametrize(
    "path,reason",
    [("web", "unverified"), ("web", "admin_rejected"), ("telegram", "admin_rejected")],
)
def test_sample18h3_reject_reason(enrollment: LoginWorld, path: str, reason: str) -> None:
    world = enrollment
    app = world.apply("45678")
    with world.client() as client:
        assert browser_login(client, admin_path(world)).status_code == 303
        if path == "web":
            result = client.post(
                f"/api/admin/applications/{app.id}/reject",
                headers=headers(client),
                json={
                    "command_id": uuid4().hex,
                    "expected_version": app.version,
                    "reason_code": reason,
                },
            )
            assert result.status_code == 200 and result.json()["status"] == "accepted"
        else:
            raw = world.action_token(app.id, "admin_new_application", 1)
            assert world.post(callback(raw, ADMIN, 987)).status_code == 200
        changed = world.runtime.accounts.application(app.id)
        assert changed and changed.status == "rejected" and changed.decision_reason == reason
        row = next(r for r in client.get("/api/admin/applications").json() if r["id"] == app.id)
        assert row["decision_reason"] == reason
        assert row["decided_at"] == world.clock().isoformat()
        assert row["suspended_at"] is None and row["suspension_reason"] is None


def test_sample18h3_legacy_and_coverage(enrollment: LoginWorld) -> None:
    world = enrollment
    service = world.runtime.accounts
    pending = world.apply("45678")
    legacy = world.apply("45679")
    assert (
        service.reject(
            RejectDoctor(
                command_id=uuid4().hex,
                actor=world.actor(),
                application_id=legacy.id,
                expected_application_version=legacy.version,
                reason_code="legacy_other",
            )
        ).status
        == "accepted"
    )
    stored = world.store.get(service.scope, "application", legacy.id)
    assert stored
    # Remove the optional key from the actual stored body to simulate an old deployment.
    from sanad.store.records import record_item

    raw = record_item(stored)
    body = json.loads(raw["body"])
    body.pop("decision_reason")
    raw["body"] = json.dumps(body)
    assert world.store._atomic([Write(raw, stored.version)], [])
    loaded = service.application(legacy.id)
    assert isinstance(loaded, Application) and loaded.decision_reason is None
    with world.client() as client:
        assert browser_login(client, admin_path(world)).status_code == 303
        rows = client.get("/api/admin/applications").json()
        for id in (pending.id, legacy.id):
            row = next(r for r in rows if r["id"] == id)
            assert {
                "decided_at",
                "decision_reason",
                "suspended_at",
                "suspension_reason",
            } <= row.keys()
            assert row["decision_reason"] is None
        doctor = world.doctor
        response = client.post(
            f"/api/admin/doctors/{doctor.id}/suspend",
            headers=headers(client),
            json={
                "command_id": uuid4().hex,
                "expected_version": doctor.version,
                "reason_code": "coverage",
            },
        )
        assert response.status_code == 200 and response.json()["status"] == "accepted"
        row = next(
            r for r in client.get("/api/admin/applications").json() if r["doctor_id"] == doctor.id
        )
        assert (
            row["suspended_at"] == world.clock().isoformat()
            and row["suspension_reason"] == "coverage"
        )
        assert (
            client.post(
                f"/api/admin/doctors/{doctor.id}/reinstate",
                headers=headers(client),
                json={"command_id": uuid4().hex, "expected_version": row["doctor_version"]},
            ).status_code
            == 200
        )
        row = next(
            r for r in client.get("/api/admin/applications").json() if r["doctor_id"] == doctor.id
        )
        assert row["suspended_at"] is None and row["suspension_reason"] is None
