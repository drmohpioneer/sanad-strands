import os

import pytest

from .check09b import run_check


@pytest.mark.skipif(os.environ.get("SANAD_LIVE") != "1", reason="explicit live account check only")
def test_contract09b_live(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--live"):
        pytest.skip("only the isolated opt-in suite may use the account")
    report = run_check()
    assert report["state"] == "passed", "Read the redacted evidence; do not repeat the live check."
