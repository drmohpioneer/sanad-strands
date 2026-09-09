"""Account transitions through the same conditional store transaction as the Steward."""

from collections.abc import Callable
from datetime import datetime
from secrets import token_urlsafe
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, JsonValue

from sanad.accounts.commands import (
    AccountCommand,
    AccountPolicy,
    AccountResult,
    AlreadyInState,
    ApplyAsDoctor,
    ApproveDoctor,
    CallbackRefused,
    ReinstateDoctor,
    RejectDoctor,
    SuspendDoctor,
)
from sanad.accounts.records import (
    AccountAcknowledgment,
    Application,
    CallbackToken,
    Doctor,
    OperationalIssue,
    SubjectBinding,
)
from sanad.domain import Principal, TenantScope, VersionRef
from sanad.domain.language import default_language
from sanad.domain.language import effective as contest_language
from sanad.store import keys
from sanad.store.keys import AccountScope
from sanad.store.protocol import Store
from sanad.store.records import (
    AuditEvent,
    Claim,
    CommandEnvelope,
    CommitRequest,
    CommitResult,
    DoctorAuthority,
    Forbidden,
    IdentityConfig,
    InboundReceipt,
    OperationalClock,
    OutboundIntent,
    ReceiptCompletion,
    StaleVersion,
    StoredRecord,
    WorkerCapability,
    canonical_json,
    from_record,
    model_scope,
    to_record,
)

type Presenter = Callable[[str, str, dict[str, str]], str]
DEFAULT_ACCOUNT_POLICY = AccountPolicy()


class AccountService:
    def __init__(
        self,
        store: Store,
        clock: Callable[[], datetime],
        identity: IdentityConfig,
        present: Presenter,
        *,
        approve_label: tuple[str, str],
        reject_label: tuple[str, str],
        policy: AccountPolicy = DEFAULT_ACCOUNT_POLICY,
    ):
        self.store, self.clock, self.identity = store, clock, identity
        self.scope = AccountScope(bot_id=identity.bot_id)
        self.present, self.policy = present, policy
        self.approve_label, self.reject_label = approve_label, reject_label
        store.configure_identity(identity)

    def application(self, id: str) -> Application | None:
        row = self.store.get(self.scope, "application", id)
        return from_record(row, Application) if row else None

    def doctor(self, id: str) -> Doctor | None:
        row = self.store.get_account_source(
            self.scope, VersionRef(entity_type="doctor", id=id, version=1)
        )
        return from_record(row, Doctor) if row else None

    def language(self, subject: str) -> str:
        auth = self.store.authorize(self.identity.bot_id, subject)
        doctor_id = auth.principal.doctor_id or (auth.binding.doctor_id if auth.binding else None)
        # A patient binding's doctor is not the recipient of this message.
        doctor = (
            self.doctor(doctor_id)
            if doctor_id and (not auth.binding or "patient" not in auth.binding.role_set)
            else None
        )
        # Every doctor-facing surface reads its language here, so the contest
        # override belongs at this accessor rather than at each of its callers.
        return contest_language(doctor.language) if doctor else default_language

    def _admin(self, actor: Principal) -> bool:
        return (
            actor.bot_id == self.identity.bot_id
            and actor.subject == self.identity.admin_user_id
            and "admin" in actor.verified_roles
            and "admin"
            in self.store.authorize(actor.bot_id, actor.subject).principal.verified_roles
        )

    def _envelope(self, command: AccountCommand, claim: Claim | None = None) -> CommandEnvelope:
        now = self.clock()
        return CommandEnvelope(
            command_id=command.command_id,
            principal=command.actor,
            scope=self.scope,
            requested_at=now,
            payload=command.model_dump(mode="json"),
            expected_versions=command.expected_versions,
            work_claim=claim,
            worker=WorkerCapability(
                service_subject="accounts",
                permitted_lanes=frozenset({"account"}),
                resolved_scope=self.scope,
                auth_expiry=now + self.policy.operations.claim_ttl,
                invocation_id=command.command_id,
            ),
        )

    def _prior(self, command: AccountCommand) -> CommitResult | None:
        return self.store.lookup_command(self._envelope(command))

    def _commit(
        self,
        command: AccountCommand,
        models: tuple[BaseModel, ...] = (),
        intents: tuple[OutboundIntent, ...] = (),
        *,
        claim: Claim | None = None,
        result_code: str | None = None,
        complete_receipt: bool = True,
    ) -> CommitResult:
        now = self.clock()
        rows = tuple(to_record(m, model_scope(m)) for m in models)
        outgoing = tuple(to_record(i, self.scope) for i in intents)
        event_id = keys.digest("account:" + command.command_id)
        event = AuditEvent(
            id=event_id,
            event_id=event_id,
            command_id=command.command_id,
            scope=self.scope,
            event_type=type(command).__name__,
            actor=command.actor,
            accepted_at=now,
            created_at=now,
            updated_at=now,
            aggregate_refs=tuple(r.ref for r in rows),
            before_versions=tuple(
                VersionRef(entity_type=r.entity_type, id=r.id, version=r.version - 1)
                for r in rows
                if r.version > 1
            ),
            after_versions=tuple(r.ref for r in rows),
            policy_versions=(self.policy.policy_version,),
        )
        events = (to_record(event, self.scope),)
        return self.store.commit_account(
            CommitRequest(
                command=self._envelope(command, claim),
                puts=rows,
                events=events,
                intents=outgoing,
                expected=tuple(r.ref for r in (*rows, *events, *outgoing)),
                receipt_completion=ReceiptCompletion(claim=claim, result_event_ids=(event_id,))
                if claim and complete_receipt
                else None,
                reason_code=result_code
                or (
                    command.reason_code
                    if isinstance(command, (RejectDoctor, SuspendDoctor))
                    else None
                ),
            )
        )

    def intent(
        self,
        source: StoredRecord,
        template_id: str,
        subject: str,
        chat: str,
        audience: Literal["applicant", "admin", "doctor"],
        *,
        logical_suffix: str = "",
        fields: dict[str, str] | None = None,
        markup: JsonValue = None,
        text: str | None = None,
        auth_epoch: int | None = None,
        safety: bool = False,
        source_version: int | None = None,
    ) -> OutboundIntent:
        now = self.clock()
        ref = VersionRef(
            entity_type=source.entity_type,
            id=source.id,
            version=source.version if source_version is None else source_version,
        )
        if audience == "admin" and auth_epoch is None:
            admin = self.store.authorize(self.identity.bot_id, subject)
            if "doctor" in admin.principal.verified_roles:
                auth_epoch = admin.auth_epoch
        logical = keys.digest(f"{source.id}:{template_id}:{ref.version}:{logical_suffix}")
        payload: dict[str, JsonValue] = {
            "text": text
            if text is not None
            else self.present(template_id, self.language(subject), fields or {}),
        }
        if markup is not None:
            payload["reply_markup"] = markup
        return OutboundIntent(
            id=logical,
            scope=self.scope,
            scope_kind="account",
            audience=audience,
            logical_key=logical,
            source_event_ids=(source.id,),
            source_versions=(ref,),
            recipient_subject=subject,
            recipient_ref=chat,
            bot_id=self.identity.bot_id,
            notification_purpose="patient_safety_response" if safety else "solicited_reply",
            eligibility_class="urgent" if safety else "account",
            payload_ref="account:" + logical,
            payload=payload,
            payload_digest=keys.digest(canonical_json(payload).decode()),
            conversation_sequence=0,
            expires_at=now + self.policy.callback_ttl,
            status="queued",
            delivery_lease_seconds=int(self.policy.operations.lease_ttl.total_seconds()),
            created_at=now,
            updated_at=now,
            work_clock=OperationalClock(next_action_at=now, work_lane="delivery"),
            template_id=template_id,
            recipient_auth_epoch_seen=auth_epoch,
        )

    def _ack(
        self,
        application: Application,
    ) -> tuple[AccountAcknowledgment, OutboundIntent] | None:
        row = self.store.get(self.scope, "account_ack", application.id)
        prior = from_record(row, AccountAcknowledgment) if row else None
        now = self.clock()
        if (
            prior
            and prior.application_version == application.version
            and now < prior.last_queued_at + self.policy.application_ack_interval
        ):
            return None
        ack = AccountAcknowledgment(
            id=application.id,
            scope=self.scope,
            application_id=application.id,
            application_version=application.version,
            last_queued_at=now,
            version=prior.version + 1 if prior else 1,
            created_at=prior.created_at if prior else now,
            updated_at=now,
        )
        return ack, self.intent(
            to_record(application, self.scope),
            "application_rejected" if application.status == "rejected" else "application_received",
            application.telegram_user_id,
            application.private_chat_id,
            "applicant",
            logical_suffix=f"ack:{ack.version}",
        )

    def apply(self, command: ApplyAsDoctor) -> AccountResult:
        actor = command.actor
        if (
            actor.bot_id != self.identity.bot_id
            or actor.user_id != actor.subject
            or command.private_chat_id != actor.subject
        ):
            return Forbidden()
        auth = self.store.authorize(self.identity.bot_id, actor.subject)
        if (
            auth.binding is not None
            or "patient" in actor.verified_roles
            or "doctor" in actor.verified_roles
        ):
            return Forbidden()
        prior = self._prior(command)
        if prior is not None:
            return prior
        id = keys.digest(f"{self.identity.bot_id}:{actor.subject}")
        application = self.application(id)
        if application is not None and application.private_chat_id != command.private_chat_id:
            return Forbidden()
        new = application is None or (application.status == "rejected" and command.restart_rejected)
        models: list[BaseModel] = []
        intents: list[OutboundIntent] = []
        now = self.clock()
        if new:
            application = Application(
                id=id,
                scope=self.scope,
                telegram_user_id=actor.subject,
                private_chat_id=command.private_chat_id,
                claimed_name=command.claimed_name,
                claimed_specialty=command.claimed_specialty,
                claimed_city=command.claimed_city,
                version=application.version + 1 if application else 1,
                created_at=application.created_at if application else now,
                updated_at=now,
                work_clock=OperationalClock(
                    next_action_at=now + self.policy.application_review_interval,
                    work_lane="account",
                ),
            )
            models.append(application)
            buttons: list[JsonValue] = []
            for action, label in (("approve", self.approve_label), ("reject", self.reject_label)):
                raw = token_urlsafe(24)
                token = CallbackToken.model_validate(
                    {
                        "id": keys.digest(raw),
                        "scope": self.scope,
                        "application_id": id,
                        "expected_application_version": application.version,
                        "actor_subject": self.identity.admin_user_id,
                        "action": action,
                        "expires_at": now + self.policy.callback_ttl,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                models.append(token)
                buttons.append(
                    {
                        "text": label[self.language(self.identity.admin_user_id) == "en"],
                        "callback_data": raw,
                    }
                )
            intents.append(
                self.intent(
                    to_record(application, self.scope),
                    "admin_new_application",
                    self.identity.admin_user_id,
                    self.identity.admin_user_id,
                    "admin",
                    fields={
                        "name": application.claimed_name,
                        "specialty": application.claimed_specialty,
                        "city": application.claimed_city,
                    },
                    markup={"inline_keyboard": [buttons]},
                )
            )
        assert application is not None
        if application.status == "approved":
            return AlreadyInState(entity_id=id, state="approved")
        ack = self._ack(application)
        if ack:
            models.append(ack[0])
            intents.append(ack[1])
        if not models:
            return AlreadyInState(entity_id=id, state=application.status)
        return self._commit(command, tuple(models), tuple(intents))

    def approve(
        self, command: ApproveDoctor, *, token: CallbackToken | None = None
    ) -> AccountResult:
        if not self._admin(command.actor):
            return Forbidden()
        prior = self._prior(command)
        if prior is not None:
            return prior
        application = self.application(command.application_id)
        if application is None or application.status == "rejected":
            return Forbidden()
        if application.status == "approved":
            return AlreadyInState(entity_id=application.id, state="approved")
        if application.version != command.expected_application_version:
            return StaleVersion(conflicts=("application",))
        auth = self.store.authorize(self.identity.bot_id, application.telegram_user_id)
        # This release cannot recover, replace or transfer any prior clinical binding.
        if auth.binding is not None:
            return Forbidden()
        now, id = self.clock(), uuid4().hex
        doctor = Doctor(
            id=id,
            scope=TenantScope(doctor_id=id),
            telegram_bot_id=self.identity.bot_id,
            telegram_user_id=application.telegram_user_id,
            private_chat_id=application.private_chat_id,
            name=application.claimed_name,
            specialty=application.claimed_specialty,
            city=application.claimed_city,
            status="approved",
            approved_by=command.actor.subject,
            approved_at=now,
            auth_epoch=1,
            policy_version=self.policy.policy_version,
            application_id=application.id,
            created_at=now,
            updated_at=now,
        )
        decided = Application.model_validate(
            application.model_dump()
            | {
                "version": application.version + 1,
                "updated_at": now,
                "status": "approved",
                "reviewer_id": command.actor.subject,
                "reviewed_at": now,
                "doctor_id": id,
                "approval_reference": command.command_id,
                "work_clock": None,
            }
        )
        binding = SubjectBinding(
            id=keys.subject(self.identity.bot_id, application.telegram_user_id).pk,
            scope=self.scope,
            telegram_user_id=application.telegram_user_id,
            role_set=frozenset({"doctor"}),
            doctor_id=id,
            private_chat_id=application.private_chat_id,
            created_at=now,
            updated_at=now,
        )
        authority = DoctorAuthority(
            id=id,
            doctor_id=id,
            approved=True,
            subject=application.telegram_user_id,
            recipient_ref=application.private_chat_id,
            auth_epoch=1,
            created_at=now,
            updated_at=now,
        )
        models: tuple[BaseModel, ...] = (decided, doctor, authority, binding)
        if token:
            models += (self._consume(token),)
        intent = self.intent(
            to_record(decided, self.scope),
            "doctor_approved",
            application.telegram_user_id,
            application.private_chat_id,
            "doctor",
            auth_epoch=1,
        )
        result = self._commit(command, models, (intent,))
        if result.status == "stale_version":
            current = self.application(application.id)
            if current and current.status == "approved":
                return AlreadyInState(entity_id=current.id, state="approved")
        return result

    def reject(self, command: RejectDoctor, *, token: CallbackToken | None = None) -> AccountResult:
        if not self._admin(command.actor):
            return Forbidden()
        prior = self._prior(command)
        if prior is not None:
            return prior
        application = self.application(command.application_id)
        if application is None or application.status == "approved":
            return Forbidden()
        if application.status == "rejected":
            return AlreadyInState(entity_id=application.id, state="rejected")
        if application.version != command.expected_application_version:
            return StaleVersion(conflicts=("application",))
        now = self.clock()
        decided = Application.model_validate(
            application.model_dump()
            | {
                "version": application.version + 1,
                "updated_at": now,
                "status": "rejected",
                "reviewer_id": command.actor.subject,
                "reviewed_at": now,
                "work_clock": None,
            }
        )
        # Decision notice consumes this application's new acknowledgment interval too.
        ack = self._ack(decided)
        assert ack is not None
        models: tuple[BaseModel, ...] = (decided, ack[0])
        if token:
            models += (self._consume(token),)
        return self._commit(command, models, (ack[1],))

    def suspend(self, command: SuspendDoctor) -> AccountResult:
        return self._coverage(command, suspend=True)

    def reinstate(self, command: ReinstateDoctor) -> AccountResult:
        return self._coverage(command, suspend=False)

    def _coverage(
        self, command: SuspendDoctor | ReinstateDoctor, *, suspend: bool
    ) -> AccountResult:
        if not self._admin(command.actor):
            return Forbidden()
        prior = self._prior(command)
        if prior is not None:
            return prior
        doctor = self.doctor(command.doctor_id)
        if doctor is None:
            return Forbidden()
        target = "suspended" if suspend else "approved"
        if doctor.status == target:
            return AlreadyInState(entity_id=doctor.id, state=target)
        if doctor.status != ("approved" if suspend else "suspended"):
            return Forbidden()
        if doctor.version != command.expected_doctor_version:
            return StaleVersion(conflicts=("doctor",))
        binding = self.store.authorize(self.identity.bot_id, doctor.telegram_user_id).binding
        row = self.store.get(doctor.scope, "doctor_authority", doctor.id)
        if binding is None or binding.doctor_id != doctor.id or row is None:
            return Forbidden()
        authority = from_record(row, DoctorAuthority)
        now, epoch = self.clock(), doctor.auth_epoch + 1
        changed = Doctor.model_validate(
            doctor.model_dump()
            | {
                "version": doctor.version + 1,
                "updated_at": now,
                "status": target,
                "auth_epoch": epoch,
            }
        )
        bound = SubjectBinding.model_validate(
            binding.model_dump()
            | {
                "version": binding.version + 1,
                "updated_at": now,
                "status": "frozen" if suspend else "active",
                "binding_epoch": binding.binding_epoch + 1,
            }
        )
        authority = DoctorAuthority.model_validate(
            authority.model_dump()
            | {
                "version": authority.version + 1,
                "updated_at": now,
                "approved": not suspend,
                "auth_epoch": epoch,
            }
        )
        issue_id = "coverage:" + doctor.id
        issue_row = self.store.get(self.scope, "operational_issue", issue_id)
        old_issue = from_record(issue_row, OperationalIssue) if issue_row else None
        if not suspend and old_issue is None:
            return Forbidden()
        issue = OperationalIssue(
            id=issue_id,
            scope=self.scope,
            kind="coverage_review",
            affected_id=doctor.id,
            owner_id=self.identity.admin_user_id,
            status="open" if suspend else "resolved",
            due_at=now,
            reason=command.reason_code if isinstance(command, SuspendDoctor) else "reinstated",
            version=old_issue.version + 1 if old_issue else 1,
            created_at=old_issue.created_at if old_issue else now,
            updated_at=now,
            work_clock=OperationalClock(next_action_at=now, work_lane="operational")
            if suspend
            else None,
        )
        intents = (
            (
                self.intent(
                    to_record(changed, changed.scope),
                    "doctor_suspended_notice",
                    doctor.telegram_user_id,
                    doctor.private_chat_id,
                    "doctor",
                    auth_epoch=epoch,
                ),
            )
            if suspend
            else ()
        )
        return self._commit(command, (changed, authority, bound, issue), intents)

    def _consume(self, token: CallbackToken) -> CallbackToken:
        return CallbackToken.model_validate(
            token.model_dump()
            | {
                "version": token.version + 1,
                "updated_at": self.clock(),
                "consumed_at": self.clock(),
            }
        )

    def callback(
        self, raw: str, actor: Principal, command_id: str
    ) -> AccountResult | CallbackRefused:
        return self.callback_by_hash(keys.digest(raw), actor, command_id)

    def callback_by_hash(
        self, token_hash: str, actor: Principal, command_id: str
    ) -> AccountResult | CallbackRefused:
        if not self._admin(actor):
            return CallbackRefused(reason="actor")
        row = self.store.get(self.scope, "callback_token", token_hash)
        if row is None:
            return CallbackRefused(reason="unknown")
        token = from_record(row, CallbackToken)
        if token.actor_subject != actor.subject:
            return CallbackRefused(reason="actor")
        if token.consumed_at is not None:
            return CallbackRefused(reason="consumed")
        if token.expires_at <= self.clock():
            return CallbackRefused(reason="expired")
        application = self.application(token.application_id)
        if application is None or application.version != token.expected_application_version:
            return CallbackRefused(reason="stale_version")
        if token.action == "approve":
            result = self.approve(
                ApproveDoctor(
                    command_id=command_id,
                    actor=actor,
                    application_id=application.id,
                    expected_application_version=application.version,
                ),
                token=token,
            )
        else:
            result = self.reject(
                RejectDoctor(
                    command_id=command_id,
                    actor=actor,
                    application_id=application.id,
                    expected_application_version=application.version,
                    reason_code="admin_rejected",
                ),
                token=token,
            )
        if result.status not in {"accepted", "duplicate"}:
            return CallbackRefused(reason="stale_version")
        return result

    def fail_receipt(self, receipt: InboundReceipt, claim: Claim) -> CommitResult:
        """Exhausted account processing remains visibly owned without retrying its action."""
        assert receipt.principal is not None
        now = self.clock()
        issue = OperationalIssue(
            id="inbound:" + keys.digest(receipt.id),
            scope=self.scope,
            kind="inbound_failure",
            affected_id=receipt.id,
            owner_id=self.identity.admin_user_id,
            due_at=now,
            reason="attempts_exhausted",
            created_at=now,
            updated_at=now,
            work_clock=OperationalClock(next_action_at=now, work_lane="operational"),
        )
        changed = InboundReceipt.model_validate(
            receipt.model_dump()
            | {
                "version": receipt.version + 1,
                "updated_at": now,
                "state": "needs_attention",
                "processing_claim": None,
                "review_obligation_id": issue.id,
                "work_clock": OperationalClock(
                    next_action_at=now + self.policy.application_review_interval,
                    work_lane="ingress",
                    last_error_code="attempts_exhausted",
                ),
            }
        )
        return self._commit(
            AccountCommand(command_id="attention:" + receipt.id, actor=receipt.principal),
            (issue, changed),
            claim=claim,
            result_code="needs_attention",
            complete_receipt=False,
        )

    def finish_receipt(
        self,
        receipt: InboundReceipt,
        claim: Claim,
        *,
        result_code: str,
        template_id: str | None = None,
        text: str | None = None,
        safety: bool = False,
    ) -> CommitResult:
        assert receipt.principal is not None
        command = AccountCommand(
            command_id="route:" + receipt.id,
            actor=receipt.principal,
        )
        intents: tuple[OutboundIntent, ...] = ()
        if template_id:
            source = to_record(receipt, receipt.scope)
            # The receipt becomes the authority source only with this completion commit.
            source_version = receipt.version + 1
            audience: Literal["applicant", "admin", "doctor"] = "applicant"
            epoch = None
            if receipt.principal.actor_kind == "doctor" and receipt.principal.doctor_id:
                doctor = self.doctor(receipt.principal.doctor_id)
                if doctor:
                    source, audience, epoch = (
                        to_record(doctor, doctor.scope),
                        "doctor",
                        doctor.auth_epoch,
                    )
                    source_version = doctor.version
            elif receipt.principal.actor_kind == "admin":
                audience = "admin"
            intents = (
                self.intent(
                    source,
                    template_id,
                    receipt.source_subject,
                    receipt.source_chat,
                    audience,
                    logical_suffix=receipt.id,
                    text=text,
                    auth_epoch=epoch,
                    safety=safety,
                    source_version=source_version,
                ),
            )
        return self._commit(command, intents=intents, claim=claim, result_code=result_code)
