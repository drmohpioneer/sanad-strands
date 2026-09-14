"""Short-lived Telegram login exchange and revocable, rotated browser sessions."""

import hmac
from typing import Literal, overload
from uuid import uuid4

from pydantic import BaseModel, SecretStr

from sanad.auth.commands import (
    ExchangeRefused,
    IssueAdminLogin,
    IssueDoctorLogin,
    IssuePatientLogin,
)
from sanad.auth.service import IdentityService, InternalCommand, internal_actor, revise
from sanad.auth.tokens import issue_token, token_hash
from sanad.domain import PatientScope, Principal
from sanad.domain.boundaries import _BoundaryValue
from sanad.store import keys
from sanad.store.records import (
    AdminAccount,
    Claim,
    CommitResult,
    Forbidden,
    IdentityRead,
    LoginExchange,
    PatientBinding,
    PreSession,
)
from sanad.store.records import (
    AnyWebSession as WebSession,
)
from sanad.store.records import WebSession as ClinicalWebSession


class PreSessionIssued(_BoundaryValue):
    cookie: SecretStr
    csrf: SecretStr


class SessionIssued(_BoundaryValue):
    status: Literal["consumed"] = "consumed"
    cookie: SecretStr
    csrf: SecretStr
    session: WebSession
    destination: str | None = None


def log_revocation(reason: str, path: str, session_id: str) -> None:
    import logging
    import re

    safe_path = re.sub(
        r"/(ad|d|pl|p|patients|evidence|claims|applications|doctors|questions|media)/[^/]+",
        r"/\1/<redacted>",
        path.split("?", 1)[0],
    )
    logging.getLogger("sanad.web").info(
        "session revoked reason=%s path=%s session=%s", reason, safe_path, session_id[:8]
    )


class LoginService(IdentityService):
    def issue(
        self,
        command: IssueDoctorLogin | IssuePatientLogin | IssueAdminLogin,
        *,
        claim: Claim | None = None,
    ) -> CommitResult:
        if isinstance(command, IssueAdminLogin):
            return self.issue_admin(command, claim=claim)
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
        removal_patient = None
        if isinstance(command, IssueDoctorLogin) and command.removal_patient_id:
            from sanad.store.records import Patient

            target = PatientScope(doctor_id=doctor.id, patient_id=command.removal_patient_id)
            removal_patient = self.load(target, "patient", target.patient_id, Patient)
            target_profile = self.store.get_patient_profile(target)
            if removal_patient is None or target_profile is None or target_profile.removed_at:
                return Forbidden()
            fields["removal_destination"] = "/a/patients/" + removal_patient.id
        now, token = self.clock(), issue_token()
        if removal_patient:
            fields["removal_binding_hash"] = keys.digest(
                f"{token.hash}|{doctor.id}|{fields['removal_destination']}"
            )
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
                "scribe_removal_link" if removal_patient else "doctor_login_link",
                actor.subject,
                "doctor",
                fields={"link": link, "name": removal_patient.display_name}
                if removal_patient
                else {"link": link},
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

    def admin_actor(self, subject: str, epoch: int | None) -> Principal:
        return Principal(
            subject=subject,
            user_id=subject,
            bot_id=self.scope.bot_id,
            actor_kind="admin",
            verified_roles=frozenset({"admin"}),
            auth_epoch=epoch,
        )

    def issue_admin(self, command: IssueAdminLogin, *, claim: Claim | None = None) -> CommitResult:
        actor = command.actor
        auth = self.store.authorize(self.scope.bot_id, actor.subject)
        if (
            actor.bot_id != self.scope.bot_id
            or "admin" not in actor.verified_roles
            or "admin" not in auth.principal.verified_roles
        ):
            return Forbidden()
        prior = self.store.lookup_command(self.envelope(command, claim))
        if prior is not None:
            return prior
        now, token = self.clock(), issue_token()
        account = self.load(self.scope, "admin_account", actor.subject, AdminAccount)
        models: list[BaseModel] = []
        if account is None:
            account = AdminAccount(
                id=actor.subject, scope=self.scope, created_at=now, updated_at=now
            )
            models.append(account)
        exchange = LoginExchange(
            id=token.hash,
            entity_type="admin_login",
            scope=self.scope,
            intended_role="admin",
            subject=actor.subject,
            doctor_id=None,
            auth_epoch=account.auth_epoch,
            issued_at=now,
            expires_at=now + self.policy.login_ttl,
            policy_version=self.policy.version,
            created_at=now,
            updated_at=now,
        )
        head, old_head, condition = self.token_head("admin_login", actor.subject, token.hash)
        models.extend([exchange, head])
        if old_head:
            old = self.load(self.scope, "admin_login", old_head.token_hash, LoginExchange)
            if old and old.state == "issued":
                models.append(revise(old, now, state="revoked"))
        intent = self.account_intent(
            exchange,
            "admin_login_link",
            actor.subject,
            "admin",
            fields={"link": f"{self.public_base_url}/ad/{token.secret.get_secret_value()}"},
            auth_epoch=account.auth_epoch,
            expires_at=exchange.expires_at,
        )
        return self.commit(
            command,
            tuple(models),
            (intent,),
            reads=(
                condition,
                IdentityRead(
                    scope=self.scope,
                    entity_type="admin_account",
                    id=actor.subject,
                    version=None if account in models else account.version,
                ),
            ),
            claim=claim,
        )

    def revoke_admin(
        self, subject: str, command_id: str, *, epoch: int | None = None, claim: Claim | None = None
    ) -> CommitResult:
        auth = self.store.authorize(self.scope.bot_id, subject)
        account = self.load(self.scope, "admin_account", subject, AdminAccount)
        if (
            "admin" not in auth.principal.verified_roles
            or account is None
            or (epoch is not None and epoch != account.auth_epoch)
        ):
            return Forbidden()
        return self.commit(
            InternalCommand(
                type="RevokeAdminSessions",
                target_id=subject,
                command_id=command_id,
                actor=self.admin_actor(subject, account.auth_epoch),
            ),
            (revise(account, self.clock(), auth_epoch=account.auth_epoch + 1),),
            claim=claim,
        )

    def revoke_roles(
        self, subject: str, command_id: str, *, claim: Claim | None = None
    ) -> CommitResult:
        """Revoke only the sender's doctor/admin sessions, discovered from login audits."""
        from sanad.steward.types import records
        from sanad.store.records import AuditEvent

        auth = self.store.authorize(self.scope.bot_id, subject)
        roles = auth.principal.verified_roles & {"doctor", "admin"}
        if not roles:
            return Forbidden()
        command = InternalCommand(
            type="FinishIdentityReceipt",
            target_id=subject,
            command_id=command_id,
            actor=auth.principal,
        )
        prior = self.store.lookup_command(self.envelope(command, claim))
        if prior is not None:
            return prior
        session_ids = {
            ref.id
            for row in records(self.store, self.scope, "audit_event")
            for ref in AuditEvent.model_validate(row.body).aggregate_refs
            if ref.entity_type == "web_session"
        }
        for id in session_ids:
            session = self.load(self.scope, "web_session", id, WebSession)
            if session and session.subject == subject and session.role in roles:
                self.revoke(session, reason="logout", path="telegram/logout")
                current = self.load(self.scope, "web_session", id, WebSession)
                if current and current.revoked_at is None:
                    return Forbidden()
        # Preserve the administrator's epoch invalidation of pending exchanges.
        if "admin" in roles and auth.admin_epoch is not None:
            result = self.revoke_admin(subject, command_id + ":admin")
            if result.status not in {"accepted", "duplicate"}:
                return result
        # Receipt completion and the solicited acknowledgement are committed by routing.
        return self.commit(command)

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
        role: Literal["doctor", "patient", "admin"],
        raw: str,
        pre_cookie: str,
        csrf: str,
        *,
        previous_cookie: str = "",
    ) -> SessionIssued | ExchangeRefused:
        digest, pre_hash = token_hash(raw), token_hash(pre_cookie)
        if digest is None or pre_hash is None:
            return ExchangeRefused(status="malformed")
        pre = self.load(self.scope, "pre_session", pre_hash, PreSession)
        now = self.clock()
        if pre is None or pre.consumed_at or pre.expires_at <= now:
            return ExchangeRefused(status="no_pre_session")
        if not hmac.compare_digest(keys.digest(csrf), pre.csrf_secret_ref):
            return ExchangeRefused(status="bad_csrf")
        purpose = role + "_login"
        exchange = self.load(self.scope, purpose, digest, LoginExchange)
        if exchange is None:
            return ExchangeRefused(status="unknown_link")
        if exchange.state == "consumed":
            return ExchangeRefused(status="already_used")
        if exchange.state != "issued" or exchange.expires_at <= now:
            return ExchangeRefused(status="expired")
        auth = self.store.authorize(self.scope.bot_id, exchange.subject)
        if (
            role == "admin"
            and (
                "admin" not in auth.principal.verified_roles
                or auth.admin_epoch != exchange.auth_epoch
            )
        ) or (
            role != "admin"
            and (
                auth.principal.actor_kind != role
                or auth.auth_epoch != exchange.auth_epoch
                or auth.doctor_status != "approved"
            )
        ):
            return ExchangeRefused(status="wrong_account")
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
                actor=self.admin_actor(auth.principal.subject, auth.admin_epoch)
                if role == "admin"
                else auth.principal,
            ),
            tuple(models),
        )
        if result.status != "accepted":
            return ExchangeRefused(status="commit_failed")
        if previous and previous.revoked_at is None:
            log_revocation("session_replaced", "auth/exchange", previous.id)
        return SessionIssued(
            cookie=cookie.secret,
            csrf=secret.secret,
            session=session,
            destination=exchange.removal_destination,
        )

    def session(self, raw_cookie: str) -> WebSession | None:
        digest = token_hash(raw_cookie)
        return self.load(self.scope, "web_session", digest, WebSession) if digest else None

    def revoke(self, session: WebSession, *, reason: str = "explicit", path: str = "auth") -> None:
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
                (revise(current, self.clock(), revoked_at=self.clock(), revocation_reason=reason),),
            )
            if result.status == "accepted":
                log_revocation(reason, path, current.id)
                return

    @overload
    def require(
        self, raw_cookie: str, role: Literal["doctor", "patient"], *, path: str = "auth"
    ) -> ClinicalWebSession | None: ...

    @overload
    def require(
        self, raw_cookie: str, role: Literal["doctor", "patient", "admin"], *, path: str = "auth"
    ) -> WebSession | None: ...

    def require(
        self, raw_cookie: str, role: Literal["doctor", "patient", "admin"], *, path: str = "auth"
    ) -> WebSession | None:
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
            self.revoke(session, reason="role_or_expiry", path=path)
            return None
        from sanad.store.records import AuthorizationUnavailable
        from sanad.store.retry import BACKOFF, sleep

        for delay in (0, *BACKOFF):
            if delay:
                sleep(delay)
            snapshot = self.store.web_session_snapshot(session)
            if snapshot is None:
                self.revoke(session, reason="binding_consent_or_authority", path=path)
                return None
            now = self.clock()
            if snapshot.revoked_at:
                return None
            if (
                snapshot.role != role
                or min(snapshot.idle_expires_at, snapshot.absolute_expires_at) <= now
            ):
                self.revoke(snapshot, reason="role_or_expiry", path=path)
                return None
            auth = self.store.authorize(self.scope.bot_id, snapshot.subject)
            changed = revise(
                snapshot,
                now,
                last_seen_at=now,
                idle_expires_at=min(now + self.policy.idle_ttl, snapshot.absolute_expires_at),
            )
            result = self.commit(
                InternalCommand(
                    type="TouchWebSession",
                    target_id=snapshot.id,
                    command_id=uuid4().hex,
                    actor=self.admin_actor(auth.principal.subject, auth.admin_epoch)
                    if role == "admin"
                    else auth.principal,
                ),
                (changed,),
            )
            if result.status == "accepted":
                return changed
            session = snapshot
        # A failed touch is not revocation; validate again before skipping the touch.
        snapshot = self.store.web_session_snapshot(session)
        if snapshot is None:
            self.revoke(session, reason="binding_consent_or_authority", path=path)
            return None
        current = self.session(raw_cookie)
        if current is None or current.revoked_at:
            return None
        if current.consent_version != snapshot.consent_version:
            raise AuthorizationUnavailable("session_pin_busy")
        if min(current.idle_expires_at, current.absolute_expires_at) <= self.clock():
            self.revoke(current, reason="role_or_expiry", path=path)
            return None
        return snapshot
