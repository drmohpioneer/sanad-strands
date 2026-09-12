import pytest

from sanad.agents.factory import bedrock_model
from sanad.api.lambda_entry import media_caller
from sanad.models.gemini import GeminiCaller
from sanad.models.registry import ModelRegistry, ModelRole


def test_media_roles_and_refusal() -> None:
    registry = ModelRegistry()
    assert registry.speech == registry.vision == "gemini-3.8-flash"
    assert registry.cross_check == "gemini-3.5-flash-lite"
    roles: tuple[ModelRole, ...] = ("vision", "cross_check", "speech")
    for role in roles:
        with pytest.raises(AssertionError, match="media-only"):
            bedrock_model(registry, role)
    for model_id in (registry.speech, registry.vision, registry.cross_check):
        assert isinstance(media_caller(model_id, "synthetic", "synthetic"), GeminiCaller)
    rejected = {r.model_id: r.reason for r in registry.rejected}
    for rejected_id in (
        "mistral.voxtral-small-24b-2507",
        "whisper-large-v3-turbo",
        registry.worker,
    ):
        assert "lane/spikes/bakeoff-2026-09-10.json" in rejected[rejected_id]
    assert rejected["gemini-2.5-flash"] == "retired for new projects, 404 on 2026-09-10"
    assert rejected["gemini-3-flash-preview"] == "thinking exhausts the output budget; preview"
