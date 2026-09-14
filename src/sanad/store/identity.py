"""Store-clock and authority guards for the released identity transaction family.

No auth/web/channel import: both backends use the same conditional read set.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sanad.domain import PatientScope, TenantScope
from sanad.store import keys
from sanad.store.keys import AccountScope, Key
from sanad.store.records import (
    MODELS,
    AdminAccount,
    ClaimCallback,
    CommitRequest,
    Consent,
    Doctor,
    Invitation,
    LoginExchange,
    Patient,
    PatientBinding,
    PatientClaim,
    PatientProfile,
    PreSession,
    StoredRecord,
    SubjectBinding,
    TokenHead,
    from_record,
    model_scope,
)
from sanad.store.records import (
    AnyWebSession as WebSession,
)

if TYPE_CHECKING:
    from sanad.store._base import Check, StoreBase
    from sanad.store.protocol import Store

DOCTOR_COMMANDS = {
    "CreatePatientStub",
    "IssueInvitation",
    "ConfirmPatientClaim",
    "RejectClaim",
    "RevokeBinding",
    "IssueDoctorLogin",
}

WRITE_SETS = {
    "CreatePatientStub": {"patient", "patient_profile"},
    "IssueInvitation": {"invitation", "token_head", "patient", "patient_claim"},
    "ClaimInvitation": {"invitation", "patient_claim", "claim_callback"},
    "RecordConsent": {"consent", "patient_claim", "claim_callback"},
    "RefreshConsentOffer": {"patient_claim", "claim_callback"},
    "ReadConsentTerms": set(),
    "ConsentContactUnavailable": set(),
    "ConfirmPatientClaim": {
        "mission",
        "review",
        "invitation",
        "patient_claim",
        "patient_binding",
        "subject_binding",
        "patient",
        "patient_profile",
        "claim_callback",
    },
    "RejectClaim": {"invitation", "patient_claim", "claim_callback"},
    "RevokeBinding": {"patient", "patient_profile", "patient_binding", "subject_binding"},
    "IssueAdminLogin": {"admin_account", "admin_login", "token_head"},
    "RevokeAdminSessions": {"admin_account"},
    "IssueDoctorLogin": {"doctor_login", "token_head"},
    "IssuePatientLogin": {"patient_login", "token_head"},
    "ExchangeLogin": {"admin_login", "doctor_login", "patient_login", "pre_session", "web_session"},
    "CreatePreSession": {"pre_session"},
    "TouchWebSession": {"web_session"},
    "RevokeWebSession": {"web_session"},
    "ExpireInvitation": {"invitation", "patient_claim"},
    "FinishIdentityReceipt": set(),
}


def identity_guards(
    store: "StoreBase", request: CommitRequest, now: datetime
) -> tuple[list["Check"], datetime | None] | None:
    from sanad.store._base import Check

    command, scope = request.command, request.command.scope
    if (
        not isinstance(scope, AccountScope)
        or store._identity is None
        or scope.bot_id != store._identity.bot_id
    ):
        return None
    actor, kind = command.principal, command.payload.get("type")
    if kind not in DOCTOR_COMMANDS | {
        "ClaimInvitation",
        "RecordConsent",
        "RefreshConsentOffer",
        "ReadConsentTerms",
        "ConsentContactUnavailable",
        "IssueAdminLogin",
        "RevokeAdminSessions",
        "IssuePatientLogin",
        "ExchangeLogin",
        "CreatePreSession",
        "TouchWebSession",
        "RevokeWebSession",
        "ExpireInvitation",
        "FinishIdentityReceipt",
    }:
        return None
    if any(r.entity_type not in WRITE_SETS[str(kind)] for r in request.puts):
        return None
    checks: list[Check] = []
    if kind in {"IssueInvitation", "ClaimInvitation", "RecordConsent", "ConfirmPatientClaim"}:
        for removal_row in request.puts:
            if removal_row.entity_type in {"patient", "patient_claim", "invitation"}:
                if removal_row.entity_type == "patient":
                    removal_scope = PatientScope(
                        doctor_id=str(removal_row.doctor_id), patient_id=removal_row.id
                    )
                else:
                    removal_scope = PatientScope(
                        doctor_id=str(removal_row.body["doctor_id"]),
                        patient_id=str(removal_row.body["patient_id"]),
                    )
                profile = store.get_patient_profile(removal_scope)
                if profile and profile.removed_at:
                    return None
                if profile:
                    checks.append(Check(keys.patient(removal_scope), profile.version))
    expiries: list[datetime] = []
    if command.worker:
        expiries.append(command.worker.auth_expiry)
    if command.work_claim:
        expiries.append(command.work_claim.expires_at)
    reads: dict[tuple[str, str], StoredRecord | None] = {}
    for read in request.identity_reads:
        row = store.get(read.scope, read.entity_type, read.id)
        if (row.version if row else None) != read.version:
            return None
        if row:
            key = row.key
        elif read.entity_type == "subject_binding" and read.scope == scope:
            key = keys.subject(scope.bot_id, read.id.removeprefix(f"SUBJECT#{scope.bot_id}#"))
        elif read.entity_type == "admin_account" and read.scope == scope:
            key = Key(keys.partition(scope), f"ADMIN_ACCOUNT#{keys.component(read.id)}")
        elif read.entity_type == "token_head" and read.scope == scope:
            key = Key(keys.partition(scope), f"TOKEN_HEAD#{read.id}")
        else:
            return None
        checks.append(Check(key, read.version))
        reads[read.entity_type, read.id] = row
    # Every revision is guarded by its exact old row, including actor-bound callbacks.
    for row in request.puts:
        if row.version > 1:
            prior_row = reads.get((row.entity_type, row.id))
            if prior_row is None or prior_row.version != row.version - 1:
                return None
    auth = store.authorize(scope.bot_id, actor.subject)
    if kind in {"IssueAdminLogin", "RevokeAdminSessions"}:
        if actor.subject != store._identity.admin_user_id or "admin" not in actor.verified_roles:
            return None
        admin_row = store.get(scope, "admin_account", actor.subject)
        checks.append(
            Check(
                Key(keys.partition(scope), f"ADMIN_ACCOUNT#{keys.component(actor.subject)}"),
                admin_row.version if admin_row else None,
            )
        )
        if kind == "RevokeAdminSessions" and (
            admin_row is None or actor.auth_epoch != admin_row.body["auth_epoch"]
        ):
            return None
    if kind in DOCTOR_COMMANDS:
        if (
            actor.actor_kind != "doctor"
            or "doctor" not in actor.verified_roles
            or auth.principal.actor_kind != "doctor"
            or actor.doctor_id != auth.principal.doctor_id
            or actor.auth_epoch != auth.auth_epoch
            or auth.binding is None
        ):
            return None
    if kind in DOCTOR_COMMANDS | {"IssuePatientLogin", "ClaimInvitation", "RecordConsent"}:
        bound = store.get(scope, "subject_binding", actor.subject)
        checks.append(
            Check(keys.subject(scope.bot_id, actor.subject), bound.version if bound else None)
        )
        if kind in {"ClaimInvitation", "RecordConsent"} and (
            bound is not None
            or actor.subject == store._identity.admin_user_id
            or auth.principal.verified_roles
        ):
            return None
    if kind in {"ReadConsentTerms", "RefreshConsentOffer", "ConsentContactUnavailable"}:
        target = store.get(scope, "patient_claim", str(command.payload.get("claim_id", "")))
        if target is None:
            return None
        pending = from_record(target, PatientClaim)
        inv_row = store.get(scope, "invitation", pending.invitation_id)
        patient_scope = PatientScope(doctor_id=pending.doctor_id, patient_id=pending.patient_id)
        patient_row = store.get(patient_scope, "patient", pending.patient_id)
        doctor_row = store.get(
            TenantScope(doctor_id=pending.doctor_id), "doctor", pending.doctor_id
        )
        if (
            pending.state != "pending"
            or pending.consent_id is not None
            or pending.candidate_subject != actor.subject
            or pending.private_chat_id != actor.subject
            or pending.review_at <= now
            or auth.binding is not None
            or auth.principal.verified_roles
            or actor.bot_id != scope.bot_id
            or actor.user_id != actor.subject
            or inv_row is None
            or inv_row.body.get("state") != "claimed"
            or inv_row.body.get("pending_claim_id") != pending.id
            or patient_row is None
            or patient_row.body.get("invitation_id") != pending.invitation_id
            or doctor_row is None
            or doctor_row.body.get("status") != "approved"
            or doctor_row.body.get("telegram_bot_id") != scope.bot_id
        ):
            return None
        checks.extend(Check(r.key, r.version) for r in (target, inv_row, patient_row, doctor_row))
        checks.append(Check(keys.subject(scope.bot_id, actor.subject), None))
        expiries.append(pending.review_at)
        if kind == "ReadConsentTerms":
            token_row = store.get(
                scope, "claim_callback", str(command.payload.get("callback_hash", ""))
            )
            if token_row is None:
                return None
            token = from_record(token_row, ClaimCallback)
            if (
                token.action != "read_terms"
                or token.actor_subject != actor.subject
                or token.claim_id != pending.id
                or token.consumed_at
                or token.expires_at <= now
                or token.offer_generation != pending.offer_generation
                or command.payload.get("offer_generation") != pending.offer_generation
                or not pending.consent_offers
                or len(request.intents) != 1
            ):
                return None
            intent = request.intents[0]
            payload = intent.body.get("payload")
            if (
                intent.body.get("template_id") != "consent_terms"
                or not isinstance(payload, dict)
                or payload.get("text") != pending.consent_offers[-1].full_text
            ):
                return None
            checks.append(Check(token_row.key, token_row.version))
            expiries.append(token.expires_at)
    admin_exchange = kind == "ExchangeLogin" and any(
        r.entity_type == "admin_login" and r.body.get("state") == "consumed" for r in request.puts
    )
    clinical_rows = [
        r
        for r in request.puts
        if not (admin_exchange and r.entity_type == "web_session" and r.body.get("revoked_at"))
    ]
    doctor_ids = {str(r.body["doctor_id"]) for r in clinical_rows if r.body.get("doctor_id")} | {
        r.doctor_id for r in clinical_rows if r.doctor_id
    }
    if kind in DOCTOR_COMMANDS and doctor_ids - {actor.doctor_id}:
        return None
    for doctor_id in doctor_ids:
        row = store.get(TenantScope(doctor_id=doctor_id), "doctor", doctor_id)
        if row is None or row.body.get("telegram_bot_id") != scope.bot_id:
            return None
        doctor = from_record(row, Doctor)
        checks.append(Check(row.key, row.version))
        if kind not in {"ExpireInvitation", "RevokeWebSession", "FinishIdentityReceipt"}:
            if doctor.status != "approved":
                return None
        if kind in DOCTOR_COMMANDS and doctor.auth_epoch != actor.auth_epoch:
            return None
    for row in request.puts:
        body = row.body
        old_row = reads.get((row.entity_type, row.id))
        old = old_row.body if old_row else {}
        actual_scope = model_scope(from_record(row, MODELS[row.entity_type]))
        if isinstance(actual_scope, AccountScope) and actual_scope != scope:
            return None
        if row.entity_type == "admin_account":
            admin = from_record(row, AdminAccount)
            if admin.id != actor.subject or admin.id != store._identity.admin_user_id:
                return None
            if kind == "IssueAdminLogin":
                if old or admin.version != 1 or admin.auth_epoch != 1:
                    return None
            elif kind == "RevokeAdminSessions":
                if (
                    not old
                    or admin.auth_epoch != int(str(old["auth_epoch"])) + 1
                    or admin.created_at != datetime.fromisoformat(str(old["created_at"]))
                ):
                    return None
            else:
                return None
        if row.entity_type == "mission":
            from sanad.contact.binding import valid_binding_mission

            if (
                kind != "ConfirmPatientClaim"
                or not old_row
                or not datetime.fromisoformat(str(old_row.body["updated_at"]))
                <= datetime.fromisoformat(str(row.body["updated_at"]))
                <= command.requested_at
                <= now
                or not valid_binding_mission(
                    old_row, row, datetime.fromisoformat(str(row.body["updated_at"]))
                )
            ):
                return None
        if row.entity_type == "token_head":
            head = from_record(row, TokenHead)
            target = next(
                (
                    r
                    for r in request.puts
                    if r.id == head.token_hash and r.entity_type == head.purpose
                ),
                None,
            )
            if target is None or target.version != 1 or target.body.get("state") != "issued":
                return None
            expected_owner = (
                keys.partition(
                    PatientScope(
                        doctor_id=str(target.body["doctor_id"]),
                        patient_id=str(target.body["patient_id"]),
                    )
                )
                if head.purpose == "invitation"
                else actor.subject
            )
            if head.owner_key != expected_owner:
                return None
            if old:
                if head.owner_key != old["owner_key"] or head.purpose != old["purpose"]:
                    return None
                old_token = store.get(scope, head.purpose, str(old["token_hash"]))
                if old_token and old_token.body.get("state") in {"issued", "claimed"}:
                    revoked = next(
                        (
                            r
                            for r in request.puts
                            if r.entity_type == head.purpose and r.id == old_token.id
                        ),
                        None,
                    )
                    if revoked is None or revoked.body.get("state") != "revoked":
                        return None
        if row.entity_type == "subject_binding":
            binding = from_record(row, SubjectBinding)
            if binding.role_set != frozenset({"patient"}):
                return None
            if kind == "ConfirmPatientClaim":
                if row.version != 1 or binding.status != "active":
                    return None
                if binding.telegram_user_id == store._identity.admin_user_id:
                    return None
            elif kind == "RevokeBinding":
                if binding.status != "revoked" or not old or old.get("status") != "active":
                    return None
                if binding.binding_epoch != int(str(old["binding_epoch"])) + 1:
                    return None
            else:
                return None
        if row.entity_type in {"doctor_login", "patient_login", "admin_login"}:
            exchange = from_record(row, LoginExchange)
            if exchange.removal_destination:
                removal_target = PatientScope(
                    doctor_id=exchange.doctor_id or "",
                    patient_id=exchange.removal_destination.rsplit("/", 1)[1],
                )
                patient = store.get(removal_target, "patient", removal_target.patient_id)
                if (
                    exchange.intended_role != "doctor"
                    or not patient
                    or removal_target.doctor_id != actor.doctor_id
                ):
                    return None
                checks.append(Check(patient.key, patient.version))
            if row.version == 1:
                if (
                    kind
                    != (
                        "IssueDoctorLogin"
                        if exchange.intended_role == "doctor"
                        else "IssueAdminLogin"
                        if exchange.intended_role == "admin"
                        else "IssuePatientLogin"
                    )
                    or exchange.subject != actor.subject
                ):
                    return None
            elif exchange.state == "consumed":
                if kind != "ExchangeLogin" or old.get("state") != "issued":
                    return None
                if exchange.subject != actor.subject or exchange.expires_at <= now:
                    return None
                expiries.append(exchange.expires_at)
            elif exchange.state not in {"revoked", "expired"} or old.get("state") != "issued":
                return None
            if exchange.intended_role == "admin" and row.version == 1:
                admin_source = next(
                    (r for r in request.puts if r.entity_type == "admin_account"), None
                ) or store.get(scope, "admin_account", actor.subject)
                if (
                    admin_source is None
                    or exchange.auth_epoch != admin_source.body["auth_epoch"]
                    or actor.subject != store._identity.admin_user_id
                ):
                    return None
            if old and any(
                body.get(k) != v
                for k, v in old.items()
                if k not in {"version", "updated_at", "state", "consumed_at"}
            ):
                return None
        if row.entity_type == "invitation":
            inv = from_record(row, Invitation)
            allowed: dict[str, set[tuple[str | None, str]]] = {
                "IssueInvitation": {
                    (None, "issued"),
                    ("issued", "revoked"),
                    ("claimed", "revoked"),
                },
                "ClaimInvitation": {("issued", "claimed")},
                "ConfirmPatientClaim": {("claimed", "consumed")},
                "RejectClaim": {("claimed", "revoked")},
                "ExpireInvitation": {("issued", "expired"), ("claimed", "expired")},
            }
            if (str(old["state"]) if old else None, inv.state) not in allowed.get(str(kind), set()):
                return None
            if kind in {"ClaimInvitation", "ConfirmPatientClaim"} or (
                kind == "IssueInvitation" and inv.state == "issued"
            ):
                if inv.expires_at <= now:
                    return None
                expiries.append(inv.expires_at)
            if kind == "ExpireInvitation" and now < inv.expires_at:
                return None
        if row.entity_type == "patient_claim":
            claim = from_record(row, PatientClaim)
            if kind == "ClaimInvitation" and (
                old
                or claim.state != "pending"
                or claim.candidate_subject != actor.subject
                or claim.private_chat_id != actor.subject
                or claim.consent_id is not None
            ):
                return None
            if kind == "RecordConsent" and (
                old.get("state") != "pending"
                or old.get("consent_id") is not None
                or claim.candidate_subject != actor.subject
            ):
                return None
            if old:
                assert old_row is not None
                old_claim = from_record(old_row, PatientClaim)
                if kind == "RefreshConsentOffer":
                    if (
                        old_claim.state != "pending"
                        or old_claim.consent_id is not None
                        or claim.offer_generation != old_claim.offer_generation + 1
                        or claim.consent_offers[:-1] != old_claim.consent_offers
                        or len(claim.consent_offers) != len(old_claim.consent_offers) + 1
                        or claim.consent_offers[-1].generation != claim.offer_generation
                        or claim.model_dump(
                            exclude={
                                "version",
                                "updated_at",
                                "consent_policy",
                                "offer_generation",
                                "consent_offers",
                            }
                        )
                        != old_claim.model_dump(
                            exclude={
                                "version",
                                "updated_at",
                                "consent_policy",
                                "offer_generation",
                                "consent_offers",
                            }
                        )
                    ):
                        return None
                elif (
                    claim.consent_offers != old_claim.consent_offers
                    or claim.offer_generation != old_claim.offer_generation
                    or claim.consent_policy != old_claim.consent_policy
                ):
                    return None
            if kind in {
                "RecordConsent",
                "RefreshConsentOffer",
                "ConfirmPatientClaim",
                "RejectClaim",
            }:
                if claim.review_at <= now:
                    return None
                expiries.append(claim.review_at)
        if row.entity_type == "consent" and kind == "RecordConsent":
            consent = from_record(row, Consent)
            pending_row = next((r for r in request.puts if r.entity_type == "patient_claim"), None)
            if pending_row is None:
                return None
            pending = from_record(pending_row, PatientClaim)
            if not pending.consent_offers:
                return None
            offer = pending.consent_offers[-1]
            if (
                consent.offer_claim_id != pending.id
                or consent.offer_generation != offer.generation
                or consent.policy_digest != offer.digest
                or consent.policy_text_version != offer.text_version
                or consent.language != offer.language
            ):
                return None
        if row.entity_type == "claim_callback":
            callback = from_record(row, ClaimCallback)
            if old:
                if any(
                    body.get(k) != v
                    for k, v in old.items()
                    if k not in {"version", "updated_at", "consumed_at"}
                ):
                    return None
                if (
                    old.get("consumed_at") is not None
                    or callback.consumed_at is None
                    or callback.consumed_at > now
                ):
                    return None
                if callback.actor_subject != actor.subject or callback.expires_at <= now:
                    return None
                expiries.append(callback.expires_at)
                target_row = store.get(scope, "patient_claim", callback.claim_id)
                if target_row is None:
                    return None
                callback_claim = from_record(target_row, PatientClaim)
                if callback.offer_generation != callback_claim.offer_generation:
                    return None
                for ref in callback.expected_versions:
                    target_scope = PatientScope(
                        doctor_id=callback_claim.doctor_id, patient_id=callback_claim.patient_id
                    )
                    source_row = store.get(
                        target_scope if ref.entity_type in {"patient", "consent"} else scope,
                        ref.entity_type,
                        ref.id,
                    )
                    if source_row is None or source_row.version != ref.version:
                        return None
                    checks.append(Check(source_row.key, source_row.version))
        if row.entity_type == "pre_session":
            pre = from_record(row, PreSession)
            if old:
                if kind != "ExchangeLogin" or old.get("consumed_at") is not None:
                    return None
                if pre.expires_at <= now or pre.consumed_at is None or pre.consumed_at > now:
                    return None
                expiries.append(pre.expires_at)
            elif kind != "CreatePreSession":
                return None
            elif pre.expires_at <= now:
                return None
            else:
                expiries.append(pre.expires_at)
        if row.entity_type == "web_session":
            session = from_record(row, WebSession)
            if old:
                immutable = {
                    "version",
                    "updated_at",
                    "last_seen_at",
                    "idle_expires_at",
                    "revoked_at",
                }
                if kind == "TouchWebSession":
                    immutable.add("consent_version")
                if kind == "RevokeWebSession":
                    immutable.add("revocation_reason")
                if any(body.get(k) != v for k, v in old.items() if k not in immutable):
                    return None
                if kind in {"ExchangeLogin", "RevokeWebSession"} and (
                    session.revoked_at is None or session.revoked_at > now
                ):
                    return None
                if kind not in {"ExchangeLogin", "TouchWebSession", "RevokeWebSession"}:
                    return None
                if kind == "TouchWebSession":
                    if old.get("revoked_at") or session.subject != actor.subject:
                        return None
                    expiries.extend(
                        datetime.fromisoformat(str(old[k]))
                        for k in ("idle_expires_at", "absolute_expires_at")
                    )
            else:
                consumed = [
                    r
                    for r in request.puts
                    if r.entity_type == session.role + "_login"
                    and r.body.get("state") == "consumed"
                ]
                if kind != "ExchangeLogin" or len(consumed) != 1:
                    return None
                exchange = from_record(consumed[0], LoginExchange)
                if any(
                    getattr(session, k) != getattr(exchange, k)
                    for k in (
                        "subject",
                        "doctor_id",
                        "patient_id",
                        "binding_id",
                        "binding_epoch",
                        "consent_version",
                        "auth_epoch",
                    )
                ):
                    return None
        if row.entity_type == "patient" and kind not in {
            "CreatePatientStub",
            "IssueInvitation",
            "ConfirmPatientClaim",
            "RevokeBinding",
        }:
            return None
    if kind == "ConfirmPatientClaim" and not _confirmation(request, reads, now):
        return None
    if kind in {"IssuePatientLogin", "ExchangeLogin", "TouchWebSession"}:
        snapshots = [
            r
            for r in request.puts
            if r.entity_type in {"doctor_login", "patient_login", "admin_login", "web_session"}
            and (r.version == 1 or kind == "TouchWebSession")
        ]
        for row in snapshots:
            if row.body.get("revoked_at"):
                continue
            if not live_snapshot(
                store,
                scope,
                from_record(row, WebSession)
                if row.entity_type == "web_session"
                else from_record(row, LoginExchange),
                checks,
            ):
                return None
    return checks, min(expiries) if expiries else None


def live_snapshot(
    store: "Store",
    scope: AccountScope,
    snapshot: LoginExchange | WebSession,
    checks: list["Check"],
) -> bool:
    from sanad.store._base import Check

    auth = store.authorize(scope.bot_id, snapshot.subject)
    role = snapshot.role if snapshot.entity_type == "web_session" else snapshot.intended_role
    if role == "admin":
        row = store.get(scope, "admin_account", snapshot.subject)
        if (
            "admin" not in auth.principal.verified_roles
            or row is None
            or auth.admin_epoch != snapshot.auth_epoch
            or row.body.get("auth_epoch") != snapshot.auth_epoch
        ):
            return False
        checks.append(Check(row.key, row.version))
        return True
    if snapshot.doctor_id is None:
        return False
    bound = store.get(scope, "subject_binding", snapshot.subject)
    doctor = store.get(TenantScope(doctor_id=snapshot.doctor_id), "doctor", snapshot.doctor_id)
    if bound is None or doctor is None:
        return False
    checks.extend([Check(bound.key, bound.version), Check(doctor.key, doctor.version)])
    subject = from_record(bound, SubjectBinding)
    if (
        subject.status != "active"
        or auth.principal.actor_kind != role
        or role not in subject.role_set
        or subject.doctor_id != snapshot.doctor_id
        or doctor.body.get("auth_epoch") != snapshot.auth_epoch
        or doctor.body.get("status") != "approved"
        or doctor.body.get("telegram_bot_id") != scope.bot_id
        or (role == "doctor" and doctor.body.get("telegram_user_id") != snapshot.subject)
    ):
        return False
    if role == "patient":
        patient_scope = PatientScope(
            doctor_id=snapshot.doctor_id, patient_id=snapshot.patient_id or ""
        )
        binding_row = store.get(patient_scope, "patient_binding", snapshot.binding_id or "")
        patient_row = store.get(patient_scope, "patient", snapshot.patient_id or "")
        profile_row = store.get(patient_scope, "patient_profile", snapshot.patient_id or "")
        if not binding_row or not patient_row or not profile_row:
            return False
        binding = from_record(binding_row, PatientBinding)
        patient = from_record(patient_row, Patient)
        profile = from_record(profile_row, PatientProfile)
        consent_row = store.get(patient_scope, "consent", binding.consent_id)
        if not consent_row:
            return False
        consent = from_record(consent_row, Consent)
        checks.extend(
            Check(r.key, r.version) for r in (binding_row, patient_row, profile_row, consent_row)
        )
        if (
            binding.status != "active"
            or binding.subject != snapshot.subject
            or binding.binding_epoch != snapshot.binding_epoch
            or subject.binding_epoch != snapshot.binding_epoch
            or subject.patient_id != snapshot.patient_id
            or patient.active_binding_id != snapshot.binding_id
            or patient.contact_status in {"awaiting_link", "frozen"}
            or consent.version != snapshot.consent_version
            or binding.consent_version != consent.version
            or patient.consent_version != consent.version
            or consent.withdrawn_at is not None
            or "telegram" not in consent.permitted_channels
            or not profile.binding_active
            or not profile.consent_active
            or profile.binding_epoch != snapshot.binding_epoch
            or profile.consent_version != consent.version
            or profile.recipient_subject != snapshot.subject
            or profile.recipient_ref != binding.private_chat_id
            or consent.binding_id != binding.id
            or consent.accepted_by != snapshot.subject
        ):
            return False
    return True


def _confirmation(
    request: CommitRequest,
    reads: dict[tuple[str, str], StoredRecord | None],
    now: datetime,
) -> bool:
    def one(kind: str) -> StoredRecord | None:
        return next((r for r in request.puts if r.entity_type == kind), None)

    inv_row, claim_row, pat_row = one("invitation"), one("patient_claim"), one("patient")
    binding_row, subject_row, profile_row = (
        one("patient_binding"),
        one("subject_binding"),
        one("patient_profile"),
    )
    if (
        inv_row is None
        or claim_row is None
        or pat_row is None
        or binding_row is None
        or subject_row is None
        or profile_row is None
    ):
        return False
    inv, claim, pat = (
        from_record(inv_row, Invitation),
        from_record(claim_row, PatientClaim),
        from_record(pat_row, Patient),
    )
    binding, subject = (
        from_record(binding_row, PatientBinding),
        from_record(subject_row, SubjectBinding),
    )
    old_claim_row = reads.get(("patient_claim", claim.id))
    old_pat_row = reads.get(("patient", pat.id))
    consent_row = reads.get(("consent", claim.consent_id or ""))
    if not old_claim_row or not old_pat_row or not consent_row:
        return False
    old_claim, old_pat, consent = (
        from_record(old_claim_row, PatientClaim),
        from_record(old_pat_row, Patient),
        from_record(consent_row, Consent),
    )
    return (
        old_claim.state == "pending"
        and old_claim.consent_version == consent.version
        and old_pat.contact_status in {"awaiting_link", "frozen"}
        and old_pat.version == claim.patient_version
        and old_pat.invitation_id == inv.id == claim.invitation_id
        and inv.pending_claim_id == claim.id
        and inv.generation == claim.invitation_generation == old_pat.invitation_generation
        and claim.doctor_id == pat.scope.doctor_id == inv.doctor_id == subject.doctor_id
        and claim.patient_id == pat.id == inv.patient_id == subject.patient_id
        and binding.scope == pat.scope == consent.scope
        and binding.id == claim.binding_id == pat.active_binding_id == consent.binding_id
        and binding.claim_id == claim.id
        and binding.subject
        == claim.candidate_subject
        == consent.accepted_by
        == subject.telegram_user_id
        and binding.private_chat_id == subject.private_chat_id == claim.private_chat_id
        and claim.doctor_confirmed_by
        == binding.doctor_confirmed_by
        == request.command.principal.subject
        and claim.doctor_confirmed_at == binding.doctor_confirmed_at
        and binding.doctor_confirmed_at <= now
        and consent.withdrawn_at is None
        and consent.routine_contact_enabled
        and consent.version
        == claim.consent_version
        == binding.consent_version
        == pat.consent_version
        and consent.id == binding.consent_id == pat.consent_id
        and binding.binding_epoch == pat.binding_epoch == subject.binding_epoch
        and profile_row.body["binding_active"] is True
        and profile_row.body["consent_active"] is True
        and profile_row.body["binding_epoch"] == binding.binding_epoch
        and profile_row.body["consent_version"] == consent.version
        and profile_row.body["recipient_subject"] == binding.subject
        and profile_row.body["recipient_ref"] == binding.private_chat_id
    )


def preference_session(
    store: "StoreBase", request: CommitRequest, receipt: StoredRecord, now: datetime
) -> list["Check"] | None:
    """Only a receipt-bound preference may advance its own live consent snapshot."""
    from sanad.store._base import Check

    rows = [r for r in request.puts if r.entity_type == "web_session"]
    payload = receipt.body.get("payload")
    session_id = payload.get("web_session_id") if isinstance(payload, dict) else None
    preference = request.command.payload.get("type") == "SetContactPreference"
    browser = receipt.body.get("transport") in {"web-message", "web-preference"}
    if not browser:
        return None if rows else []
    if not isinstance(session_id, str):
        return None
    actor = request.command.principal
    scope = AccountScope(bot_id=actor.bot_id or "")
    old_row = store.get(scope, "web_session", session_id)
    if old_row is None:
        return None
    old = from_record(old_row, WebSession)
    checks: list[Check] = []
    if (
        old.role != "patient"
        or old.subject != actor.subject
        or old.doctor_id != actor.doctor_id
        or old.patient_id != actor.patient_id
        or old.revoked_at
        or min(old.idle_expires_at, old.absolute_expires_at) <= now
        or not live_snapshot(store, scope, old, checks)
    ):
        return None
    checks.append(Check(old_row.key, old_row.version))
    if not preference:
        return None if rows else checks
    if len(rows) != 1 or request.command.payload.get("web_session_id") != session_id:
        return None
    new = from_record(rows[0], WebSession)
    consent = next((r for r in request.puts if r.entity_type == "consent"), None)
    if (
        consent is None
        or new.consent_version != consent.version
        or new.version != old.version + 1
        or not old.updated_at <= new.updated_at == request.command.requested_at <= now
        or old.model_dump(exclude={"version", "updated_at", "consent_version"})
        != new.model_dump(exclude={"version", "updated_at", "consent_version"})
    ):
        return None
    return checks
