from typing import Annotated
from uuid import uuid4

import pytest
from fastapi import Depends

from sanad.store.records import WebSession
from sanad.web.routes import require_session
from store.account_fixtures import APPLICANT, PATIENT
from store.login_fixtures import ORIGIN, LoginWorld, browser_login
from store.test_login_claim import enrollment as enrollment


@pytest.mark.parametrize("csrf", ["valid", "missing", "mismatch", "cookie_missing", "cross_origin"])
def test_doctor_session_confirmation_post(enrollment: LoginWorld, csrf: str) -> None:
    world = enrollment
    pending = world.consent(world.claim(world.invite(world.stub())))
    versions = world.claims.confirmation_versions(world.owner, pending.id)
    with world.client() as client:
        assert browser_login(client, world.login_path()).status_code == 303
        session_raw = client.cookies["sanad_session"]
        csrf_raw = client.cookies["sanad_csrf"]
        headers = {"Origin": ORIGIN}
        if csrf != "missing":
            headers["X-CSRF-Token"] = "wrong" if csrf == "mismatch" else csrf_raw
        if csrf == "cookie_missing":
            client.cookies.delete("sanad_csrf")
        elif csrf == "cross_origin":
            headers["Origin"] = "https://foreign.example"
        response = client.post(
            "/api/claims/" + pending.id + "/confirm",
            headers=headers,
            json={
                "command_id": uuid4().hex,
                "expected_versions": [v.model_dump() for v in versions],
            },
        )
        assert response.status_code == (200 if csrf == "valid" else 403)
        assert (world.actor(PATIENT).actor_kind == "patient") == (csrf == "valid")
        if csrf != "valid":
            saved = world.login.session(session_raw)
            assert saved and saved.revoked_at is not None


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("field", [True, False])
def test_shared_guard_every_write_accepts_form_or_header_csrf(
    enrollment: LoginWorld, method: str, field: bool
) -> None:
    world = enrollment

    # Test-only route checks the reusable guard that later slices must use.
    @world.app.api_route("/synthetic-write", methods=[method])
    async def write(
        session: Annotated[WebSession, Depends(require_session("doctor"))],
    ) -> dict[str, str]:
        return {"role": session.role}

    with world.client() as client:
        assert browser_login(client, world.login_path()).status_code == 303
        token = client.cookies["sanad_csrf"]
        headers = {"Origin": ORIGIN}
        data = {"csrf": token} if field else {}
        if not field:
            headers["X-CSRF-Token"] = token
        assert (
            client.request(method, "/synthetic-write", headers=headers, data=data).status_code
            == 200
        )
        assert (
            client.request(method, "/synthetic-write", headers={"Origin": ORIGIN}).status_code
            == 403
        )


def test_other_doctor_confirm_post_is_neutral(enrollment: LoginWorld) -> None:
    world = enrollment
    pending = world.consent(world.claim(world.invite(world.stub())))
    world.approve("40004")
    from sanad.auth.commands import IssueDoctorLogin

    assert (
        world.login.issue(
            IssueDoctorLogin(command_id=uuid4().hex, actor=world.actor("40004"))
        ).status
        == "accepted"
    )
    intent = next(
        i
        for i in world.intents()
        if i.template_id == "doctor_login_link" and i.recipient_subject == "40004"
    )
    assert intent.payload
    path = str(intent.payload["text"]).splitlines()[-1]
    with world.client() as client:
        assert browser_login(client, path).status_code == 303
        response = client.post(
            "/api/claims/" + pending.id + "/confirm",
            headers={"Origin": ORIGIN, "X-CSRF-Token": client.cookies["sanad_csrf"]},
            json={"command_id": uuid4().hex, "expected_versions": []},
        )
        assert response.status_code == 403 and "Synthetic" not in response.text
        assert world.doctor.id not in response.text and APPLICANT not in response.text
