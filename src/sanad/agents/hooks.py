"""Defense in depth: cancellation occurs before the selected tool body."""

from collections.abc import Callable
from time import monotonic
from typing import Any

from strands.hooks import BeforeModelCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry

from sanad.agents.tools import ALLOWLIST, AgentRole, AgentScope, scope_argument
from sanad.domain.boundaries import _BoundaryValue


class GuardRefusal(_BoundaryValue):
    role: AgentRole
    tool_name: str
    reason: str
    ordinal: int


class Guard(HookProvider):
    def __init__(
        self,
        role: AgentRole,
        binding: AgentScope,
        *,
        clock: Callable[[], float] = monotonic,
    ):
        self.role, self.binding, self.clock = role, binding, clock
        self.started = clock()
        self.calls = 0
        self.refusals: list[GuardRefusal] = []
        self.failure: str | None = None

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before_tool)
        registry.add_callback(BeforeModelCallEvent, self.before_model)

    def before_model(self, event: BeforeModelCallEvent) -> None:
        if self.refusals or self.failure or self.clock() - self.started >= 20:
            self.failure = self.failure or "guard_refused"
            event.cancel = self.failure

    def before_tool(self, event: BeforeToolCallEvent) -> None:
        self.calls += 1
        name = str(event.tool_use.get("name", ""))
        reason = scope_argument(event.tool_use.get("input", {}), self.binding.scope)
        if (
            not self.binding.valid()
            or event.invocation_state.get("sanad_scope") is not self.binding
        ):
            reason = "scope_unavailable"
        elif name not in ALLOWLIST[self.role]:
            reason = "tool_not_allowed"
        elif self.calls > 6:
            reason = "tool_budget"
        elif self.clock() - self.started >= 20:
            reason = "wall_clock"
        if reason:
            event.cancel_tool = reason
            self.refusals.append(
                GuardRefusal(
                    role=self.role,
                    tool_name=name if name in ALLOWLIST[self.role] else "unlisted",
                    reason=reason,
                    ordinal=self.calls,
                )
            )
