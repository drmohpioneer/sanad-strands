"""Evidence web replay and identity obligations persist on both store adapters."""

import pytest
from harness import FakeClock
from store import evidence_fixtures as f
from store.login_fixtures import ORIGIN, browser_login

from sanad.store._base import StoreBase


def test_unmatched_identity_replay_then_associate(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "1")
    w = f.world(store, clock)
    lab = f.lab(printed_name="Foreign Person")
    f.providers(w, lab, lab)
    assert f.upload(w) == "accepted"
    e = f.current(w)
    assert e.mission_id is None
    with w.client() as client:
        assert browser_login(client, w.login_path()).status_code == 303
        headers = {"Origin": ORIGIN, "X-CSRF-Token": client.cookies["sanad_csrf"]}
        base = "/api/evidence/" + e.evidence_id
        body = {"command_id": "identity-6h", "evidence_version": e.version}
        result = client.post(base + "/confirm-identity", json=body, headers=headers)
        assert result.status_code == 200, result.text
        confirmed = f.current(w)
        assert "identity_confirmed" in confirmed.flags and confirmed.mission_id is None
        assert any(
            r.body["review_kind"] == "evidence_association" and r.body["state"] == "open"
            for r in w.rows("review")
        )
        replay = client.post(base + "/confirm-identity", json=body, headers=headers)
        assert replay.status_code == 200 and replay.json()["status"] == "duplicate"
        assert "already handled" in replay.json()["detail"]
        assert f.current(w) == confirmed
        # A different payload cannot consume a previously accepted command ID.
        conflict = client.post(
            base + "/reject", json=body | {"reason": "Not this document"}, headers=headers
        )
        assert conflict.status_code == 409 and conflict.json()["reason"] == "stale_version"
        assert f.current(w) == confirmed
        mission = f.mission(w)
        body = {
            "command_id": "associate-6h",
            "evidence_version": confirmed.version,
            "mission_id": mission.id,
        }
        result = client.post(base + "/associate", json=body, headers=headers)
        assert result.status_code == 200, result.text
        accepted = f.current(w)
        assert accepted.association_state == "accepted"
        assert (
            client.post(base + "/associate", json=body, headers=headers).json()["status"]
            == "duplicate"
        )
        assert f.current(w) == accepted
        assert all(
            r.body["state"] == "resolved"
            for r in w.rows("review")
            if r.body["review_kind"] == "evidence_association"
        )
        assert any(
            r.body["state"] != "resolved"
            for r in w.rows("review")
            if r.body["review_kind"] == "result_review"
        )
