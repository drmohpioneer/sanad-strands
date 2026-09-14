"""Real session routes, existing Scribe command agreement and isolation on both stores."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from harness import FakeClock

from sanad.auth.service import revise
from sanad.store._base import StoreBase
from sanad.store.records import AuditEvent, from_record
from sanad.web.security import HEADERS
from store.account_fixtures import APPLICANT, update
from store.login_fixtures import ORIGIN, LoginWorld, browser_login, replace_model

CSP = (
    "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "style-src 'self'; font-src 'self'; script-src 'self'; img-src 'self' data:; "
    "connect-src 'self'"
)


@pytest.fixture
def dashboard(store: StoreBase, clock: FakeClock) -> LoginWorld:
    world = LoginWorld.create(store, clock)
    world.approve()
    return world


def test_exact_csp_and_same_origin_assets(dashboard: LoginWorld) -> None:
    assert HEADERS["Content-Security-Policy"] == CSP
    with dashboard.client() as client:
        browser_login(client, dashboard.login_path())
        for path in ("/a", "/a/inbox", "/a/history", "/a/preferences", "/demo"):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["content-security-policy"] == CSP
            assert "unsafe-" not in CSP and "https:" not in CSP
            assert response.headers["cache-control"] == "no-store"
        for asset in (
            "browser.css",
            "theme.js",
            "browser.js",
            "inter.ttf",
            "playfairdisplay.ttf",
            "notonaskharabic.ttf",
            "notosansarabic.ttf",
            "demo.json",
        ):
            response = client.get("/assets/" + asset)
            assert response.status_code == 200 and response.content
            assert response.headers["content-security-policy"] == CSP
            assert "charset" in response.headers["content-type"] or asset.endswith(
                (".ttf", ".json")
            )
        assert client.get("/assets/security.py").status_code == 404
        assert client.get("/assets/../security.py").status_code == 404


@pytest.mark.parametrize("locale", ["en", "ar"])
def test_locale_uses_resolver_and_real_patient_scope(dashboard: LoginWorld, locale: str) -> None:
    world = dashboard
    replace_model(world, revise(world.doctor, world.clock(), language=locale))
    patient = world.bound()
    from store.account_fixtures import PATIENT

    with world.client() as client:
        browser_login(client, world.login_path())
        for path in ("/a", "/a/inbox", "/a/history", "/a/preferences", "/a/patients/" + patient.id):
            result = client.get(path)
            assert result.status_code == 200
            assert 'lang="en" dir="ltr"' in result.text
        foreign = client.get("/a/patients/not-owned")
        assert foreign.status_code == 404 and patient.display_name not in foreign.text
        record = client.get("/api/patients/" + patient.id).json()
        assert record["review_history"] == [] and record["age"] is None
    with world.client() as client:
        browser_login(client, world.login_path(PATIENT, id=201))
        assert client.get("/pp").status_code == 200
        assert client.get("/a").status_code == 401


def language_events(world: LoginWorld) -> list[AuditEvent]:
    from sanad.steward.types import records

    return [
        from_record(r, AuditEvent)
        for r in records(world.store, world.doctor.scope, "audit_event")
        if r.body.get("event_type") == "ScribeLanguage"
    ]


def test_browser_telegram_language_agreement(
    dashboard: LoginWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = dashboard

    def no_model(*args: object, **kwargs: object) -> Any:
        raise AssertionError("language must never invoke a model")

    monkeypatch.setattr(world.app.state.scribe, "model_factory", no_model)
    with world.client() as client:
        browser_login(client, world.login_path())
        transport_before = len(world.transport.calls)
        pref = client.get("/api/preferences").json()
        result = client.post(
            "/api/preferences",
            json={
                "language": "ar",
                "expected_version": pref["version"],
                "command_id": "browser-language",
            },
            headers={"Origin": ORIGIN, "X-CSRF-Token": client.cookies["sanad_csrf"]},
        )
        assert result.status_code == 200, result.text
        assert len(world.transport.calls) == transport_before
        assert world.doctor.language == "ar"
        browser_event = language_events(world)[-1]
        assert world.post(update(APPLICANT, "/lang ar", 4500)).status_code == 200
        assert str(world.doctor.language) == "ar"
        telegram_event = max(language_events(world), key=lambda e: e.after_versions[0].version)
        assert browser_event.event_type == telegram_event.event_type == "ScribeLanguage"
        assert browser_event.actor == telegram_event.actor == world.owner
        assert browser_event.policy_versions == telegram_event.policy_versions == ("scribe-v1",)
        assert [r.entity_type for r in browser_event.aggregate_refs] == ["doctor"]
        assert [r.entity_type for r in telegram_event.aggregate_refs] == ["doctor"]
        assert (
            telegram_event.after_versions[0].version == browser_event.after_versions[0].version + 1
        )
        # A stale browser action cannot overwrite the Telegram preference.
        response = client.post(
            "/api/preferences",
            json={"language": "en", "expected_version": pref["version"], "command_id": "stale"},
            headers={"Origin": ORIGIN, "X-CSRF-Token": client.cookies["sanad_csrf"]},
        )
        assert response.status_code == 409 and str(world.doctor.language) == "ar"
        assert len(language_events(world)) == 2


@pytest.mark.parametrize("failure", ["csrf", "origin", "epoch", "expiry"])
def test_preference_authority_and_csrf(dashboard: LoginWorld, failure: str) -> None:
    world = dashboard
    with world.client() as client:
        browser_login(client, world.login_path())
        version = world.doctor.version
        headers = {"Origin": ORIGIN, "X-CSRF-Token": client.cookies["sanad_csrf"]}
        if failure == "csrf":
            headers["X-CSRF-Token"] = "forged"
        if failure == "origin":
            headers["Origin"] = "https://foreign.example"
        if failure == "expiry":
            world.clock.advance(timedelta(hours=13))
        if failure == "epoch":
            replace_model(
                world, revise(world.doctor, world.clock(), auth_epoch=world.doctor.auth_epoch + 1)
            )
        result = client.post(
            "/api/preferences",
            json={"language": "ar", "expected_version": version, "command_id": "denied"},
            headers=headers,
        )
        assert result.status_code == (403 if failure in {"csrf", "origin"} else 401)
        assert not language_events(world)


def test_demo_does_not_read_store_or_open_session(
    dashboard: LoginWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> Any:
        raise AssertionError("demo cannot access a clinical record or session")

    monkeypatch.setattr(dashboard.store, "get", forbidden)
    monkeypatch.setattr(dashboard.login, "require", forbidden)
    with dashboard.client() as client:
        response = client.get("/demo")
        assert response.status_code == 200
        assert "set-cookie" not in response.headers
        data = client.get("/assets/demo.json").json()
        assert len(data) == 18
        assert all(p["patient_id"].startswith("demo-") for p in data)
    script = Path("src/sanad/web/static/browser.js").read_text()
    assert "credentials:demo?'omit':'same-origin'" in script


def test_fixture_records_through_real_projections(store: StoreBase, clock: FakeClock) -> None:
    from dashboard18_fixture import LONG_DRUG, LONG_NAME, fixture

    from store.account_fixtures import PATIENT

    world = fixture(store, clock, count=6)
    with world.client() as client:
        browser_login(client, world.login_path())
        panel = client.get("/api/patients").json()
        assert len(panel) == 6
        record = client.get("/api/patients/" + world.patient_scope.patient_id).json()
        assert record["display_name"] == LONG_NAME and record["age"] == "63"
        assert len(record["missions"][0]["details"]["slots"]) == 2
        assert len(record["missions"][0]["details"]["readings"]) == 1
        assert record["orders"][0]["current_version"]["structured_instruction"]["drug"] == LONG_DRUG
        assert record["reviews"][0]["review_kind"] == "incident_response"
        assert record["reviews"][0]["last_material_change_version"] == 3
        all_records = [client.get("/api/patients/" + p["patient_id"]).json() for p in panel]
        assert any(r["review_history"] for r in all_records)
        assert any(any(v["state"] == "acknowledged" for v in r["reviews"]) for r in all_records)
    with world.client() as client:
        browser_login(client, world.login_path(PATIENT, id=201))
        plan = client.get("/api/patient/plan").json()
        assert plan["orders"][0]["drug"] == LONG_DRUG
        page = client.get("/pp")
        assert page.status_code == 200 and LONG_DRUG in page.text and "40 mg" in page.text
        assert "browser-order" not in page.text and "browser-history" not in page.text
        assert plan["last_reading"] is None
        assert "browser-history" not in str(plan)
        assert client.post("/api/patient/plan", json={}).status_code == 405


def test_existing_foreign_patient_refused_by_browser_route(dashboard: LoginWorld) -> None:
    from sanad.auth.commands import CreatePatientStub

    world = dashboard
    other = world.approve("40004")
    created = world.claims.create_stub(
        CreatePatientStub(
            command_id="foreign-patient",
            actor=world.actor("40004"),
            display_name="Foreign synthetic chart",
        )
    )
    assert created.status == "accepted"
    ref = next(r for r in created.resulting_versions if r.entity_type == "patient")
    with world.client() as client:
        browser_login(client, world.login_path())
        for path in ("/a/patients/", "/api/patients/"):
            result = client.get(path + ref.id)
            assert result.status_code == 404
            assert other.name not in result.text and "Foreign synthetic chart" not in result.text


def test_inbox_index_includes_unassigned_reviews_without_new_verbs(
    dashboard: LoginWorld,
) -> None:
    from domain_fixtures import review_payload

    from sanad.domain import DRAFT_POLICY_2026_09, ReviewObligation, create_review
    from sanad.store._base import Write
    from sanad.store.records import model_scope, record_item, to_record

    world = dashboard
    value = create_review(
        review_payload(
            event_id="browser-unassigned-review",
            owner_doctor_id=world.doctor.id,
            patient_id=None,
            source_mission_id=None,
            source_type="intake",
            source_id="unassigned-synthetic-intake",
            review_kind="intake_clarification",
        ),
        world.clock(),
        DRAFT_POLICY_2026_09,
    ).aggregate
    assert isinstance(value, ReviewObligation)
    row = to_record(value, model_scope(value))
    assert world.store._atomic([Write(record_item(row), None)], [])
    with world.client() as client:
        browser_login(client, world.login_path())
        result = client.get("/api/browser/reviews")
        assert result.status_code == 200
        assert result.json()[0]["source_id"] == "unassigned-synthetic-intake"
        assert result.json()[0]["patient_id"] is None
        assert client.post("/api/browser/reviews", json={}).status_code == 405


def test_resolved_unassigned_history_uses_owned_scopes(dashboard: LoginWorld) -> None:
    from domain_fixtures import review_payload
    from evidence_cases import read

    from sanad.domain import DRAFT_POLICY_2026_09, ReviewObligation, create_review
    from sanad.store._base import Write
    from sanad.store.records import Doctor, IntakeDraft, model_scope, record_item, to_record

    world = dashboard
    other = world.approve("40004")

    def seed(doctor: Doctor, prefix: str, *, intake: bool) -> str:
        value = create_review(
            review_payload(
                event_id=prefix + "-review",
                owner_doctor_id=doctor.id,
                patient_id=None,
                source_mission_id=None,
                source_type="intake" if intake else "outbound_intent",
                source_id=prefix + "-source",
                review_kind="intake_clarification" if intake else "delivery_failure",
            ),
            world.clock(),
            DRAFT_POLICY_2026_09,
        ).aggregate
        assert isinstance(value, ReviewObligation)
        resolved = ReviewObligation.model_validate(
            value.model_dump()
            | {
                "state": "resolved",
                "work_clock": None,
                "resolved_by": doctor.id,
                "resolved_at": world.clock(),
                "resolved_reason": "Synthetic disposition",
                "resolved_action_event_id": prefix + "-resolved",
            }
        )
        row = to_record(resolved, model_scope(resolved))
        writes = [Write(record_item(row), None)]
        if intake:
            draft = IntakeDraft(
                id=value.source_id,
                scope=doctor.scope,
                owner_doctor_id=doctor.id,
                source_receipt_ids=(prefix + "-receipt",),
                media_work_ids=(),
                reads=read(),
                reader_policy_version="synthetic-reader",
                kind="lab",
                state="rejected",
                review_at=world.clock(),
                work_clock=None,
                review_obligation_id=value.id,
                created_at=world.clock(),
                updated_at=world.clock(),
            )
            writes.append(Write(record_item(to_record(draft, draft.scope)), None))
        assert world.store._atomic(writes, [])
        return value.source_id

    own = {seed(world.doctor, "own-intake", intake=True)}
    own.add(seed(world.doctor, "own-delivery", intake=False))
    seed(other, "foreign-intake", intake=True)
    seed(other, "foreign-delivery", intake=False)
    with world.client() as client:
        browser_login(client, world.login_path())
        assert client.get("/api/browser/reviews").json() == []
        result = client.get("/api/browser/reviews?history=true")
        assert result.status_code == 200
        assert {r["source_id"] for r in result.json()} == own
        assert all(r["state"] == "resolved" and r["patient_id"] is None for r in result.json())
        assert "foreign" not in result.text and "printed_identity_hint" not in result.text
        assert client.post("/api/browser/reviews?history=true", json={}).status_code == 405
