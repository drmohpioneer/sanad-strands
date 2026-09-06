"""Short-lived Telegram login exchange and revocable, rotated browser sessions."""

import hmac
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, SecretStr

from sanad.auth.commands import ExchangeRefused, IssueDoctorLogin, IssuePatientLogin
from sanad.auth.service import IdentityService, InternalCommand, internal_actor, revise
from sanad.auth.tokens import issue_token, token_hash
from sanad.domain import PatientScope
from sanad.domain.boundaries import _BoundaryValue
from sanad.store import keys
from sanad.store.records import (
    Claim,
    CommitResult,
    Forbidden,
    LoginExchange,
    PatientBinding,
    PreSession,
    WebSession,
)


class PreSessionIssued(_BoundaryValue):
    cookie: SecretStr
    csrf: SecretStr


class SessionIssued(_BoundaryValue):
    status: Literal["consumed"] = "consumed"
    cookie: SecretStr
    csrf: SecretStr
    session: WebSession


class LoginService(IdentityService):
    def issue(
        self, command: IssueDoctorLogin | IssuePatientLogin, *, claim: Claim | None = None
    ) -> CommitResult:
        role: Literal["doctor", "patient"] = (
            "doctor" if isinstance(command, IssueDoctorLogin) else "patient"
        )
        actor = command.actor
        auth = self.store.authorize(self.scope.bot_id, actor.subject)
        if (
            actor.bot_id != self.scope.bot_id
            or actor.actor_kind != role
            or auth.principal.actor_kind != role
            or actor.doctor_id != auth.principal.doctor_id
            or actor.auth_epoch != auth.auth_epoch
            or not auth.binding
            or auth.private_chat_id != actor.subject
        ):
            return Forbidden()
        doctor = self.accounts.doctor(actor.doctor_id or "")
        if doctor is None or doctor.status != "approved":
            return Forbidden()
        prior = self.store.lookup_command(self.envelope(command, claim))
        if prior is not None:
            return prior
        binding = None
        fields: dict[str, object] = {}
        profile = None
        if role == "patient":
            scope = PatientScope(doctor_id=doctor.id, patient_id=actor.patient_id or "")
            from sanad.store.records import Patient

            patient = self.load(scope, "patient", scope.patient_id, Patient)
            if patient is None or not patient.active_binding_id:
                return Forbidden()
            binding = self.load(scope, "patient_binding", patient.active_binding_id, PatientBinding)
            profile = self.store.get_patient_profile(scope)
            if binding is None or profile is None or not profile.consent_active:
                return Forbidden()
            fields = dict(
                patient_id=scope.patient_id,
                binding_id=binding.id,
                binding_epoch=binding.binding_epoch,
                consent_version=binding.consent_version,
            )
        now, token = self.clock(), issue_token()
        purpose: Literal["doctor_login", "patient_login"] = (
            "doctor_login" if role == "doctor" else "patient_login"
        )
        exchange = LoginExchange.model_validate(
            dict(
                id=token.hash,
                entity_type=purpose,
                scope=self.scope,
                intended_role=role,
                subject=actor.subject,
                doctor_id=doctor.id,
                auth_epoch=doctor.auth_epoch,
                issued_at=now,
                expires_at=now + self.policy.login_ttl,
                policy_version=self.policy.version,
                created_at=now,
                updated_at=now,
                **fields,
            )
        )
        head, old_head, condition = self.token_head(purpose, actor.subject, token.hash)
        models: list[BaseModel] = [exchange, head]
        if old_head:
            old = self.load(self.scope, purpose, old_head.token_hash, LoginExchange)
            if old and old.state == "issued":
                models.append(revise(old, now, state="revoked"))
        path = "d" if role == "doctor" else "pl"
        link = f"{self.public_base_url}/{path}/{token.secret.get_secret_value()}"
        intent = (
            self.account_intent(
                exchange,
                "doctor_login_link",
                actor.subject,
                "doctor",
                fields={"link": link},
                auth_epoch=doctor.auth_epoch,
                expires_at=exchange.expires_at,
            )
            if role == "doctor"
            else self.patient_intent(
                binding,
                profile,
                doctor,
                "patient_login_link",
                fields={"link": link},
                credential_hash=exchange.id,
                expires_at=exchange.expires_at,
            )
            if binding and profile
            else None
        )
        if intent is None:
            return Forbidden()
        return self.commit(command, tuple(models), (intent,), reads=(condition,), claim=claim)

    def pre_session(self) -> PreSessionIssued | None:
        cookie, csrf, now = issue_token(), issue_token(), self.clock()
        pre = PreSession(
            id=cookie.hash,
            scope=self.scope,
            csrf_secret_ref=csrf.hash,
            expires_at=now + self.policy.login_ttl,
            created_at=now,
            updated_at=now,
        )
        result = self.commit(
            InternalCommand(
                type="CreatePreSession",
                target_id=pre.id,
                command_id=uuid4().hex,
                actor=internal_actor(self.scope.bot_id),
            ),
            (pre,),
        )
        return (
            PreSessionIssued(cookie=cookie.secret, csrf=csrf.secret)
            if result.status == "accepted"
            else None
        )

    def exchange(
        self,
        role: Literal["doctor", "patient"],
        raw: str,
        pre_cookie: str,
        csrf: str,
        *,
        previous_cookie: str = "",
    ) -> SessionIssued | ExchangeRefused:
        digest, pre_hash = token_hash(raw), token_hash(pre_cookie)
        if digest is None or pre_hash is None:
            return ExchangeRefused()
        pre = self.load(self.scope, "pre_session", pre_hash, PreSession)
        now = self.clock()
        if (
            pre is None
            or pre.consumed_at
            or pre.expires_at <= now
            or not hmac.compare_digest(keys.digest(csrf), pre.csrf_secret_ref)
        ):
            return ExchangeRefused()
        purpose = "doctor_login" if role == "doctor" else "patient_login"
        exchange = self.load(self.scope, purpose, digest, LoginExchange)
        if exchange is None:
            return ExchangeRefused()
        if exchange.state == "consumed":
            return ExchangeRefused(status="already_used")
        if exchange.state != "issued" or exchange.expires_at <= now:
            return ExchangeRefused()
        auth = self.store.authorize(self.scope.bot_id, exchange.subject)
        if (
            auth.principal.actor_kind != role
            or auth.auth_epoch != exchange.auth_epoch
            or auth.doctor_status != "approved"
        ):
            return ExchangeRefused()
        cookie, secret = issue_token(), issue_token()
        session = WebSession(
            id=cookie.hash,
            scope=self.scope,
            role=role,
            subject=exchange.subject,
            doctor_id=exchange.doctor_id,
            patient_id=exchange.patient_id,
            binding_id=exchange.binding_id,
            binding_epoch=exchange.binding_epoch,
            consent_version=exchange.consent_version,
            auth_epoch=exchange.auth_epoch,
            csrf_secret_ref=secret.hash,
            issued_at=now,
            last_seen_at=now,
            idle_expires_at=now + self.policy.idle_ttl,
            absolute_expires_at=now + self.policy.absolute_ttl,
            created_at=now,
            updated_at=now,
        )
        models: list[BaseModel] = [
            revise(exchange, now, state="consumed", consumed_at=now),
            revise(pre, now, consumed_at=now),
            session,
        ]
        previous = self.session(previous_cookie)
        if previous and previous.revoked_at is None:
            models.append(revise(previous, now, revoked_at=now))
        result = self.commit(
            InternalCommand(
                type="ExchangeLogin",
                target_id=digest,
                command_id=uuid4().hex,
                actor=auth.principal,
            ),
            tuple(models),
        )
        if result.status != "accepted":
            current = self.load(self.scope, purpose, digest, LoginExchange)
            return ExchangeRefused(
                status="already_used" if current and current.state == "consumed" else "forbidden"
            )
        return SessionIssued(cookie=cookie.secret, csrf=secret.secret, session=session)

    def session(self, raw_cookie: str) -> WebSession | None:
        digest = token_hash(raw_cookie)
        return self.load(self.scope, "web_session", digest, WebSession) if digest else None

    def revoke(self, session: WebSession) -> None:
        for _ in range(3):
            current = self.load(self.scope, "web_session", session.id, WebSession)
            if current is None or current.revoked_at is not None:
                return
            result = self.commit(
                InternalCommand(
                    type="RevokeWebSession",
                    target_id=session.id,
                    command_id=uuid4().hex,
                    actor=internal_actor(self.scope.bot_id),
                ),
                (revise(current, self.clock(), revoked_at=self.clock()),),
            )
            if result.status == "accepted":
                return

    def require(self, raw_cookie: str, role: Literal["doctor", "patient"]) -> WebSession | None:
        session = self.session(raw_cookie)
        if session is None:
            return None
        now = self.clock()
        if (
            session.revoked_at
            or session.role != role
            or session.idle_expires_at <= now
            or session.absolute_expires_at <= now
        ):
            self.revoke(session)
            return None
        auth = self.store.authorize(self.scope.bot_id, session.subject)
        if (
            auth.principal.actor_kind != role
            or auth.auth_epoch != session.auth_epoch
            or auth.doctor_status != "approved"
        ):
            self.revoke(session)
            return None
        changed = revise(
            session,
            now,
            last_seen_at=now,
            idle_expires_at=min(now + self.policy.idle_ttl, session.absolute_expires_at),
        )
        result = self.commit(
            InternalCommand(
                type="TouchWebSession",
                target_id=session.id,
                command_id=uuid4().hex,
                actor=auth.principal,
            ),
            (changed,),
        )
        if result.status != "accepted":
            self.revoke(session)
            return None
        return changed
