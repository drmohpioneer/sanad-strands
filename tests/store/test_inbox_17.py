"""Contract 17: real doctor receipt, delivery and conditional review actions."""

from datetime import timedelta

import pytest
from domain_fixtures import review
from harness import FakeClock

from sanad.auth.service import revise
from sanad.domain import ReviewKind, ReviewObligation
from sanad.domain.entities import review_source_key
from sanad.liaison.records import Notice, ReviewOffer
from sanad.store._base import StoreBase
from sanad.store.records import CommandEnvelope, OutboundIntent, from_record, model_scope
from store.account_fixtures import APPLICANT
from store.concierge_fixtures import PatientWorld
from store.executors_15_fixtures import add, doctor, world


def setup_review(
    store: StoreBase, clock: FakeClock, kind: ReviewKind = ReviewKind.result_review
) -> tuple[PatientWorld, ReviewObligation]:
    w = world(store, clock)
    mission = add(w)
    r = review(
        id="review-17",
        owner_doctor_id=w.doctor.id,
        patient_id=w.patient_scope.patient_id,
        source_type="mission",
        source_id=mission.id,
        source_version=mission.version,
        source_mission_id=mission.id,
        review_kind=kind,
        unique_source_key=review_source_key(
            w.doctor.id, "mission", mission.id, mission.version, kind
        ),
        created_at=clock(),
        updated_at=clock(),
    )
    w.seed(r)
    return w, r


def offered(w: PatientWorld) -> tuple[OutboundIntent, Notice, list[ReviewOffer]]:
    intent = doctor(w, "/inbox")
    sent = w.dispatch(intent)
    assert sent and sent.status == "provider_accepted", sent
    rows, _ = w.store.list_records(w.doctor.scope, "liaison_notice")
    assert len(rows) == 1
    notice = from_record(rows[0], Notice)
    rows, _ = w.store.list_records(w.doctor.scope, "review_offer")
    return intent, notice, [from_record(r, ReviewOffer) for r in rows]


def command(
    w: PatientWorld, offer: ReviewOffer, id: str = "action-17", reason: str = "Reviewed the report"
) -> CommandEnvelope:
    return CommandEnvelope(
        command_id=id,
        principal=w.actor(APPLICANT),
        scope=offer.snapshot.scope,
        requested_at=w.clock(),
        payload={
            "type": "AcknowledgeReview" if offer.action == "acknowledge" else "ResolveReview",
            "offer_id": offer.id,
            "expected_source_version": offer.snapshot.source_version,
            **({"reason": reason} if offer.action != "acknowledge" else {}),
        },
    )


def test_inbox_ack_resolve_and_replay(store: StoreBase, clock: FakeClock) -> None:
    w, r = setup_review(store, clock)
    intent, notice, offers = offered(w)
    assert intent.payload and notice.payload["text"] == intent.payload["text"]
    ack = next(o for o in offers if o.action == "acknowledge")
    result = w.runtime.steward.handle(command(w, ack))
    assert result.status == "accepted", result
    assert w.runtime.steward.handle(command(w, ack)).status == "accepted"
    current = store.get_review(model_scope(r), r.id)
    assert current and current.state == "acknowledged" and current.resolved_at is None
    old = next(o for o in offers if o.action == "review")
    assert w.runtime.steward.handle(command(w, old, "old")).status == "stale_version"
    fresh = doctor(w, "/inbox", 3001)
    assert fresh.payload and "Acknowledged; still unresolved" in str(fresh.payload["text"])
    sent = w.dispatch(fresh)
    assert sent and sent.status == "provider_accepted"
    rows, _ = store.list_records(w.doctor.scope, "review_offer")
    selected = next(
        from_record(row, ReviewOffer)
        for row in rows
        if from_record(row, ReviewOffer).snapshot.review_ref.version == current.version
    )
    result = w.runtime.steward.handle(command(w, selected, "resolve"))
    assert result.status == "accepted", result
    resolved = store.get_review(model_scope(r), r.id)
    assert (
        resolved
        and resolved.state == "resolved"
        and resolved.resolved_reason == "Reviewed the report"
    )
    assert resolved.resolved_by == APPLICANT
    assert resolved.resolved_action_event_id
    assert store.get(model_scope(r), "audit_event", resolved.resolved_action_event_id)


@pytest.mark.parametrize("kind", list(ReviewKind))
def test_only_permitted_dispositions_and_explicit_reason(
    store: StoreBase, clock: FakeClock, kind: ReviewKind
) -> None:
    from sanad.domain.transitions import ALLOWED_REVIEW_ACTIONS

    w, r = setup_review(store, clock, kind)
    _, _, offers = offered(w)
    assert {o.action for o in offers} == {"acknowledge", *ALLOWED_REVIEW_ACTIONS[kind]}
    for offer in offers:
        if offer.action != "acknowledge":
            assert w.runtime.steward.handle(command(w, offer, reason="")).status != "accepted"
    selected = next(o for o in offers if o.action != "acknowledge")
    before = w.store.get(w.patient_scope, "mission", r.source_id)
    assert w.runtime.steward.handle(command(w, selected)).status == "accepted"
    assert w.store.get(w.patient_scope, "mission", r.source_id) == before


@pytest.mark.parametrize(
    "change", ["review", "material", "source", "epoch", "suspension", "expiry", "forged_source"]
)
def test_stale_offer_never_refreshed(store: StoreBase, clock: FakeClock, change: str) -> None:
    w, r = setup_review(store, clock)
    _, _, offers = offered(w)
    offer = offers[0]
    cmd = command(w, offer)
    if change == "review":
        w.seed(revise(r, clock(), review_at=r.review_at + timedelta(hours=1)))
    elif change == "material":
        w.seed(revise(r, clock(), last_material_change_version=r.last_material_change_version + 1))
    elif change == "source":
        m = store.get_mission(w.patient_scope, r.source_id)
        assert m
        w.seed(revise(m, clock(), title="Changed request"))
    elif change == "epoch":
        w.seed(revise(w.doctor, clock(), auth_epoch=w.doctor.auth_epoch + 1))
    elif change == "suspension":
        w.seed(revise(w.doctor, clock(), status="suspended"))
    elif change == "expiry":
        clock.advance(timedelta(hours=2))
    else:
        cmd = cmd.model_copy(update={"payload": cmd.payload | {"expected_source_version": 999}})
    result = w.runtime.steward.handle(cmd)
    assert result.status != "accepted", result
    current = store.get_review(model_scope(r), r.id)
    assert current and current.state == "open"


@pytest.mark.parametrize("patientless", ["tenant", "intake"])
@pytest.mark.parametrize("action", ["acknowledge", "dispose"])
def test_exact_patientless_review_branch(
    store: StoreBase, clock: FakeClock, patientless: str, action: str
) -> None:
    from evidence_cases import read

    from sanad.store.records import IntakeDraft

    w, original = setup_review(store, clock)
    # The normal synthetic world owns an actual durable doctor reply source.
    source = doctor(w, "/questions", 2999)
    kind = (
        ReviewKind.delivery_failure if patientless == "tenant" else ReviewKind.intake_clarification
    )
    source_type = "outbound_intent" if patientless == "tenant" else "intake"
    source_id = "tenant-failed" if patientless == "tenant" else "private-intake"
    if patientless == "tenant":
        from sanad.store.records import OutboundIntent

        tenant = w.doctor.scope
        source = OutboundIntent(
            id=source_id,
            scope=tenant,
            scope_kind="doctor",
            audience="doctor",
            logical_key="bundle:" + w.doctor.id + ":1",
            source_event_ids=("synthetic",),
            source_versions=(),
            recipient_ref=w.doctor.private_chat_id,
            notification_purpose="DEADLINE",
            eligibility_class="bundle",
            payload_ref="synthetic",
            payload_digest="synthetic",
            conversation_sequence=0,
            expires_at=clock() + timedelta(days=7),
            created_at=clock(),
            updated_at=clock(),
            recipient_auth_epoch_seen=w.doctor.auth_epoch,
            bot_id=w.doctor.telegram_bot_id,
            template_id="doctor_weekly_bundle",
            delivery_lease_seconds=60,
            status="failed",
            review_obligation_id="review-17",
            work_clock=None,
        )
        w.seed(source)
    else:
        w.seed(
            IntakeDraft(
                id=source_id,
                scope=w.doctor.scope,
                owner_doctor_id=w.doctor.id,
                source_receipt_ids=("synthetic",),
                media_work_ids=(),
                reads=read(),
                reader_policy_version="synthetic",
                kind="other",
                state="rejected",
                review_at=clock(),
                work_clock=None,
                created_at=clock(),
                updated_at=clock(),
            )
        )
    r = review(
        id="patientless-17",
        owner_doctor_id=w.doctor.id,
        patient_id=None,
        source_type=source_type,
        source_id=source_id,
        source_version=1,
        source_mission_id=None,
        review_kind=kind,
        unique_source_key=review_source_key(w.doctor.id, source_type, source_id, 1, kind),
        created_at=clock(),
        updated_at=clock(),
    )
    w.seed(r)
    _, _, offers = offered(w)
    offer = next(o for o in offers if o.snapshot.review_ref.id == r.id and o.action == action)
    before = store.get_review(model_scope(original), original.id)
    result = w.runtime.steward.handle(command(w, offer))
    assert result.status == "accepted", result
    assert store.get_review(model_scope(original), original.id) == before
    changed = store.get_review(model_scope(r), r.id)
    assert changed and changed.state == ("acknowledged" if action == "acknowledge" else "resolved")
    # None of these dispositions associates the intake or recovers a failed bundle.
    if patientless == "intake":
        draft_row = store.get(w.doctor.scope, "intake_draft", source_id)
        assert draft_row and draft_row.body["state"] == "rejected"
    else:
        source_row = store.get(w.doctor.scope, "outbound_intent", source_id)
        assert source_row and source_row.body["status"] == "failed"


def test_200_reviews_paging_order_empty_and_bilingual(store: StoreBase, clock: FakeClock) -> None:
    from sanad.concierge.inbox import listing_text, owned_reviews
    from sanad.domain import ReviewState
    from sanad.presentation.context import PresentationContext

    w, r = setup_review(store, clock)
    for n in range(199):
        w.seed(
            r.model_copy(
                update={
                    "id": f"review-extra-{n:03}",
                    "created_at": clock() - timedelta(hours=n + 1),
                    "review_at": clock() - timedelta(hours=1)
                    if n % 2
                    else clock() + timedelta(hours=1),
                }
            )
        )
    values = owned_reviews(store, w.doctor.id, clock())
    assert len(values) == 200
    flags = [v.review_at > clock() for v in values]
    assert flags == sorted(flags)
    for page in (1, 2, 40):
        intent = doctor(w, f"/inbox {page}", 3000 + page)
        assert len(intent.review_listing) == 5
        assert intent.payload and len(str(intent.payload["text"])) < 4096
    bad = doctor(w, "/inbox 41", 3041)
    assert bad.template_id == "liaison_usage"
    acknowledged = review(
        ReviewState.acknowledged,
        **r.model_dump(exclude={"state", "acknowledged_at", "acknowledged_by"}),
    )
    three = [values[0], acknowledged, r]
    for locale in ("en", "ar"):
        context = PresentationContext(locale=locale, audience="doctor")
        text = listing_text(store, three, context, clock(), 1, 1)
        assert "Synthetic Patient" in text
        assert "acknowledged" in text.lower() if locale == "en" else "لسه محتاجة حسم" in text
        empty = listing_text(store, [], context, clock(), 1, 1)
        assert "no open" in empty if locale == "en" else "مفيش مراجعات" in empty


def test_first_deadline_stamp_and_notice_actions_share_completion(
    store: StoreBase, clock: FakeClock
) -> None:
    from store.contact_fixtures import world as contact_world
    from store.test_contact_outage_bundle import deadline

    w = contact_world(store, clock)
    r = deadline(w, clock)
    rows, _ = store.list_records(w.doctor.scope, "review_offer")
    offer = next(
        from_record(row, ReviewOffer) for row in rows if row.body["action"] == "acknowledge"
    )
    assert offer.snapshot.review_ref.version == r.version
    assert r.first_notice_at == clock()
    first = r.first_notice_at
    result = w.runtime.steward.handle(command(w, offer))
    assert result.status == "accepted", result
    changed = store.get_review(model_scope(r), r.id)
    assert changed and changed.first_notice_at == first and changed.state == "acknowledged"


def test_callback_reason_and_atomic_recovery(store: StoreBase, clock: FakeClock) -> None:
    from harness import SimulatedCrash

    from store.account_fixtures import callback, update

    w, r = setup_review(store, clock)
    _, notice, offers = offered(w)
    offered_review = next(o for o in offers if o.action == "review")
    markup = notice.payload["reply_markup"]
    assert isinstance(markup, dict)
    buttons = markup["inline_keyboard"]
    assert isinstance(buttons, list)
    from sanad.store.keys import digest

    raw = next(
        str(b[0]["callback_data"])
        for b in buttons
        if isinstance(b, list)
        and isinstance(b[0], dict)
        and digest(str(b[0]["callback_data"])) == offered_review.id
    )
    assert w.post(callback(raw, APPLICANT, 4000)).status_code == 200
    current = store.get_review(model_scope(r), r.id)
    assert current and current.state == "open"
    assert "Supply your reason" in str(w.cards()[-1].payload) or any(
        "Supply your reason" in str(i.payload) for i in w.cards()
    )

    def crash(name: str) -> None:
        if name == "doctor_clinical_committed":
            raise SimulatedCrash(name)

    w.scribe.checkpoint = crash
    with pytest.raises((BaseExceptionGroup, SimulatedCrash)) as failure:
        w.post(
            update(APPLICANT, f"/resolve {offered_review.id} Reviewed the original report", 4001)
        )
    if isinstance(failure.value, BaseExceptionGroup):
        assert failure.value.subgroup(SimulatedCrash) is not None
    current = store.get_review(model_scope(r), r.id)
    assert current and current.state == "resolved"
    w.scribe.checkpoint = lambda name: None
    clock.advance(timedelta(minutes=10))
    assert (
        w.post(
            update(APPLICANT, f"/resolve {offered_review.id} Reviewed the original report", 4001)
        ).status_code
        == 200
    )
    assert w.receipt(4001).state == "completed"
    events = [
        row for row in w.rows("audit_event") if row.body.get("event_type") == "REVIEW_RESOLVED"
    ]
    assert len(events) == 1


def test_rendered_acceptance_example(store: StoreBase, clock: FakeClock) -> None:
    from sanad.concierge.inbox import listing_text, owned_reviews
    from sanad.presentation.context import PresentationContext
    from store.account_fixtures import callback

    w, r = setup_review(store, clock)
    for n, kind in enumerate((ReviewKind.media_failure, ReviewKind.coverage_review), 1):
        w.seed(
            r.model_copy(
                update={
                    "id": f"review-example-{n}",
                    "review_kind": kind,
                    "created_at": clock() - timedelta(hours=n),
                    "unique_source_key": review_source_key(
                        w.doctor.id, r.source_type, r.source_id, r.source_version, kind
                    ),
                }
            )
        )
    _, notice, offers = offered(w)
    values = owned_reviews(store, w.doctor.id, clock())
    assert len(values) == 3
    for locale in ("en", "ar"):
        rendered = listing_text(
            store, values, PresentationContext(locale=locale, audience="doctor"), clock(), 1, 1
        )
        print(f"\nEXAMPLE {locale}\n{rendered}")
    ack = next(o for o in offers if o.snapshot.review_ref.id == r.id and o.action == "acknowledge")
    from sanad.store.keys import digest

    markup = notice.payload["reply_markup"]
    assert isinstance(markup, dict) and isinstance(markup["inline_keyboard"], list)
    raw = next(
        str(button[0]["callback_data"])
        for button in markup["inline_keyboard"]
        if isinstance(button, list)
        and isinstance(button[0], dict)
        and digest(str(button[0]["callback_data"])) == ack.id
    )
    assert w.post(callback(raw, APPLICANT, 4700)).status_code == 200
    ack_card = next(i for i in w.cards() if i.template_id == "liaison_ack_done")
    print(f"\nEXAMPLE acknowledge\n{ack_card.payload}")
    stale = next(o for o in offers if o.snapshot.review_ref.id == r.id and o.action == "review")
    rejected = doctor(w, f"/resolve {stale.id} Reviewed the report", 4701)
    assert rejected.template_id == "liaison_stale"
    print(f"\nEXAMPLE stale\n{rejected.payload}")
    direct = next(o for o in offers if o.action == "dispose")
    resolved = doctor(w, f"/resolve {direct.id} Duplicate unreadable copy; original reviewed", 4702)
    assert resolved.template_id == "liaison_resolve_done"
    print(f"\nEXAMPLE resolve\n{resolved.payload}")
