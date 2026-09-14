"""Injected Telegram enrollment commands; the channel never imports auth."""

from sanad.auth.claim import ClaimService
from sanad.auth.commands import (
    ClaimInvitation,
    IssueAdminLogin,
    IssueDoctorLogin,
    IssuePatientLogin,
)
from sanad.auth.login import LoginService
from sanad.auth.service import InternalCommand, revise
from sanad.channels.telegram import wording
from sanad.channels.telegram.router import RouteResult, TelegramRuntime
from sanad.domain import PatientScope
from sanad.presentation import consent_terms
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
        login = text in {"/login", "login", "/login admin", "/logout"}
        if hash and auth.binding is None and self.runtime.accounts.access_code_matches(str(hash)):
            hash = None  # doctor access code: continue to the doctor application route
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
        elif text == "/login admin":
            result = self.login.issue(
                IssueAdminLogin(command_id=command_id, actor=auth.principal), claim=work
            )
        elif text == "/logout":
            result = self.login.revoke_roles(auth.principal.subject, command_id)
            if result.status in {"accepted", "duplicate"}:
                result = runtime.accounts.finish_receipt(
                    receipt,
                    work,
                    result_code="signed_out",
                    template_id="dashboard_signed_out"
                    if auth.principal.actor_kind == "doctor"
                    else "admin_no_sessions",
                    text=wording.render(
                        "dashboard_signed_out", runtime.accounts.language(receipt.source_subject)
                    ),
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
        template = (
            None
            if accepted
            else "admin_no_sessions"
            if text == "/logout"
            and "admin" in auth.principal.verified_roles
            and auth.admin_epoch is None
            else "claim_refused"
            if hash or known_callback
            else "account_suspended"
            if auth.doctor_status == "suspended"
            else "patient_not_linked"
            if text in {"login", "/login"} and not auth.binding
            else "login_refused"
        )
        if known_callback:
            refusal = wording.render(
                "claim_refused", runtime.accounts.language(receipt.source_subject)
            )
            token = self.claims.load(
                self.claims.scope, "claim_callback", callback_hash, ClaimCallback
            )
            pending = self.claims.patient_claim(token.claim_id) if token else None
            if (
                token
                and pending
                and token.actor_subject == receipt.source_subject
                and token.expires_at > now
                and pending.state == "pending"
                and pending.consent_id is None
                and pending.review_at > now
                and token.offer_generation < pending.offer_generation
            ):
                refusal = consent_terms.OFFER_SUPERSEDED
            runtime.transport.answer_callback(
                str(payload.get("callback_query_id", "")),
                "" if accepted else refusal,
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
                    runtime.accounts.intent(
                        to_record(
                            source,
                            receipt.scope
                            if isinstance(source, InboundReceipt)
                            else self.claims.scope,
                        ),
                        "admin_no_sessions" if template == "admin_no_sessions" else "claim_refused",
                        receipt.source_subject,
                        receipt.source_chat,
                        "doctor" if auth.principal.actor_kind == "doctor" else "applicant",
                        text=wording.render(
                            template or "claim_refused",
                            runtime.accounts.language(receipt.source_subject),
                        ),
                        auth_epoch=auth.auth_epoch
                        if auth.principal.actor_kind == "doctor"
                        else None,
                        logical_suffix=receipt.id,
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
