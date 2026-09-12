"""Required English citations at the schema and real turn boundaries."""

import hashlib
from typing import Any

import pytest
from providers.fixtures import ScriptedModel, candidate
from pydantic import ValidationError, create_model
from store.account_fixtures import APPLICANT, update

from sanad.agents.schema import describe_schema
from sanad.scribe.extract import (
    DictationCandidate,
    EnglishDictationCandidate,
    EnglishOrderCandidate,
    candidate_issues,
)
from scribe.test_grounding_invariant import world_for


def test_english_required_description_and_frozen_arabic_description() -> None:
    envelope = create_model("Envelope", value=(EnglishDictationCandidate, ...))
    assert (
        "- value.orders[].action_quote: string; required; minLength: 1; maxLength: 200; "
        "The exact words, copied from the same sentence as the drug, "
        "that told you this order's action. Always present.."
    ) in describe_schema(envelope)
    assert hashlib.sha256(describe_schema(DictationCandidate).encode()).hexdigest() == (
        "1422c472f3372b8338369f757b1111540fa4d2a1ce0c2f683fcd23482edd7af8"
    )


@pytest.mark.parametrize("quote", [None, "", "  ", "null", "none"])
def test_english_rejects_invalid_citation(quote: str | None) -> None:
    with pytest.raises(ValidationError):
        EnglishDictationCandidate.model_validate(
            {"orders": [{"action": "start", "drug": "Forxiga", "action_quote": quote}]}
        )


def test_missing_citation_is_required_but_legacy_still_loads() -> None:
    raw = {"orders": [{"action": "start", "drug": "Forxiga"}]}
    with pytest.raises(ValidationError) as error:
        EnglishDictationCandidate.model_validate(raw)
    assert error.value.errors()[0]["loc"] == ("orders", 0, "action_quote")
    assert DictationCandidate.model_validate(raw).orders[0].action_quote is None
    assert EnglishOrderCandidate(
        action="start", drug="Forxiga", action_quote=" give "
    ).action_quote == ("give")


@pytest.mark.parametrize("quote", [False, True])
def test_invalid_action_without_citation_uses_clarification(quote: bool) -> None:
    order: dict[str, Any] = {"action": "add", "drug": "Forxiga"}
    if quote:
        order["action_quote"] = "add"
    value = EnglishDictationCandidate.model_validate({"orders": [order]})
    assert not value.orders
    assert value.ambiguities == (("add Forxiga",) if quote else ())
    assert any(i.code == "clarification" for i in candidate_issues(value, "add Forxiga"))


@pytest.mark.parametrize("recover", [False, True])
def test_missing_english_citation_retries_each_reader_once(recover: bool) -> None:
    world = world_for("en")
    missing = {"orders": [{"action": "start", "drug": "Forxiga"}]}
    fixed = {"orders": [{"action": "start", "drug": "Forxiga", "action_quote": "Start"}]}
    models: list[ScriptedModel] = []

    def factory(*args: Any) -> ScriptedModel:
        model = ScriptedModel(candidate(fixed if recover and len(models) >= 2 else missing))
        models.append(model)
        return model

    retries: list[str] = []
    world.scribe.model_factory = factory
    world.scribe.observe_retry = retries.append
    assert world.post(update(APPLICANT, "Start Forxiga", 10)).status_code == 200
    assert world.receipt(10).state == "completed"
    assert retries == ["schema_validation", "schema_validation"]
    assert len(models) == 4 and all(len(m.script.calls) == 1 for m in models)
    pending = world.scribe.repo.pending(world.doctor.scope)
    if recover:
        assert pending and pending.candidate.orders[0].action_quote == "Start"
    else:
        assert pending is None
        assert any(i.template_id == "doctor_model_unavailable" for i in world.cards())


def test_overlong_citation_is_dropped_by_existing_sanitizer() -> None:
    value = EnglishDictationCandidate.model_validate(
        {"orders": [{"action": "start", "drug": "Forxiga", "action_quote": "x" * 201}]}
    )
    assert not value.orders and not value.ambiguities
    assert any(i.code == "clarification" for i in candidate_issues(value, "Start Forxiga"))
