"""Independent resolver examples and architectural ownership checks for 11e."""

import ast
from pathlib import Path

import pytest
from pydantic import create_model

from sanad.agents.schema import describe_schema
from sanad.domain.boundaries import _BoundaryValue
from sanad.scribe.extract import DictationCandidate, scribe_prompt
from sanad.scribe.lookup import DrugLookup
from sanad.scribe.names import normalize
from sanad.scribe.resolver import Context, NameKind, hint_names, resolve_fragments, resolve_name

# These are expected names, not captured model or resolver output.
ROWS: tuple[tuple[str, NameKind, str | None, str | None, str], ...] = (
    ("كونكور", "drug", None, "Concor", "seed"),
    ("Concord", "drug", None, "Concor", "seed"),
    ("إكس فورش إتش سي تي", "drug", None, "Exforge HCT", "seed"),
    ("X-Force HCT", "drug", None, "Exforge HCT", "seed"),
    ("فورسيجا", "drug", None, "Forxiga", "seed"),
    ("Forsige", "drug", None, "Forxiga", "seed"),
    ("أملوديبين", "drug", None, "Amlodipine", "seed"),
    ("بيزوبرولول", "drug", None, "Bisoprolol", "seed"),
    ("اسم مجهول تماما", "drug", "Concor", None, "unresolved"),
    ("كونكور", "drug", "Forxiga", None, "unresolved"),
    ("Rarebrand", "drug", None, "Rarebrand", "proposal"),
    ("سوديم", "test", None, "Na", "seed"),
    ("سوديوم", "test", None, "Na", "seed"),
    ("sodium", "test", None, "Na", "seed"),
    ("بانو كريات", "test", None, "BUN, creatinine", "seed"),
    ("Bano Creatine", "test", None, "BUN, creatinine", "seed"),
    ("بوتاسيوم", "test", None, "K", "seed"),
    ("potassium", "test", None, "K", "seed"),
    ("كرياتينين", "test", None, "creatinine", "seed"),
    ("CBC", "test", None, "CBC", "seed"),
    ("زيلورا", "test", "Xelora", "Xelora", "proposal"),
    ("اسم غريب جدا", "test", "CBC", None, "unresolved"),
    ("تي أوف إنفرجين", "finding", None, "T wave inversion", "seed"),
    ("أنجينا", "finding", None, "angina", "seed"),
    ("ضغطه سكر", "finding", None, "hypertension, diabetes", "seed"),
    ("لاترال", "finding", None, "lateral", "seed"),
    (
        "سيجمنتال إنفروبوسترو لاترال",
        "finding",
        None,
        "segmental hypokinesia inferoposterolateral",
        "seed",
    ),
    ("ECG", "finding", None, "ECG", "seed"),
    ("طنين", "finding", "tinnitus", None, "unresolved"),
    ("Novel finding", "finding", None, "Novel finding", "proposal"),
)


@pytest.mark.parametrize("spoken,kind,proposal,latin,tier", ROWS)
def test_resolver_table(
    spoken: str, kind: NameKind, proposal: str | None, latin: str | None, tier: str
) -> None:
    result = resolve_name(spoken, kind, spoken, proposal)
    assert (result.latin, result.tier, result.spoken) == (latin, tier, spoken)


def test_proposal_requires_source_or_bounded_transliteration_and_fetched_lookup() -> None:
    assert resolve_name("Rarebrand", "drug", "different source").latin is None
    assert (
        resolve_name("غير معروف", "finding", "غير معروف Novel finding", "Novel finding").latin
        == "Novel finding"
    )
    assert resolve_name("45", "finding", "45 99", "EF 99").latin is None
    ctx = Context(
        lookups={
            normalize("Rarebrand"): DrugLookup(
                found=True, canonical="Rarebrand", generic="ingredient"
            )
        }
    )
    assert resolve_name("Rarebrand", "drug", "Rarebrand", ctx=ctx).tier == "lookup"
    conflict = Context(lookups=ctx.lookups, generic="other")
    assert resolve_name("Rarebrand", "drug", "Rarebrand", ctx=conflict).conflict


def test_per_analyte_resolves_words_and_preserves_unknown_fragment() -> None:
    result = resolve_fragments(
        "طلبت منه بانو كريات وسوديوم وبوتاسيوم وزيلورانا يعملوه",
        "test",
        "طلبت منه بانو كريات وسوديوم وبوتاسيوم وزيلورانا يعملوه",
    )
    assert [r.latin or r.spoken for r in result] == ["BUN, creatinine", "Na", "K", "وزيلورانا"]
    assert [
        r.latin or r.spoken
        for r in resolve_fragments(
            "ECG في تي أوف انفرجين في اللاترال", "finding", "ECG في تي أوف انفرجين في اللاترال"
        )
    ] == ["ECG", "T wave inversion", "lateral"]


def test_matching_and_learning_import_graph_has_one_owner() -> None:
    root = Path("src/sanad")
    calls: dict[str, set[str]] = {}
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        names = {
            n.func.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        names |= {
            n.func.attr
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        calls[str(path)] = names
        if path.name != "resolver.py":
            assert not names & {"edit_distance", "_phonetic", "proposal_anchored"}, path
            assert not any(
                isinstance(n, ast.FunctionDef) and n.name in {"_phonetic", "proposal_anchored"}
                for n in ast.walk(tree)
            ), path
    assert "learn" in calls["src/sanad/scribe/commit.py"]
    assert "learn" in calls["src/sanad/store/scribe.py"]
    assert "resolve_name" in calls["src/sanad/concierge/plan.py"]
    # Compatibility wrappers contain no matching loops or edit algorithm.
    for file, functions in {
        "names.py": {"resolve", "latin_terms", "entry_for", "edit_distance", "known_names"},
        "lookup.py": {"resolve"},
        "clinical.py": {"test_names"},
    }.items():
        tree = ast.parse((root / "scribe" / file).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in functions:
                assert not any(isinstance(n, (ast.While, ast.For)) for n in ast.walk(node)), (
                    file,
                    node.name,
                )


def test_v8_schema_and_shared_capped_hints() -> None:
    hint = hint_names(learned=tuple(f"Known{i}" for i in range(450)))
    assert len(hint.split(", ")) == 400
    prompt = scribe_prompt(hint, language="ar")
    assert len(prompt.split("Known names (spelling hints): ")[1].split(", ")) == 200
    schema = describe_schema(
        create_model("SanadCandidate", __base__=_BoundaryValue, value=(DictationCandidate, ...))
    )
    assert all(word not in prompt + schema for word in ("clinical_en", ".terms", "clinical_kind"))
    assert "name_latin" in schema and "One fact per clinical item" in prompt
    assert "scribe-v8" in prompt
