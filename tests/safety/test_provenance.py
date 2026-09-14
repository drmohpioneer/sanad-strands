import ast
import hashlib
from base64 import b64decode
from pathlib import Path

import pytest

from sanad.safety._provenance import REUSED_MODULES, SCRUBBED_TEST_LINES, TABLE_HASHES

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "record", TABLE_HASHES, ids=lambda record: f"{record.module}.{record.name}"
)
def test_copied_constants_are_byte_identical_to_recorded_source(record: object) -> None:
    from sanad.safety._provenance import TableHash

    assert isinstance(record, TableHash)
    text = (ROOT / "src" / "sanad" / "safety" / f"{record.module}.py").read_text()
    for node in ast.parse(text).body:
        name = (
            node.targets[0].id
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
            else node.target.id
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
            else None
        )
        if name == record.name:
            source = ast.get_source_segment(text, node)
            assert source is not None
            assert hashlib.sha256(source.encode()).hexdigest() == record.sha256
            return
    pytest.fail(f"copied table removed: {record.name}")


def test_provenance_headers_and_inventory() -> None:
    assert len(REUSED_MODULES) == 10  # five source modules, normalizer split, four tests
    assert len(TABLE_HASHES) > 75
    for record in REUSED_MODULES:
        assert record.source_commit == "b65f569" and str(record.copy_date) == "2026-09-06"
        assert record.modifications
        assert type(record).model_validate_json(record.model_dump_json()) == record
        text = (ROOT / record.destination).read_text()
        if record.destination.startswith("tests/"):
            for published, original in SCRUBBED_TEST_LINES.get(record.destination, ()):
                assert text.count(published + "\n") == 1
                text = text.replace(published + "\n", b64decode(original).decode() + "\n")
            restored = text.replace("from sanad.safety import ", "from core import ")
            assert hashlib.sha256(restored.encode()).hexdigest() == record.source_sha256
        else:
            header = ast.get_docstring(ast.parse(text))
            assert header is not None
            for expected in (
                record.source_path,
                record.source_commit,
                str(record.copy_date),
                "Unknown is never normal",
                "media_failure",
                "diagnosis",
            ):
                assert expected in header


def test_a_changed_table_row_fails_its_recorded_fingerprint() -> None:
    record = next(row for row in TABLE_HASHES if row.name == "CRITICAL_LABS")
    text = (ROOT / "src/sanad/safety/labs.py").read_text()
    changed = text.replace(
        'LabRule("K", "mmol/L", low=2.5, high=6.0)', 'LabRule("K", "mmol/L", low=2.5, high=60.0)'
    )
    assert changed != text
    for node in ast.parse(changed).body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "CRITICAL_LABS"
        ):
            segment = ast.get_source_segment(changed, node)
            assert segment is not None
            assert hashlib.sha256(segment.encode()).hexdigest() != record.sha256
            break
    else:
        pytest.fail("critical table missing")
