"""Shared identity command transaction builder and deterministic message intents."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, JsonValue

from sanad.accounts.commands import AccountCommand
from sanad.accounts.service import AccountService
from sanad.auth.commands import AuthPolicy
from sanad.auth.tokens import consume_token
from sanad.domain import PatientScope, Principal
from sanad.domain.language import default_language
from sanad.store import keys
from sanad.store.keys import Scope
from sanad.store.records import (
    AuditEvent,
    Claim,
    CommandEnvelope,
    CommitRequest,
    CommitResult,
    Doctor,
    IdentityRead,
    OperationalClock,
    OutboundIntent,
    Patient,
    PatientProfile,
    ReceiptCompletion,
    TokenHead,
    WorkerCapability,
    canonical_json,
    from_record,
    model_scope,
    to_record,
)


def revise[T: BaseModel](model: T, now: datetime, **changes: object) -> T:
    body = model.model_dump()
    body.update(version=body["version"] + 1, updated_at=now, **changes)
    if body.get("entity_type") == "patient":
        body["record_version"] = body["version"]
    return type(model).model_validate(body)


def read_of(model: BaseModel) -> IdentityRead:
    row = to_record(model, model_scope(model))
    return IdentityRead(
        scope=model_scope(model), entity_type=row.entity_type, id=row.id, version=row.version
    )


DEFAULT_AUTH_POLICY = AuthPolicy()


class IdentityService:
    def __init__(
        self,
        accounts: AccountService,
        public_base_url: str,
        *,
        policy: AuthPolicy = DEFAULT_AUTH_POLICY,
    ):
        self.accounts = accounts
        self.store, self.clock, self.scope = accounts.store, accounts.clock, accounts.scope
        self.public_base_url, self.policy = public_base_url, policy

    def load[T: BaseModel](self, scope: Scope, kind: str, id: str, model: type[T]) -> T | None:
        if not id.strip():
            return None
        row = self.store.get(scope, kind, id)
        return from_record(row, model) if row else None

    def doctor(self, actor: Principal) -> Doctor | None:
        auth = self.store.authorize(self.scope.bot_id, actor.subject)
        if (
            actor.bot_id != self.scope.bot_id
            or actor.actor_kind != "doctor"
            or "doctor" not in actor.verified_roles
            or auth.principal.actor_kind != "doctor"
            or actor.doctor_id != auth.principal.doctor_id
            or actor.auth_epoch != auth.auth_epoch
        ):
            return None
        return self.accounts.doctor(actor.doctor_id or "")

    def envelope(self, command: AccountCommand, claim: Claim | None = None) -> CommandEnvelope:
        return CommandEnvelope(
            command_id=command.command_id,
            principal=command.actor,
            scope=self.scope,
            requested_at=self.clock(),
            payload=command.model_dump(mode="json"),
            work_claim=claim,
            worker=WorkerCapability(
                service_subject="auth",
                permitted_lanes=frozenset({"account", "claim"}),
                resolved_scope=self.scope,
                auth_expiry=self.clock() + self.accounts.policy.operations.claim_ttl,
                invocation_id=command.command_id,
            ),
        )

    def commit(
        self,
        command: AccountCommand,
        models: tuple[BaseModel, ...] = (),
        intents: tuple[OutboundIntent, ...] = (),
        *,
        reads: tuple[IdentityRead, ...] = (),
        claim: Claim | None = None,
        domain_events: tuple[AuditEvent, ...] = (),
    ) -> CommitResult:
        now = self.clock()
        rows = tuple(to_record(m, model_scope(m)) for m in models)
        conditions = {(r.entity_type, r.id): r for r in reads}
        for model, row in zip(models, rows, strict=True):
            if row.version > 1 and (row.entity_type, row.id) not in conditions:
                conditions[row.entity_type, row.id] = IdentityRead(
                    scope=model_scope(model),
                    entity_type=row.entity_type,
                    id=row.id,
                    version=row.version - 1,
                )
        event_id = keys.digest("identity:" + command.command_id)
        event = AuditEvent(
            id=event_id,
            event_id=event_id,
            command_id=command.command_id,
            scope=self.scope,
            event_type=str(command.model_dump().get("type", type(command).__name__)),
            actor=command.actor,
            accepted_at=now,
            created_at=now,
            updated_at=now,
            aggregate_refs=tuple(r.ref for r in rows),
            after_versions=tuple(r.ref for r in rows),
            before_versions=tuple(r for r in command.expected_versions),
            policy_versions=(self.policy.version,),
        )
        outgoing = tuple(to_record(i, i.scope) for i in intents)
        events = (to_record(event, self.scope), *(to_record(e, self.scope) for e in domain_events))
        request = CommitRequest(
            command=self.envelope(command, claim),
            puts=rows,
            intents=outgoing,
            events=events,
            identity_reads=tuple(conditions.values()),
            expected=tuple(r.ref for r in (*rows, *events, *outgoing)),
            receipt_completion=ReceiptCompletion(
                claim=claim, result_event_ids=tuple(e.id for e in events)
            )
            if claim
            else None,
        )
        if request.command.payload.get("type") == "ConfirmPatientClaim":
            return self.store.confirm_claim(request)
        return (
            consume_token(self.store, request)
            if request.command.payload.get("type") == "ExchangeLogin"
            else self.store.commit_account(request)
        )

    def account_intent(
        self,
        source: BaseModel,
        template: str,
        subject: str,
        audience: Literal["applicant", "doctor", "admin"],
        *,
        fields: dict[str, str] | None = None,
        text: str | None = None,
        markup: JsonValue = None,
        suffix: str = "",
        expires_at: datetime | None = None,
        auth_epoch: int | None = None,
    ) -> OutboundIntent:
        intent = self.accounts.intent(
            to_record(source, model_scope(source)),
            template,
            subject,
            subject,
            audience,
            fields=fields,
            text=text,
            markup=markup,
            logical_suffix=suffix,
            auth_epoch=auth_epoch,
        )
        if expires_at:
            return OutboundIntent.model_validate(intent.model_dump() | {"expires_at": expires_at})
        return intent

    def patient_intent(
        self,
        source: BaseModel,
        profile: PatientProfile,
        doctor: Doctor,
        template: str,
        *,
        fields: dict[str, str] | None = None,
        credential_hash: str | None = None,
        expires_at: datetime | None = None,
    ) -> OutboundIntent:
        row = to_record(source, model_scope(source))
        scope = PatientScope(doctor_id=profile.doctor_id, patient_id=profile.patient_id)
        now = self.clock()
        logical = keys.digest(f"{row.id}:{template}:{row.version}:{credential_hash or ''}")
        patient = self.load(scope, "patient", profile.patient_id, Patient)
        payload: dict[str, JsonValue] = {
            "text": self.accounts.present(
                template, patient.language if patient else default_language, fields or {}
            )
        }
        return OutboundIntent(
            id=logical,
            scope=scope,
            scope_kind="patient",
            audience="patient",
            logical_key=logical,
            source_event_ids=(row.id,),
            source_versions=(row.ref,),
            recipient_ref=profile.recipient_ref or "",
            recipient_subject=profile.recipient_subject,
            bot_id=self.scope.bot_id,
            notification_purpose="solicited_reply",
            eligibility_class="routine",
            payload_ref="patient_login:" + credential_hash
            if credential_hash
            else "binding:" + row.id,
            payload=payload,
            payload_digest=keys.digest(canonical_json(payload).decode()),
            conversation_sequence=0,
            expires_at=expires_at or now + self.policy.invitation_ttl,
            status="queued",
            delivery_lease_seconds=int(self.accounts.policy.operations.lease_ttl.total_seconds()),
            work_clock=OperationalClock(next_action_at=now, work_lane="delivery"),
            created_at=now,
            updated_at=now,
            template_id=template,
            recipient_auth_epoch_seen=0,
            doctor_auth_epoch_seen=doctor.auth_epoch,
            binding_epoch_seen=profile.binding_epoch,
            consent_version_seen=profile.consent_version,
            safety_epoch_seen=profile.safety_epoch,
            delivery_epoch_seen=profile.delivery_epoch,
        )

    def token_head(
        self,
        purpose: Literal["doctor_login", "patient_login", "admin_login", "invitation"],
        owner_key: str,
        token_hash: str,
    ) -> tuple[TokenHead, TokenHead | None, IdentityRead]:
        id = keys.digest(f"{purpose}:{owner_key}")
        old = self.load(self.scope, "token_head", id, TokenHead)
        now = self.clock()
        head = TokenHead(
            id=id,
            scope=self.scope,
            purpose=purpose,
            owner_key=owner_key,
            token_hash=token_hash,
            version=old.version + 1 if old else 1,
            created_at=old.created_at if old else now,
            updated_at=now,
        )
        return (
            head,
            old,
            IdentityRead(
                scope=self.scope,
                entity_type="token_head",
                id=id,
                version=old.version if old else None,
            ),
        )


class InternalCommand(AccountCommand):
    type: Literal[
        "RevokeAdminSessions",
        "ExchangeLogin",
        "CreatePreSession",
        "TouchWebSession",
        "RevokeWebSession",
        "ExpireInvitation",
        "FinishIdentityReceipt",
    ]
    target_id: str


def internal_actor(bot_id: str) -> Principal:
    return Principal(subject="0", user_id="0", bot_id=bot_id, actor_kind="unknown")
