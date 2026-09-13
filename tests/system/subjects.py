"""Explicit synthetic actors; no inherited APPLICANT/PATIENT routing defaults."""

from typing import cast

import httpx
from harness import FakeClock
from providers.fixtures import ScriptedModel, candidate
from store.account_fixtures import callback, update
from store.concierge_fixtures import PatientWorld
from store.login_fixtures import ORIGIN

from sanad.auth.commands import ConfirmPatientClaim, IssuedInvitation, IssueInvitation
from sanad.domain import PatientScope, Principal
from sanad.scribe.proposal import Proposal
from sanad.steward.types import records
from sanad.store._base import StoreBase
from sanad.store.keys import IntakeScope
from sanad.store.records import Doctor, OutboundIntent, Patient, from_record

DOCTOR_A, DOCTOR_B = "20002", "20003"
PATIENT_A, PATIENT_B, PATIENT_C = "30003", "30004", "30005"


class SubjectWorld(PatientWorld):
    doctor_subject: str
    patient_subject: str

    @classmethod
    def for_subjects(
        cls,
        store: StoreBase,
        clock: FakeClock,
        *,
        doctor: str,
        patient: str,
        scope: PatientScope | None = None,
    ) -> "SubjectWorld":
        w = cast(SubjectWorld, cls.create(store, clock))
        w.doctor_subject, w.patient_subject = doctor, patient
        if scope is not None:
            w.patient_scope = scope
        w.concierge.synthetic = True
        w.scribe.rxnorm_client = httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))
        )
        return w

    @property
    def owner(self) -> Principal:
        return self.actor(self.doctor_subject)

    @property
    def doctor(self) -> Doctor:
        value = self.runtime.accounts.doctor(self.owner.doctor_id or "")
        assert value is not None
        return value

    def enroll_named(self, name: str, *, id: int) -> Patient:
        patient = self.named_stub(name, subject=self.doctor_subject)
        self.bind(patient, id=id)
        return patient

    def bind(self, patient: Patient, *, id: int) -> None:
        invitation = self.claims.issue_invitation(
            IssueInvitation(
                command_id=f"invite-{id}",
                actor=self.owner,
                patient_id=patient.id,
            )
        )
        assert isinstance(invitation, IssuedInvitation)
        pending = self.consent(self.claim(invitation, self.patient_subject, id=id), id=id + 1)
        assert (
            self.claims.confirm(
                ConfirmPatientClaim(
                    command_id=f"confirm-{id}",
                    actor=self.owner,
                    claim_id=pending.id,
                    expected_versions=self.claims.confirmation_versions(self.owner, pending.id),
                )
            ).status
            == "accepted"
        )
        self.patient_scope = patient.scope
        assert self.actor(self.patient_subject).patient_id == patient.id
        assert self.actor(self.patient_subject).doctor_id == self.doctor.id

    def dictate(self, text: str, value: dict[str, object], *, id: int = 10) -> Proposal:
        model = ScriptedModel(candidate(value), candidate(value))
        self.scribe.model_factory = lambda registry, role: model
        assert self.post(update(self.doctor_subject, text, id)).status_code == 200
        assert self.receipt(id).state == "completed"
        assert len(model.script.calls) == 2
        return self.proposal

    def cards(self) -> list[OutboundIntent]:
        scope = IntakeScope(doctor_id=self.doctor.id, intake_id="scribe")
        return [
            from_record(r, OutboundIntent) for r in records(self.store, scope, "outbound_intent")
        ]

    def tap(self, label: str = "✅ Confirm", *, id: int = 20, raw: str | None = None) -> None:
        assert (
            self.post(callback(raw or self.button(label), self.doctor_subject, id)).status_code
            == 200
        )

    def send(
        self, text: str, value: dict[str, object] | None = None, *, id: int | None = None
    ) -> tuple[ScriptedModel, OutboundIntent]:
        assert id is not None, "System journeys require explicit, globally unique receipt IDs"
        model = ScriptedModel(
            candidate(
                value
                or {
                    "reply": "This question needs your doctor.",
                    "kind": "cannot_answer",
                    "needs_doctor": True,
                }
            )
        )
        from store.medication_fixtures import scripted_barrier_factory

        self.concierge.barrier_model_factory = scripted_barrier_factory
        self.concierge.model_factory = lambda registry, role: model
        assert self.post(update(self.patient_subject, text, id)).status_code == 200
        receipt = self.receipt(id)
        assert receipt.state == "completed"
        intents = [
            i for i in self.patient_intents() if "patient-turn:" + receipt.id in i.source_event_ids
        ]
        assert intents
        return model, intents[-1]

    def doctor_says(self, text: str, *, id: int) -> None:
        assert self.post(update(self.doctor_subject, text, id)).status_code == 200
        assert self.receipt(id).state == "completed"

    def login_path(self, subject: str | None = None, id: int = 200) -> str:
        assert subject is not None
        scope = (
            self.runtime.accounts.scope if subject == self.doctor_subject else self.patient_scope
        )
        previous = {r.id for r in records(self.store, scope, "outbound_intent")}
        assert self.post(update(subject, "/login", id)).status_code == 200
        intent = next(
            from_record(r, OutboundIntent)
            for r in records(self.store, scope, "outbound_intent")
            if r.id not in previous and str(r.body.get("template_id", "")).endswith("login_link")
        )
        assert self.dispatch(intent).status == "provider_accepted"
        assert intent.payload
        return str(intent.payload["text"]).splitlines()[-1].removeprefix(ORIGIN)
