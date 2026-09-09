"""Decision 023: the judged build speaks English whatever a doctor record stores.

The suite runs with the switch off so the Arabic machinery keeps its coverage, so
these are the only tests that turn it on. They assert the property at every place
a stored language can still reach a reader.
"""

import pytest

from sanad.domain.language import contest_english, effective


@pytest.fixture
def contest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "1")


@pytest.fixture
def own_language(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "0")


def test_switch_defaults_on_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SANAD_CONTEST_ENGLISH", raising=False)
    assert contest_english()
    assert effective("ar") == "en"


def test_only_an_explicit_zero_restores_a_stored_preference(own_language: None) -> None:
    assert not contest_english()
    assert effective("ar") == "ar"
    assert effective("en") == "en"


@pytest.mark.usefixtures("contest")
@pytest.mark.parametrize("stored", ["ar", "en"])
def test_every_render_entry_point_ignores_a_stored_arabic_preference(stored: str) -> None:
    from sanad.channels.telegram import wording
    from sanad.concierge import templates as concierge
    from sanad.contact import templates as contact
    from sanad.evidence import templates as evidence

    assert effective(stored) == "en"
    # One key per module, chosen because each renders visibly different text per language.
    assert contact.render("patient_monitor_prompt", stored, metric="blood pressure").isascii()
    assert concierge.render("patient_barrier_missing", stored).isascii()
    assert evidence.render("unverified", stored).isascii()
    assert wording.label("patient", stored).isascii()


@pytest.mark.usefixtures("own_language")
def test_the_arabic_surface_still_exists_when_the_switch_is_off() -> None:
    from sanad.contact import templates as contact

    assert not contact.render("patient_monitor_prompt", "ar", metric="الضغط").isascii()


@pytest.mark.usefixtures("contest")
def test_no_doctor_record_no_longer_falls_back_to_arabic() -> None:
    """The router and the evidence path both hardcoded "ar" for a missing record."""
    import inspect

    from sanad.channels.telegram import router
    from sanad.evidence import commit

    for module in (router, commit):
        source = inspect.getsource(module)
        assert 'else "ar"' not in source, module.__name__


@pytest.mark.usefixtures("contest")
def test_no_language_selection_bypasses_the_resolver() -> None:
    """The bug this rail exists for: the card was rendered in English but the
    proposal saved "ar", and the message the doctor receives is drawn from the
    saved value. Patching the render call alone left two sites unguarded, and no
    behavioural test noticed. Any new site that selects behaviour from a stored
    language must go through the resolver, so the rail is on the source itself.
    """
    import re

    from sanad.scribe import turn

    source = inspect_source(turn)
    bare = [
        line.strip()
        for line in source.splitlines()
        if re.search(r"(?<!contest_language\()\bdoctor\.language\b", line)
        and "contest_language(doctor.language)" not in line
        and "wording." not in line
        and "templates.render" not in line
        and "render_evidence" not in line
        and "label(" not in line
    ]
    assert not bare, "these select behaviour from a stored language: " + "; ".join(bare)


def inspect_source(module: object) -> str:
    import inspect

    return inspect.getsource(module)  # type: ignore[arg-type]
