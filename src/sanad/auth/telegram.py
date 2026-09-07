"""Injected Telegram enrollment commands; the channel never imports auth."""

from sanad.auth.claim import ClaimService
from sanad.auth.commands import ClaimInvitation, IssueDoctorLogin, IssuePatientLogin
from sanad.auth.login import LoginService
from sanad.auth.service import InternalCommand, revise
from sanad.channels.telegram import wording
from sanad.channels.telegram.router import RouteResult, TelegramRuntime
from sanad.domain import PatientScope
from sanad.store.records import (
    Authorization,
    ClaimCallback,
    InboundReceipt,
    OutboundIntent,
    from_record,
    to_record,
)


class IdentityRouting:
    def __init__(self, runtime: TelegramRuntime, login: LoginService, claims: ClaimService):
        self.runtime, self.login, self.claims = runtime, login, claims

    def __call__(self, receipt: InboundReceipt, auth: Authorization) -> RouteResult | None:
        payload = receipt.payload or {}
        text = str(payload.get("text", "")).strip()
        hash = payload.get("invitation_hash")
        callback_hash = str(payload.get("callback_token_hash", ""))
        known_callback = bool(
            callback_hash
            and self.claims.store.get(self.claims.scope, "claim_callback", callback_hash)
        )
        login = text in {"/login", "login"}
        if not (hash or known_callback or login):
            return None
        runtime, store, now = self.runtime, self.runtime.store, self.runtime.clock()
        work = store.claim_work(
            to_record(receipt, receipt.scope).scoped_key(receipt.scope),
            receipt.version,
            "identity",
            now,
            runtime.accounts.policy.operations.claim_ttl,
        )
        if work is None:
            return RouteResult(route="busy", status="processing")
        saved = store.get(receipt.scope, "inbound_receipt", receipt.id)
        assert saved is not None
        receipt = from_record(saved, InboundReceipt)
        command_id = "identity-route:" + receipt.id
        result = None
        if known_callback:
            result = self.claims.callback(callback_hash, auth.principal, command_id, claim=work)
        elif hash:
            result = self.claims.claim_invitation(
                ClaimInvitation(
                    command_id=command_id,
                    actor=auth.principal,
                    invitation_hash=str(hash),
                    private_chat_id=receipt.source_chat,
                ),
                claim=work,
            )
        elif auth.principal.actor_kind == "doctor":
            result = self.login.issue(
                IssueDoctorLogin(command_id=command_id, actor=auth.principal), claim=work
            )
        elif auth.principal.actor_kind == "patient":
            result = self.login.issue(
                IssuePatientLogin(command_id=command_id, actor=auth.principal), claim=work
            )
        accepted = result is not None and result.status in {"accepted", "duplicate"}
        template = None if accepted else "claim_refused"
        if known_callback:
            runtime.transport.answer_callback(
                str(payload.get("callback_query_id", "")),
                "" if accepted else wording.render("claim_refused"),
            )
        if not accepted:
            source = auth.binding or revise(
                receipt,
                now,
                state="completed",
                processing_claim=None,
                work_clock=None,
                result_event_ids=(),
            )
            intents: tuple[OutboundIntent, ...] = ()
            if not known_callback:
                intents = (
                    self.claims.account_intent(
                        source,
                        "claim_refused",
                        receipt.source_subject,
                        "doctor" if auth.principal.actor_kind == "doctor" else "applicant",
                        auth_epoch=auth.auth_epoch
                        if auth.principal.actor_kind == "doctor"
                        else None,
                        suffix=receipt.id,
                    ),
                )
            self.claims.commit(
                InternalCommand(
                    type="FinishIdentityReceipt",
                    target_id=receipt.id,
                    command_id="identity-finish:" + receipt.id,
                    actor=auth.principal,
                ),
                intents=intents,
                claim=work,
            )
        delivery_patient = None
        if known_callback and accepted:
            token = self.claims.load(
                self.claims.scope, "claim_callback", callback_hash, ClaimCallback
            )
            pending = self.claims.patient_claim(token.claim_id) if token else None
            if pending and token and token.actor_subject == receipt.source_subject:
                delivery_patient = PatientScope(
                    doctor_id=pending.doctor_id, patient_id=pending.patient_id
                )
        return RouteResult(
            route="callback"
            if known_callback
            else "patient"
            if auth.principal.actor_kind == "patient"
            else "doctor"
            if auth.principal.actor_kind == "doctor"
            else "unknown",
            status=result.status if result else "forbidden",
            template_id=template,
            delivery_patient=delivery_patient,
        )
