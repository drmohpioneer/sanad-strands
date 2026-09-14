"""The browser supplies a profile version and confirms an owned, exact name."""

from uuid import uuid4

import pytest
from store.conftest import clock as clock
from store.conftest import ddb_server as ddb_server
from store.conftest import pytest_generate_tests as pytest_generate_tests
from store.conftest import store as store
from store.login_fixtures import ORIGIN, LoginWorld, browser_login
from store.test_login_claim import enrollment as enrollment


def test_removal_http_authority_versions_and_replay(enrollment: LoginWorld) -> None:
    w = enrollment
    patient = w.stub()
    with w.client() as client:
        path = "/api/patients/" + patient.id + "/remove"
        assert client.post(path, json={}).status_code in {401, 403, 422}
        assert browser_login(client, w.login_path()).status_code == 303
        record = client.get("/api/patients/" + patient.id).json()
        payload = {
            "expected_version": record["profile_version"],
            "command_id": uuid4().hex,
            "name": patient.display_name,
        }
        headers = {"Origin": ORIGIN, "X-CSRF-Token": client.cookies["sanad_csrf"]}
        assert (
            client.post("/api/patients/foreign/remove", json=payload, headers=headers).status_code
            == 404
        )
        assert (
            client.post(path, json=payload | {"name": "wrong"}, headers=headers).status_code == 422
        )
        assert (
            client.post(
                path, json={k: v for k, v in payload.items() if k != "name"}, headers=headers
            ).status_code
            == 422
        )
        assert (
            client.post(path, json=payload | {"expected_version": 999}, headers=headers).status_code
            == 409
        )
        result = client.post(
            path, json=payload | {"name": "  SYNTHETIC   PATIENT  "}, headers=headers
        )
        assert result.status_code == 200, result.text
        before = w.store.get_patient_profile(patient.scope)
        replay = client.post(
            path, json=payload | {"name": "  SYNTHETIC   PATIENT  "}, headers=headers
        )
        assert replay.json() == result.json()
        assert w.store.get_patient_profile(patient.scope) == before
        again = client.post(path, json=payload | {"command_id": uuid4().hex}, headers=headers)
        assert again.status_code == 200
        assert w.store.get_patient_profile(patient.scope) == before
        assert client.get("/api/patients/" + patient.id).json()["removed_at"]
        assert client.post(path, json=payload).status_code == 403


def test_removal_lease_compares_inside_acquisition(
    enrollment: LoginWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import Any

    w = enrollment
    patient = w.stub()
    original = w.store.acquire_patient
    with w.client() as client:
        assert browser_login(client, w.login_path()).status_code == 303
        record = client.get("/api/patients/" + patient.id).json()

        def acquire(*args: Any, **kwargs: Any) -> Any:
            profile = w.store.get_patient_profile(patient.scope)
            assert profile
            # An independent lease write lands after the HTTP read, before conditional acquisition.
            lease = original(
                patient.scope,
                "intervening",
                w.clock(),
                w.runtime.accounts.policy.operations.lease_ttl,
            )
            assert lease
            w.store.release_patient(lease)
            return original(*args, **kwargs)

        monkeypatch.setattr(w.store, "acquire_patient", acquire)
        response = client.post(
            "/api/patients/" + patient.id + "/remove",
            json={
                "expected_version": record["profile_version"],
                "command_id": uuid4().hex,
                "name": patient.display_name,
            },
            headers={"Origin": ORIGIN, "X-CSRF-Token": client.cookies["sanad_csrf"]},
        )
        assert response.status_code == 409
        profile = w.store.get_patient_profile(patient.scope)
        assert profile and not profile.removed_at
