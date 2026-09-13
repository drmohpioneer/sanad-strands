"""Doctor callbacks bind the actual evidence, identity gate, permitted actions and refusals."""

from datetime import timedelta

import pytest
from harness import FakeClock
from providers.fixtures import document, png
from store import evidence_fixtures as f
from store.account_fixtures import update
from store.concierge_fixtures import PatientWorld
from store.test_evidence_identity import buttons, current, language

from sanad.domain import EvidencePredicate, TaskDetails
from sanad.evidence.doctor import decide, owned
from sanad.store._base import StoreBase
from sanad.store.records import Evidence, OutboundIntent, from_record


def listing(w: PatientWorld, e: Evidence, n: int) -> dict[str, str]:
    prior = {i.id for i in w.rows("outbound_intent")}
    assert w.post(update(w.owner.subject, "/inbox", n)).status_code == 200
    card = next(
        from_record(r, OutboundIntent)
        for r in w.rows("outbound_intent")
        if r.id not in prior
        and r.body.get("template_id") == "doctor_evidence_card"
        and any(ref.id == e.evidence_id for ref in from_record(r, OutboundIntent).source_versions)
    )
    return {b["text"]: b["callback_data"] for row in buttons(card) for b in row}


def tap(w: PatientWorld, raw: str, n: int) -> str:
    prior = {i.id for i in w.cards()}
    w.tap(raw=raw, id=n)
    return " ".join(str(i.payload) for i in w.cards() if i.id not in prior)


def mismatch(w: PatientWorld) -> Evidence:
    f.mission(w, title="CBC, K")
    reply = f.lab(printed_name="Different Person")
    f.providers(w, reply, reply)
    f.upload(w)
    e = f.current(w)
    assert "identity_mismatch" in e.flags and e.association_state == "candidate"
    return e


def test_confirm_then_associate_and_consumed_replay(store: StoreBase, clock: FakeClock) -> None:
    w = f.world(store, clock)
    language(w, "en")
    e = mismatch(w)
    menu = listing(w, e, 8100)
    assert set(menu) == {"Confirm it is this patient", "Not this patient ❌"}
    assert "recorded" in tap(w, menu["Confirm it is this patient"], 8101)
    confirmed = current(w, e.evidence_id)
    assert "identity_confirmed" in confirmed.flags and confirmed.association_state == "candidate"
    assert w.rows("mission")[0].body["state"] == "open"
    menu = listing(w, confirmed, 8102)
    assert set(menu) == {"Associate: CBC, K", "Reject"}
    assert "recorded" in tap(w, menu["Associate: CBC, K"], 8103)
    accepted = current(w, e.evidence_id)
    assert accepted.association_state == "accepted"
    assert w.rows("mission")[0].body["state"] == "fulfilled"
    assert "already handled" in tap(w, menu["Associate: CBC, K"], 8104)
    assert current(w, e.evidence_id).version == accepted.version


def test_task_accept_callback_targets_second_document(store: StoreBase, clock: FakeClock) -> None:
    w = f.world(store, clock)
    language(w, "en")
    old = mismatch(w)
    f.mission(
        w,
        id="proof",
        kind="TASK",
        title="Requested proof",
        details=TaskDetails(
            category="proof", instruction="Send a document", completion_rule="doctor_acceptance"
        ),
        objective_predicate=EvidencePredicate(evaluator="task_evidence"),
    )
    reply = document(
        document_type="other", printed_name="Synthetic Patient", items=[{"name": "Requested proof"}]
    )
    f.providers(w, reply, reply, data=png(20, 20))
    f.upload(w, id=1101)
    e = next(v for v in owned(w.store, w.doctor.id) if v.evidence_id != old.evidence_id)
    menu = listing(w, e, 8110)
    assert "Accept" in menu
    assert "recorded" in tap(w, menu["Accept"], 8111)
    accepted = current(w, e.evidence_id)
    assert accepted.association_state == "accepted" and "doctor_accepted" in accepted.flags
    assert current(w, old.evidence_id).version == old.version
    assert "already handled" in tap(w, menu["Accept"], 8112)
    assert current(w, e.evidence_id).version == accepted.version


@pytest.mark.parametrize(
    "cause, words",
    [
        ("identity", "Identity is not confirmed"),
        ("closed", "This request is closed"),
        ("handled", "already handled"),
        ("changed", "changed since listing"),
    ],
)
def test_callback_refusal_words(
    store: StoreBase, clock: FakeClock, cause: str, words: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = f.world(store, clock)
    language(w, "en")
    e = mismatch(w)
    if cause == "identity":
        # Recreate a legacy Associate offer, then exercise current command validation.
        with monkeypatch.context() as legacy:
            legacy.setattr("sanad.evidence.associate.identity_required", lambda value: False)
            menu = listing(w, e, 8120)
    else:
        assert (
            decide(w.runtime.steward, w.owner, e, "confirm_identity", "identity").status
            == "accepted"
        )
        e = current(w, e.evidence_id)
        menu = listing(w, e, 8120)
    raw = menu["Associate: CBC, K"]
    if cause == "closed":
        mission = w.rows("mission")[0]
        from sanad.domain import Mission, MissionState

        w.seed(
            from_record(mission, Mission).model_copy(
                update={
                    "state": MissionState.cancelled,
                    "work_clock": None,
                    "version": mission.version + 1,
                }
            )
        )
    elif cause == "changed":
        clock.advance(timedelta(days=2))
    elif cause == "handled":
        assert "recorded" in tap(w, raw, 8121)
    assert words in tap(w, raw, 8122)
