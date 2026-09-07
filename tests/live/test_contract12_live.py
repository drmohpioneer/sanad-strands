import os

import pytest

from .check12 import run_check


@pytest.mark.skipif(os.environ.get("SANAD_LIVE") != "1", reason="explicit live account check only")
def test_contract12_live(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--live"):
        pytest.skip("only the isolated opt-in suite may use the account")
    report = run_check()
    assert report["state"] == "passed", "Failure preserved; do not repeat the live check."
