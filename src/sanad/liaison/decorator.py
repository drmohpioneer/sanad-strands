"""Final normal doctor notice decoration and authenticated offer issuance."""

from copy import deepcopy
from datetime import datetime
from secrets import token_urlsafe
from typing import TYPE_CHECKING

from pydantic import JsonValue

from sanad.agents.tools import AgentScope
from sanad.concierge.policy import DRAFT_CONCIERGE_POLICY
from sanad.domain import Principal, Provenance, ReviewObligation, TenantScope, VersionRef
from sanad.domain.transitions import ALLOWED_REVIEW_ACTIONS
from sanad.liaison import agent, permitted, policy, templates
from sanad.liaison.records import Notice, ReviewOffer, ReviewSnapshot
from sanad.liaison.snapshot import snapshot, source_row
from sanad.presentation.context import resolve
from sanad.safety import screen_text, validate_patient_output
from sanad.safety.models import OutputContext
from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT as SAFETY
from sanad.store import keys
from sanad.store.keys import AccountScope
from sanad.store.records import (
    CommandEnvelope,
    CommitRequest,
    Doctor,
    OutboundIntent,
    WorkerCapability,
    from_record,
    model_scope,
    to_record,
)

if TYPE_CHECKING:
    from sanad.store._base import Check, StoreBase
    from sanad.store.protocol import Store
    from sanad.store.records import DeliveryResolution, StoredRecord


class NoticeChanged(ValueError):
    pass


def displayed(
    store: "Store", intent: OutboundIntent, now: datetime, basis: tuple[VersionRef, ...] = ()
) -> tuple[ReviewSnapshot, ...]:
    if intent.template_id == "liaison_inbox":
        return intent.review_listing
    if intent.scope_kind == "doctor":
        from sanad.contact.bundle import eligible
        from sanad.contact.policy import DRAFT_CONTACT_POLICY

        assert type(intent.scope) is TenantScope
        reviews = eligible(store, intent.scope, now)
        if tuple(to_record(r, model_scope(r)).ref for r in reviews) != basis:
            raise NoticeChanged("bundle_changed")
        return tuple(
            snapshot(store, r)
            for r in reviews[: min(DRAFT_CONTACT_POLICY.bundle_max_lines, policy.max_named_items)]
        )
    if intent.review_obligation_id:
        r = store.get(intent.scope, "review", intent.review_obligation_id)
        return (snapshot(store, from_record(r, ReviewObligation)),) if r else ()
    # DONE sources may own an independent result review without an intent link.
    if intent.notification_purpose in {"DONE:FULFILLMENT", "DEADLINE"}:
        from sanad.steward.types import records

        refs = {(r.entity_type, r.id) for r in intent.source_versions}
        return tuple(
            snapshot(store, from_record(r, ReviewObligation))
            for r in records(store, intent.scope, "review")
            if r.body.get("state") != "resolved"
            and (r.body.get("source_type"), r.body.get("source_id")) in refs
        )[: policy.max_named_items]
    return ()


def reviews_for(
    store: "Store", snapshots: tuple[ReviewSnapshot, ...]
) -> tuple[ReviewObligation, ...]:
    result = []
    for s in snapshots:
        row = store.get(s.scope, "review", s.review_ref.id)
        if not row:
            raise NoticeChanged("review_missing")
        review = from_record(row, ReviewObligation)
        if snapshot(store, review) != s or review.state == "resolved":
            raise NoticeChanged("review_or_source_changed")
        result.append(review)
    return tuple(result)


def decorate(
    store: "Store",
    intent: OutboundIntent,
    payload: dict[str, JsonValue],
    now: datetime,
    basis: tuple[VersionRef, ...] = (),
) -> tuple[dict[str, JsonValue], CommitRequest | None]:
    if (
        intent.audience != "doctor"
        or intent.notification_purpose == "DANGER"
        or intent.template_id == "scribe_photo_column"
        or isinstance(intent.scope, AccountScope)
    ):
        return payload, None
    from sanad.evidence.correction_doctor import correction_keyboard

    original_payload = payload
    payload = correction_keyboard(intent, payload)
    snapshots = displayed(store, intent, now, basis)
    if not snapshots:
        return payload, None
    reviews = reviews_for(store, snapshots)
    tenant = TenantScope(doctor_id=intent.scope.doctor_id)
    d = store.get(tenant, "doctor", tenant.doctor_id)
    if not d:
        return payload, None  # Legacy fixtures without a registered doctor retain their template.
    doctor = from_record(d, Doctor)
    context = resolve(doctor.language, "doctor")
    notice_id = keys.digest("notice:" + str(intent.active_attempt_id))
    existing = store.get(tenant, "liaison_notice", notice_id)
    if existing:
        return from_record(existing, Notice).payload, None
    actor = Principal(subject="steward:notice-decorator", actor_kind="system", doctor_id=doctor.id)
    worker = WorkerCapability(
        service_subject=actor.subject,
        resolved_scope=tenant,
        permitted_lanes=frozenset({"delivery"}),
        auth_expiry=intent.delivery_claim.expires_at if intent.delivery_claim else now,
        invocation_id=notice_id,
    )
    bundle = permitted.compute(notice_id, reviews, context)

    def fresh() -> bool:
        row = store.get(intent.scope, "outbound_intent", intent.id)
        return bool(row and row.version == intent.version and reviews_for(store, snapshots))

    from sanad.liaison.attempt import start

    remaining = min(intent.expires_at, worker.auth_expiry) - now
    can_call = remaining.total_seconds() > policy.call_timeout_s
    started = start(store, intent, actor, worker, now) if can_call else False
    choice = (
        agent.bounded_call(
            bundle,
            lambda alive: AgentScope(
                principal=actor,
                scope=tenant,
                source=Provenance(
                    source_observation_id=notice_id,
                    actor_kind="system",
                    actor_id=actor.subject,
                    source_kind="doctor_statement",
                    received_at=now,
                ),
                policy=SAFETY,
                authority_check=lambda: alive() and fresh(),
                worker=worker,
            ),
        )
        if started
        else agent.Refusal("attempt_already_started" if can_call else "notice_window_short")
    )
    extra = ""
    refusal = choice.reason if isinstance(choice, agent.Refusal) else None
    if isinstance(choice, agent.NoticeProposal):
        checked = agent.validate(choice.model_dump(), bundle)
        if isinstance(checked, agent.Refusal):
            refusal = checked.reason
        else:
            facts = {f.id: f.value for f in bundle.facts}
            sentences = [facts[id] for id in checked.fact_ids]
            if any(
                not validate_patient_output(
                    s,
                    context=OutputContext(mode="plan_explanation", language=context.locale),
                    policy=SAFETY,
                ).ok
                for s in sentences
            ):
                refusal = "output_validation"
            elif any(screen_text(s, policy=SAFETY).level != "none" for s in sentences):
                refusal = "safety_kernel"
            else:
                extra = "\n\n" + "\n".join(sentences)
    rendered = deepcopy(payload)
    rendered["text"] = str(rendered.get("text", "")) + extra
    markup = deepcopy(rendered.get("reply_markup", {}))
    if not isinstance(markup, dict) or not isinstance(markup.get("inline_keyboard", []), list):
        raise NoticeChanged("unsupported_keyboard")
    keyboard = markup.setdefault("inline_keyboard", [])
    assert isinstance(keyboard, list)
    offers = []
    expiry = min(
        intent.expires_at,
        intent.review_listing_expires_at or now + DRAFT_CONCIERGE_POLICY.question_list_ttl,
    )
    for n, (s, review) in enumerate(zip(snapshots, reviews, strict=True), 1):
        for action in (("acknowledge",) if review.state == "open" else ()) + tuple(
            sorted(ALLOWED_REVIEW_ACTIONS[review.review_kind])
        ):
            raw = token_urlsafe(32)
            offer = ReviewOffer(
                id=keys.digest(raw),
                scope=tenant,
                notice_id=notice_id,
                doctor_subject=doctor.telegram_user_id,
                bot_id=doctor.telegram_bot_id,
                auth_epoch=doctor.auth_epoch,
                snapshot=s,
                action=action,
                expires_at=expiry,
                created_at=now,
                updated_at=now,
            )
            offers.append(offer)
            label = (
                templates.render("ack", context, n=str(n))
                if action == "acknowledge"
                else templates.render("dispose", context, n=str(n), action=str(action))
            )
            keyboard.append([{"text": label, "callback_data": raw}])
    rendered["reply_markup"] = markup
    notice = Notice(
        id=notice_id,
        scope=tenant,
        created_at=now,
        updated_at=now,
        intent_scope=intent.scope,
        intent_id=intent.id,
        attempt_id=intent.active_attempt_id or "",
        doctor_subject=doctor.telegram_user_id,
        bot_id=doctor.telegram_bot_id,
        auth_epoch=doctor.auth_epoch,
        snapshots=snapshots,
        payload=rendered,
        refusal=refusal,
    )
    command = CommandEnvelope(
        command_id=notice_id,
        principal=actor,
        worker=worker,
        scope=tenant,
        requested_at=now,
        payload={
            "type": "_DecorateNotice",
            "notice_id": notice_id,
            "basis": [r.model_dump(mode="json") for r in basis],
            "original": original_payload,
        },
    )
    rows = tuple(to_record(r, tenant) for r in (notice, *offers))
    return rendered, CommitRequest(command=command, puts=rows, expected=tuple(r.ref for r in rows))


def issuance_guards(
    store: "StoreBase", request: CommitRequest, now: datetime
) -> list["Check"] | None:
    from sanad.store._base import Check
    from sanad.store.reviews import doctor_checks

    command, worker = request.command, request.command.worker
    if (
        type(command.scope) is not TenantScope
        or not worker
        or not store._identity
        or command.principal.subject != "steward:notice-decorator"
        or command.principal.actor_kind != "system"
        or worker.service_subject != command.principal.subject
        or worker.resolved_scope != command.scope
        or worker.permitted_lanes != frozenset({"delivery"})
        or worker.auth_expiry <= now
    ):
        return None
    if (
        request.events
        or request.intents
        or request.markers
        or request.identity_reads
        or request.receipt_completion
        or command.work_claim
        or command.fence
    ):
        return None
    notices = [from_record(r, Notice) for r in request.puts if r.entity_type == "liaison_notice"]
    if len(notices) != 1 or any(
        r.entity_type not in {"liaison_notice", "review_offer"} or r.version != 1
        for r in request.puts
    ):
        return None
    notice = notices[0]
    if (
        notice.scope != command.scope
        or notice.id != command.command_id
        or notice.id != keys.digest("notice:" + notice.attempt_id)
        or notice.created_at != command.requested_at
        or notice.updated_at != notice.created_at
    ):
        return None
    auth = store.authorize(store._identity.bot_id, notice.doctor_subject)
    checks = doctor_checks(store, auth.principal, command.scope.doctor_id)
    if (
        checks is None
        or notice.bot_id != store._identity.bot_id
        or notice.auth_epoch != auth.principal.auth_epoch
    ):
        return None
    row = store.get(notice.intent_scope, "outbound_intent", notice.intent_id)
    if not row:
        return None
    intent = from_record(row, OutboundIntent)
    if (
        intent.audience != "doctor"
        or intent.notification_purpose == "DANGER"
        or intent.template_id == "scribe_photo_column"
        or intent.status != "sending"
        or intent.active_attempt_id != notice.attempt_id
        or not intent.delivery_claim
        or intent.delivery_claim.expires_at <= now
        or notice.intent_scope.doctor_id != command.scope.doctor_id
    ):
        return None
    doctor_row = store.get(command.scope, "doctor", command.scope.doctor_id)
    assert doctor_row
    doctor = from_record(doctor_row, Doctor)
    if (
        intent.recipient_ref != doctor.private_chat_id
        or intent.recipient_auth_epoch_seen != doctor.auth_epoch
    ):
        return None
    try:
        basis = tuple(VersionRef.model_validate(r) for r in command.payload.get("basis", []))  # type: ignore[union-attr]
        if displayed(store, intent, now, basis) != notice.snapshots:
            return None
        reviews = reviews_for(store, notice.snapshots)
    except (ValueError, TypeError):
        return None
    original = command.payload.get("original")
    if not isinstance(original, dict):
        return None
    # Stored notices have an independently authenticated original payload.
    if intent.payload is not None and original != intent.payload:
        return None
    if intent.scope_kind == "doctor":
        from sanad.contact.bundle import payload_snapshot

        if original != payload_snapshot(store, intent, now)[0]:
            return None
    if intent.payload is None and intent.scope_kind == "patient":
        from sanad.contact.delivery import doctor_payload

        if original != doctor_payload(store, intent):
            return None
    text = str(original.get("text", ""))
    rendered_text = notice.payload.get("text")
    if not isinstance(rendered_text, str) or not rendered_text.startswith(text):
        return None
    suffix = rendered_text[len(text) :]
    allowed = permitted.compute(notice.id, reviews, resolve(doctor.language, "doctor"))
    if suffix and (
        not suffix.startswith("\n\n")
        or any(s not in {f.value for f in allowed.facts} for s in suffix[2:].split("\n"))
        or len(suffix[2:].split("\n")) > policy.max_named_items
    ):
        return None
    if {k: v for k, v in notice.payload.items() if k not in {"text", "reply_markup"}} != {
        k: v for k, v in original.items() if k not in {"text", "reply_markup"}
    }:
        return None
    from sanad.evidence.correction_doctor import correction_keyboard

    old_markup, new_markup = (
        correction_keyboard(intent, original).get("reply_markup", {}),
        notice.payload.get("reply_markup", {}),
    )
    if not isinstance(old_markup, dict) or not isinstance(new_markup, dict):
        return None
    if {k: v for k, v in old_markup.items() if k != "inline_keyboard"} != {
        k: v for k, v in new_markup.items() if k != "inline_keyboard"
    }:
        return None
    old_buttons, buttons = (
        old_markup.get("inline_keyboard", []),
        new_markup.get("inline_keyboard", []),
    )
    if (
        not isinstance(old_buttons, list)
        or not isinstance(buttons, list)
        or buttons[: len(old_buttons)] != old_buttons
    ):
        return None
    offers = [from_record(r, ReviewOffer) for r in request.puts if r.entity_type == "review_offer"]
    expected_actions = [
        (s, action)
        for s, r in zip(notice.snapshots, reviews, strict=True)
        for action in (("acknowledge",) if r.state == "open" else ())
        + tuple(sorted(ALLOWED_REVIEW_ACTIONS[r.review_kind]))
    ]
    if len(offers) != len(expected_actions) or len(buttons) != len(old_buttons) + len(offers):
        return None
    for offer, (s, action), button in zip(
        offers, expected_actions, buttons[len(old_buttons) :], strict=True
    ):
        if not isinstance(button, list) or len(button) != 1 or not isinstance(button[0], dict):
            return None
        n = str(notice.snapshots.index(s) + 1)
        label = (
            templates.render("ack", doctor.language, n=n)
            if action == "acknowledge"
            else templates.render("dispose", doctor.language, n=n, action=str(action))
        )
        if set(button[0]) != {"text", "callback_data"} or button[0]["text"] != label:
            return None
        raw = button[0].get("callback_data")
        if (
            not isinstance(raw, str)
            or len(raw) < 32
            or keys.digest(raw) != offer.id
            or offer.scope != notice.scope
            or offer.notice_id != notice.id
            or offer.snapshot != s
            or offer.action != action
            or offer.doctor_subject != notice.doctor_subject
            or offer.bot_id != notice.bot_id
            or offer.auth_epoch != notice.auth_epoch
            or offer.consumed_at
            or offer.consumed_by
            or offer.created_at != notice.created_at
            or offer.updated_at != offer.created_at
            or offer.expires_at
            > min(
                intent.expires_at,
                intent.review_listing_expires_at
                or notice.created_at + DRAFT_CONCIERGE_POLICY.question_list_ttl,
            )
            or offer.expires_at <= now
        ):
            return None
    checks.append(Check(row.key, row.version))
    for review in reviews:
        r = to_record(review, model_scope(review))
        checks.append(Check(r.key, r.version))
        source = source_row(store, review)
        if source:
            checks.append(Check(source.key, source.version))
    return checks


def issued_records(resolution: "DeliveryResolution") -> tuple["StoredRecord", ...]:
    """Bind the first issued reference to the same transaction's notice stamp.

    No offer exists before this transaction. Only its deterministic first-notice
    metadata write may advance the displayed version; a concurrent review/source
    change fails issuance_guards and its CAS. Action-time checks never adapt it.
    """
    assert resolution.notice is not None
    stamp = resolution.obligation_stamp
    request = resolution.notice

    def stamped(s: ReviewSnapshot) -> ReviewSnapshot:
        if (
            stamp is not None
            and s.scope == model_scope(from_record(stamp, ReviewObligation))
            and s.review_ref.id == stamp.id
        ):
            if stamp.version != s.review_ref.version + 1:
                raise NoticeChanged("notice_stamp_changed")
            return s.model_copy(update={"review_ref": stamp.ref})
        return s

    rows = []
    for row in request.puts:
        if row.entity_type == "liaison_notice":
            notice = from_record(row, Notice)
            notice = notice.model_copy(
                update={"snapshots": tuple(stamped(s) for s in notice.snapshots)}
            )
            rows.append(to_record(notice, notice.scope))
        else:
            offer = from_record(row, ReviewOffer)
            offer = offer.model_copy(update={"snapshot": stamped(offer.snapshot)})
            rows.append(to_record(offer, offer.scope))
    return tuple(rows)
