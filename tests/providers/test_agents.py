import asyncio
import json
import logging
from dataclasses import replace
from typing import Any, Literal, cast

import pytest
from pydantic import Field, ValidationError
from store.fixtures import ACTOR, SCOPE
from strands import Agent
from strands.hooks import BeforeToolCallEvent
from strands.types.tools import ToolContext

from sanad.agents.factory import Proposal, ProposalFailure, make_agent
from sanad.agents.hooks import Guard
from sanad.agents.hygiene import clean_text, json_object
from sanad.agents.schema import describe_schema
from sanad.agents.tools import ALLOWLIST, AgentRole, AgentScope, scoped_tool
from sanad.domain.boundaries import _BoundaryValue
from sanad.models.io import BedrockCaller, CallMetadata, ModelUnavailable
from sanad.models.registry import ModelRegistry
from sanad.safety.models import OutputContext
from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT as POLICY

from .fixtures import SOURCE, ScriptedConverse, ScriptedModel, candidate, response


class Answer(_BoundaryValue):
    text: str = Field(min_length=1)


def binding(**changes: Any) -> AgentScope:
    return replace(AgentScope(ACTOR, SCOPE, SOURCE, POLICY, lambda: True), **changes)


def agent(model: ScriptedModel, *, role: AgentRole = "scribe", **changes: Any) -> Any:
    return make_agent(
        role,
        scope=binding(),
        tools=[],
        system_prompt="Synthetic test",
        session_key="synthetic",
        model_factory=lambda registry, selected: model,
        **changes,
    )


def test_registry_is_frozen_and_exact() -> None:
    registry = ModelRegistry()
    assert registry.worker == registry.vision == "us.amazon.nova-lite-v1:0"
    assert registry.cross_check == "us.amazon.nova-pro-v1:0"
    assert registry.classifier == "us.amazon.nova-micro-v1:0"
    assert registry.speech == "mistral.voxtral-small-24b-2507"
    assert {r.model_id for r in registry.rejected} == {
        "us.amazon.nova-2-lite-v1:0",
        "mistral.voxtral-mini-3b-2507",
    }
    with pytest.raises(ValidationError):
        ModelRegistry.model_validate({"temperature": 1})
    with pytest.raises(ValidationError):
        field = "worker"
        setattr(registry, field, "us.amazon.nova-lite-v1:0")


# Independent expected outcomes, not generated from the implementation allow-list.
GUARD_TABLE: list[tuple[AgentRole, str, dict[str, Any], str | None]] = [
    ("scribe", "get_record", {"payload": {}}, None),
    ("scribe", "send_message", {"payload": {}}, "tool_not_allowed"),
    ("scribe", "SanadCandidate", {"value": {"text": "old tool"}}, "tool_not_allowed"),
    ("concierge", "get_plan", {"payload": {}}, None),
    ("concierge", "get_record", {"payload": {}}, "tool_not_allowed"),
    ("coordinator", "pause_mission", {"payload": {}}, None),
    ("resolver", "find_places", {"payload": {}}, None),
    ("evidence_reader", "read_scoped_file", {"payload": {}}, None),
    ("liaison", "get_report_facts", {"payload": {}}, None),
    ("classifier", "classify", {"payload": {}}, None),
    ("worker", "classify", {"payload": {}}, None),
    ("cross_check", "classify", {"payload": {}}, None),
    ("scribe", "get_record", {"payload": {"patient_id": "foreign"}}, "scope_mismatch"),
    ("scribe", "get_record", {"payload": {"doctor_id": SCOPE.doctor_id}}, "scope_argument"),
    ("scribe", "get_record", {"payload": {"nested": [{"scope": "other"}]}}, "scope_mismatch"),
]


@pytest.mark.parametrize("role,name,args,expected", GUARD_TABLE)
def test_guard_table(
    role: AgentRole, name: str, args: dict[str, Any], expected: str | None
) -> None:
    context = binding()
    guard = Guard(role, context)
    event = BeforeToolCallEvent(
        agent=cast(Agent, None),
        selected_tool=None,
        tool_use={"name": name, "toolUseId": "synthetic", "input": args},
        invocation_state={"sanad_scope": context},
    )
    guard.before_tool(event)
    assert event.cancel_tool == (expected or False)
    assert len(guard.refusals) == bool(expected)


def test_guard_seventh_call_and_twenty_second_boundary() -> None:
    context = binding()
    time = [0.0]
    guard = Guard("scribe", context, clock=lambda: time[0])

    def event() -> BeforeToolCallEvent:
        return BeforeToolCallEvent(
            agent=cast(Agent, None),
            selected_tool=None,
            tool_use={"name": "get_record", "toolUseId": "test", "input": {"payload": {}}},
            invocation_state={"sanad_scope": context},
        )

    for _ in range(6):
        e = event()
        guard.before_tool(e)
        assert not e.cancel_tool
    e = event()
    guard.before_tool(e)
    assert e.cancel_tool == "tool_budget"
    guard = Guard("scribe", context, clock=lambda: time[0])
    time[0] = 20
    e = event()
    guard.before_tool(e)
    assert e.cancel_tool == "wall_clock"


def test_real_sdk_hook_prevents_tool_body() -> None:
    context = binding()
    entered = []

    def body(value: Answer) -> Answer:
        entered.append(True)
        return value

    tool = scoped_tool("erase_record", Answer, body, binding=context)
    scripted = ScriptedModel(response(calls=[("erase_record", {"payload": {"text": "hello"}})]))
    instance = make_agent(
        "scribe",
        scope=context,
        tools=[tool],
        system_prompt="test",
        session_key="test",
        model_factory=lambda registry, selected: scripted,
    )
    result = asyncio.run(instance.propose(Answer, "synthetic source"))
    assert isinstance(result, ProposalFailure) and result.refusals[0].reason == "tool_not_allowed"
    assert entered == [] and len(scripted.script.calls) == 1


def test_tool_body_refuses_even_without_hook() -> None:
    context = binding()
    entered = []

    def body(value: Answer) -> Answer:
        entered.append(True)
        return value

    tool = scoped_tool("get_record", Answer, body, binding=context)
    use: Any = {
        "name": "get_record",
        "toolUseId": "x",
        "input": {"payload": {"patient_id": "other"}},
    }
    sdk_tool: Any = tool.sdk_tool
    result = sdk_tool(
        payload={"text": "test"}, tool_context=ToolContext(use, None, {"sanad_scope": context})
    )
    assert result["status"] == "refused" and not entered


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("<thinking>private</thinking> أهلاً", "أهلاً"),
        ("<thinking>private", ""),
        ("Analysis: private\nFinal answer: أهلاً", "أهلاً"),
        ("Reasoning: private", ""),
        ("Final: أهلاً", "أهلاً"),
        ("<thinking>private</thinking>", ""),
    ],
)
def test_hygiene_table(raw: str, expected: str) -> None:
    assert clean_text(raw) == expected


@pytest.mark.parametrize(
    "text,reason",
    [
        ("<thinking>hidden</thinking>أهلاً بيك", None),
        ("I cannot do that.", "language_drift"),
        ("متقلقش", "unsafe_output"),
        ("زوّد الجرعة إلى ٥ مجم", "unsafe_output"),
        ("<thinking>hidden</thinking>", "schema_validation"),
    ],
)
def test_patient_gate_before_proposal(text: str, reason: str | None) -> None:
    context = binding(output_context=OutputContext(mode="plan_explanation", language="ar"))
    model = ScriptedModel(candidate({"text": text}))
    instance = make_agent(
        "concierge",
        scope=context,
        tools=[],
        system_prompt="test",
        session_key="x",
        model_factory=lambda registry, selected: model,
    )
    result = asyncio.run(instance.propose(Answer, "أهلاً", patient_fields=("text",)))
    if reason:
        assert isinstance(result, ProposalFailure) and result.reason == reason
    else:
        assert isinstance(result, Proposal) and result.value.text == "أهلاً بيك"


def test_structured_candidate_provenance_and_no_partial_result() -> None:
    instance = agent(
        ScriptedModel(candidate({"text": "source"}, [{"field": "text", "start": 0, "end": 6}]))
    )
    result = asyncio.run(instance.propose(Answer, "source"))
    assert isinstance(result, Proposal) and result.unsupported_spans == ()
    assert result.provenance[0].source_span is not None
    assert result.metadata[0].input_tokens == 100
    invalid = asyncio.run(
        agent(ScriptedModel(candidate({"missing": "value"}))).propose(Answer, "test")
    )
    assert isinstance(invalid, ProposalFailure) and invalid.reason == "schema_validation"
    assert not hasattr(invalid, "value")


def test_missing_or_out_of_bounds_span_stays_unsupported() -> None:
    result = asyncio.run(
        agent(
            ScriptedModel(candidate({"text": "test"}, [{"field": "text", "start": 0, "end": 1000}]))
        ).propose(Answer, "source")
    )
    assert isinstance(result, Proposal) and result.unsupported_spans == ("text",)
    assert result.provenance[0].source_span is None


def test_proposal_plain_json_never_uses_tool_forced_output(monkeypatch: pytest.MonkeyPatch) -> None:
    model = ScriptedModel(candidate({"text": "أهلاً"}))
    instance = agent(model)
    original = instance.sdk.invoke_async
    invocations = []

    async def invoke(*args: Any, **kwargs: Any) -> Any:
        invocations.append(kwargs)
        assert "structured_output_model" not in kwargs
        return await original(*args, **kwargs)

    monkeypatch.setattr(instance.sdk, "invoke_async", invoke)
    result = asyncio.run(instance.propose(Answer, "أهلاً"))
    assert isinstance(result, Proposal)
    assert len(invocations) == len(model.script.calls) == 1
    assert "toolConfig" not in model.script.calls[0]
    assert result.unsupported_spans == ("text",)
    prompt = model.script.calls[0]["messages"][-1]["content"][0]["text"]
    assert "one JSON object and nothing else" in prompt
    assert "value.text: string" in prompt
    assert "spans[]: object" in prompt and "spans[].field: string" in prompt
    assert "spans[].start: integer" in prompt and "spans[].end: integer" in prompt


@pytest.mark.parametrize(
    "reply",
    [
        "invalid JSON",
        '{"value":{"text":"a"}}{"value":{"text":"b"}}',
        '{"value":{"text":"a","text":"b"}}',
        '{"value":{}}',
    ],
)
def test_invalid_plain_json_fails_once_without_validation_retries(reply: str) -> None:
    model = ScriptedModel(response(reply), candidate({"text": "must not retry"}))
    result = asyncio.run(agent(model).propose(Answer, "synthetic"))
    assert isinstance(result, ProposalFailure) and result.reason == "schema_validation"
    assert len(model.script.calls) == len(result.metadata) == 1
    assert not hasattr(result, "value")


@pytest.mark.parametrize(
    "spans",
    [
        {"start": 0, "end": 2},
        [{"field": "text", "start": 2, "end": 1}],
        [{"field": "text", "start": -1, "end": 1}],
        [None, "bad", {}, {"field": "text", "start": "bad", "end": 1}],
        [{"field": "value.text", "start": 0, "end": 2}],
        [{"field": "text[]", "start": 0, "end": 2}],
    ],
)
def test_malformed_spans_leave_candidate_usable_without_retry(spans: object) -> None:
    model = ScriptedModel(response(json.dumps({"value": {"text": "ok"}, "spans": spans})))
    result = asyncio.run(agent(model).propose(Answer, "ok"))
    assert isinstance(result, Proposal) and result.value.text == "ok"
    assert result.unsupported_spans == ("text",)
    assert result.provenance[0].source_span is None
    assert len(model.script.calls) == len(result.metadata) == 1


def test_malformed_span_does_not_discard_valid_sibling_claim() -> None:
    model = ScriptedModel(
        response(
            json.dumps(
                {
                    "value": {"text": "ok"},
                    "spans": [
                        {"field": "text", "start": 0, "end": 2},
                        {"field": "unknown", "start": "bad", "end": 1},
                    ],
                }
            )
        )
    )
    result = asyncio.run(agent(model).propose(Answer, "ok"))
    assert isinstance(result, Proposal) and result.unsupported_spans == ()
    assert result.provenance[0].source_span is not None


def test_opt_out_omits_span_request_and_ignores_unsolicited_claims() -> None:
    from sanad.agents.factory import propose

    model = ScriptedModel(candidate({"text": "ok"}, [{"field": "text", "start": 0, "end": 2}]))
    instance = agent(model)
    result = asyncio.run(propose("scribe", Answer, "ok", agent=instance, want_spans=False))
    assert isinstance(result, Proposal) and result.unsupported_spans == ("text",)
    assert result.provenance[0].source_span is None
    prompt = model.script.calls[0]["messages"][-1]["content"][0]["text"]
    assert "spans" not in prompt and "value.text: string" in prompt
    assert len(model.script.calls) == 1


def test_plain_json_hygiene_preserves_list_spans_and_filters_unsupported_claims() -> None:
    raw = {
        "value": {"text": "<thinking>private</thinking>أهلاً"},
        "spans": [
            {"field": "text", "start": 0, "end": 4},
            {"field": "unknown", "start": 0, "end": 4},
        ],
    }
    model = ScriptedModel(response("Here: ```json\n" + json.dumps(raw) + "\n```"))
    result = asyncio.run(agent(model).propose(Answer, "أهلاً"))
    assert isinstance(result, Proposal) and result.value.text == "أهلاً"
    assert len(result.provenance) == 1 and result.unsupported_spans == ()
    span = result.provenance[0].source_span
    assert span is not None and span.model_dump() == {"kind": "text", "start": 0, "end": 4}
    duplicate = ScriptedModel(
        candidate(
            {"text": "ok"},
            [
                {"field": "text", "start": 0, "end": 2},
                {"field": "text", "start": 1, "end": 2},
            ],
        )
    )
    result = asyncio.run(agent(duplicate).propose(Answer, "ok"))
    assert isinstance(result, Proposal) and result.unsupported_spans == ("text",)


@pytest.mark.parametrize("echo_request", [False, True])
def test_proposal_rejects_schema_description_echo(echo_request: bool) -> None:
    def echo(kwargs: dict[str, Any]) -> dict[str, Any]:
        prompt = kwargs["messages"][-1]["content"][0]["text"]
        text = (
            prompt
            if echo_request
            else prompt.split("\nSource JSON:")[0].split("Do not repeat the schema description.\n")[
                1
            ]
        )
        return response("<thinking>private</thinking>\n" + text)

    model = ScriptedModel(echo)
    result = asyncio.run(agent(model).propose(Answer, "source"))
    assert isinstance(result, ProposalFailure) and result.reason == "template_echo"
    assert len(model.script.calls) == 1


def test_schema_description_lists_nested_types_allowed_values_and_constraints() -> None:
    class Item(_BoundaryValue):
        kind: Literal["lab", "prescription"]
        dose: str | None = Field(description="Copy only source quantities.")

    class Batch(_BoundaryValue):
        items: list[Item]
        count: int = Field(ge=0, le=3)

    description = describe_schema(Batch)
    assert 'items[].kind: string; required; allowed values: "lab", "prescription"' in description
    assert "items[].dose: string" in description and "items[].dose: null" in description
    assert "Copy only source quantities." in description
    assert "minimum: 0" in description and "maximum: 3" in description
    assert "{" not in description


def test_json_inside_prose_two_objects_and_duplicate_key() -> None:
    assert json_object('Here: ```json\n{"a":1}\n```') == {"a": 1}
    for text in ('{"a":1}{"b":2}', '{"a":1,"a":2}', "not json"):
        with pytest.raises(ValueError):
            json_object(text)


def test_unavailable_and_classifier_cannot_extract() -> None:
    result = asyncio.run(agent(ScriptedModel(TimeoutError("private payload"))).propose(Answer, "x"))
    assert isinstance(result, ModelUnavailable)
    model = ScriptedModel()
    result = asyncio.run(agent(model, role="classifier").propose(Answer, "x"))
    assert isinstance(result, ProposalFailure) and result.reason == "classification_only"
    assert model.script.calls == []


def test_provider_metadata_and_no_exception_content(caplog: pytest.LogCaptureFixture) -> None:
    client = ScriptedConverse(response("<thinking>secret</thinking>reply"))
    seen: list[CallMetadata] = []
    caller = BedrockCaller(client, "synthetic-policy", observe=seen.append)
    result = asyncio.run(caller.call("test-model", [{"text": "private input"}]))
    assert not isinstance(result, ModelUnavailable) and seen[0].output_tokens == 25
    assert "private input" not in repr(seen)
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("botocore.endpoint").error("private input")
    assert "private input" not in caplog.text


def test_fresh_agents_never_share_context() -> None:
    a, b = agent(ScriptedModel()), agent(ScriptedModel())
    assert a.sdk is not b.sdk and a.sdk.messages is not b.sdk.messages
    assert a.sdk._session_manager is None
    assert set(ALLOWLIST) >= {
        "scribe",
        "concierge",
        "coordinator",
        "resolver",
        "evidence_reader",
        "liaison",
    }


def test_turn_timeout_is_bounded_and_cancels_work(monkeypatch: pytest.MonkeyPatch) -> None:
    instance = agent(ScriptedModel())
    cancelled = []

    async def blocked(*args: Any, **kwargs: Any) -> Any:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(instance.sdk, "invoke_async", blocked)
    result = asyncio.run(instance.propose(Answer, "private synthetic source", timeout=0.01))
    assert isinstance(result, ModelUnavailable) and result.reason == "timeout"
    assert cancelled == [True] and not hasattr(result, "value")
    with pytest.raises(ValueError, match="25"):
        asyncio.run(agent(ScriptedModel()).propose(Answer, "x", timeout=25.1))


def test_actual_allowed_tool_returns_typed_proposal_with_bound_context() -> None:
    context = binding()
    entered = []

    def body(value: Answer) -> Answer:
        entered.append(value.text)
        return Answer(text="scoped read")

    tool = scoped_tool("get_record", Answer, body, binding=context)
    model = ScriptedModel(
        response(calls=[("get_record", {"payload": {"text": "test"}})]),
        candidate({"text": "result"}),
    )
    instance = make_agent(
        "scribe",
        scope=context,
        tools=[tool],
        system_prompt="test",
        session_key="x",
        model_factory=lambda registry, selected: model,
    )
    result = asyncio.run(instance.propose(Answer, "test"))
    assert isinstance(result, Proposal) and entered == ["test"]
    assert len(result.metadata) == 2
    assert "tool_context" not in str(tool.sdk_tool.tool_spec["inputSchema"])
