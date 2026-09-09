"""Notice preservation, closed-set model refusals and delivery-bound issuance."""

from datetime import timedelta
from typing import Any, get_args

import pytest
from harness import FakeClock

from sanad.channels.transport import SendOutcome
from sanad.liaison import agent
from sanad.liaison.records import Notice
from sanad.store._base import StoreBase
from sanad.store.records import OutboundIntent, from_record, model_scope
from store.executors_15_fixtures import doctor
from store.test_inbox_17 import offered, setup_review


@pytest.mark.parametrize("reason", get_args(agent.RefusalReason.__value__))
def test_every_typed_refusal_preserves_the_template(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, reason: agent.RefusalReason
) -> None:
    w, _ = setup_review(store, clock)
    monkeypatch.setattr(agent, "bounded_call", lambda *args: agent.Refusal(reason))
    intent, notice, offers = offered(w)
    assert intent.payload and notice.payload["text"] == intent.payload["text"]
    assert notice.refusal == reason and offers


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({}, "empty_proposal"),
        ({"fact_ids": ["foreign"]}, "unknown_fact"),
        (
            {"fact_ids": ["foreign"], "text": "The result is safe; take 5 mg tomorrow"},
            "invalid_shape",
        ),
        ({"fact_ids": [], "dose": 5}, "invalid_shape"),
        ({"fact_ids": ["foreign"], "doctor_id": "other"}, "invalid_shape"),
    ],
)
def test_closed_set_rejects_values_clinical_judgments_and_foreign_facts(
    raw: dict[str, Any], expected: str
) -> None:
    from sanad.liaison.permitted import Fact, Permitted

    result = agent.validate(raw, Permitted("notice", (Fact("owned", "Code sentence."),)))
    assert isinstance(result, agent.Refusal) and result.reason == expected


def test_model_selection_is_only_code_sentences(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, _ = setup_review(store, clock)
    selected: list[str] = []

    def choose(bundle: Any, scope_factory: Any) -> agent.NoticeProposal:
        selected.extend(f.value for f in reversed(bundle.facts))
        return agent.NoticeProposal(fact_ids=tuple(f.id for f in reversed(bundle.facts)))

    monkeypatch.setattr(agent, "bounded_call", choose)
    intent, notice, _ = offered(w)
    assert intent.payload
    assert notice.payload["text"] == str(intent.payload["text"]) + "\n\n" + "\n".join(selected)
    assert notice.refusal is None


@pytest.mark.parametrize("gate", ["output", "kernel"])
def test_each_output_guard_falls_back(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, gate: str
) -> None:
    from types import SimpleNamespace

    from sanad.liaison import decorator

    w, _ = setup_review(store, clock)
    monkeypatch.setattr(
        agent,
        "bounded_call",
        lambda bundle, scope: agent.NoticeProposal(fact_ids=(bundle.facts[0].id,)),
    )
    if gate == "output":
        monkeypatch.setattr(
            decorator, "validate_patient_output", lambda *a, **kw: SimpleNamespace(ok=False)
        )
    else:
        monkeypatch.setattr(
            decorator, "screen_text", lambda *a, **kw: SimpleNamespace(level="danger")
        )
    intent, notice, _ = offered(w)
    assert intent.payload and notice.payload["text"] == intent.payload["text"]
    assert notice.refusal == ("output_validation" if gate == "output" else "safety_kernel")


def test_no_offer_exists_at_transport_or_after_definite_failure(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, r = setup_review(store, clock)
    calls = 0
    original_send = w.transport.send

    def send(*args: Any, **kwargs: Any) -> SendOutcome:
        nonlocal calls
        if "Review inbox" not in str(args):
            return original_send(*args, **kwargs)
        calls += 1
        assert store.list_records(w.doctor.scope, "review_offer")[0] == ()
        assert store.list_records(w.doctor.scope, "liaison_notice")[0] == ()
        return SendOutcome(status="failed", retryable=False, code="blocked")

    monkeypatch.setattr(w.transport, "send", send)
    intent = doctor(w, "/inbox")
    assert calls == 1
    assert store.list_records(w.doctor.scope, "review_offer")[0] == ()
    assert store.list_records(w.doctor.scope, "liaison_notice")[0] == ()
    current = store.get_review(model_scope(r), r.id)
    assert current and current.state == "open" and current.acknowledged_at is None
    assert w.dispatch(intent).status == "failed"


def test_uncertain_delivery_never_acknowledges(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, r = setup_review(store, clock)
    monkeypatch.setattr(w.transport, "send", lambda *a, **kw: SendOutcome(status="uncertain"))
    intent = doctor(w, "/inbox")
    assert w.dispatch(intent).status == "uncertain"
    current = store.get_review(model_scope(r), r.id)
    assert (
        current
        and current.state == "open"
        and current.first_notice_at is None
        and current.acknowledged_at is None
    )


def test_no_second_model_attempt_after_retryable_send_failure(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, _ = setup_review(store, clock)
    count = 0

    def choose(*args: Any) -> agent.Refusal:
        nonlocal count
        count += 1
        return agent.Refusal("provider_error")

    monkeypatch.setattr(agent, "bounded_call", choose)
    original = w.transport.send
    monkeypatch.setattr(
        w.transport,
        "send",
        lambda *a, **kw: SendOutcome(status="failed", retryable=True, code="temporary"),
    )
    intent = doctor(w, "/inbox")
    assert count == 1
    monkeypatch.setattr(w.transport, "send", original)
    clock.advance(timedelta(minutes=2))
    result = w.dispatch(intent)
    assert result.status == "provider_accepted", result
    assert count == 1
    rows, _ = store.list_records(w.doctor.scope, "liaison_notice")
    assert rows and from_record(rows[0], Notice).refusal == "attempt_already_started"


def test_task_keyboard_and_text_survive_final_decorator(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from store.executors_15_fixtures import add

    w, original_review = setup_review(store, clock)
    task = add(w, "TASK", id="task-17", title="Send the clinic form")
    dispatch = w.runtime.dispatcher.dispatch_one
    monkeypatch.setattr(w.runtime.dispatcher, "dispatch_one", lambda *a, **kw: None)
    w.send("done Send the clinic form")
    monkeypatch.setattr(w.runtime.dispatcher, "dispatch_one", dispatch)
    current_task = store.get_mission(w.patient_scope, task.id)
    assert current_task
    task = current_task
    from sanad.domain.entities import review_source_key

    w.seed(
        original_review.model_copy(
            update={
                "id": "task-review-17",
                "source_id": task.id,
                "source_version": task.version,
                "source_mission_id": task.id,
                "unique_source_key": review_source_key(
                    w.doctor.id, "mission", task.id, task.version, original_review.review_kind
                ),
            }
        )
    )
    rows = [
        from_record(r, OutboundIntent)
        for r in w.rows("outbound_intent")
        if r.body.get("notification_purpose") == "DONE:FULFILLMENT"
        and any(ref.id == task.id for ref in from_record(r, OutboundIntent).source_versions)
    ]
    assert rows
    intent = rows[-1]
    assert intent.payload and "reply_markup" in intent.payload
    before = intent.payload
    sent = w.dispatch(intent)
    assert sent.status == "provider_accepted"
    notices, _ = store.list_records(w.doctor.scope, "liaison_notice")
    notice = next(from_record(r, Notice) for r in notices if r.body.get("intent_id") == intent.id)
    assert notice.payload["text"] == before["text"]
    old, new = before["reply_markup"], notice.payload["reply_markup"]
    assert isinstance(old, dict) and isinstance(new, dict)
    old_keys, new_keys = old["inline_keyboard"], new["inline_keyboard"]
    assert isinstance(old_keys, list) and isinstance(new_keys, list)
    assert new_keys[: len(old_keys)] == old_keys


def test_material_change_marker_uses_actual_notice(store: StoreBase, clock: FakeClock) -> None:
    from sanad.auth.service import revise
    from sanad.concierge.inbox import material_changed

    w, r = setup_review(store, clock)
    assert not material_changed(store, r)
    _, _, _ = offered(w)
    changed = revise(r, clock(), last_material_change_version=r.last_material_change_version + 1)
    w.seed(changed)
    assert material_changed(store, changed)


def test_real_sdk_turn_refuses_foreign_fact_with_one_model_call(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from providers.fixtures import ScriptedModel, response

    w, _ = setup_review(store, clock)
    model = ScriptedModel(response('{"fact_ids":["another-notice-fact"]}'))
    monkeypatch.setattr(agent, "model_factory", lambda *args: model)
    intent, notice, _ = offered(w)
    assert len(model.script.calls) == 1
    assert notice.refusal == "unknown_fact"
    assert intent.payload and notice.payload["text"] == intent.payload["text"]


def test_blocking_provider_cannot_hold_the_notice_window(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from time import monotonic, sleep

    from sanad.liaison import policy

    w, _ = setup_review(store, clock)
    calls = 0

    async def blocked(*args: Any) -> agent.Refusal:
        nonlocal calls
        calls += 1
        sleep(0.25)
        return agent.Refusal("provider_error")

    monkeypatch.setattr(agent, "propose", blocked)
    monkeypatch.setattr(policy, "call_timeout_s", 0.02)
    durations = []
    bounded = agent.bounded_call

    def measured(*args: Any) -> agent.NoticeProposal | agent.Refusal:
        started = monotonic()
        result = bounded(*args)
        durations.append(monotonic() - started)
        return result

    monkeypatch.setattr(agent, "bounded_call", measured)
    intent, notice, _ = offered(w)
    assert len(durations) == 1 and durations[0] < 0.2
    assert calls == 1 and notice.refusal == "provider_timeout"
    assert intent.payload and notice.payload["text"] == intent.payload["text"]


def test_danger_excluded_before_the_decorator(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sanad.liaison import decorator
    from sanad.steward.urgent import UrgentService
    from store.contact_fixtures import world as contact_world

    w = contact_world(store, clock)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("DANGER must never enter the notice decorator")

    monkeypatch.setattr(decorator, "decorate", forbidden)
    urgent = UrgentService(w.runtime.steward, patient_template_id="patient_emergency")
    from sanad.domain import ObservationRef
    from sanad.safety import screen_text, to_incident_facts

    verdict = screen_text("severe chest pain", policy=w.runtime.safety_policy)
    assert verdict.level == "danger"
    facts, severity = to_incident_facts(
        verdict,
        source=ObservationRef(observation_id="synthetic-danger-17"),
        policy=w.runtime.safety_policy,
    )
    urgent.raise_incident(
        w.patient_scope, facts.unique_source_key, facts.as_payload(), severity, clock()
    )
    intents = [
        from_record(r, OutboundIntent)
        for r in w.rows("outbound_intent")
        if r.body.get("notification_purpose") == "DANGER"
    ]
    assert intents
    for intent in intents:
        assert w.dispatch(intent).status == "provider_accepted"


@pytest.mark.parametrize("change", ["source", "review", "lease", "authority", "authority_record"])
def test_change_during_model_call_refuses_send(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from sanad.auth.service import revise

    w, review = setup_review(store, clock)
    tenant = w.doctor.scope
    before = len(w.transport.calls)

    def choose(*args: Any) -> agent.Refusal:
        if change == "source":
            source = store.get_mission(w.patient_scope, review.source_id)
            assert source
            w.seed(revise(source, clock()))
        elif change == "review":
            w.seed(revise(review, clock(), last_material_change_version=2))
        elif change == "lease":
            clock.advance(timedelta(minutes=6))
        elif change == "authority_record":
            from sanad.store.records import DoctorAuthority

            row = store.get(tenant, "doctor_authority", tenant.doctor_id)
            assert row
            w.seed(revise(from_record(row, DoctorAuthority), clock(), approved=False, auth_epoch=2))
        else:
            w.seed(revise(w.doctor, clock(), status="suspended", auth_epoch=2))
        return agent.Refusal("provider_error")

    monkeypatch.setattr(agent, "bounded_call", choose)
    from store.account_fixtures import APPLICANT, update

    assert w.post(update(APPLICANT, "/inbox", 3000)).status_code == 200
    assert not any("Review inbox" in str(s.payload) for s in w.transport.calls[before:])
    assert store.list_records(tenant, "review_offer")[0] == ()


def test_notice_issuance_rejects_forged_or_generic_commit(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sanad.liaison import decorator
    from sanad.store.records import CommitRequest

    w, _ = setup_review(store, clock)
    original = decorator.decorate
    inspected = False

    def decorate(*args: Any, **kwargs: Any) -> Any:
        nonlocal inspected
        rendered, request = original(*args, **kwargs)
        if request is None:
            return rendered, request
        inspected = True
        assert decorator.issuance_guards(store, request, clock()) is not None
        assert store.commit(request).status != "accepted"
        for field, value in (
            ("principal", w.actor("10001")),
            ("worker", None),
        ):
            forged: CommitRequest = request.model_copy(
                update={"command": request.command.model_copy(update={field: value})}
            )
            assert decorator.issuance_guards(store, forged, clock()) is None
        offer = next(row for row in request.puts if row.entity_type == "review_offer")
        assert store.commit(request.model_copy(update={"puts": (offer,)})).status != "accepted"
        return rendered, request

    monkeypatch.setattr(decorator, "decorate", decorate)
    offered(w)
    assert inspected


def test_bundle_offers_bind_only_displayed_capped_subset(
    store: StoreBase, clock: FakeClock
) -> None:
    from sanad.contact import bundle
    from sanad.contact.policy import DRAFT_CONTACT_POLICY
    from store.contact_fixtures import world as contact_world
    from store.test_contact_outage_bundle import bundle_intent, deadline

    w = contact_world(store, clock)
    r = deadline(w, clock)
    for n in range(DRAFT_CONTACT_POLICY.bundle_max_lines + 2):
        w.seed(r.model_copy(update={"id": f"bundle-extra-{n:03}"}))
    clock.advance(timedelta(days=7))
    schedule = store.get(w.doctor.scope, "bundle_schedule", w.doctor.id)
    assert schedule
    bundle.wake(w.runtime.steward, schedule)
    intent = bundle_intent(w)
    expected = bundle.eligible(store, w.doctor.scope, clock())
    sent = w.dispatch(intent)
    assert sent.status == "provider_accepted"
    rows, _ = store.list_records(w.doctor.scope, "liaison_notice")
    notice = next(from_record(row, Notice) for row in rows if row.body["intent_id"] == intent.id)
    assert tuple(s.review_ref.id for s in notice.snapshots) == tuple(r.id for r in expected[:3])
    assert len(expected) > DRAFT_CONTACT_POLICY.bundle_max_lines
    assert "بنود تانية" in str(notice.payload["text"])


def test_delayed_inbox_expires_with_its_displayed_snapshot(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sanad.liaison import decorator

    w, _ = setup_review(store, clock)
    dispatch = w.runtime.dispatcher.dispatch_one
    monkeypatch.setattr(w.runtime.dispatcher, "dispatch_one", lambda *a, **kw: None)
    intent = doctor(w, "/inbox")
    monkeypatch.setattr(w.runtime.dispatcher, "dispatch_one", dispatch)
    assert intent.expires_at == intent.review_listing_expires_at
    clock.advance(timedelta(hours=1, seconds=1))
    before = len(w.transport.calls)

    def forbidden(*a: Any, **kw: Any) -> Any:
        raise AssertionError("An expired listing must not reach the model or decorator")

    monkeypatch.setattr(decorator, "decorate", forbidden)
    assert w.dispatch(intent).status == "suppressed"
    assert len(w.transport.calls) == before
    assert store.list_records(w.doctor.scope, "review_offer")[0] == ()


def test_lease_expiry_during_final_authority_reads_prevents_send(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sanad.store import reviews

    w, _ = setup_review(store, clock)
    before = len(w.transport.calls)
    original = reviews.doctor_checks

    def choose(*a: Any, **kw: Any) -> agent.Refusal:
        def delayed(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            clock.advance(timedelta(minutes=6))
            return result

        monkeypatch.setattr(reviews, "doctor_checks", delayed)
        return agent.Refusal("provider_refusal")

    monkeypatch.setattr(agent, "bounded_call", choose)
    doctor(w, "/inbox")
    assert not any("Review inbox" in str(c.payload) for c in w.transport.calls[before:])
    assert store.list_records(w.doctor.scope, "review_offer")[0] == ()
