"""Adversarial authority, CAS, claim and write-set evidence on both backends."""

from datetime import timedelta

import pytest
from harness import FakeClock

from sanad.auth.service import revise
from sanad.domain import Principal, ReviewKind, TenantScope
from sanad.domain.transitions import ALLOWED_REVIEW_ACTIONS
from sanad.liaison.records import ReviewOffer
from sanad.steward.reviews import prepare
from sanad.store._base import Check, StoreBase, Write
from sanad.store.records import from_record, model_scope, to_record
from store.account_fixtures import APPLICANT, AccountWorld
from store.test_inbox_17 import command, offered, setup_review


@pytest.mark.parametrize(
    "kind,action",
    [(k, a) for k, actions in ALLOWED_REVIEW_ACTIONS.items() for a in sorted(actions)],
)
def test_every_permitted_disposition_changes_only_review(
    store: StoreBase, clock: FakeClock, kind: ReviewKind, action: str
) -> None:
    w, r = setup_review(store, clock, kind)
    _, _, offers = offered(w)
    offer = next(o for o in offers if o.action == action)
    source = store.get(w.patient_scope, "mission", r.source_id)
    result = w.runtime.steward.handle(command(w, offer))
    assert result.status == "accepted", result
    current = store.get_review(model_scope(r), r.id)
    assert current and current.state == "resolved" and current.resolved_by == APPLICANT
    assert store.get(w.patient_scope, "mission", r.source_id) == source
    consumed = store.get(w.doctor.scope, "review_offer", offer.id)
    assert consumed and from_record(consumed, ReviewOffer).consumed_at == clock()


@pytest.mark.parametrize(
    "actor_kind",
    ["patient", "unknown", "admin", "system", "foreign", "wrong_subject", "wrong_bot", "old_epoch"],
)
def test_reference_is_never_authority(store: StoreBase, clock: FakeClock, actor_kind: str) -> None:
    w, r = setup_review(store, clock)
    _, _, offers = offered(w)
    cmd = command(w, offers[0])
    if actor_kind == "foreign":
        other = AccountWorld.approve(w, "20003")
        actor = w.actor("20003")
        assert actor.doctor_id == other.id
    elif actor_kind in {"wrong_subject", "wrong_bot", "old_epoch"}:
        field, value = {
            "wrong_subject": ("subject", "44444"),
            "wrong_bot": ("bot_id", "77777"),
            "old_epoch": ("auth_epoch", 900),
        }[actor_kind]
        actor = cmd.principal.model_copy(update={field: value})
    elif actor_kind == "patient":
        from store.account_fixtures import PATIENT

        actor = w.actor(PATIENT)
    elif actor_kind == "unknown":
        actor = Principal(subject="40004", actor_kind="unknown")
    else:
        actor = Principal(
            subject="40004",
            actor_kind="admin" if actor_kind == "admin" else "system",
            verified_roles=frozenset({"admin"}) if actor_kind == "admin" else frozenset(),
        )
    denied = w.runtime.steward.handle(cmd.model_copy(update={"principal": actor}))
    assert denied.status != "accepted"
    assert store.get_review(model_scope(r), r.id) == r


def test_authority_race_is_conditioned_inside_commit(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, r = setup_review(store, clock)
    _, _, offers = offered(w)
    original = store._atomic
    raced = False

    def atomic(writes: list[Write], checks: list[Check]) -> bool:
        nonlocal raced
        if not raced and any(write.item.get("entity_type") == "review_offer" for write in writes):
            raced = True
            doctor = revise(
                w.doctor, clock(), status="suspended", auth_epoch=w.doctor.auth_epoch + 1
            )
            row = to_record(doctor, doctor.scope)
            assert original(
                [
                    Write(
                        __import__("sanad.store.records", fromlist=["record_item"]).record_item(
                            row
                        ),
                        doctor.version - 1,
                    )
                ],
                [],
            )
        return original(writes, checks)

    monkeypatch.setattr(store, "_atomic", atomic)
    result = w.runtime.steward.handle(command(w, offers[0]))
    assert raced and result.status != "accepted"
    assert store.get_review(model_scope(r), r.id) == r
    row = store.get(offers[0].scope, "review_offer", offers[0].id)
    assert row and from_record(row, ReviewOffer).consumed_at is None


def test_store_refuses_hidden_writes_and_claimed_review(store: StoreBase, clock: FakeClock) -> None:
    w, r = setup_review(store, clock)
    _, _, offers = offered(w)
    offer = offers[0]
    lease = store.acquire_patient(w.patient_scope, "review-test", clock(), timedelta(minutes=1))
    assert lease
    cmd = command(w, offer).model_copy(update={"fence": lease})
    request = prepare(store, cmd, clock())
    m = store.get_mission(w.patient_scope, r.source_id)
    assert m
    hidden = to_record(revise(m, clock(), title="Forged mutation"), w.patient_scope)
    bad = request.model_copy(
        update={"puts": (*request.puts, hidden), "expected": (*request.expected, hidden.ref)}
    )
    assert store.commit(bad).status == "forbidden"
    claimed = store.claim_work(
        to_record(r, model_scope(r)).scoped_key(model_scope(r)),
        r.version,
        "other-review-worker",
        clock(),
        timedelta(minutes=1),
    )
    assert claimed
    result = store.commit(request)
    assert result.status != "accepted"
    store.release_patient(lease)


@pytest.mark.parametrize(
    "kind",
    [
        "ConfirmProposal",
        "ExtendMission",
        "AssociateEvidence",
        "SetContactPreference",
        "_Wake",
        "_EvidenceTurn",
        "RestoreCoverage",
        "ApproveDoctor",
    ],
)
def test_patientless_branch_cannot_reach_other_operations(
    store: StoreBase, clock: FakeClock, kind: str
) -> None:
    w, _ = setup_review(store, clock)
    _, _, offers = offered(w)
    cmd = command(w, offers[0]).model_copy(
        update={
            "scope": TenantScope(doctor_id=w.doctor.id),
            "payload": {"type": kind, "patient_id": w.patient_scope.patient_id},
        }
    )
    assert w.runtime.steward.handle(cmd).status == "unsupported"


def test_real_elapsed_time_between_prepare_and_commit(store: StoreBase, clock: FakeClock) -> None:
    w, r = setup_review(store, clock)
    _, _, offers = offered(w)
    lease = store.acquire_patient(w.patient_scope, "review-test", clock(), timedelta(minutes=1))
    assert lease
    cmd = command(w, offers[0]).model_copy(update={"fence": lease})
    request = prepare(store, cmd, clock())
    clock.advance(timedelta(milliseconds=10))
    assert store.commit(request).status == "accepted"
    store.release_patient(lease)
