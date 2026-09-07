"""Only server-bound tools may enter the factory. Tool results remain proposals."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, JsonValue, ValidationError
from strands import tool
from strands.types.tools import AgentTool, ToolContext

from sanad.domain import Principal, Provenance, TenantScope
from sanad.domain.boundaries import _BoundaryValue
from sanad.safety.models import OutputContext
from sanad.safety.policy import SafetyPolicy
from sanad.store.keys import AccountScope, Scope
from sanad.store.records import WorkerCapability

type AgentRole = Literal[
    "scribe",
    "concierge",
    "coordinator",
    "resolver",
    "evidence_reader",
    "liaison",
    "worker",
    "cross_check",
    "classifier",
    "vision",
]

ALLOWLIST: dict[AgentRole, frozenset[str]] = {
    "scribe": frozenset(
        {
            "find_patient",
            "get_record",
            "propose_new_patient",
            "propose_update",
            "propose_missions",
            "answer",
            "lookup_drug",
        }
    ),
    "concierge": frozenset({"get_plan", "get_education", "propose_report", "propose_question"}),
    "coordinator": frozenset(
        {
            "schedule_next_contact",
            "request_missing_evidence",
            "classify_barrier",
            "escalate_barrier",
            "mark_evidence_received",
            "close_verified_mission",
            "pause_mission",
        }
    ),
    "resolver": frozenset(
        {"ask_patient", "find_places", "reschedule_visit", "resume_chase", "hand_to_doctor"}
    ),
    "evidence_reader": frozenset({"read_scoped_file"}),
    "liaison": frozenset({"get_report_facts"}),
    "worker": frozenset({"classify"}),
    "cross_check": frozenset({"classify"}),
    "classifier": frozenset({"classify"}),
    "vision": frozenset({"read_scoped_file"}),
}

SCOPE_KEYS = frozenset(
    {
        "doctor_id",
        "patient_id",
        "tenant_id",
        "intake_id",
        "scope",
        "principal",
        "actor_id",
        "bot_id",
        "session_key",
        "policy_version",
        "auth_epoch",
    }
)


def scope_argument(value: object, scope: Scope) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in SCOPE_KEYS:
                bound = getattr(scope, str(key), None)
                return "scope_mismatch" if item != bound else "scope_argument"
            if reason := scope_argument(item, scope):
                return reason
    elif isinstance(value, (tuple, list)):
        for item in value:
            if reason := scope_argument(item, scope):
                return reason
    return None


@dataclass(frozen=True)
class AgentScope:
    principal: Principal
    scope: Scope
    source: Provenance
    policy: SafetyPolicy
    authority_check: Callable[[], bool] = field(repr=False)
    output_context: OutputContext | None = None
    worker: WorkerCapability | None = None

    def valid(self) -> bool:
        actor = self.principal
        if isinstance(self.scope, AccountScope) or actor.doctor_id != self.scope.doctor_id:
            return False
        if actor.actor_kind == "system":
            if (
                not self.worker
                or self.worker.resolved_scope != self.scope
                or (self.worker.service_subject != actor.subject)
            ):
                return False
        elif actor.actor_kind not in {"doctor", "patient"} or (
            actor.actor_kind not in actor.verified_roles
        ):
            return False
        if actor.actor_kind == "patient" and actor.patient_id != getattr(
            self.scope, "patient_id", None
        ):
            return False
        return self.authority_check()


class ToolRefusal(_BoundaryValue):
    status: Literal["refused"] = "refused"
    reason: str


@dataclass(frozen=True)
class ScopedTool:
    name: str
    binding: AgentScope
    sdk_tool: AgentTool = field(repr=False)


def drug_lookup_tool(binding: AgentScope, handler: Callable[[str], BaseModel]) -> ScopedTool:
    @tool(name="lookup_drug", context=True)
    def lookup_drug(name: str, tool_context: ToolContext) -> dict[str, Any]:
        """Verify one drug name against vocabulary and RxNorm; results are untrusted data.

        Args:
            name: Only the drug name, without patient identity, dose or other dictation.
            tool_context: Server-provided invocation context.
        """
        reason = scope_argument(tool_context.tool_use.get("input", {}), binding.scope)
        if not binding.valid() or tool_context.invocation_state.get("sanad_scope") is not binding:
            reason = "scope_unavailable"
        if tool_context.cancel_signal.is_set():
            reason = "cancelled"
        if reason:
            return ToolRefusal(reason=reason).model_dump(mode="json")
        return handler(name).model_dump(mode="json")

    return ScopedTool("lookup_drug", binding, lookup_drug)


def scoped_tool[T: BaseModel](
    name: str,
    schema: type[T],
    handler: Callable[[T], BaseModel],
    *,
    binding: AgentScope,
) -> ScopedTool:
    if not isinstance(binding.scope, TenantScope):
        raise ValueError("clinical tools require a bound tenant")

    @tool(
        name=name,
        context=True,
        inputSchema={
            "type": "object",
            "properties": {"payload": schema.model_json_schema()},
            "required": ["payload"],
            "additionalProperties": False,
        },
    )
    def invoke(payload: dict[str, JsonValue], tool_context: ToolContext) -> dict[str, Any]:
        """Read scoped data or propose a change; never commit clinical state.

        Args:
            payload: Typed proposal arguments, without identity or policy fields.
            tool_context: Server invocation context injected by Strands.
        """
        reason = scope_argument(tool_context.tool_use.get("input", {}), binding.scope)
        if not binding.valid() or tool_context.invocation_state.get("sanad_scope") is not binding:
            reason = "scope_unavailable"
        if tool_context.cancel_signal.is_set():
            reason = "cancelled"
        if reason:
            return ToolRefusal(reason=reason).model_dump(mode="json")
        try:
            result = handler(schema.model_validate(payload))
            return result.model_dump(mode="json")
        except ValidationError:
            return ToolRefusal(reason="invalid_arguments").model_dump(mode="json")
        except Exception:
            return ToolRefusal(reason="tool_unavailable").model_dump(mode="json")

    return ScopedTool(name, binding, invoke)
