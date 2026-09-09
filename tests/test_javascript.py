"""The build rejects broken browser assets and missing parsers before packaging."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("asset", sorted((ROOT / "src/sanad/web/static").glob("*.js")))
def test_browser_asset_parses(asset: Path) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required to parse browser JavaScript; install it before testing"
    result = subprocess.run([node, "--check", str(asset)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("missing_node", [False, True], ids=["invalid-js", "missing-node"])
def test_build_refuses_unchecked_javascript(tmp_path: Path, missing_node: bool) -> None:
    make = shutil.which("make")
    assert make, "make is required for the build regression"
    shutil.copyfile(ROOT / "Makefile", tmp_path / "Makefile")
    assets = tmp_path / "src/sanad/web/static"
    assets.mkdir(parents=True)
    (assets / "browser.js").write_text("open(html, data => { return send({});\n};\n")
    (assets / "theme.js").write_text("'use strict';\n")
    env = dict(os.environ)
    if missing_node:
        env["PATH"] = str(tmp_path / "absent-tools")
    result = subprocess.run([make, "build"], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert ("Node.js is required" if missing_node else "SyntaxError") in output
    assert "uv build" not in output, "packaging must not start after a failed parse check"
