"""18h-4 current scoped reading history, without changing active-plan truth."""

import json
from datetime import timedelta
from typing import cast

import pytest
from harness import FakeClock
from store import evidence_fixtures as f
from store.concierge_fixtures import PatientWorld
from store.conftest import clock as clock
from store.conftest import ddb_server as ddb_server
from store.conftest import pytest_generate_tests as pytest_generate_tests
from store.conftest import store as store
from store.test_corrections_19 import fact_change
from store.test_monitor import monitor

from sanad.concierge.plan import load, projection, summary
from sanad.concierge.records import Reading, ReportFactPayload
from sanad.domain import PatientScope
from sanad.scribe.records import ClinicalFact
from sanad.store._base import StoreBase
from sanad.store.records import from_record


@pytest.fixture
def reading_world(store: StoreBase, clock: FakeClock) -> PatientWorld:
    return f.world(store, clock)


def history(w: PatientWorld) -> list[dict[str, object]]:
    snapshot = load(w.store, w.patient_scope, w.clock())
    assert snapshot
    rows = json.loads(json.dumps(projection(snapshot)["reading_history"]))
    assert isinstance(rows, list)
    assert all(isinstance(row, dict) and all(isinstance(key, str) for key in row) for row in rows)
    return cast(list[dict[str, object]], rows)


def test_reading_history18h4_completed_corrected_detached(reading_world: PatientWorld) -> None:
    w = reading_world
    monitor(w, count=1)
    before = load(w.store, w.patient_scope, w.clock())
    assert before and history(w) == []
    w.send("BP 120/80", id=1700)
    complete = load(w.store, w.patient_scope, w.clock())
    assert complete and not complete.missions
    assert summary(complete)["next_mission"] is None
    assert all(ref.entity_type != "mission" for ref in complete.expected)
    assert len(history(w)) == 1
    assert history(w)[0]["value"] == "120/80"
    assert history(w)[0]["observed_at"] == w.clock().isoformat()
    assert set(history(w)[0]) == {"metric", "unit", "value", "observed_at", "received_at"}
    fact = w.rows("clinical_fact")[0]
    assert fact_change(w, fact.id, "BP 130/85").status == "accepted"
    assert [r["value"] for r in history(w)] == ["130/85"]
    current = load(w.store, w.patient_scope, w.clock())
    assert current and not current.missions
    assert fact_change(w, current.facts[0].id, None).status == "accepted"
    assert history(w) == []
    assert complete.acknowledgment_refs == before.acknowledgment_refs


def test_reading_history18h4_extra_private_scope(reading_world: PatientWorld) -> None:
    w = reading_world
    monitor(w, count=2)
    w.send("BP 120/80", id=1700)
    w.clock.advance(timedelta(hours=10))
    w.send("BP 140/90", id=1701)
    values = [r["value"] for r in history(w)]
    assert all(isinstance(value, str) for value in values)
    assert sorted(cast(list[str], values)) == ["120/80", "140/90"]
    fact = next(
        from_record(r, ClinicalFact)
        for r in w.rows("clinical_fact")
        if "120/80" in str(r.body["payload"])
    )
    w.seed(fact.model_copy(update={"visibility": "doctor_private"}))
    assert [r["value"] for r in history(w)] == ["140/90"]
    assert (
        load(
            w.store,
            PatientScope(doctor_id="another-doctor", patient_id=w.patient_scope.patient_id),
            w.clock(),
        )
        is None
    )
    assert (
        load(
            w.store,
            PatientScope(doctor_id=w.patient_scope.doctor_id, patient_id="another-patient"),
            w.clock(),
        )
        is None
    )


def test_reading_history18h4_report_only_mixed_and_unplottable(reading_world: PatientWorld) -> None:
    w = reading_world
    monitor(w, count=2)
    w.send("BP 120/80", id=1700)
    original = from_record(w.rows("clinical_fact")[0], ClinicalFact)
    values = (
        Reading(
            analyte="Weight",
            quoted="Weight 72 kg",
            raw_value="72",
            raw_unit="kg",
            judgment="reported",
        ),
        Reading(
            analyte="Weight",
            quoted="Weight 160 lb",
            raw_value="160",
            raw_unit="lb",
            judgment="reported",
        ),
        Reading(
            analyte="Glucose",
            quoted="Glucose not readable",
            raw_value="not readable",
            judgment="cannot_judge",
        ),
    )
    w.seed(
        original.model_copy(
            update={
                "id": "report-only",
                "payload": ReportFactPayload(
                    report_kind="reading", text="Recorded synthetic readings", readings=values
                ),
            }
        )
    )
    rows = history(w)
    assert len(rows) == 4
    reports = [r for r in rows if r["observed_at"] is None]
    assert [(r["value"], r["unit"]) for r in reports] == [
        ("72", "kg"),
        ("160", "lb"),
        ("not readable", None),
    ]
    assert all(
        r["recorded_at"] == original.created_at.isoformat() and r["received_at"] is None
        for r in reports
    )
    assert all("judgment" not in r and "rule_id" not in r and "source_ref" not in r for r in rows)


def test_reading_history18h4_evidence_rejection(reading_world: PatientWorld) -> None:
    from sanad.evidence.doctor import decide

    w = reading_world
    monitor(w, count=2)
    doc = f.lab(items=[{"name": "BP", "value": "130/85", "unit": "mmHg"}], document_type="other")
    f.providers(w, doc, doc)
    assert f.upload(w, caption="BP monitor screen") == "accepted"
    assert [r["value"] for r in history(w)] == ["130/85"]
    evidence = f.current(w)
    assert (
        decide(
            w.runtime.steward, w.owner, evidence, "reject", "history-reject", reason="Wrong patient"
        ).status
        == "accepted"
    )
    assert history(w) == []


def test_reading_history18h4_first_response_and_stored_words() -> None:
    from sanad.presentation.patient_browser import CATALOG
    from sanad.web.browser import patient_content

    html = patient_content(
        {"next_missions": [{"title": "Recorded request", "due_at": "2026-09-12T20:00:00Z"}]}, "en"
    )
    assert 'data-browser-instant hidden datetime="2026-09-12T20:00:00Z"' in html
    assert "<bdi>2026-09-12T20:00:00Z</bdi>" not in html
    assert CATALOG["patient_browser.show_all"] == {"en": "Show all {N}", "ar": "اعرض الكل ({N})"}
    assert CATALOG["patient_browser.show_latest"] == {"en": "Show latest 5", "ar": "اعرض آخر 5"}
    assert "Telegram" not in CATALOG["patient_browser.reminder_explanation"]["en"]
    assert "تيليجرام" not in CATALOG["patient_browser.reminder_explanation"]["ar"]
