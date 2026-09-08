"""Contract 14: confirmed synthetic plans, no model factories or external transports."""

from datetime import timedelta
from typing import NoReturn
from uuid import uuid4

from harness import FakeClock

from sanad.auth.service import revise
from sanad.concierge.plan import Snapshot, load
from sanad.domain import MissionKind, Provenance
from sanad.domain.deadlines import ResolvedTiming, resolve_timing
from sanad.scribe.amend import prepare
from sanad.scribe.extract import DictationCandidate
from sanad.scribe.proposal import ItemTiming, Proposal, ScribeCallback, ScribeState
from sanad.store._base import StoreBase
from sanad.store.records import OperationalClock, OutboundIntent, from_record
from store.account_fixtures import PATIENT, update
from store.concierge_fixtures import PatientWorld


def no_model(*args: object, **kwargs: object) -> NoReturn:
    raise AssertionError("Contract 14 must make zero model provider calls")


def world(store: StoreBase, clock: FakeClock) -> PatientWorld:
    clock.now = clock().replace(hour=7)
    w = PatientWorld.create(store, clock)
    assert isinstance(w, PatientWorld)
    patient = w.enroll(medication=False)
    w.seed(revise(w.doctor, clock(), language="en"))
    w.seed(revise(patient, clock(), language="en"))
    w.concierge.model_factory = no_model
    w.scribe.model_factory = no_model
    return w


def snapshot(w: PatientWorld) -> Snapshot:
    result = load(w.store, w.patient_scope, w.clock())
    assert result
    return result


def confirm(
    w: PatientWorld, *orders: dict[str, object], facts: tuple[dict[str, str], ...] = ()
) -> Proposal:
    """Enter at doctor confirmation, below the unrelated extraction/model lane."""
    now, id = w.clock(), uuid4().hex
    candidate = DictationCandidate.model_validate(
        {
            "patient": {"name_as_spoken": "Synthetic Patient"},
            "orders": orders,
            "facts": facts,
        }
    )
    candidate, changes, issues = prepare(w.scribe.repo, w.patient_scope, candidate)
    assert not issues
    timing = resolve_timing(
        MissionKind.MEDICATION, now, w.runtime.steward.policy_provider(w.patient_scope).timing
    )
    assert isinstance(timing, ResolvedTiming)
    nonce = uuid4().hex * 2
    proposal = Proposal(
        id=id,
        scope=w.doctor.scope,
        doctor_id=w.doctor.id,
        language="en",
        selected_patient_id=w.patient_scope.patient_id,
        candidate=candidate,
        amendments=changes,
        timings=tuple(ItemTiming(item=f"order:{i}", resolved=timing) for i in range(len(orders))),
        created_at=now,
        updated_at=now,
        source_receipt_id="synthetic-doctor:" + id,
        source_text="Synthetic doctor-confirmed medication plan",
        source_provenance=(
            Provenance(
                source_observation_id="synthetic-doctor:" + id,
                actor_kind="doctor",
                actor_id=w.owner.subject,
                source_kind="doctor_statement",
                received_at=now,
            ),
        ),
        expires_at=now + timedelta(minutes=30),
        review_at=now + timedelta(minutes=30),
        confirmation_nonce_hash=nonce,
        work_clock=OperationalClock(work_lane="scribe", next_action_at=now),
        base_versions=tuple(r.ref for r in w.rows("care_order_head")),
    )
    token = ScribeCallback(
        id=nonce,
        scope=w.doctor.scope,
        proposal_id=id,
        proposal_version=1,
        actor_subject=w.owner.subject,
        action="confirm",
        expires_at=proposal.expires_at,
        created_at=now,
        updated_at=now,
    )
    old = w.scribe.repo.state(w.doctor.scope)
    state = (
        revise(old, now, pending_proposal_id=id)
        if old
        else ScribeState(
            id="current",
            scope=w.doctor.scope,
            pending_proposal_id=id,
            created_at=now,
            updated_at=now,
        )
    )
    for model in (proposal, token, state):
        w.seed(model)
    result = w.scribe.committer.confirm(proposal, token, w.owner, "synthetic-confirm:" + id)
    assert result.status == "accepted", result
    return proposal


def send(w: PatientWorld, text: str) -> OutboundIntent:
    id = w.next_message
    w.next_message += 1
    w.concierge.model_factory = no_model
    assert w.post(update(PATIENT, text, id)).status_code == 200
    receipt = w.receipt(id)
    assert receipt.state == "completed"
    replies = [
        from_record(r, OutboundIntent)
        for r in w.rows("outbound_intent")
        if "patient-turn:" + receipt.id in from_record(r, OutboundIntent).source_event_ids
    ]
    assert replies
    return replies[-1]
