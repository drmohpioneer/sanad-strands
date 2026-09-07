import os

import pytest

from .check11d import run_check


@pytest.mark.skipif(
    os.environ.get("SANAD_LIVE") != "1", reason="explicit single 11d allowance only"
)
def test_contract11d_live() -> None:
    result = run_check()
    assert result["state"] == "passed", "11d live gate failed; preserve evidence and do not rerun"
