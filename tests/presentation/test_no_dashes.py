"""Product prose cannot contain long dashes; verbatim clinical data is exempt."""

import ast
from importlib import import_module
from pathlib import Path
from string import Formatter

import pytest
from store.photo_fixtures import photo, prescription, providers
from test_catalog_equivalence import CASES, legacy
from test_language_rail import dictation, scribe_world

from sanad.domain.language import Language
from sanad.presentation.catalog import OpaqueValue, render
from sanad.presentation.context import PresentationContext
from sanad.scribe.card import render_card

EXEMPT_FILES = {
    "scribe/extract.py",
    "scribe/grounding.py",
    "scribe/merge.py",  # Model instructions.
    "web/static/demo.json",  # Synthetic data.
    "concierge/education/sources.yaml",  # Titles are data; labels checked separately below.
}


def no_dashes(text: str, surface: str) -> None:
    locations = [(n, f"U+{ord(c):04X}") for n, c in enumerate(text) if c in "—–"]
    assert not locations, f"{surface}: {locations}: {text}"


@pytest.mark.parametrize("surface,key,locale", CASES)
def test_rendered_catalog(surface: str, key: str, locale: Language) -> None:
    original = legacy(surface)[key][locale == "en"]
    fields = {
        name: OpaqueValue("Synthetic " + name)
        for _, name, _, _ in Formatter().parse(original)
        if name
    }
    catalog = import_module("sanad.presentation." + surface).CATALOG
    context = PresentationContext(
        locale, "doctor" if surface == "doctor" or key.startswith("doctor_") else "patient"
    )
    no_dashes(render(catalog, surface + "." + key, context, **fields), surface + "." + key)


@pytest.mark.parametrize("case", ["dictation", "photo", "correction"])
def test_scribe_cards(monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "1")
    w = scribe_world()
    if case == "photo":
        providers(w, prescription(), prescription("50 mg"))
        w.post(photo("أحمد رضا"))
    else:
        dictation(w)
        if case == "correction":
            value = w.proposal.candidate.model_dump()
            value["orders"][1]["dose"] = "5 mg"
            w.dictate("Start Concor 5 mg", value, id=11)
            assert w.proposal.version > 1
    for locale in ("en", "ar"):
        for part in render_card(w.proposal, locale):
            no_dashes(part, case + ":" + locale)


def test_module_template_dictionaries_and_source_labels() -> None:
    root = Path("src/sanad")
    failures = []
    for path in sorted(root.rglob("*.py")):
        if path.relative_to(root).as_posix() in EXEMPT_FILES:
            continue
        for node in ast.parse(path.read_text()).body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not isinstance(
                node.value, ast.Dict
            ):
                continue
            for literal in ast.walk(node.value):
                if (
                    isinstance(literal, ast.Constant)
                    and isinstance(literal.value, str)
                    and any(c in literal.value for c in "—–")
                ):
                    failures.append(f"{path}:{literal.lineno}: {literal.value!r}")
    import json

    for entry in json.loads((root / "concierge/education/sources.yaml").read_text()):
        for field in ("source_label", "source_label_en"):
            if any(c in entry[field] for c in "—–"):
                failures.append(f"sources.yaml:{entry['id']}.{field}")
    assert not failures, "\n".join(failures)
