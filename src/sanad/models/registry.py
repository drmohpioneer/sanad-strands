"""Model selection from the 2026-09-06 account measurements, never model input."""

from typing import Literal

from sanad.domain.boundaries import _BoundaryValue

type ModelRole = Literal["worker", "cross_check", "classifier", "vision", "speech"]


class RejectedModel(_BoundaryValue):
    model_id: str
    reason: str


class ModelRegistry(_BoundaryValue):
    region: Literal["us-east-1"] = "us-east-1"
    temperature: Literal[0] = 0
    worker: Literal["us.amazon.nova-lite-v1:0"] = "us.amazon.nova-lite-v1:0"
    cross_check: Literal["us.amazon.nova-pro-v1:0"] = "us.amazon.nova-pro-v1:0"
    classifier: Literal["us.amazon.nova-micro-v1:0"] = "us.amazon.nova-micro-v1:0"
    vision: Literal["us.amazon.nova-lite-v1:0"] = "us.amazon.nova-lite-v1:0"
    speech: Literal["mistral.voxtral-small-24b-2507"] = "mistral.voxtral-small-24b-2507"

    @property
    def rejected(self) -> tuple[RejectedModel, ...]:
        return (
            RejectedModel(
                model_id="us.amazon.nova-2-lite-v1:0",
                reason="Silently dropped a doctor's instruction in structured extraction.",
            ),
            RejectedModel(
                model_id="mistral.voxtral-mini-3b-2507",
                reason="Misheard 200 as 100 in the measured Egyptian clip.",
            ),
        )

    def model_id(self, role: ModelRole) -> str:
        return str(getattr(self, role))
