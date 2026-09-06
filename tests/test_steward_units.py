import ast
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from domain_fixtures import NOW, POLICY, mission, review

from sanad.domain import (
    MissionState,
    ReviewState,
    transition_mission,
    transition_review,
)
from sanad.domain.events import TransitionResult
from sanad.domain.operations import AccountabilityWake, OperationsPolicy


@pytest.mark.parametrize("count,minutes", [(1, 1), (2, 5), (3, 15), (4, 60), (5, 240), (100, 240)])
def test_retry_arithmetic_saturates_at_last_policy_interval(count: int, minutes: int) -> None:
    assert OperationsPolicy().retry_backoff(count) == timedelta(minutes=minutes)


@pytest.mark.parametrize("count", [0, -1, True])
def test_retry_arithmetic_rejects_non_attempts(count: int) -> None:
    with pytest.raises(ValueError):
        OperationsPolicy().retry_backoff(count)


def test_internal_review_wake_preserves_clinical_source_ack_and_original_due() -> None:
    original = review(ReviewState.acknowledged)
    now = original.review_at + timedelta(days=3)
    result = transition_review(original, AccountabilityWake(event_id="synthetic-tick"), now, POLICY)
    assert isinstance(result, TransitionResult)
    updated = result.aggregate
    assert isinstance(updated, type(original))
    assert updated.state == "acknowledged"
    assert updated.review_at == original.review_at
    assert updated.source_version == original.source_version
    assert updated.last_material_change_version == original.last_material_change_version
    assert updated.work_clock.next_action_at == now + POLICY.overdue_review_interval  # type: ignore[union-attr]


def test_expired_proposal_or_pause_rearms_without_activation_or_shifted_due() -> None:
    for state in (MissionState.proposed, MissionState.blocked):
        original = mission(state)
        now = (
            original.review_at + timedelta(days=3)
            if state == MissionState.proposed
            else NOW + timedelta(days=2)
        )
        from sanad.domain import WorkClock

        original = type(original).model_validate(
            original.model_dump()
            | {"work_clock": WorkClock(next_action_at=NOW, work_lane="mission")}
        )
        result = transition_mission(
            original, AccountabilityWake(event_id="synthetic-tick"), now, POLICY
        )
        assert isinstance(result, TransitionResult)
        assert isinstance(result.aggregate, type(original))
        assert result.aggregate.state == state
        assert result.aggregate.due_at == original.due_at
        assert result.aggregate.work_clock.next_action_at > now  # type: ignore[union-attr]


def test_steward_imports_no_web_storage_adapter_or_provider() -> None:
    root = Path(__file__).parents[1] / "src" / "sanad"
    forbidden = {"boto3", "botocore", "fastapi", "strands", "httpx", "openai", "anthropic"}
    for path in [*root.joinpath("steward").glob("*.py"), *root.joinpath("channels").glob("*.py")]:
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [n.name for n in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            assert not any(
                name.split(".")[0] in forbidden
                or name in {"sanad.store._base", "sanad.store.dynamodb", "sanad.store.memory"}
                for name in names
            )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import sanad.steward.service, sanad.steward.dispatch; "
            "import sanad.steward.inbound, sanad.steward.urgent, sanad.steward.sweep; "
            "assert not {'boto3', 'fastapi', 'strands', 'httpx'} & set(sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
