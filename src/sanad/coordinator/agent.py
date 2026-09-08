"""One tool-free Strands turn selecting identifiers; no mutation or clinical prose."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from queue import Empty, Queue
from threading import Thread
from time import monotonic
from typing import Literal

from pydantic import Field, ValidationError
from strands.hooks import BeforeModelCallEvent, BeforeToolCallEvent
from strands.models import Model

from sanad.agents.factory import bedrock_model, make_agent
from sanad.agents.tools import AgentScope
from sanad.coordinator import policy
from sanad.coordinator.permitted import Permitted
from sanad.domain.boundaries import NonblankStr, _BoundaryValue
from sanad.models.registry import ModelRegistry, ModelRole

type RefusalReason = Literal[
    "no_contact_plan",
    "scope_mismatch",
    "mission_ineligible",
    "followup_ineligible",
    "unsupported_contact",
    "empty_proposal",
    "invalid_shape",
    "unknown_move",
    "pause_redundant",
    "unknown_fact",
    "duplicate_fact",
    "incomplete_facts",
    "provider_timeout",
    "provider_error",
    "provider_refusal",
    "tool_loop",
    "stale_source",
    "attempt_already_started",
    "contact_window_short",
    "output_validation",
    "safety_kernel",
]


class ContactProposal(_BoundaryValue):
    move: NonblankStr
    fact_ids: tuple[NonblankStr, ...] = Field(max_length=256)


@dataclass(frozen=True)
class Refusal:
    reason: RefusalReason


def validate(raw: object, bundle: Permitted) -> ContactProposal | Refusal:
    if not raw:
        return Refusal("empty_proposal")
    if isinstance(raw, dict) and raw.get("move") == "pause_mission":
        return Refusal("pause_redundant")
    try:
        proposal = ContactProposal.model_validate(raw)
    except ValidationError:
        return Refusal("invalid_shape")
    move = next((m for m in bundle.moves if m.id == proposal.move), None)
    if move is None:
        return Refusal("unknown_move")
    if any(id not in move.fact_ids for id in proposal.fact_ids):
        return Refusal("unknown_fact")
    if len(set(proposal.fact_ids)) != len(proposal.fact_ids):
        return Refusal("duplicate_fact")
    if set(proposal.fact_ids) != set(move.fact_ids):
        return Refusal("incomplete_facts")
    return proposal


def model_factory(registry: ModelRegistry, role: ModelRole) -> Model:
    return bedrock_model(registry, role, timeout=policy.call_timeout_s)


async def propose(bundle: Permitted, scope: AgentScope) -> ContactProposal | Refusal:
    """No SDK proposal helper: its generic eight-turn ceiling is too permissive here."""
    calls = 0
    tools_seen = False

    def before_model(event: BeforeModelCallEvent) -> None:
        nonlocal calls
        calls += 1
        if calls > policy.max_turns or not scope.valid():
            event.cancel = "coordinator_turn_limit"

    def before_tool(event: BeforeToolCallEvent) -> None:
        nonlocal tools_seen
        tools_seen = True
        event.cancel_tool = "coordinator_no_tools"

    try:
        async with asyncio.timeout(policy.call_timeout_s):
            agent = make_agent(
                "coordinator",
                scope=scope,
                tools=[],
                system_prompt=(
                    "Choose one permitted move and order all its fact IDs. Return only JSON "
                    '{"move":"permitted identifier","fact_ids":["permitted identifiers"]}. '
                    "Never write a sentence, value, time, place or new fact. No tools."
                ),
                session_key=f"coordinator:{bundle.source_id}",
                model_factory=model_factory,
            )
            agent.sdk.hooks.add_callback(BeforeModelCallEvent, before_model)
            agent.sdk.hooks.add_callback(BeforeToolCallEvent, before_tool)
            prompt = json.dumps(
                {
                    "first_contact": bundle.first_contact,
                    "pre_visit_brief": bundle.pre_visit_brief,
                    "moves": [{"id": m.id, "fact_ids": m.fact_ids} for m in bundle.moves],
                    "facts": [{"id": f.id, "kind": f.kind, "value": f.value} for f in bundle.facts],
                },
                ensure_ascii=False,
            )
            result = await agent.sdk.invoke_async(
                prompt,
                invocation_state={"sanad_scope": scope},
                limits={"turns": 1, "output_tokens": 2048, "total_tokens": 8000},
            )
            if tools_seen or calls > 1:
                return Refusal("tool_loop")
            if not scope.valid():
                return Refusal("stale_source")
            blocks = result.message["content"]
            if any("toolUse" in block for block in blocks):
                return Refusal("tool_loop")
            body = "\n".join(b["text"] for b in blocks if "text" in b)
            if not body.strip():
                return Refusal("empty_proposal")

            def unique_fields(items: list[tuple[str, object]]) -> dict[str, object]:
                value = dict(items)
                if len(value) != len(items):
                    raise ValueError("duplicate_field")
                return value

            raw = json.loads(body, object_pairs_hook=unique_fields)
            if raw == {"refused": True}:
                return Refusal("provider_refusal")
            return validate(raw, bundle)
    except TimeoutError:
        return Refusal("provider_timeout")
    except (ValueError, ValidationError):
        return Refusal("invalid_shape")
    except Exception:
        return Refusal("tool_loop" if tools_seen or calls > 1 else "provider_error")


def bounded_call(
    bundle: Permitted, scope_factory: Callable[[Callable[[], bool]], AgentScope]
) -> ContactProposal | Refusal:
    """A blocking/uncancellable SDK adapter cannot hold the scheduling worker.

    The daemon owns no builder or write capability. A late result is discarded;
    scope freshness also expires before any late model invocation can begin.
    """
    deadline = monotonic() + policy.call_timeout_s
    scope = scope_factory(lambda: monotonic() < deadline)
    outcomes: Queue[ContactProposal | Refusal] = Queue(maxsize=1)

    def work() -> None:
        try:
            outcomes.put(asyncio.run(propose(bundle, scope)))
        except Exception:
            outcomes.put(Refusal("provider_error"))

    Thread(target=work, name="sanad-coordinator", daemon=True).start()
    try:
        return outcomes.get(timeout=max(0, deadline - monotonic()))
    except Empty:
        return Refusal("provider_timeout")
