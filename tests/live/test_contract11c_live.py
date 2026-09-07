import os

import pytest

from .check11c import run_check


@pytest.mark.skipif(
    os.environ.get("SANAD_LIVE") != "1", reason="explicit single 11c allowance only"
)
def test_contract11c_live(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--live"):
        pytest.skip("only the isolated opt-in suite may use providers")
    report = run_check()
    assert report["state"] == "passed", "Failure preserved in evidence; do not repeat this check."
