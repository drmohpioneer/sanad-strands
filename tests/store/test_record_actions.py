"""Record decisions use real sessions and conditional commits on both stores."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest
from harness import FakeClock

from sanad.auth.service import revise
from sanad.domain import Mission, ReviewKind, ReviewObligation, ReviewState
from sanad.domain.transitions import ALLOWED_REVIEW_ACTIONS
from sanad.store._base import Check, StoreBase, Write
from sanad.store.keys import IntakeScope
from sanad.store.records import CommitRequest, CommitResult, OutboundIntent, from_record
from store.account_fixtures import APPLICANT
from store.concierge_fixtures import PatientWorld
from store.executors_15_fixtures import add, get, question, world
from store.login_fixtures import ORIGIN, browser_login
from store.test_inbox_17 import setup_review

if TYPE_CHECKING:
    from starlette.testclient import TestClient


def present[T](value: T | None) -> T:
    assert value is not None
    return value


def headers(client: TestClient) -> dict[str, str]:
    return {"Origin": ORIGIN, "X-CSRF-Token": client.cookies["sanad_csrf"]}


def endpoint(w: PatientWorld) -> str:
    return f"/api/patients/{w.patient_scope.patient_id}/actions"


def listing(client: TestClient, w: PatientWorld) -> dict[str, Any]:
    reply = client.get(endpoint(w))
    assert reply.status_code == 200, reply.text
    return cast(dict[str, Any], reply.json())


def body_for(data: dict[str, Any], id: str, action: str, **extra: Any) -> dict[str, Any]:
    item = next(i for i in data["items"] if i["id"] == id)
    body = {
        "item_kind": item["item_kind"],
        "item_id": id,
        "action": action,
        "expected_version": item["version"],
        "command_id": "record-action",
        **extra,
    }
    if item["item_kind"] == "review":
        body.update(
            listing_token=data["listing_token"],
            expected_source_version=item["review"]["source_version"],
        )
    return body


@pytest.mark.parametrize(
    "kind,action", [(k, a) for k, aa in ALLOWED_REVIEW_ACTIONS.items() for a in sorted(aa)]
)
def test_record_dispositions_change_only_review(
    store: StoreBase, clock: FakeClock, kind: ReviewKind, action: str
) -> None:
    w, review = setup_review(store, clock, kind)
    before = w.rows("mission"), w.rows("followup"), w.rows("order")
    with w.client() as client:
        browser_login(client, w.login_path())
        data = listing(client, w)
        body = body_for(data, review.id, "resolve", disposition=action, reason="Reviewed together")
        result = client.post(endpoint(w), headers=headers(client), json=body)
        assert result.status_code == 200, result.text
        assert result.json()["detail"] == "Saved."
        assert client.post(endpoint(w), headers=headers(client), json=body).json() == result.json()
    assert (w.rows("mission"), w.rows("followup"), w.rows("order")) == before
    current = present(store.get_review(w.patient_scope, review.id))
    assert current and current.state == "resolved" and current.resolved_by == APPLICANT
    assert len([r for r in w.rows("audit_event") if r.body["event_type"] == "REVIEW_RESOLVED"]) == 1
    assert not w.rows("review_offer")


def test_record_listing_is_suppressed_session_bound_and_not_question_selection(
    store: StoreBase, clock: FakeClock
) -> None:
    w, review = setup_review(store, clock)
    with w.client() as client:
        browser_login(client, w.login_path())
        data = listing(client, w)
        row = present(
            store.get(
                IntakeScope(doctor_id=w.doctor.id, intake_id="scribe"),
                "outbound_intent",
                data["listing_token"],
            )
        )
        assert row
        intent = from_record(row, OutboundIntent)
        assert intent.status == "suppressed" and intent.suppression_reason == "browser_listing"
        assert intent.template_id == "doctor_questions" and intent.audience == "doctor"
        assert intent.record_listing_session_id and intent.record_listing_subject == APPLICANT
        assert intent.record_listing_auth_epoch == w.doctor.auth_epoch
        assert intent.expires_at == clock() + timedelta(minutes=30)
        assert intent.work_clock is None and intent.question_listing_token is None
        assert not intent.question_listing_targets and not intent.question_bindings
        assert [s.review_ref.id for s in intent.review_listing] == [review.id]
        body = body_for(data, review.id, "acknowledge")
        result = client.post(endpoint(w), headers=headers(client), json=body)
        assert result.status_code == 200, result.text
        assert result.json()["detail"] == "Saved."
        fresh = listing(client, w)
        item = next(i for i in fresh["items"] if i["id"] == review.id)
        assert item["review"]["state"] == "acknowledged"
        assert all(c["action"] != "acknowledge" for c in item["actions"])


@pytest.mark.parametrize("change", ["review", "source", "material", "expiry", "session", "epoch"])
def test_record_review_rejects_changed_snapshot_and_authority(
    store: StoreBase, clock: FakeClock, change: str
) -> None:
    w, review = setup_review(store, clock)
    with w.client() as client:
        browser_login(client, w.login_path())
        data = listing(client, w)
        body = body_for(data, review.id, "acknowledge")
        if change == "review":
            w.seed(revise(review, clock()))
        elif change == "material":
            w.seed(revise(review, clock(), last_material_change_version=2))
        elif change == "source":
            w.seed(revise(present(store.get_mission(w.patient_scope, review.source_id)), clock()))
        elif change == "expiry":
            clock.advance(timedelta(minutes=31))
        elif change == "session":
            browser_login(client, w.login_path(id=8800))
        else:
            w.seed(revise(w.doctor, clock(), auth_epoch=w.doctor.auth_epoch + 1))
        before = w.rows("review")
        result = client.post(endpoint(w), headers=headers(client), json=body)
        assert result.status_code in {401, 403, 409}, result.text
        assert w.rows("review") == before


@pytest.mark.parametrize("action", ["answer", "defer", "close"])
def test_record_question_commands_and_immutable_retry(
    store: StoreBase, clock: FakeClock, action: str
) -> None:
    w = world(store, clock)
    mission = question(w)
    with w.client() as client:
        browser_login(client, w.login_path())
        data = listing(client, w)
        body = body_for(
            data,
            mission.id,
            action,
            **({"text": "Please bring your diary to the clinic."} if action == "answer" else {}),
        )
        reply = client.post(endpoint(w), headers=headers(client), json=body)
        assert reply.status_code == 200, reply.text
        result = reply.json()
        assert result["detail"] in {"Queued for the patient.", "Saved."}
        for row in w.rows("outbound_intent"):
            intent = from_record(row, OutboundIntent)
            if intent.audience == "patient" and intent.status == "queued":
                w.seed(
                    revise(
                        intent,
                        clock(),
                        status="suppressed",
                        suppression_reason="later_change",
                        work_clock=None,
                    )
                )
        assert client.post(endpoint(w), headers=headers(client), json=body).json() == result
        changed = body | {"expected_version": 999}
        stale = client.post(endpoint(w), headers=headers(client), json=changed)
        assert stale.status_code == 409 and stale.json()["status"] == "stale_version"
    current = get(w, mission)
    assert (
        current.state
        == {"defer": mission.state, "answer": "fulfilled", "close": "closed_unfulfilled"}[action]
    )


@pytest.mark.parametrize("action", ["extend", "close_unfulfilled"])
def test_record_missed_mission_commands(store: StoreBase, clock: FakeClock, action: str) -> None:
    w = world(store, clock)
    mission = add(w, due_at=clock() - timedelta(days=1), state="overdue")
    with w.client() as client:
        browser_login(client, w.login_path())
        data = listing(client, w)
        body = body_for(
            data,
            mission.id,
            action,
            reason="Agreed with patient",
            **({"due_at": (clock() + timedelta(days=2)).isoformat()} if action == "extend" else {}),
        )
        result = client.post(endpoint(w), headers=headers(client), json=body)
        assert result.status_code == 200, result.text
        assert client.post(endpoint(w), headers=headers(client), json=body).json() == result.json()
    current = get(w, mission)
    assert current.state == ("open" if action == "extend" else "closed_unfulfilled")
    if action == "extend":
        assert current.due_at == clock() + timedelta(days=2)
        assert current.escalation_at == current.due_at + timedelta(seconds=mission.grace_seconds)


def test_record_complete_pagination_and_cursor_scope(store: StoreBase, clock: FakeClock) -> None:
    w, review = setup_review(store, clock)
    for i in range(230):
        add(w, id=f"request-{i:03}")
    for i in range(40):
        w.seed(
            review.model_copy(
                update={
                    "id": f"history-{i}",
                    "state": ReviewState.resolved,
                    "work_clock": None,
                    "resolved_by": APPLICANT,
                    "resolved_at": clock(),
                    "resolved_reason": "Reviewed",
                    "resolved_action_event_id": "earlier",
                }
            )
        )
    for i in range(30):
        w.seed(review.model_copy(update={"id": f"review-{i:03}"}))
    with w.client() as client:
        browser_login(client, w.login_path())
        ids: list[str] = []
        url: str | None = endpoint(w)
        pages = []
        while url:
            response = client.get(url)
            assert response.status_code == 200, response.text
            data = response.json()
            pages.append(data)
            assert len(data["items"]) <= 25
            ids.extend(i["id"] for i in data["items"])
            url = endpoint(w) + "?cursor=" + data["cursor"] if data["cursor"] else None
        assert len(ids) == len(set(ids)) == 262
        assert all(f"request-{i:03}" in ids for i in range(230))
        assert not any(id.startswith("history-") for id in ids)
        assert client.get(endpoint(w) + "?cursor=forged").status_code == 422
        last = pages[-1]
        selected = next(i for i in last["items"] if i["item_kind"] == "review")
        body = body_for(last, selected["id"], "acknowledge")
        # An unrelated review changing does not invalidate this page's selected snapshot.
        other = present(store.get_review(w.patient_scope, "review-000"))
        w.seed(revise(other, clock()))
        response = client.post(endpoint(w), headers=headers(client), json=body)
        assert response.status_code == 200, response.text


@pytest.mark.parametrize("change", ["session", "source", "review"])
def test_record_commit_race_is_fenced(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from sanad.store._base import Write
    from sanad.store.keys import AccountScope
    from sanad.store.records import WebSession, record_item, to_record

    w, review = setup_review(store, clock)
    with w.client() as client:
        browser_login(client, w.login_path())
        data = listing(client, w)
        body = body_for(data, review.id, "acknowledge")
        intent = present(
            store.get(
                IntakeScope(doctor_id=w.doctor.id, intake_id="scribe"),
                "outbound_intent",
                data["listing_token"],
            )
        )
        original = store._atomic
        raced = False

        def atomic(writes: list[Write], checks: list[Check]) -> bool:
            nonlocal raced
            if not raced and any(x.item.get("entity_type") == "review" for x in writes):
                raced = True
                current: WebSession | Mission | ReviewObligation
                modified: WebSession | Mission | ReviewObligation
                if change == "session":
                    scope = AccountScope(bot_id=w.doctor.telegram_bot_id)
                    session = from_record(
                        present(
                            store.get(
                                scope, "web_session", str(intent.body["record_listing_session_id"])
                            )
                        ),
                        WebSession,
                    )
                    current = session
                    modified = revise(session, clock(), revoked_at=clock())
                elif change == "source":
                    current = present(store.get_mission(w.patient_scope, review.source_id))
                    modified = revise(current, clock(), title="Revised title")
                elif change == "review":
                    current = review
                    modified = revise(review, clock())
                row = to_record(
                    modified, modified.scope if hasattr(modified, "scope") else w.patient_scope
                )
                assert original([Write(record_item(row), current.version)], [])
            return original(writes, checks)

        monkeypatch.setattr(store, "_atomic", atomic)
        result = client.post(endpoint(w), headers=headers(client), json=body)
        assert raced and result.status_code == 409, result.text
        assert present(store.get_review(w.patient_scope, review.id)).state == "open"


@pytest.mark.parametrize("action", ["extend", "answer", "acknowledge"])
def test_record_session_expiry_before_commit(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    w, review = setup_review(store, clock)
    mission = (
        question(w)
        if action == "answer"
        else present(store.get_mission(w.patient_scope, review.source_id))
    )
    with w.client() as client:
        browser_login(client, w.login_path())
        data = listing(client, w)
        body = body_for(
            data,
            review.id if action == "acknowledge" else mission.id,
            action,
            **(
                {"text": "Please bring your diary."}
                if action == "answer"
                else {"reason": "Agreed", "due_at": (clock() + timedelta(days=10)).isoformat()}
                if action == "extend"
                else {}
            ),
        )
        original = store.commit

        def commit(request: CommitRequest) -> CommitResult:
            if request.command.command_id.startswith("web:"):
                clock.advance(timedelta(days=2))
            return original(request)

        monkeypatch.setattr(store, "commit", commit)
        before = get(w, mission)
        result = client.post(endpoint(w), headers=headers(client), json=body)
        assert result.status_code == 409, result.text
        assert get(w, mission) == before
        assert present(store.get_review(w.patient_scope, review.id)).state == "open"


def test_record_review_rejects_hidden_writes_and_payload_fields(
    store: StoreBase, clock: FakeClock
) -> None:
    from sanad.steward.reviews import prepare_listing
    from sanad.store.records import to_record
    from sanad.web.api_actions import ActionBody, make_command

    w, review = setup_review(store, clock)
    with w.client() as client:
        browser_login(client, w.login_path())
        data = listing(client, w)
        row = present(
            store.get(
                IntakeScope(doctor_id=w.doctor.id, intake_id="scribe"),
                "outbound_intent",
                data["listing_token"],
            )
        )
        actor = w.owner.model_copy(update={"session_id": row.body["record_listing_session_id"]})
        body = ActionBody.model_validate(body_for(data, review.id, "acknowledge"))
        cmd = make_command(
            w.runtime.steward, actor, w.patient_scope, body, to_record(review, w.patient_scope)
        )
        lease = store.acquire_patient(w.patient_scope, "record-test", clock(), timedelta(minutes=1))
        assert lease
        cmd = cmd.model_copy(update={"fence": lease})
        request = prepare_listing(store, cmd, clock())
        mission = present(store.get_mission(w.patient_scope, review.source_id))
        hidden = to_record(revise(mission, clock(), title="Forbidden write"), w.patient_scope)
        assert (
            store.commit(
                request.model_copy(
                    update={
                        "puts": (*request.puts, hidden),
                        "expected": (*request.expected, hidden.ref),
                    }
                )
            ).status
            == "forbidden"
        )
        assert (
            store.commit(
                request.model_copy(
                    update={
                        "command": cmd.model_copy(
                            update={"payload": cmd.payload | {"offer_id": "untrusted"}}
                        )
                    }
                )
            ).status
            == "forbidden"
        )
        assert present(store.get_mission(w.patient_scope, mission.id)) == mission
        assert present(store.get_review(w.patient_scope, review.id)) == review
        store.release_patient(lease)


def test_record_partial_overdue_is_reported_and_refused_action_writes_nothing(
    store: StoreBase, clock: FakeClock
) -> None:
    w = world(store, clock)
    mission = add(w, due_at=clock() - timedelta(hours=1))
    with w.client() as client:
        browser_login(client, w.login_path())
        data = listing(client, w)
        cancel = body_for(data, mission.id, "cancel", reason="Not in this surface")
        before = w.rows("mission"), w.rows("review"), w.rows("audit_event")
        assert client.post(endpoint(w), headers=headers(client), json=cancel).status_code == 422
        assert (w.rows("mission"), w.rows("review"), w.rows("audit_event")) == before
        body = body_for(
            data,
            mission.id,
            "extend",
            reason="Too early",
            due_at=(clock() - timedelta(minutes=1)).isoformat(),
        )
        result = client.post(endpoint(w), headers=headers(client), json=body)
        assert result.status_code == 422, result.text
        assert result.json()["side_effects"] == ["mission_overdue"]
        current = get(w, mission)
        assert current.state == "overdue" and current.version == mission.version + 1
        assert current.due_at == mission.due_at
        retry = client.post(endpoint(w), headers=headers(client), json=body)
        assert retry.status_code == 409
        assert get(w, mission) == current


@pytest.mark.parametrize(
    "case", ["danger", "cancelled", "missing_recipient", "answered_review", "held"]
)
def test_record_display_only_and_surviving_reviews(
    store: StoreBase, clock: FakeClock, case: str
) -> None:
    if case in {"danger", "cancelled"}:
        w, review = setup_review(store, clock)
        mission = present(store.get_mission(w.patient_scope, review.source_id))
        w.seed(
            revise(
                mission,
                clock(),
                **(
                    {"danger_history": True}
                    if case == "danger"
                    else {"state": "cancelled", "work_clock": None}
                ),
            )
        )
    else:
        w = world(store, clock)
        mission = question(w)
        review = present(
            store.get_review(
                w.patient_scope,
                next(r.id for r in w.rows("review") if r.body["review_kind"] == "question_answer"),
            )
        )
        if case == "missing_recipient":
            profile = present(store.get_patient_profile(w.patient_scope))
            w.seed(revise(profile, clock(), recipient_ref=None))
        elif case == "answered_review":
            w.seed(
                revise(
                    review,
                    clock(),
                    state="resolved",
                    work_clock=None,
                    resolved_by=APPLICANT,
                    resolved_at=clock(),
                    resolved_reason="Reviewed",
                    resolved_action_event_id="earlier",
                )
            )
        else:
            from store.executors_15_fixtures import doctor

            doctor(w, "/questions")
            doctor(w, "/answer 1 stop my medicine", 8871)
    with w.client() as client:
        browser_login(client, w.login_path())
        data = listing(client, w)
        item = next((i for i in data["items"] if i["id"] == mission.id), None)
        if case == "danger":
            assert item is not None
            assert {a["action"] for a in item["actions"]} == {"extend"}
        elif case == "cancelled":
            assert item is None
            assert next(i for i in data["items"] if i["id"] == review.id)["actions"]
        else:
            assert item and item["actions"] == []


@pytest.mark.parametrize("case", ["held", "suppressed"])
def test_record_outcome_precedence(store: StoreBase, clock: FakeClock, case: str) -> None:
    w = world(store, clock)
    mission = question(w)
    if case == "suppressed":
        patient = present(w.claims.patient(w.doctor.id, w.patient_scope.patient_id))
        w.seed(revise(patient, clock(), contact_status="unreachable"))
    with w.client() as client:
        browser_login(client, w.login_path())
        data = listing(client, w)
        body = body_for(
            data,
            mission.id,
            "answer",
            text="stop my medicine" if case == "held" else "Please bring your diary.",
        )
        result = client.post(endpoint(w), headers=headers(client), json=body)
        assert result.status_code == 200, result.text
        assert result.json()["outcome_label"] == case
        assert result.json()["detail"].startswith(
            "Held for your review." if case == "held" else "Not sent:"
        )
        assert client.post(endpoint(w), headers=headers(client), json=body).json() == result.json()


@pytest.mark.parametrize("action", ["confirm_identity", "reject"])
def test_record_evidence_uses_original_command_and_replays(
    store: StoreBase, clock: FakeClock, action: str
) -> None:
    from sanad.evidence.doctor import command_for
    from sanad.store.records import to_record
    from sanad.web.api_actions import ActionBody, make_command
    from store import evidence_fixtures as f

    w = f.world(store, clock)
    f.mission(w, title="Potassium test")
    f.providers(w, f.lab(printed_name=None), f.lab(printed_name=None))
    assert f.upload(w) == "accepted"
    evidence = f.current(w)
    with w.client() as client:
        browser_login(client, w.login_path())
        data = listing(client, w)
        intent = present(
            store.get(
                IntakeScope(doctor_id=w.doctor.id, intake_id="scribe"),
                "outbound_intent",
                data["listing_token"],
            )
        )
        actor = w.owner.model_copy(
            update={"session_id": str(intent.body["record_listing_session_id"])}
        )
        body = {
            "item_kind": "evidence",
            "item_id": evidence.evidence_id,
            "action": action,
            "expected_version": evidence.version,
            "command_id": "evidence-action",
            **({"reason": "Not this patient"} if action == "reject" else {}),
        }
        actual = make_command(
            w.runtime.steward,
            actor,
            w.patient_scope,
            ActionBody.model_validate(body),
            to_record(evidence, w.patient_scope),
        )
        original = command_for(
            w.runtime.steward,
            actor,
            evidence,
            action,
            actual.command_id,
            mission_id=evidence.mission_id,
            reason="Not this patient" if action == "reject" else None,
        )
        assert actual.payload == original.payload
        result = client.post(endpoint(w), headers=headers(client), json=body)
        assert result.status_code == 200, result.text
        assert client.post(endpoint(w), headers=headers(client), json=body).json() == result.json()
        assert (
            client.post(
                endpoint(w), headers=headers(client), json=body | {"expected_version": 999}
            ).status_code
            == 409
        )
    assert f.current(w).association_state == ("rejected" if action == "reject" else "accepted")


def test_record_access_csrf_scope_and_item_before_dispatch(
    store: StoreBase, clock: FakeClock
) -> None:
    from store.account_fixtures import PATIENT

    w, review = setup_review(store, clock)
    with w.client() as client:
        assert client.get(endpoint(w)).status_code == 401
        assert client.post(endpoint(w), json={}).status_code == 401
        browser_login(client, w.login_path())
        data = listing(client, w)
        body = body_for(data, review.id, "acknowledge")
        before = w.rows("review"), w.rows("mission")
        assert client.get("/api/patients/foreign/actions").status_code == 404
        assert (
            client.post(
                "/api/patients/foreign/actions", headers=headers(client), json=body
            ).status_code
            == 404
        )
        assert (
            client.post(
                endpoint(w), headers=headers(client), json=body | {"item_id": "foreign"}
            ).status_code
            == 404
        )
        assert client.post(endpoint(w), json=body).status_code == 403
        browser_login(client, w.login_path(id=8801))
        assert (
            client.post(
                endpoint(w),
                headers=headers(client) | {"Origin": "https://foreign.invalid"},
                json=body,
            ).status_code
            == 403
        )
        browser_login(client, w.login_path(PATIENT, id=8802))
        patient_headers = headers(client)
        assert client.get(endpoint(w)).status_code == 401
        assert client.post(endpoint(w), headers=patient_headers, json=body).status_code == 401
        assert (w.rows("review"), w.rows("mission")) == before


def test_record_removal_eligibility_seam_preserves_questions_and_reviews(
    store: StoreBase, clock: FakeClock
) -> None:
    from sanad.store.records import to_record
    from sanad.web.api_actions import permitted_actions

    w, review = setup_review(store, clock)
    mission = present(store.get_mission(w.patient_scope, review.source_id))
    q = question(w)
    profile = present(store.get_patient_profile(w.patient_scope))
    w.seed(revise(profile, clock(), recipient_ref=None))
    assert (
        permitted_actions(
            store, w.patient_scope, "mission", to_record(mission, w.patient_scope), removed=True
        )
        == []
    )
    assert permitted_actions(
        store, w.patient_scope, "review", to_record(review, w.patient_scope), removed=True
    )
    assert {
        a["action"]
        for a in permitted_actions(
            store, w.patient_scope, "question", to_record(q, w.patient_scope), removed=True
        )
    } == {"answer", "defer", "close"}


def test_record_missing_source_appearance_is_conditioned(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sanad.domain.entities import review_source_key
    from sanad.store._base import Write
    from sanad.store.records import record_item, to_record

    w, review = setup_review(store, clock)
    original_mission = present(store.get_mission(w.patient_scope, review.source_id))
    review = revise(
        review,
        clock(),
        source_id="new-source",
        source_mission_id="new-source",
        unique_source_key=review_source_key(
            w.doctor.id, "mission", "new-source", review.source_version, review.review_kind
        ),
    )
    w.seed(review)
    with w.client() as client:
        browser_login(client, w.login_path())
        data = listing(client, w)
        body = body_for(data, review.id, "acknowledge")
        original = store._atomic
        raced = False

        def atomic(writes: list[Write], checks: list[Check]) -> bool:
            nonlocal raced
            if not raced and any(x.item.get("entity_type") == "review" for x in writes):
                raced = True
                row = to_record(
                    original_mission.model_copy(update={"id": "new-source"}), w.patient_scope
                )
                assert original([Write(record_item(row), None)], [])
            return original(writes, checks)

        monkeypatch.setattr(store, "_atomic", atomic)
        result = client.post(endpoint(w), headers=headers(client), json=body)
        assert raced and result.status_code == 409, result.text
        assert present(store.get_review(w.patient_scope, review.id)).state == "open"


def test_record_page_two_commit_checks_only_selected_review(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, review = setup_review(store, clock)
    for i in range(30):
        w.seed(review.model_copy(update={"id": f"review-{i:03}"}))
    with w.client() as client:
        browser_login(client, w.login_path())
        first = listing(client, w)
        second = client.get(endpoint(w), params={"cursor": first["cursor"]}).json()
        selected = second["items"][0]
        assert selected["item_kind"] == "review"
        original = store._atomic
        examined = []

        def atomic(writes: list[Write], checks: list[Check]) -> bool:
            if any(x.item.get("entity_type") == "review" for x in writes):
                keys = {x.key for x in writes} | {x.key for x in checks}
                reviews = [k for k in keys if k.sk.startswith("REVIEW#")]
                sources = [k for k in keys if k.sk.startswith("MISSION#")]
                assert len(reviews) == 1 and len(sources) == 1 and len(keys) < 100
                examined.append(reviews[0].sk)
            return original(writes, checks)

        monkeypatch.setattr(store, "_atomic", atomic)
        result = client.post(
            endpoint(w),
            headers=headers(client),
            json=body_for(second, selected["id"], "acknowledge"),
        )
        assert result.status_code == 200, result.text
        assert examined == ["REVIEW#" + selected["id"]]


@pytest.mark.parametrize("boundary", ["listing", "decision"])
@pytest.mark.parametrize("change", ["mutation", "appearance"])
def test_record_source_changes_after_snapshot_validation(
    store: StoreBase,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    change: str,
) -> None:
    from sanad.domain.entities import review_source_key
    from sanad.liaison.records import ReviewSnapshot

    w, review = setup_review(store, clock)
    source = present(store.get_mission(w.patient_scope, review.source_id))
    if change == "appearance":
        review = revise(
            review,
            clock(),
            source_id="new-source",
            source_mission_id="new-source",
            unique_source_key=review_source_key(
                w.doctor.id, "mission", "new-source", review.source_version, review.review_kind
            ),
        )
        w.seed(review)
    with w.client() as client:
        browser_login(client, w.login_path())
        data = listing(client, w)
        before = w.rows("review"), w.rows("audit_event")
        intake = IntakeScope(doctor_id=w.doctor.id, intake_id="scribe")
        listings_before = store.list_records(intake, "outbound_intent")
        active = False
        raced = False
        original_equal = ReviewSnapshot.__eq__

        def equal(left: ReviewSnapshot, right: object) -> bool:
            nonlocal raced
            result = original_equal(left, right)
            if active and not raced and left.review_ref.id == review.id:
                assert result
                raced = True
                w.seed(
                    revise(source, clock())
                    if change == "mutation"
                    else source.model_copy(update={"id": "new-source"})
                )
            return result

        monkeypatch.setattr(ReviewSnapshot, "__eq__", equal)
        if boundary == "listing":
            original_save = store.save_record_listing

            def save(*args: Any, **kwargs: Any) -> Any:
                nonlocal active
                active = True
                try:
                    return original_save(*args, **kwargs)
                finally:
                    active = False

            monkeypatch.setattr(store, "save_record_listing", save)
            result = client.get(endpoint(w))
        else:
            original_commit = store.commit

            def commit(request: CommitRequest) -> CommitResult:
                nonlocal active
                active = True
                try:
                    return original_commit(request)
                finally:
                    active = False

            monkeypatch.setattr(store, "commit", commit)
            result = client.post(
                endpoint(w),
                headers=headers(client),
                json=body_for(data, review.id, "acknowledge"),
            )
        assert raced and result.status_code == 409, result.text
        assert (w.rows("review"), w.rows("audit_event")) == before
        assert store.list_records(intake, "outbound_intent") == listings_before
        changed = present(store.get_mission(w.patient_scope, review.source_id))
        assert changed.version == (source.version + 1 if change == "mutation" else 1)


def test_record_identical_mission_retry_after_accepted_grace_change(
    store: StoreBase,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sanad.web import api_actions
    from sanad.web.api_actions import ActionBody, make_command

    w = world(store, clock)
    mission = add(w, due_at=clock() - timedelta(days=1), state="overdue")
    with w.client() as client:
        browser_login(client, w.login_path())
        body = body_for(
            listing(client, w),
            mission.id,
            "extend",
            reason="Agreed with patient",
            due_at=(clock() + timedelta(days=2)).isoformat(),
        )
        first = client.post(endpoint(w), headers=headers(client), json=body)
        assert first.status_code == 200, first.text
        current = get(w, mission)
        amendment = make_command(
            w.runtime.steward,
            w.owner,
            w.patient_scope,
            ActionBody.model_validate(
                body
                | {
                    "command_id": "grace-amendment",
                    "expected_version": current.version,
                    "due_at": (clock() + timedelta(days=3)).isoformat(),
                }
            ),
            present(store.get(w.patient_scope, "mission", mission.id)),
        )
        amendment = amendment.model_copy(
            update={"payload": amendment.payload | {"grace_seconds": current.grace_seconds + 3600}}
        )
        accepted = w.runtime.steward.handle(amendment)
        assert accepted.status == "accepted", accepted
        assert get(w, mission).grace_seconds == current.grace_seconds + 3600
        before = w.rows("mission"), w.rows("audit_event"), w.rows("outbound_intent")

        def no_rebuild(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("An identical retry must not reconstruct an execution payload")

        monkeypatch.setattr(api_actions, "make_command", no_rebuild)
        retry = client.post(endpoint(w), headers=headers(client), json=body)
        assert retry.status_code == 200 and retry.json() == first.json()
        for change in ({"reason": "Another reason"}, {"expected_version": 999}):
            collision = client.post(endpoint(w), headers=headers(client), json=body | change)
            assert collision.status_code == 409, collision.text
        assert (w.rows("mission"), w.rows("audit_event"), w.rows("outbound_intent")) == before


@pytest.mark.parametrize("change", ["csrf", "patient", "session", "epoch"])
def test_record_committed_replay_keeps_authority_checks(
    store: StoreBase,
    clock: FakeClock,
    change: str,
) -> None:
    w, review = setup_review(store, clock)
    with w.client() as client:
        browser_login(client, w.login_path())
        body = body_for(listing(client, w), review.id, "acknowledge")
        first = client.post(endpoint(w), headers=headers(client), json=body)
        assert first.status_code == 200, first.text
        target, request_headers = endpoint(w), headers(client)
        if change == "csrf":
            request_headers = {"Origin": ORIGIN}
        elif change == "patient":
            target = "/api/patients/foreign/actions"
        elif change == "session":
            browser_login(client, w.login_path(id=8800))
            request_headers = headers(client)
        else:
            w.seed(revise(w.doctor, clock(), auth_epoch=w.doctor.auth_epoch + 1))
        before = w.rows("review"), w.rows("audit_event")
        refused = client.post(target, headers=request_headers, json=body)
        assert refused.status_code in {401, 403, 404, 409}, refused.text
        assert (w.rows("review"), w.rows("audit_event")) == before


@pytest.mark.parametrize("change", ["expiry", "revocation"])
def test_record_replay_rechecks_session_after_route_authentication(
    store: StoreBase,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    from sanad.domain import PatientScope
    from sanad.store.records import WebSession

    w, review = setup_review(store, clock)
    with w.client() as client:
        browser_login(client, w.login_path())
        body = body_for(listing(client, w), review.id, "acknowledge")
        first = client.post(endpoint(w), headers=headers(client), json=body)
        assert first.status_code == 200, first.text
        before = w.rows("review"), w.rows("audit_event")
        original = store.lookup_record_action
        raced = False

        def lookup(
            session: WebSession, scope: PatientScope, command_id: str
        ) -> CommitResult | None:
            nonlocal raced
            assert not raced
            raced = True
            if change == "expiry":
                clock.advance(session.idle_expires_at - clock())
            else:
                w.seed(revise(session, clock(), revoked_at=clock()))
            return original(session, scope, command_id)

        monkeypatch.setattr(store, "lookup_record_action", lookup)
        result = client.post(endpoint(w), headers=headers(client), json=body)
        assert raced and result.status_code == 409, result.text
        assert (w.rows("review"), w.rows("audit_event")) == before


def seed_removal_question(w: PatientWorld) -> Mission:
    """Seed an existing question without ever linking this synthetic patient."""
    from domain_fixtures import mission, review

    from sanad.domain import DoctorAnswerPredicate, ObservationRef
    from sanad.domain.entities import QuestionDetails, review_source_key

    q = mission(
        id="removal-question",
        doctor_id=w.doctor.id,
        patient_id=w.patient_scope.patient_id,
        kind="QUESTION",
        objective_predicate=DoctorAnswerPredicate(),
        order_refs=(),
        details=QuestionDetails(
            question_text="May I bring my diary?",
            source_observation_ref=ObservationRef(observation_id="synthetic-question"),
        ),
    )
    r = review(
        id="removal-question-review",
        owner_doctor_id=w.doctor.id,
        patient_id=w.patient_scope.patient_id,
        source_type="mission",
        source_id=q.id,
        source_mission_id=q.id,
        source_version=q.version,
        review_kind=ReviewKind.question_answer,
        unique_source_key=review_source_key(
            w.doctor.id, "mission", q.id, q.version, ReviewKind.question_answer
        ),
        created_at=w.clock(),
        updated_at=w.clock(),
    )
    w.seed(q)
    w.seed(r)
    return q


def remove_from_record(client: TestClient, w: PatientWorld) -> None:
    """Exercise the accepted HTTP -> RemovePatient -> atomic service path."""
    url = endpoint(w).removesuffix("/actions")
    record = client.get(url).json()
    result = client.post(
        url + "/remove",
        headers=headers(client),
        json={
            "command_id": "integrated-removal",
            "expected_version": record["profile_version"],
            "name": record["display_name"],
        },
    )
    assert result.status_code == 200, result.text
    assert present(w.store.get_patient_profile(w.patient_scope)).removed_at == w.clock()
    assert len([r for r in w.rows("audit_event") if r.body["event_type"] == "PatientRemoved"]) == 1


def test_record_actual_removal_refuses_missions_and_retains_review_question_actions(
    store: StoreBase, clock: FakeClock
) -> None:
    w, review = setup_review(store, clock)
    q = question(w)
    mission = add(w, id="removed-overdue", state="overdue", due_at=clock() - timedelta(days=1))
    with w.client() as client:
        browser_login(client, w.login_path())
        before = listing(client, w)
        stale_extend = body_for(
            before,
            mission.id,
            "extend",
            reason="Agreed",
            due_at=(clock() + timedelta(days=2)).isoformat(),
        )
        remove_from_record(client, w)
        data = listing(client, w)
        items = {item["id"]: item for item in data["items"]}
        assert items[mission.id]["actions"] == []
        assert {a["action"] for a in items[q.id]["actions"]} == {"answer", "defer", "close"}
        assert {a["action"] for a in items[review.id]["actions"]} == {"acknowledge", "resolve"}
        before_rows = w.rows("mission"), w.rows("review"), w.rows("audit_event")
        # Both a pre-removal tab and fresh forged actions are refused before mutation.
        bodies = [stale_extend, body_for(data, mission.id, "close_unfulfilled", reason="Agreed")]
        for body in bodies:
            result = client.post(endpoint(w), headers=headers(client), json=body)
            assert result.status_code == 422, result.text
            assert (w.rows("mission"), w.rows("review"), w.rows("audit_event")) == before_rows
        cancel = client.post(
            endpoint(w),
            headers=headers(client),
            json=body_for(data, mission.id, "cancel", reason="Agreed"),
        )
        assert cancel.status_code == 422
        assert (w.rows("mission"), w.rows("review"), w.rows("audit_event")) == before_rows
        for item_id, action in [(review.id, "acknowledge"), (q.id, "defer")]:
            body = body_for(data, item_id, action)
            result = client.post(endpoint(w), headers=headers(client), json=body)
            assert result.status_code == 200, result.text
            if action == "defer":
                # Defer hydrates the revoked binding as awaiting_link.
                # Capture the accepted result before checking its immutable replay.
                q = get(w, q)
                assert q.state == "awaiting_link"
            committed_rows = w.rows("mission"), w.rows("review"), w.rows("audit_event")
            assert (
                client.post(endpoint(w), headers=headers(client), json=body).json() == result.json()
            )
            assert (w.rows("mission"), w.rows("review"), w.rows("audit_event")) == committed_rows
        assert present(store.get_review(w.patient_scope, review.id)).state == "acknowledged"
        assert get(w, mission) == mission
        assert get(w, q).state == q.state


def test_record_never_linked_removed_answer_persists_and_replays_suppression(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sanad.web import api_actions

    w = PatientWorld.create(store, clock)
    assert isinstance(w, PatientWorld)
    w.approve()
    patient = w.stub()
    w.patient_scope = patient.scope
    assert patient.active_binding_id is None
    assert w.profile.recipient_ref is None
    assert not w.rows("patient_binding")
    q = seed_removal_question(w)
    with w.client() as client:
        browser_login(client, w.login_path())
        remove_from_record(client, w)
        data = listing(client, w)
        body = body_for(data, q.id, "answer", text="Bring your diary.")
        result = client.post(endpoint(w), headers=headers(client), json=body)
        assert result.status_code == 200, result.text
        outcome = result.json()
        assert outcome["outcome_label"] == "suppressed"
        assert outcome["outcome_reason"] == "patient_removed"
        assert outcome["detail"] == "Not sent: this patient was removed."
        assert not w.patient_intents()
        assert get(w, q).state == "fulfilled"
        before = (
            w.rows("mission"),
            w.rows("review"),
            w.rows("audit_event"),
            w.rows("outbound_intent"),
        )

        def no_reconstruction(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("Replay rebuilt the command instead of reading its saved outcome")

        monkeypatch.setattr(api_actions, "make_command", no_reconstruction)
        # A new client still carrying the same authenticated session reads persisted outcome.
        with w.client() as retry:
            retry.cookies.update(client.cookies)
            replay = retry.post(endpoint(w), headers=headers(retry), json=body)
        assert replay.status_code == 200, replay.text
        assert replay.json() == outcome
        assert (
            w.rows("mission"),
            w.rows("review"),
            w.rows("audit_event"),
            w.rows("outbound_intent"),
        ) == before


def test_record_actual_removal_retains_evidence_and_correction_actions(
    store: StoreBase, clock: FakeClock
) -> None:
    from sanad.store.records import to_record
    from store import evidence_fixtures as f

    w = f.world(store, clock)
    f.mission(w, order_refs=())
    f.providers(w, f.lab(printed_name=None), f.lab(printed_name=None))
    assert f.upload(w) == "accepted"
    old = f.current(w)
    with w.client() as client:
        browser_login(client, w.login_path())
        remove_from_record(client, w)
        evidence_body = {
            "item_kind": "evidence",
            "item_id": old.evidence_id,
            "action": "confirm_identity",
            "expected_version": old.version,
            "command_id": "removed-evidence",
        }
        result = client.post(endpoint(w), headers=headers(client), json=evidence_body)
        assert result.status_code == 200, result.text
        assert (
            client.post(endpoint(w), headers=headers(client), json=evidence_body).json()
            == result.json()
        )
        current = f.current(w)
        url = endpoint(w).removesuffix("/actions")
        record = client.get(url).json()
        correction = {
            "command_id": "removed-correction",
            "expected_binding_epoch": record["correction_authority"]["binding_epoch"],
            "expected_delivery_epoch": record["correction_authority"]["delivery_epoch"],
            "action": {
                "type": "CorrectRecord",
                "predecessor": to_record(current, current.scope).ref.model_dump(),
                "operation": "replace",
                "row_index": 0,
                "changes": {"value": "4.8"},
                "reason": "Doctor checked source",
            },
        }
        result = client.post(url + "/corrections", headers=headers(client), json=correction)
        assert result.status_code == 200, result.text
        assert (
            client.post(url + "/corrections", headers=headers(client), json=correction).json()
            == result.json()
        )
        assert len(client.get(url).json()["corrections"]) == 1
        assert store.get(old.scope, "evidence", old.id) == to_record(old, old.scope)
