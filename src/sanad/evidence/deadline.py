"""Retain the accepted pre-deadline commit discipline for evidence commands."""

from sanad.domain import Mission
from sanad.domain import events as ev
from sanad.domain.transitions import transition_mission
from sanad.steward.apply import CommitBuilder, EffectsRejected
from sanad.steward.service import system_command
from sanad.steward.types import command_result


def before_fulfillment(builder: CommitBuilder, mission: Mission) -> Mission:
    if (
        mission.escalation_at > builder.now
        or mission.handled_deadline_generation >= mission.deadline_generation
    ):
        return mission
    id = builder.command.command_id + ":deadline"
    result = transition_mission(
        mission, ev.DeadlineReached(event_id=id), builder.now, builder.policy.timing
    )
    if not isinstance(result, ev.TransitionResult) or not isinstance(result.aggregate, Mission):
        raise EffectsRejected("evidence_deadline_rejected")
    command = system_command(
        builder.scope,
        id,
        {"type": "_Deadline", "mission_id": mission.id},
        builder.now,
        lane="mission",
    ).model_copy(update={"fence": builder.command.fence})
    child = CommitBuilder(builder.scope, command, builder.now, builder.policy, builder.store)
    child.add(result)
    committed = command_result(builder.store.commit(child.finish(complete_receipt=False)))
    if committed.status != "accepted":
        raise EffectsRejected("evidence_deadline_conflict")
    return result.aggregate
