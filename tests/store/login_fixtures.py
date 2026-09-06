"""Slice 06 extends the accepted synthetic account/transport harness."""

import re
import warnings
from typing import cast
from uuid import uuid4

import httpx
from harness import FakeClock
from starlette.exceptions import StarletteDeprecationWarning

from sanad.api.app import create_app
from sanad.auth.claim import ClaimService
from sanad.auth.commands import (
    ConfirmPatientClaim,
    ConsentPolicy,
    CreatePatientStub,
    IssuedInvitation,
    IssueInvitation,
)
from sanad.auth.login import LoginService
from sanad.channels.telegram.router import TelegramRuntime
from sanad.channels.transport import CapturedTransport
from sanad.domain import Principal
from sanad.store._base import StoreBase, Write
from sanad.store.records import (
    Doctor,
    Patient,
    PatientClaim,
    from_record,
    model_scope,
    record_item,
    to_record,
)
from sanad.web.settings import WebSettings
from store.account_fixtures import APPLICANT, PATIENT, AccountWorld, callback, settings, update

# Keep the accepted HTTPX pin. Starlette's documented compatibility fallback
# emits this import-only warning before using HTTPX 0.28.1.
with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message="Using `httpx` with `starlette.testclient` is deprecated.*",
        category=StarletteDeprecationWarning,
    )
    warnings.filterwarnings(
        "ignore",
        message="The anyio.abc.BlockingPortal alias is deprecated.*",
        category=DeprecationWarning,
    )
    from fastapi.testclient import TestClient

ORIGIN = "https://sanad.example"
CONSENT_POLICY = ConsentPolicy(
    quiet_hours=("22:00", "08:00"),
    clinic_contact="Synthetic clinic contact",
    retention="Synthetic test data only; clinic retention review pending",
    urgent_response_policy_id="synthetic-urgent-policy",
)


class LoginWorld(AccountWorld):
    @classmethod
    def create(cls, store: StoreBase, clock: FakeClock, *, process: bool = True) -> "LoginWorld":
        transport = CapturedTransport()
        app = create_app(
            telegram_settings=settings(),
            store=store,
            clock=clock,
            transport=transport,
            process_receipts=process,
            web_settings=WebSettings(public_base_url=ORIGIN, bot_username="synthetic_sanad_bot"),
            consent_policy=lambda doctor_id: CONSENT_POLICY,
        )
        return cls(store, clock, cast(TelegramRuntime, app.state.telegram), app, transport)

    @property
    def login(self) -> LoginService:
        return cast(LoginService, self.app.state.login)

    @property
    def claims(self) -> ClaimService:
        return cast(ClaimService, self.app.state.claims)

    @property
    def owner(self) -> Principal:
        return self.actor(APPLICANT)

    @property
    def doctor(self) -> Doctor:
        doctor = self.runtime.accounts.doctor(self.owner.doctor_id or "")
        assert doctor is not None
        return doctor

    def stub(self) -> Patient:
        result = self.claims.create_stub(
            CreatePatientStub(
                command_id=uuid4().hex, actor=self.owner, display_name="Synthetic Patient"
            )
        )
        assert result.status == "accepted"
        ref = next(r for r in result.resulting_versions if r.entity_type == "patient")
        patient = self.claims.patient(self.doctor.id, ref.id)
        assert patient is not None
        return patient

    def invite(self, patient: Patient) -> IssuedInvitation:
        result = self.claims.issue_invitation(
            IssueInvitation(command_id=uuid4().hex, actor=self.owner, patient_id=patient.id)
        )
        assert isinstance(result, IssuedInvitation)
        return result

    def claim(
        self, invitation: IssuedInvitation, subject: str = PATIENT, id: int = 100
    ) -> PatientClaim:
        assert (
            self.post(
                update(subject, "/start " + invitation.token.get_secret_value(), id)
            ).status_code
            == 200
        )
        from sanad.store import keys

        inv = self.claims.invitation(keys.digest(invitation.token.get_secret_value()))
        assert inv and inv.pending_claim_id
        pending = self.claims.patient_claim(inv.pending_claim_id)
        assert pending is not None
        return pending

    def action_token(self, claim_id: str, template: str, index: int = 0) -> str:
        intent = next(
            i
            for i in self.intents()
            if i.template_id == template and claim_id in i.source_event_ids
        )
        assert intent.payload
        markup = intent.payload["reply_markup"]
        assert isinstance(markup, dict)
        keyboard = markup["inline_keyboard"]
        assert isinstance(keyboard, list) and isinstance(keyboard[0], list)
        button = keyboard[0][index]
        assert isinstance(button, dict)
        return str(button["callback_data"])

    def consent(self, pending: PatientClaim, *, accept: bool = True, id: int = 101) -> PatientClaim:
        token = self.action_token(pending.id, "consent_request", 0 if accept else 1)
        assert self.post(callback(token, pending.candidate_subject, id)).status_code == 200
        changed = self.claims.patient_claim(pending.id)
        assert changed is not None
        assert changed.consent_version == 1 if accept else changed.state == "rejected"
        return changed

    def confirm_command(self, pending: PatientClaim) -> ConfirmPatientClaim:
        return ConfirmPatientClaim(
            command_id=uuid4().hex,
            actor=self.owner,
            claim_id=pending.id,
            expected_versions=self.claims.confirmation_versions(self.owner, pending.id),
        )

    def bound(self, subject: str = PATIENT) -> Patient:
        patient = self.stub()
        pending = self.consent(self.claim(self.invite(patient), subject))
        assert self.claims.confirm(self.confirm_command(pending)).status == "accepted"
        active = self.claims.patient(patient.scope.doctor_id, patient.id)
        assert active is not None and active.contact_status == "active"
        return active

    def login_path(self, subject: str = APPLICANT, id: int = 200) -> str:
        assert self.post(update(subject, "/login", id)).status_code == 200
        if subject == APPLICANT:
            intents = self.intents()
        else:
            actor = self.actor(subject)
            from sanad.domain import PatientScope

            scope = PatientScope(doctor_id=actor.doctor_id or "", patient_id=actor.patient_id or "")
            rows, _ = self.store.list_records(scope, "outbound_intent")
            from sanad.store.records import OutboundIntent

            intents = [from_record(row, OutboundIntent) for row in rows]
        template = "doctor_login_link" if subject == APPLICANT else "patient_login_link"
        intent = next(
            i for i in reversed(intents) if i.template_id == template and i.status == "queued"
        )
        assert self.dispatch(intent).status == "provider_accepted"
        assert intent.payload
        return str(intent.payload["text"]).splitlines()[-1].removeprefix(ORIGIN)

    def client(self) -> TestClient:
        return TestClient(self.app, base_url=ORIGIN, follow_redirects=False)


def browser_login(client: TestClient, path: str) -> httpx.Response:
    page = client.get(path)
    assert page.status_code == 200
    match = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert match
    return cast(
        httpx.Response, client.post(path, data={"csrf": match[1]}, headers={"Origin": ORIGIN})
    )


def replace_model(world: LoginWorld, model: Patient | Doctor | PatientClaim) -> None:
    row = to_record(model, model_scope(model))
    assert world.store._atomic([Write(record_item(row), row.version - 1)], [])
