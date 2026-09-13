"""Frozen pre-refactor cards and the explicitly released English differences."""

import hashlib
import json
import re
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from domain_fixtures import NOW
from harness import FakeClock
from pydantic import ValidationError
from store.account_fixtures import APPLICANT, update
from store.contact_fixtures import required, world
from store.login_fixtures import browser_login
from store.test_contact_outage_bundle import bundle_intent, deadline

from sanad.auth.service import revise
from sanad.contact import bundle
from sanad.scribe.card import render_card
from sanad.scribe.english import render_view
from sanad.scribe.proposal import Proposal
from sanad.scribe.view import REASONS, build_view, questions
from sanad.store.memory import MemoryStore
from scribe.test_grounding_invariant import world_for

ORACLE: list[dict[str, Any]] = json.loads(Path(__file__).with_name("oracle_11j.json").read_text())
SIX_D: dict[str, list[str]] = json.loads(Path(__file__).with_name("oracle_20_6d.json").read_text())
SIX_J: dict[str, list[str]] = json.loads(Path(__file__).with_name("oracle_20_6j.json").read_text())
CHANGED_BY_11J = {
    "long-quote:e0a88a8c7d62": "C.1: bound the 300-character verification quote at a word boundary",
}


def saved(name: str) -> Proposal:
    return Proposal.model_validate(
        next(r["proposal"] for r in ORACLE if r["id"].split(":")[0] == name)
    )


@pytest.mark.parametrize("row", ORACLE, ids=[r["id"] for r in ORACLE])
def test_frozen_oracle(row: dict[str, Any]) -> None:
    p = Proposal.model_validate(row["proposal"])
    assert hashlib.sha256(p.source_text.encode()).hexdigest() == row["source_sha256"]
    assert (
        hashlib.sha256(p.candidate.model_dump_json().encode()).hexdigest()
        == row["candidate_sha256"]
    )
    assert row["id"].endswith(":" + row["candidate_sha256"][:12])
    expected = tuple(part.replace(" — ", ": ") for part in row["rendered"])
    if row["id"] in CHANGED_BY_11J:
        old_quote = ("word " * 60).strip() + "x"
        new_quote = ("word " * 24).strip() + "…"
        expected = tuple(
            part.replace('"' + old_quote + '"', '"' + new_quote + '"') for part in expected
        )
        assert expected != tuple(row["rendered"])
    # 6d supersedes placement of blocked items; preserve the original oracle
    # and its source/candidate hashes alongside explicit new card snapshots.
    if row["id"] in SIX_D:
        expected = tuple(SIX_D[row["id"]])
    if row["id"] in SIX_J:
        expected = tuple(SIX_J[row["id"]])
    assert render_card(p) == expected


def test_oracle_inventory_and_exercised_branches() -> None:
    assert len(ORACLE) == 72
    names = {r["id"].split(":")[0] for r in ORACLE}
    assert len(names) == len(ORACLE)
    assert len([n for n in names if re.match(r"\d\d-", n)]) == 28
    assert {"question-" + code for code in REASONS} <= names
    for reason, text in (
        ("doctor", "explicit: tomorrow"),
        ("default", "default 1 days"),
        ("scribe", "suggested deadline"),
    ):
        row = next(r for r in ORACLE if r["id"].startswith("deadline-" + reason + ":"))
        assert text in "\n".join(row["rendered"])
    assert set(CHANGED_BY_11J) <= {r["id"] for r in ORACLE}


def test_view_sections_are_frozen_and_preserve_content() -> None:
    view = build_view(saved("english_dictations"))
    assert view.heading.prefix == "New patient: "
    assert view.heading.parts == ("Ahmed Saad", "53")
    assert view.medications == ("Exforge 5/160 → Exforge HCT 10/160/25 (change)",)
    assert view.history == (
        "Dx: diabetes, hypertension",
        "ECG: T wave inversion, lateral leads",
        "Echo: EF 45%",
    )
    assert view.questions == ("Forxiga (start)", "What dose of Forxiga did you intend?")
    assert any(line.startswith("MONITOR:") for line in view.requested)
    assert view.alerts is None and view.history_hidden == 0
    for model, field in (
        (view, "buttons"),
        (view.heading, "prefix"),
        (build_view(saved("choices")).choices[0], "parts"),
    ):
        with pytest.raises(ValidationError, match="frozen"):
            setattr(model, field, "changed")
    assert build_view(saved("corrected")).notices == ("Card updated from your reply",)
    assert "replaces the previous card" in build_view(saved("superseded")).notices[0]


def test_empty_and_filtered_sections_keep_legacy_headers() -> None:
    view = build_view(saved("filtered-alerts"))
    assert view.alerts == ()
    text = "\n".join(render_view(view))
    assert "Notify me if:\nNeeds confirmation:" in text
    # Unsafe blocked items do not leave empty clinical lines or headings.
    assert view.medications == () and view.history == ()
    assert "Medications:" not in text and "History:" not in text
    absent = view.model_copy(
        update={"alerts": None, "medications": (), "history": (), "questions": ()}
    )
    text = "\n".join(render_view(absent))
    assert all(
        title not in text
        for title in ("Notify me if:", "Medications:", "History:", "Needs confirmation:")
    )


def test_history_overflow_and_editing() -> None:
    compact = build_view(saved("oversized"))
    expanded = build_view(saved("oversized-editing"))
    assert (len(compact.history), compact.history_hidden) == (6, 3)
    assert (len(expanded.history), expanded.history_hidden) == (9, 0)
    assert expanded.history[:6] == compact.history
    assert all(len(part) <= 3500 for part in render_view(expanded))


@pytest.mark.parametrize("text", ["x" * 120, "x" * 121, "word " * 60, "a" * 300])
def test_quotes_bounded_without_mutating_stored_issues(text: str) -> None:
    p = saved("verification")
    issue = p.issues[0].model_copy(update={"question": f'Please verify "{text}".'})
    p = p.model_copy(update={"issues": (issue,)})
    before = p.model_dump_json()
    rendered = questions(p)
    quote = re.findall(r'"([^"]*)"', rendered[0])[0]
    assert len(quote) <= 120
    assert quote.endswith("…") == (len(text.strip()) > 120)
    assert p.model_dump_json() == before


def test_clipping_keeps_independent_occurrences_and_full_text_deduplication() -> None:
    p = saved("verification")
    text = 'Please verify "' + "word " * 60 + '".'
    issues = tuple(
        p.issues[0].model_copy(update={"question": text, "occurrence": span})
        for span in ((0, 310), (311, 621))
    )
    p = p.model_copy(update={"issues": issues})
    assert len(questions(p)) == 2
    assert questions(p)[0] == questions(p)[1]
    assert len(questions(p.model_copy(update={"issues": (issues[0], issues[0])}))) == 1
    # Different full questions may have the same bounded visible prefix.
    distinct = issues[1].model_copy(update={"question": text.replace('".', 'other".')})
    assert len(questions(p.model_copy(update={"issues": (issues[0], distinct)}))) == 2


def test_change_without_target_dose_requires_confirmation() -> None:
    view = build_view(saved("amendment-Concor"))
    assert not view.medications
    assert view.questions[:2] == (
        "Concor 5 مج → Concor (change)",
        "What dose of Concor did you intend?",
    )


def test_web_import_boundary() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import sanad.scribe.view; "
            "assert not any(n == 'sanad.channels.telegram' "
            "or n.startswith('sanad.channels.telegram.') "
            "for n in sys.modules)",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("override", ["0", "1"])
@pytest.mark.parametrize("language", ["ar", "en"])
def test_language_acknowledgment_preserves_preference_and_replay(
    monkeypatch: pytest.MonkeyPatch, override: str, language: str
) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", override)
    w = world_for("en")
    message = update(APPLICANT, "/lang " + language, 51)
    w.post(message)
    changed = w.doctor
    assert changed.language == language
    intent = next(i for i in w.cards() if i.template_id == "scribe_language")
    expected = (
        "Language set to English."
        if language == "en"
        else "The app currently shows English. Your Arabic preference is saved for later."
        if override == "1"
        else "تم اختيار العربية."
    )
    assert intent.payload and intent.payload["text"] == expected
    w.post(message)
    assert w.doctor == changed
    assert len([i for i in w.cards() if i.template_id == "scribe_language"]) == 1


@pytest.mark.usefixtures("legacy_dictation_schema")
@pytest.mark.parametrize("override", ["0", "1"])
def test_api_record_resolves_stored_arabic(monkeypatch: pytest.MonkeyPatch, override: str) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "0")
    w = world_for("ar")
    patient = w.named_stub("Synthetic Person")
    p = w.dictate(
        "Synthetic Person. Taking Concor 5.",
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [{"action": "continue", "drug": "Concor", "dose": "5"}],
        },
    )
    assert p.language == "ar"
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", override)
    with w.client() as client:
        assert browser_login(client, w.login_path()).status_code == 303
        response = client.get("/api/patients/" + patient.id)
    assert response.status_code == 200
    parts = response.json()["pending_proposal"]["card_text"]
    assert parts[0].startswith("Patient: " if override == "1" else "المريض: ")
    assert w.doctor.language == "ar" and w.proposal.language == "ar"


@pytest.mark.parametrize("override", ["0", "1"])
def test_bundle_resolves_stored_arabic(monkeypatch: pytest.MonkeyPatch, override: str) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "0")
    clock = FakeClock(NOW)
    w = world(MemoryStore(clock=clock), clock)
    w.seed(revise(w.doctor, clock(), language="ar"))
    deadline(w, clock)
    clock.advance(timedelta(days=7))
    row = required(w.store.get(w.doctor.scope, "bundle_schedule", w.doctor.id))
    bundle.wake(w.runtime.steward, row)
    intent = bundle_intent(w)
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", override)
    payload, refs = bundle.payload_snapshot(w.store, intent, clock())
    assert refs
    assert ("Unfinished request" in str(payload["text"])) == (override == "1")
    assert ("days since first notice" in str(payload["text"])) == (override == "1")
    assert w.doctor.language == "ar"


@pytest.mark.parametrize(
    "branch",
    [
        "unassigned_number",
        "disputed_number",
        "extraction_conflict",
        "patient",
        "dose_unclear",
        "unsupported_number",
        "clinical_unclear",
        "ambiguities",
    ],
)
def test_every_quoted_question_branch_is_bounded(branch: str) -> None:
    from sanad.scribe.extract import ProposalIssue

    p = saved("english_dictations")
    text = ("synthetic " * 30).strip()
    issue = ProposalIssue(item="order:1", code="clinical_unclear", question=f'Confirm "{text}".')
    if branch in {"unassigned_number", "disputed_number"}:
        issue = ProposalIssue.model_validate({"item": "all", "code": branch, "numbers": [text]})
    elif branch == "patient":
        issue = ProposalIssue(
            item="patient", code="extraction_conflict", alternatives=(text, "other")
        )
    elif branch in {"extraction_conflict", "dose_unclear"}:
        issue = ProposalIssue.model_validate(
            {"item": "order:1", "field": "dose", "code": branch, "question": f'Confirm "{text}".'}
        )
    elif branch == "unsupported_number":
        text = "999 " * 75
        orders = (*p.candidate.orders[:1], p.candidate.orders[1].model_copy(update={"dose": text}))
        p = p.model_copy(update={"candidate": p.candidate.model_copy(update={"orders": orders})})
        issue = ProposalIssue(item="order:1", code="unsupported_number", numbers=("999",))
    elif branch == "ambiguities":
        p = p.model_copy(
            update={"candidate": p.candidate.model_copy(update={"ambiguities": (text,)})}
        )
    p = p.model_copy(update={"issues": () if branch == "ambiguities" else (issue,)})
    before = p.model_dump_json()
    quotes = re.findall(r'"([^"]*)"', "\n".join(questions(p)))
    if branch == "ambiguities":
        assert quotes == []
        assert questions(p) == ("Please clarify the patient and instructions.",)
    else:
        assert quotes and any(q.endswith("…") for q in quotes)
        assert all(len(q) <= 120 for q in quotes)
    assert p.model_dump_json() == before
