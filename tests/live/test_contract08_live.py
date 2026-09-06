import asyncio
import os

import pytest

from .check08 import run_check


@pytest.mark.skipif(os.environ.get("SANAD_LIVE") != "1", reason="explicit live account check only")
def test_contract08_live(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--live"):
        pytest.skip("only make live-check may run the account check")
    report = asyncio.run(run_check())
    assert report["state"] == "passed", (
        "See redacted evidence; the live check must not be repeated."
    )
