"""Exercise the real receipt/router/Strands boundary with scripted providers."""

from typing import cast

from harness import FakeClock
from providers.fixtures import ScriptedModel, candidate

from sanad.auth.commands import CreatePatientStub
from sanad.scribe.proposal import Proposal
from sanad.scribe.turn import ScribeTurn
from sanad.store._base import StoreBase
from sanad.store.keys import IntakeScope
from sanad.store.records import OutboundIntent, Patient, from_record
from store.account_fixtures import APPLICANT, callback, update
from store.login_fixtures import LoginWorld


class ScribeWorld(LoginWorld):
    @classmethod
    def create(cls, store: StoreBase, clock: FakeClock, *, process: bool = True) -> "ScribeWorld":
        world = super().create(store, clock, process=process)
        return cast(ScribeWorld, world)

    @property
    def scribe(self) -> ScribeTurn:
        return cast(ScribeTurn, self.app.state.scribe)

    @property
    def proposal(self) -> Proposal:
        value = self.scribe.repo.pending(self.doctor.scope)
        assert value is not None
        return value

    def dictate(self, text: str, value: dict[str, object], *, id: int = 10) -> Proposal:
        model = ScriptedModel(candidate(value))
        self.scribe.model_factory = lambda registry, role: model
        assert self.post(update(APPLICANT, text, id)).status_code == 200
        assert self.receipt(id).state == "completed"
        assert len(model.script.calls) == 1
        return self.proposal

    def cards(self) -> list[OutboundIntent]:
        scope = IntakeScope(doctor_id=self.doctor.id, intake_id="scribe")
        rows, _ = self.store.list_records(scope, "outbound_intent")
        return [from_record(row, OutboundIntent) for row in rows]

    def button(self, label: str, proposal: Proposal | None = None) -> str:
        p = proposal or self.proposal
        for intent in self.cards():
            if (
                intent.source_versions
                and intent.source_versions[0].id == p.id
                and intent.source_versions[0].version == p.version
            ):
                payload = intent.payload or {}
                markup = payload.get("reply_markup")
                keyboard = markup.get("inline_keyboard") if isinstance(markup, dict) else None
                if isinstance(keyboard, list):
                    for row in keyboard:
                        if isinstance(row, list):
                            for button in row:
                                if isinstance(button, dict) and button.get("text") == label:
                                    return str(button["callback_data"])
        raise AssertionError("card button missing")

    def tap(self, label: str = "✅ تمام", *, id: int = 20, raw: str | None = None) -> None:
        assert self.post(callback(raw or self.button(label), APPLICANT, id)).status_code == 200

    def named_stub(self, name: str, *, subject: str = APPLICANT) -> Patient:
        from uuid import uuid4

        result = self.claims.create_stub(
            CreatePatientStub(
                command_id=uuid4().hex,
                actor=self.actor(subject),
                display_name=name,
                timezone="Africa/Cairo",
            )
        )
        assert result.status == "accepted"
        ref = next(r for r in result.resulting_versions if r.entity_type == "patient")
        value = self.claims.patient(self.actor(subject).doctor_id or "", ref.id)
        assert value is not None
        return value
