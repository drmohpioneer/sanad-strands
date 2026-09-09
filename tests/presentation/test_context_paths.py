"""Each migrated render path resolves once and uses its frozen recipient context."""

from importlib import import_module
from typing import cast

import pytest

from sanad.coordinator.permitted import Fact, Permitted
from sanad.domain.language import Audience, Language
from sanad.presentation import context
from sanad.presentation.context import PresentationContext
from sanad.store.records import Patient

PATHS: list[tuple[str, str, Audience, dict[str, str]]] = [
    ("channels.telegram.wording", "scribe_confirmed", "doctor", {"body": "Exforge HCT 5/160/12.5"}),
    ("channels.telegram.wording", "consent_declined_ack", "patient", {}),
    (
        "contact.templates",
        "patient_chase_test",
        "patient",
        {"title": "CBC", "due_local": "2026-09-09 08:00"},
    ),
    ("contact.templates", "doctor_objective_done", "doctor", {"title": "CBC"}),
    ("concierge.templates", "patient_evidence_accepted", "patient", {"title": "CBC"}),
    ("concierge.templates", "patient_barrier_recorded", "patient", {"drug": "Exforge HCT"}),
    ("concierge.templates", "doctor_question_recorded", "doctor", {}),
    ("evidence.templates", "doctor_evidence_identity", "doctor", {"patient": "Synthetic Person"}),
    ("evidence.templates", "patient_evidence_accepted", "patient", {"title": "CBC"}),
    (
        "resolver.templates",
        "place",
        "patient",
        {"name": "Synthetic Place", "distance": "500", "details": "; street"},
    ),
]


@pytest.mark.parametrize("module,key,audience,fields", PATHS)
def test_boundary_resolves_only_once_even_when_environment_changes_mid_render(
    monkeypatch: pytest.MonkeyPatch,
    module: str,
    key: str,
    audience: Audience,
    fields: dict[str, str],
) -> None:
    wrapper = import_module("sanad." + module).render
    expected = wrapper(key, PresentationContext("ar", audience), **fields)
    seen: list[Audience] = []

    def resolve_once(language: str, *, audience: Audience = "doctor") -> Language:
        seen.append(audience)
        assert len(seen) == 1
        monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "1")
        return "ar"

    monkeypatch.setattr(context, "effective", resolve_once)
    assert wrapper(key, "ar", **fields) == expected
    assert seen == [audience]


@pytest.mark.parametrize("module,key,audience,fields", PATHS)
def test_explicit_context_never_resolves_again(
    monkeypatch: pytest.MonkeyPatch,
    module: str,
    key: str,
    audience: Audience,
    fields: dict[str, str],
) -> None:
    wrapper = import_module("sanad." + module).render
    frozen = PresentationContext("en", audience)
    expected = wrapper(key, frozen, **fields)

    def forbidden(*args: object, **kwargs: object) -> Language:
        raise AssertionError("locale read below render boundary")

    monkeypatch.setattr(context, "effective", forbidden)
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "0")
    assert wrapper(key, frozen, **fields) == expected


def bundle(facts: tuple[Fact, ...]) -> Permitted:
    return Permitted("synthetic", 1, None, "patient_chase_test", True, False, facts, ())


@pytest.mark.parametrize("locale", ["ar", "en"])
@pytest.mark.parametrize("extra", [0, 1, 2])
def test_coordinator_preserves_legacy_composition_and_overflow(
    locale: Language, extra: int
) -> None:
    from sanad.coordinator.templates import TEMPLATES
    from sanad.presentation.coordinator import compose

    categories = ("lab_result", "imaging_report", "discharge_summary", "prescription", "other")[
        : 3 + extra
    ]
    facts = (Fact("title", "title", "CBC"), Fact("due", "due", "2026-09-09 08:00")) + tuple(
        Fact(str(i), "category", value) for i, value in enumerate(categories)
    )
    values = [TEMPLATES[value][locale == "en"] for value in categories[:3]]
    if extra:
        values.append(
            TEMPLATES["more_one" if extra == 1 else "more"][locale == "en"].format(value=str(extra))
        )
    original = " ".join(
        (
            TEMPLATES["title"][locale == "en"].format(value="CBC"),
            TEMPLATES["due"][locale == "en"].format(value="2026-09-09 08:00"),
            TEMPLATES["categories"][locale == "en"].format(value=", ".join(values)),
        )
    )
    actual, _, _ = compose(
        bundle(facts), tuple(f.id for f in facts), PresentationContext(locale, "patient"), 3
    )
    assert actual.encode() == original.encode()


def test_coordinator_reads_recipient_record_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanad.coordinator.templates import render

    class Recipient:
        reads = 0

        @property
        def language(self) -> str:
            self.reads += 1
            assert self.reads == 1
            return "ar"

    recipient = Recipient()
    seen: list[Audience] = []

    def resolve_once(language: str, *, audience: Audience = "doctor") -> Language:
        seen.append(audience)
        monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "1")
        return "ar"

    monkeypatch.setattr(context, "effective", resolve_once)
    facts = (Fact("title", "title", "CBC"),)
    assert render(bundle(facts), ("title",), cast(Patient, recipient)) == "المطلوب المسجّل: CBC."
    assert seen == ["patient"] and recipient.reads == 1


def test_coordinator_composition_uses_no_locale_source(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanad.presentation.coordinator import compose

    def forbidden(*args: object, **kwargs: object) -> Language:
        raise AssertionError("locale read below render boundary")

    monkeypatch.setattr(context, "effective", forbidden)
    facts = (Fact("slot", "slot", "2026-09-09 08:00"),)
    text, _, _ = compose(bundle(facts), ("slot",), PresentationContext("en", "patient"), 3)
    assert text == "Readings not yet recorded for: 2026-09-09 08:00."


def test_coordinator_cannot_drop_an_opaque_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanad.presentation.coordinator import CATALOG, compose

    monkeypatch.setitem(
        CATALOG, "coordinator.title", {"ar": "Generic substitute.", "en": "Generic substitute."}
    )
    facts = (Fact("title", "title", "CBC"),)
    with pytest.raises(ValueError, match="catalog_fields"):
        compose(bundle(facts), ("title",), PresentationContext("en", "patient"), 3)
