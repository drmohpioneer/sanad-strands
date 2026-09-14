"""Synthetic bound patients and captured transport through the actual patient turn."""

from typing import Any, cast

from providers.fixtures import ScriptedModel, candidate
from pydantic import BaseModel
from strands.models import Model

from sanad.concierge.turn import ConciergeTurn
from sanad.domain import PatientScope
from sanad.models.registry import ModelRegistry, ModelRole
from sanad.store._base import Write
from sanad.store.records import (
    OutboundIntent,
    Patient,
    PatientProfile,
    StoredRecord,
    from_record,
    model_scope,
    record_item,
    to_record,
)
from store.account_fixtures import PATIENT, callback, update
from store.scribe_fixtures import ScribeWorld


def no_problem(request: dict[str, Any]) -> dict[str, Any]:
    """The default problem reading: the message states no problem."""
    return candidate({"problems": []})


def no_problem_readers(registry: ModelRegistry, role: ModelRole) -> Model:
    """Problem readers scripted apart from the answer model, so answers are never consumed."""
    return ScriptedModel(lambda request: no_problem(request))


class PatientWorld(ScribeWorld):
    patient_scope: PatientScope
    next_message: int = 1000

    @property
    def profile(self) -> PatientProfile:
        profile = self.store.get_patient_profile(self.patient_scope)
        assert profile
        return profile

    @property
    def concierge(self) -> ConciergeTurn:
        return cast(ConciergeTurn, self.app.state.concierge)

    def enroll(self, *, medication: bool = True) -> Patient:
        self.approve()
        patient = self.bound()
        self.patient_scope = patient.scope
        self.concierge.synthetic = True
        if medication:
            self.dictate(
                "Synthetic Patient ابدأ أتورفاستاتين 40 مج مرة يوميا بالليل",
                {
                    "patient": {"name_as_spoken": "Synthetic Patient"},
                    "orders": [
                        {
                            "action": "start",
                            "drug": "أتورفاستاتين",
                            "dose": "40 مج",
                            "frequency": "مرة يوميا",
                            "timing": "بالليل",
                        }
                    ],
                },
                id=400,
            )
            self.tap(id=401)
        return patient

    def send(
        self, text: str, value: dict[str, object] | None = None, *, id: int | None = None
    ) -> tuple[ScriptedModel, OutboundIntent]:
        id = id or self.next_message
        self.next_message = id + 1
        model = ScriptedModel(
            candidate(
                value
                or {
                    "reply": "السؤال ده محتاج الدكتور",
                    "kind": "cannot_answer",
                    "needs_doctor": True,
                }
            )
        )
        if self.concierge.barrier_model_factory is None:
            # Readers a test installed explicitly are kept.
            self.concierge.barrier_model_factory = no_problem_readers
        if self.concierge.schedule_model_factory is None:
            self.concierge.schedule_model_factory = lambda registry, role: ScriptedModel(
                candidate({"asserted": False, "times": []})
            )
        self.concierge.model_factory = lambda registry, role: model
        assert self.post(update(PATIENT, text, id)).status_code == 200
        receipt = self.receipt(id)
        assert receipt.state == "completed", receipt.state
        intents = [
            i for i in self.patient_intents() if "patient-turn:" + receipt.id in i.source_event_ids
        ]
        assert intents, [r.body for r in self.rows("audit_event")]
        return model, intents[-1]

    def rows(self, kind: str) -> list[StoredRecord]:
        from sanad.steward.types import records

        return list(records(self.store, self.patient_scope, kind))

    def patient_intents(self) -> list[OutboundIntent]:
        return [
            from_record(r, OutboundIntent)
            for r in self.rows("outbound_intent")
            if r.body.get("audience") == "patient"
        ]

    def press(self, intent: OutboundIntent, *, index: int = 0, id: int = 2000) -> None:
        assert intent.payload
        markup = intent.payload["reply_markup"]
        assert isinstance(markup, dict)
        keyboard = markup["inline_keyboard"]
        assert isinstance(keyboard, list)
        row = keyboard[index]
        assert isinstance(row, list)
        button = row[0]
        assert isinstance(button, dict)
        assert self.post(callback(str(button["callback_data"]), PATIENT, id)).status_code == 200

    def seed(self, model: BaseModel) -> None:
        row = to_record(model, model_scope(model))
        old = self.store.get(model_scope(model), row.entity_type, row.id)
        assert self.store._atomic([Write(record_item(row), old.version if old else None)], [])
