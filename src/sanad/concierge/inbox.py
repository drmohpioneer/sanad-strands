"""Doctor-pulled review pages and explicit disposition reasons."""

from datetime import datetime
from typing import TYPE_CHECKING

from sanad.concierge.policy import DRAFT_CONCIERGE_POLICY
from sanad.contact.bundle import ENGLISH_KINDS, KINDS
from sanad.domain import Principal, ReviewObligation, TenantScope
from sanad.liaison import policy, templates
from sanad.liaison.records import Notice, ReviewOffer
from sanad.liaison.snapshot import snapshot
from sanad.presentation.context import PresentationContext, resolve
from sanad.presentation.inbox import due_row
from sanad.store.records import CommandEnvelope, Doctor, Patient, from_record, model_scope

if TYPE_CHECKING:
    from sanad.channels.telegram.router import RouteResult
    from sanad.scribe.turn import ScribeTurn
    from sanad.store.protocol import Store
    from sanad.store.records import Authorization, Claim, InboundReceipt


def owned_reviews(store: "Store", doctor_id: str, now: datetime) -> list[ReviewObligation]:
    cursor = None
    values = []
    while True:
        rows, cursor = store.list_reviews(TenantScope(doctor_id=doctor_id), cursor, limit=100)
        for row in rows:
            hint = from_record(row, ReviewObligation)
            strong = store.get(model_scope(hint), "review", hint.id)
            if strong:
                review = from_record(strong, ReviewObligation)
                if review.owner_doctor_id == doctor_id and review.state in {"open", "acknowledged"}:
                    values.append(review)
        if cursor is None:
            break
    return sorted(values, key=lambda r: (r.review_at > now, r.created_at, r.id))


def material_changed(store: "Store", review: ReviewObligation) -> bool:
    from sanad.steward.types import records

    # A version alone does not prove when the material changed. Compare actual
    # persisted notices; never claim a pre-notice change happened afterward.
    for row in records(store, TenantScope(doctor_id=review.owner_doctor_id), "liaison_notice"):
        notice = from_record(row, Notice)
        if any(
            s.scope == model_scope(review)
            and s.review_ref.id == review.id
            and s.material_version < review.last_material_change_version
            for s in notice.snapshots
        ):
            return True
    return False


def listing_text(
    store: "Store",
    reviews: list[ReviewObligation],
    language: str | PresentationContext,
    now: datetime,
    page: int,
    pages: int,
) -> str:
    context = resolve(language, "doctor")
    lines = [templates.render("page", context, page=str(page), pages=str(pages))]
    for n, review in enumerate(reviews, 1):
        row = (
            store.get(model_scope(review), "patient", review.patient_id)
            if review.patient_id
            else None
        )
        name = (
            from_record(row, Patient).display_name[:60]
            if row
            else templates.render("unnamed" if review.patient_id else "unassigned", context)
        )
        kind = (ENGLISH_KINDS if context.locale == "en" else KINDS)[review.review_kind]
        lines.append(
            f"{n}. "
            + due_row(
                context,
                name=name,
                kind=kind,
                due=review.review_at.isoformat(),
                hours=str(max(0, int((now - review.created_at).total_seconds() // 3600))),
                state=templates.render(review.state, context),
            )
        )
        if material_changed(store, review):
            lines.append(templates.render("changed", context))
    if not reviews:
        lines.append(templates.render("empty", context))
    lines.append(templates.render("usage", context))
    return "\n\n".join(lines)


def reply(
    turn: "ScribeTurn",
    receipt: "InboundReceipt",
    actor: Principal,
    claim: "Claim",
    doctor: Doctor,
    key: str,
    status: str,
    **fields: str,
) -> "RouteResult":
    if key == "invalid_action":
        fields = {}
    if key == "stale":
        fields["reason"] = templates.stale_reason(fields.get("reason", "default"), doctor.language)
    return turn._reply(
        receipt,
        actor,
        claim,
        "liaison_" + key,
        status,
        text=templates.render(key, doctor.language, **fields),
    )


def act(
    turn: "ScribeTurn",
    receipt: "InboundReceipt",
    actor: Principal,
    claim: "Claim",
    doctor: Doctor,
    offer: ReviewOffer,
    reason: str = "",
) -> "RouteResult":
    result = turn.runtime.steward.handle(
        CommandEnvelope(
            command_id="review-doctor:" + receipt.id,
            principal=actor,
            scope=offer.snapshot.scope,
            requested_at=turn.repo.clock(),
            payload={
                "type": "AcknowledgeReview" if offer.action == "acknowledge" else "ResolveReview",
                "offer_id": offer.id,
                "expected_source_version": offer.snapshot.source_version,
                **({"reason": reason} if offer.action != "acknowledge" else {}),
            },
        )
    )
    turn.checkpoint("doctor_clinical_committed")
    if result.status == "accepted":
        if offer.action == "acknowledge":
            return reply(turn, receipt, actor, claim, doctor, "ack_done", result.status)
        return reply(
            turn,
            receipt,
            actor,
            claim,
            doctor,
            "resolve_done",
            result.status,
            action=offer.action,
            reason=reason.strip(),
        )
    return reply(
        turn,
        receipt,
        actor,
        claim,
        doctor,
        "invalid_action" if result.status == "invalid_action" else "stale",
        result.status,
        reason=result.reason_code or "authority_or_snapshot_changed",
    )


def doctor_command(
    turn: "ScribeTurn",
    receipt: "InboundReceipt",
    actor: Principal,
    claim: "Claim",
    doctor: Doctor,
    command: str,
    argument: str,
) -> "RouteResult":
    from sanad.channels.telegram.router import RouteResult

    if command == "/inbox":
        if argument and (not argument.isdigit() or len(argument) > 8 or int(argument) < 1):
            return reply(turn, receipt, actor, claim, doctor, "usage", "invalid_input")
        page = int(argument or "1")
        values = owned_reviews(turn.repo.store, doctor.id, turn.repo.clock())
        pages = max(1, (len(values) + policy.inbox_page_size - 1) // policy.inbox_page_size)
        if page > pages:
            return reply(turn, receipt, actor, claim, doctor, "usage", "invalid_page")
        selected = values[(page - 1) * policy.inbox_page_size : page * policy.inbox_page_size]
        intent = turn.repo.intent(
            doctor,
            "liaison_inbox",
            {
                "text": listing_text(
                    turn.repo.store, selected, doctor.language, turn.repo.clock(), page, pages
                )
            },
            "inbox:" + receipt.id,
        )
        listing_expiry = turn.repo.clock() + DRAFT_CONCIERGE_POLICY.question_list_ttl
        intent = intent.model_copy(
            update={
                "review_listing": tuple(snapshot(turn.repo.store, r) for r in selected),
                "review_listing_expires_at": listing_expiry,
                "expires_at": min(intent.expires_at, listing_expiry),
            }
        )
        result = turn.repo.commit(
            actor, "ScribeReply", "inbox:" + receipt.id, intents=(intent,), claim=claim
        )
        return RouteResult(route="doctor", status=result.status, template_id="liaison_inbox")
    parts = argument.split(maxsplit=1)
    if (
        len(parts) != 2
        or not parts[1].strip()
        or len(parts[1]) > DRAFT_CONCIERGE_POLICY.reply_max_chars
    ):
        return reply(turn, receipt, actor, claim, doctor, "reason_required", "invalid_input")
    row = turn.repo.store.get(doctor.scope, "review_offer", parts[0])
    if not row:
        return reply(
            turn, receipt, actor, claim, doctor, "stale", "stale_version", reason="offer_missing"
        )
    offer = from_record(row, ReviewOffer)
    if offer.action == "acknowledge":
        return reply(turn, receipt, actor, claim, doctor, "reason_required", "invalid_input")
    return act(turn, receipt, actor, claim, doctor, offer, parts[1])


def callback(
    turn: "ScribeTurn", receipt: "InboundReceipt", auth: "Authorization"
) -> "RouteResult | None":
    if receipt.kind != "callback" or not auth.principal.doctor_id:
        return None
    actor = auth.principal
    row = turn.repo.store.get(
        TenantScope(doctor_id=actor.doctor_id or ""),
        "review_offer",
        str((receipt.payload or {}).get("callback_token_hash", "")),
    )
    if not row:
        return None
    claimed = turn._claim(receipt)
    if not claimed:
        from sanad.channels.telegram.router import RouteResult

        return RouteResult(route="busy", status="processing")
    receipt, claim = claimed
    doctor = turn.claims.doctor(actor)
    if not doctor:
        turn.runtime.accounts.finish_receipt(receipt, claim, result_code="authority_changed")
        from sanad.channels.telegram.router import RouteResult

        return RouteResult(route="refused", status="authority_changed")
    offer = from_record(row, ReviewOffer)
    turn.runtime.transport.answer_callback(
        str((receipt.payload or {}).get("callback_query_id", "")), ""
    )
    if offer.action == "acknowledge":
        return act(turn, receipt, actor, claim, doctor, offer)
    # Collecting a reason is not consuming the offer or refreshing its snapshot.
    return reply(
        turn,
        receipt,
        actor,
        claim,
        doctor,
        "reason",
        "needs_confirmation",
        action=offer.action,
        offer=offer.id,
    )
