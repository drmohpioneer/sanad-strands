import os

import pytest

from .check11M import test_contract11M_live as _check


@pytest.mark.skipif(
    os.environ.get("SANAD_LIVE") != "1", reason="explicit single 11M allowance only"
)
def test_contract11M_live(request: pytest.FixtureRequest) -> None:
    _check(request)
