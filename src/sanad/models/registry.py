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
    cross_check: Literal["gemini-3.5-flash-lite"] = "gemini-3.5-flash-lite"
    classifier: Literal["us.amazon.nova-micro-v1:0"] = "us.amazon.nova-micro-v1:0"
    vision: Literal["gemini-3.8-flash"] = "gemini-3.8-flash"
    speech: Literal["gemini-3.8-flash"] = "gemini-3.8-flash"

    @property
    def rejected(self) -> tuple[RejectedModel, ...]:
        return (
            RejectedModel(
                model_id="gemini-2.5-flash",
                reason="retired for new projects, 404 on 2026-09-10",
            ),
            RejectedModel(
                model_id="gemini-3-flash-preview",
                reason="thinking exhausts the output budget; preview",
            ),
            RejectedModel(
                model_id="mistral.voxtral-small-24b-2507",
                reason="Misheard follow-up and BUN, creat; lane/spikes/bakeoff-2026-09-10.json",
            ),
            RejectedModel(
                model_id="us.amazon.nova-lite-v1:0",
                reason=(
                    "Vision: 0 of 3 handwritten drugs and 4 invented; "
                    "lane/spikes/bakeoff-2026-09-10.json"
                ),
            ),
            RejectedModel(
                model_id="whisper-large-v3-turbo",
                reason=(
                    "Misheard follow-up, BUN, creat, Exforge and Forxiga; "
                    "lane/spikes/bakeoff-2026-09-10.json"
                ),
            ),
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
