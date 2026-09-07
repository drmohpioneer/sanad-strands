import asyncio
import os

import pytest

from .check11b import run_check


@pytest.mark.skipif(
    os.environ.get("SANAD_LIVE") != "1", reason="explicit single 11b live allowance only"
)
def test_contract11b_live(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--live"):
        pytest.skip("only the isolated opt-in suite may use the account")
    report = asyncio.run(run_check())
    assert report["state"] == "passed", "Read redacted evidence; do not repeat the live check."
