"""Redeploy the previous passing image without changing table data or secrets."""

import argparse

from deploy.common import (
    ROOT,
    OperationError,
    command,
    environment,
    history,
    previous_release,
    session,
)
from deploy.deploy import deploy
from sanad.store.keys import SCHEMA_VERSION


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    environment(parser)
    args = parser.parse_args()
    prior = previous_release(history(ROOT / "deploy/releases" / f"{args.env}.json"))
    if prior["schema_version"] != SCHEMA_VERSION:
        raise OperationError("Previous release schema differs; no rollback migration available")
    deploy(session(), args.env, prior["image_digest"], action="rollback")


if __name__ == "__main__":
    command(main)
