"""Consent plus intended-person confirmation, with atomic exclusive activation."""

from collections.abc import Callable
from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, JsonValue, SecretStr

from sanad.auth.commands import (
    ClaimInvitation,
    ConfirmPatientClaim,
    ConsentPolicy,
    CreatePatientStub,
    IssuedInvitation,
    IssueInvitation,
    RecordConsent,
    RejectClaim,
    RevokeBinding,
)
from sanad.auth.service import IdentityService, InternalCommand, internal_actor, read_of, revise
from sanad.auth.tokens import issue_token
from sanad.channels.telegram import wording
from sanad.domain import PatientScope, Principal, VersionRef
from sanad.store import keys
from sanad.store.records import (
    Claim,
    ClaimCallback,
    CommitResult,
    Consent,
    Forbidden,
    IdentityRead,
    Invitation,
    OperationalClock,
    OutboundIntent,
    Patient,
    PatientBinding,
    PatientClaim,
    PatientProfile,
    StaleVersion,
    SubjectBinding,
    WebSession,
    canonical_json,
)


class ClaimService(IdentityService):
    consent_policy: Callable[[str], ConsentPolicy | None] = staticmethod(lambda doctor_id: None)

    def patient(self, doctor_id: str, patient_id: str) -> Patient | None:
        scope = PatientScope(doctor_id=doctor_id, patient_id=patient_id)
        return self.load(scope, "patient", patient_id, Patient)

    def invitation(self, hash: str) -> Invitation | None:
        return self.load(self.scope, "invitation", hash, Invitation)

    def patient_claim(self, id: str) -> PatientClaim | None:
        return self.load(self.scope, "patient_claim", id, PatientClaim)

    def create_stub(self, command: CreatePatientStub) -> CommitResult:
        doctor = self.doctor(command.actor)
        if doctor is None:
            return Forbidden()
        prior = self.store.lookup_command(self.envelope(command))
        if prior is not None:
            return prior
        patient, profile = self.prepare_stub(command, doctor.id, uuid4().hex, self.clock())
        return self.commit(command, (patient, profile))

    @staticmethod
    def prepare_stub(
        command: CreatePatientStub,
        doctor_id: str,
        patient_id: str,
        now: datetime,
    ) -> tuple[Patient, PatientProfile]:
        """The existing stub factory, usable inside a larger atomic doctor confirmation."""
        scope = PatientScope(doctor_id=doctor_id, patient_id=patient_id)
        patient = Patient(
            id=patient_id,
            scope=scope,
            display_name=command.display_name,
            language=command.language,
            timezone=command.timezone,
            created_at=now,
            updated_at=now,
        )
        profile = PatientProfile(
            id=patient_id,
            doctor_id=doctor_id,
            patient_id=patient_id,
            normalized_name=command.display_name.casefold(),
            created_at=now,
            updated_at=now,
        )
        return patient, profile

    def issue_invitation(self, command: IssueInvitation) -> IssuedInvitation | CommitResult:
        doctor = self.doctor(command.actor)
        if doctor is None:
            return Forbidden()
        prior = self.store.lookup_command(self.envelope(command))
        if prior is not None:
            return prior  # Plaintext is deliberately unrecoverable after its one return.
        patient = self.patient(doctor.id, command.patient_id)
        if patient is None or patient.contact_status == "active":
            return Forbidden()
        now, token = self.clock(), issue_token()
        head, old_head, condition = self.token_head(
            "invitation", keys.partition(patient.scope), token.hash
        )
        inv = Invitation(
            id=token.hash,
            scope=self.scope,
            doctor_id=doctor.id,
            patient_id=patient.id,
            issued_by=command.actor.subject,
            generation=patient.invitation_generation + 1,
            expires_at=now + self.policy.invitation_ttl,
            review_at=now + self.policy.invitation_ttl,
            policy_version=self.policy.version,
            work_clock=OperationalClock(
                next_action_at=now + self.policy.invitation_ttl, work_lane="claim"
            ),
            created_at=now,
            updated_at=now,
        )
        models: list[BaseModel] = [
            inv,
            head,
            revise(patient, now, invitation_id=inv.id, invitation_generation=inv.generation),
        ]
        if old_head:
            old = self.invitation(old_head.token_hash)
            if old and old.state in {"issued", "claimed"}:
                models.append(revise(old, now, state="revoked", work_clock=None))
                pending = self.patient_claim(old.pending_claim_id) if old.pending_claim_id else None
                if pending and pending.state == "pending":
                    models.append(revise(pending, now, state="rejected", work_clock=None))
        intents: tuple[OutboundIntent, ...] = ()
        if command.include_qr:
            link = f"{self.public_base_url}/p/{token.secret.get_secret_value()}"
            outgoing = self.account_intent(
                inv,
                "scribe_invitation",
                doctor.telegram_user_id,
                "doctor",
                fields={"link": link},
                expires_at=inv.expires_at,
                auth_epoch=doctor.auth_epoch,
            )
            payload = {**(outgoing.payload or {}), "qr_payload": link}
            intents = (
                OutboundIntent.model_validate(
                    outgoing.model_dump()
                    | {
                        "payload": payload,
                        "payload_digest": keys.digest(canonical_json(payload).decode()),
                    }
                ),
            )
        result = self.commit(command, tuple(models), intents, reads=(condition,))
        if result.status != "accepted":
            return result
        return IssuedInvitation(
            token=token.secret,
            qr_payload=SecretStr(f"{self.public_base_url}/p/{token.secret.get_secret_value()}"),
            expires_at=inv.expires_at,
            result=result,
        )

    def _absent_subject(self, subject: str) -> IdentityRead:
        return IdentityRead(
            scope=self.scope, entity_type="subject_binding", id=subject, version=None
        )

    def _buttons(
        self,
        patient_claim: PatientClaim,
        actions: tuple[Literal["accept", "decline", "confirm", "reject"], ...],
        actor: str,
        expected: tuple[VersionRef, ...],
    ) -> tuple[tuple[ClaimCallback, ...], JsonValue]:
        tokens = []
        buttons: list[JsonValue] = []
        labels = {
            "accept": wording.CONSENT_ACCEPT_BUTTON,
            "decline": wording.CONSENT_DECLINE_BUTTON,
            "confirm": wording.CLAIM_CONFIRM_BUTTON,
            "reject": wording.REJECT_BUTTON,
        }
        for action in actions:
            token, now = issue_token(), self.clock()
            tokens.append(
                ClaimCallback(
                    id=token.hash,
                    scope=self.scope,
                    claim_id=patient_claim.id,
                    actor_subject=actor,
                    action=action,
                    expected_versions=expected,
                    expires_at=patient_claim.review_at,
                    created_at=now,
                    updated_at=now,
                )
            )
            buttons.append(
                {"text": labels[action], "callback_data": token.secret.get_secret_value()}
            )
        return tuple(tokens), {"inline_keyboard": [buttons]}

    def claim_invitation(
        self, command: ClaimInvitation, *, claim: Claim | None = None
    ) -> CommitResult:
        actor, now = command.actor, self.clock()
        auth = self.store.authorize(self.scope.bot_id, actor.subject)
        if (
            actor.bot_id != self.scope.bot_id
            or actor.user_id != actor.subject
            or command.private_chat_id != actor.subject
            or auth.binding is not None
            or auth.principal.verified_roles
            or actor.verified_roles
        ):
            return Forbidden()
        prior = self.store.lookup_command(self.envelope(command, claim))
        if prior is not None:
            return prior
        inv = self.invitation(command.invitation_hash)
        if inv is None or inv.state != "issued" or inv.expires_at <= now:
            return Forbidden()
        patient = self.patient(inv.doctor_id, inv.patient_id)
        doctor = self.accounts.doctor(inv.doctor_id)
        policy = self.consent_policy(inv.doctor_id)
        if (
            patient is None
            or patient.contact_status == "active"
            or patient.invitation_id != inv.id
            or patient.invitation_generation != inv.generation
            or doctor is None
            or doctor.status != "approved"
            or policy is None
        ):
            return Forbidden()
        pending = PatientClaim(
            id=uuid4().hex,
            scope=self.scope,
            doctor_id=doctor.id,
            patient_id=patient.id,
            invitation_id=inv.id,
            invitation_generation=inv.generation,
            patient_version=patient.version,
            candidate_subject=actor.subject,
            private_chat_id=actor.subject,
            minimal_claim_identifier=actor.subject,
            binding_id=uuid4().hex,
            consent_policy=policy.model_dump(mode="json"),
            proof_method="verified_private_telegram",
            proof_reference=command.command_id,
            review_at=inv.expires_at,
            work_clock=OperationalClock(next_action_at=inv.expires_at, work_lane="claim"),
            created_at=now,
            updated_at=now,
        )
        claimed = revise(inv, now, state="claimed", pending_claim_id=pending.id)
        tokens, markup = self._buttons(
            pending,
            ("accept", "decline"),
            actor.subject,
            (VersionRef(entity_type="patient_claim", id=pending.id, version=1),),
        )
        intent = self.account_intent(
            pending,
            "consent_request",
            actor.subject,
            "applicant",
            fields={
                "doctor": doctor.name,
                "quiet_start": policy.quiet_hours[0],
                "quiet_end": policy.quiet_hours[1],
                "timezone": doctor.timezone,
                "retention": policy.retention,
                "clinic_contact": policy.clinic_contact,
            },
            markup=markup,
            expires_at=inv.expires_at,
        )
        return self.commit(
            command,
            (claimed, pending, *tokens),
            (intent,),
            reads=(read_of(patient), self._absent_subject(actor.subject)),
            claim=claim,
        )

    def _pending(self, id: str) -> tuple[PatientClaim, Invitation, Patient] | None:
        pending = self.patient_claim(id)
        if pending is None or pending.state != "pending" or pending.review_at <= self.clock():
            return None
        inv = self.invitation(pending.invitation_id)
        patient = self.patient(pending.doctor_id, pending.patient_id)
        if (
            inv is None
            or inv.state != "claimed"
            or inv.expires_at <= self.clock()
            or inv.pending_claim_id != pending.id
            or patient is None
            or patient.invitation_id != inv.id
            or patient.invitation_generation != inv.generation
        ):
            return None
        return pending, inv, patient

    def confirmation_versions(self, actor: Principal, id: str) -> tuple[VersionRef, ...]:
        pending = self.patient_claim(id)
        doctor = self.doctor(actor)
        if pending is None or doctor is None or pending.doctor_id != doctor.id:
            return ()
        found = self._pending(id)
        if found is None:
            return ()
        pending, inv, patient = found
        consent = self.load(patient.scope, "consent", pending.consent_id or "", Consent)
        if consent is None:
            return ()
        return tuple(
            VersionRef(entity_type=m.entity_type, id=m.id, version=m.version)
            for m in (pending, inv, patient, consent)
        )

    def record_consent(
        self,
        command: RecordConsent,
        *,
        callback: ClaimCallback | None = None,
        claim: Claim | None = None,
    ) -> CommitResult:
        found = self._pending(command.claim_id)
        if found is None:
            return Forbidden()
        pending, inv, patient = found
        if pending.candidate_subject != command.actor.subject or pending.consent_id is not None:
            return Forbidden()
        now = self.clock()
        doctor = self.accounts.doctor(pending.doctor_id)
        if doctor is None or doctor.status != "approved":
            return Forbidden()
        models: list[BaseModel] = []
        intents = []
        if command.accept:
            policy = ConsentPolicy.model_validate(pending.consent_policy)
            consent = Consent(
                id=uuid4().hex,
                scope=patient.scope,
                binding_id=pending.binding_id,
                policy_text_version=wording.CONSENT_TEXT_VERSION,
                accepted_at=now,
                accepted_by=command.actor.subject,
                quiet_hours=policy.quiet_hours,
                timezone=doctor.timezone,
                urgent_response_policy_id=policy.urgent_response_policy_id,
                policy_digest=keys.digest(canonical_json(pending.consent_policy).decode()),
                created_at=now,
                updated_at=now,
            )
            changed = revise(pending, now, consent_id=consent.id, consent_version=1)
            expected = tuple(
                VersionRef(entity_type=m.entity_type, id=m.id, version=m.version)
                for m in (changed, inv, patient, consent)
            )
            tokens, markup = self._buttons(
                changed, ("confirm", "reject"), doctor.telegram_user_id, expected
            )
            models.extend((consent, *tokens))
            intents.append(
                self.account_intent(
                    changed,
                    "claim_awaiting_doctor",
                    doctor.telegram_user_id,
                    "doctor",
                    fields={"claimant": pending.minimal_claim_identifier},
                    markup=markup,
                    auth_epoch=doctor.auth_epoch,
                    expires_at=inv.expires_at,
                )
            )
            patient_template = "consent_recorded_wait_doctor"
        else:
            changed = revise(pending, now, state="rejected", work_clock=None)
            intents.append(
                self.account_intent(
                    changed,
                    "claim_declined_doctor",
                    doctor.telegram_user_id,
                    "doctor",
                    auth_epoch=doctor.auth_epoch,
                )
            )
            patient_template = "consent_declined_ack"
        models.insert(0, changed)
        if callback:
            models.append(revise(callback, now, consumed_at=now))
        intents.append(
            self.account_intent(changed, patient_template, pending.candidate_subject, "applicant")
        )
        return self.commit(
            command,
            tuple(models),
            tuple(intents),
            reads=(read_of(inv), read_of(patient), self._absent_subject(command.actor.subject)),
            claim=claim,
        )

    def confirm(
        self,
        command: ConfirmPatientClaim,
        *,
        callback: ClaimCallback | None = None,
        claim: Claim | None = None,
        session: WebSession | None = None,
    ) -> CommitResult:
        doctor = self.doctor(command.actor)
        pending = self.patient_claim(command.claim_id)
        if doctor is None or pending is None or pending.doctor_id != doctor.id:
            return Forbidden()
        found = self._pending(pending.id)
        if found is None:
            return Forbidden()
        pending, inv, patient = found
        expected = self.confirmation_versions(command.actor, pending.id)
        if not expected or set(command.expected_versions) != set(expected):
            return StaleVersion(conflicts=("claim_versions",))
        if patient.version != pending.patient_version or patient.contact_status == "active":
            return StaleVersion(conflicts=("patient",))
        auth = self.store.authorize(self.scope.bot_id, pending.candidate_subject)
        if auth.binding is not None or auth.principal.verified_roles:
            return Forbidden()
        consent = self.load(patient.scope, "consent", pending.consent_id or "", Consent)
        profile = self.store.get_patient_profile(patient.scope)
        if (
            consent is None
            or profile is None
            or consent.withdrawn_at
            or not consent.routine_contact_enabled
        ):
            return Forbidden()
        now, epoch = self.clock(), patient.binding_epoch + 1
        approved = revise(
            pending,
            now,
            state="approved",
            work_clock=None,
            doctor_confirmed_by=command.actor.subject,
            doctor_confirmed_at=now,
            proof_method="doctor_confirmation",
            proof_reference=command.command_id,
        )
        binding = PatientBinding(
            id=pending.binding_id,
            scope=patient.scope,
            bot_id=self.scope.bot_id,
            subject=pending.candidate_subject,
            private_chat_id=pending.private_chat_id,
            binding_epoch=epoch,
            claim_id=pending.id,
            consent_id=consent.id,
            consent_version=consent.version,
            doctor_confirmed_by=command.actor.subject,
            doctor_confirmed_at=now,
            created_at=now,
            updated_at=now,
        )
        subject = SubjectBinding(
            id=keys.subject(self.scope.bot_id, pending.candidate_subject).pk,
            scope=self.scope,
            telegram_user_id=pending.candidate_subject,
            role_set=frozenset({"patient"}),
            doctor_id=doctor.id,
            patient_id=patient.id,
            private_chat_id=pending.private_chat_id,
            binding_epoch=epoch,
            created_at=now,
            updated_at=now,
        )
        active = revise(
            patient,
            now,
            contact_status="active",
            active_binding_id=binding.id,
            consent_id=consent.id,
            consent_version=consent.version,
            binding_epoch=epoch,
        )
        authority = revise(
            profile,
            now,
            binding_active=True,
            binding_epoch=epoch,
            consent_active=True,
            consent_version=consent.version,
            recipient_ref=pending.private_chat_id,
            recipient_subject=pending.candidate_subject,
            recipient_auth_epoch=0,
        )
        models: tuple[BaseModel, ...] = (
            revise(inv, now, state="consumed", consumed_at=now, work_clock=None),
            approved,
            binding,
            subject,
            active,
            authority,
        )
        if callback:
            models += (revise(callback, now, consumed_at=now),)
        reads: tuple[IdentityRead, ...] = (
            read_of(consent),
            self._absent_subject(pending.candidate_subject),
        )
        if session:
            reads += (read_of(session),)
        from sanad.contact.binding import prepare_binding

        bound_models, bound_intents, bound_events = prepare_binding(self, command, patient)
        models += bound_models
        result = self.commit(
            command,
            models,
            (self.patient_intent(binding, authority, doctor, "binding_confirmed"), *bound_intents),
            reads=reads,
            claim=claim,
            domain_events=bound_events,
        )
        if result.status in {"forbidden", "stale_version"}:
            if self.store.authorize(self.scope.bot_id, pending.candidate_subject).binding:
                return Forbidden()
        return result

    def reject(
        self,
        command: RejectClaim,
        *,
        callback: ClaimCallback | None = None,
        claim: Claim | None = None,
    ) -> CommitResult:
        doctor = self.doctor(command.actor)
        found = self._pending(command.claim_id)
        if found is None or doctor is None or found[0].doctor_id != doctor.id:
            return Forbidden()
        pending, inv, patient = found
        now = self.clock()
        rejected = revise(pending, now, state="rejected", work_clock=None)
        models: tuple[BaseModel, ...] = (
            rejected,
            revise(inv, now, state="revoked", work_clock=None),
        )
        if callback:
            models += (revise(callback, now, consumed_at=now),)
        return self.commit(
            command,
            models,
            (
                self.account_intent(
                    rejected, "claim_rejected", pending.candidate_subject, "applicant"
                ),
            ),
            reads=(read_of(patient),),
            claim=claim,
        )

    def revoke_binding(self, command: RevokeBinding) -> CommitResult:
        doctor = self.doctor(command.actor)
        patient = self.patient(doctor.id, command.patient_id) if doctor else None
        if doctor is None or patient is None or not patient.active_binding_id:
            return Forbidden()
        binding = self.load(
            patient.scope, "patient_binding", patient.active_binding_id, PatientBinding
        )
        profile = self.store.get_patient_profile(patient.scope)
        subject = (
            self.load(self.scope, "subject_binding", binding.subject, SubjectBinding)
            if binding
            else None
        )
        if binding is None or binding.status != "active" or subject is None or profile is None:
            return Forbidden()
        now, epoch, delivery = self.clock(), binding.binding_epoch + 1, profile.delivery_epoch + 1
        return self.commit(
            command,
            (
                revise(
                    binding,
                    now,
                    status="revoked",
                    binding_epoch=epoch,
                    reason_code=command.reason_code,
                ),
                revise(subject, now, status="revoked", binding_epoch=epoch),
                revise(
                    patient,
                    now,
                    contact_status="frozen",
                    binding_epoch=epoch,
                    delivery_epoch=delivery,
                ),
                revise(
                    profile,
                    now,
                    binding_active=False,
                    consent_active=False,
                    binding_epoch=epoch,
                    delivery_epoch=delivery,
                ),
            ),
        )

    def callback(
        self, hash: str, actor: Principal, command_id: str, *, claim: Claim | None = None
    ) -> CommitResult | None:
        token = self.load(self.scope, "claim_callback", hash, ClaimCallback)
        if token is None:
            return None  # Slice 05's callback family may own it.
        if (
            token.actor_subject != actor.subject
            or token.consumed_at
            or token.expires_at <= self.clock()
        ):
            return Forbidden()
        pending = self.patient_claim(token.claim_id)
        if pending is None:
            return Forbidden()
        for ref in token.expected_versions:
            scope = PatientScope(doctor_id=pending.doctor_id, patient_id=pending.patient_id)
            row = self.store.get(
                scope if ref.entity_type in {"patient", "consent"} else self.scope,
                ref.entity_type,
                ref.id,
            )
            if row is None or row.version != ref.version:
                return Forbidden()
        if token.action in {"accept", "decline"}:
            return self.record_consent(
                RecordConsent(
                    command_id=command_id,
                    actor=actor,
                    claim_id=pending.id,
                    accept=token.action == "accept",
                ),
                callback=token,
                claim=claim,
            )
        if token.action == "confirm":
            return self.confirm(
                ConfirmPatientClaim(
                    command_id=command_id,
                    actor=actor,
                    claim_id=pending.id,
                    expected_versions=token.expected_versions,
                ),
                callback=token,
                claim=claim,
            )
        return self.reject(
            RejectClaim(command_id=command_id, actor=actor, claim_id=pending.id),
            callback=token,
            claim=claim,
        )

    def expire(self, id: str) -> CommitResult:
        inv = self.invitation(id)
        now = self.clock()
        if inv is None or inv.state not in {"issued", "claimed"} or inv.expires_at > now:
            return Forbidden()
        changed = revise(inv, now, state="expired", work_clock=None)
        models: list[BaseModel] = [changed]
        pending = self.patient_claim(inv.pending_claim_id) if inv.pending_claim_id else None
        if pending and pending.state == "pending":
            models.append(revise(pending, now, state="expired", work_clock=None))
        doctor = self.accounts.doctor(inv.doctor_id)
        if doctor is None:
            return Forbidden()
        return self.commit(
            InternalCommand(
                type="ExpireInvitation",
                target_id=inv.id,
                command_id="expire:" + inv.id,
                actor=internal_actor(self.scope.bot_id),
            ),
            tuple(models),
            (
                self.account_intent(
                    changed,
                    "invitation_expired_doctor",
                    doctor.telegram_user_id,
                    "doctor",
                    auth_epoch=doctor.auth_epoch,
                ),
            ),
        )
