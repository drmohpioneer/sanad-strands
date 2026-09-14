"""T38's persisted clinical chart, rendered by the real browser and accepted APIs."""

import json
from pathlib import Path
from typing import Any

import pytest
from browser.conftest import RenderedApp
from browser.conftest import rendered as rendered
from harness import FakeClock
from playwright.sync_api import expect
from store.concierge_fixtures import PatientWorld

from sanad.store._base import StoreBase
from sanad.store.keys import partition
from system.subjects import SubjectWorld
from system.test_journey import build_t38_world


@pytest.fixture
def journey(
    store: StoreBase, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> tuple[SubjectWorld, SubjectWorld, SubjectWorld]:
    worlds = build_t38_world(store, clock, monkeypatch)
    a = worlds[0]
    # T38's original assertions run unchanged. Extend only this render world with
    # a doctor-confirmed frequency so a missing frequency cannot pass vacuously.
    proposal = a.dictate(
        "Synthetic Alice. Add Concor 5 mg once daily.",
        {
            "patient": {"name_as_spoken": "Synthetic Alice"},
            "orders": [
                {
                    "action": "start",
                    "drug": "Concor",
                    "dose": "5 mg",
                    "frequency": "once daily",
                    "action_quote": "Add",
                }
            ],
        },
        id=7000,
    )
    assert not proposal.blocked("all") and not proposal.blocked("order:0")
    a.tap(id=7001)
    assert a.scribe.repo.pending(a.doctor.scope) is None
    a.dictate(
        "Synthetic Alice. We can hold Aspirin for now because of bruising; stop Lasix.",
        {
            "patient": {"name_as_spoken": "Synthetic Alice"},
            "orders": [
                {"action": "stop", "drug": "Aspirin", "action_quote": "We can hold"},
                {"action": "stop", "drug": "Lasix", "action_quote": "stop"},
            ],
        },
        id=7002,
    )
    a.tap(id=7003)
    return worlds


@pytest.fixture
def world(journey: tuple[SubjectWorld, SubjectWorld, SubjectWorld]) -> PatientWorld:
    return journey[0]


@pytest.mark.parametrize("rendered", [(1440, "light")], indirect=True)
def test_t38_all_record_kinds_render(
    rendered: RenderedApp, journey: tuple[SubjectWorld, SubjectWorld, SubjectWorld], tmp_path: Path
) -> None:
    page = rendered.page
    page.clock.set_fixed_time(journey[0].clock())
    totals: dict[str, int] = {}
    seen_frequency = False
    stored_kinds: set[str] = set()
    # This records all kinds, including persistence infrastructure; chart payload
    # coverage is separately checked by stable record identity in visible panels.
    payload_kinds: dict[str, str] = {
        "patient": "display_name/contact_status/age",
        "patient_profile": "patient_id/contact_status",
        "care_order_head": "orders",
        "care_order_version": "orders.history",
        "mission": "missions",
        "clinical_fact": "facts/fact_history",
        "evidence": "evidence",
        "followup": "followups",
        "review": "reviews/review_history",
        "correction": "corrections",
        "patient_media": "media",
        "consent": "consents",
        "patient_binding": "bindings",
    }
    for peer in journey:
        api_url = f"{rendered.origin}/api/patients/{peer.patient_scope.patient_id}"
        record_read = page.request.get(api_url)
        evidence_response = page.request.get(api_url + "/evidence")
        assert record_read.status == evidence_response.status == 200
        record, evidence = record_read.json(), evidence_response.json()
        # Read the stored kind inventory independently of the API projection.
        cursor = None
        while True:
            rows, cursor = peer.store._query(
                partition(peer.patient_scope), cursor=cursor, limit=100
            )
            stored_kinds.update(str(row["entity_type"]) for row in rows if "entity_type" in row)
            if cursor is None:
                break
        if peer is journey[0]:
            assert len(record["held_medications"]) == 1
            assert record["held_medications"][0]["drug"] == "Aspirin"
            page.goto(rendered.origin + "/a?filter=all")
            page.locator('#content[aria-busy="false"]').wait_for()
            page.locator(
                f'.patient-row:has(+ .detail a.primary[href$="/{peer.patient_scope.patient_id}"])'
            ).click()
            page.locator(".patient-row.open + .detail a.primary").click()
            record_page = page.locator("#content")
            expect(record_page.locator("[data-held-order]")).to_contain_text(
                "Aspirin is on hold since"
            )
            expect(record_page.locator("[data-held-order]")).to_contain_text("of bruising")
            expect(record_page.locator("[data-held-order]")).to_contain_text("since")
            expect(record_page.locator("[data-order]").filter(has_text="Lasix")).to_have_count(0)
        base = f"{rendered.origin}/a/patients/{peer.patient_scope.patient_id}"
        with page.expect_response(base.replace("/a/", "/api/")) as record_response:
            page.goto(base)
        page.locator('#content[aria-busy="false"]').wait_for()
        assert record_response.value.json() == record
        expect(page.locator("#feedback")).to_be_empty()
        expect(page.locator("#title")).to_have_text(record["display_name"])
        expect(page.locator("body")).to_have_attribute(
            "data-patient", peer.patient_scope.patient_id
        )
        assert peer.store.get_patient_profile(peer.patient_scope) is not None
        for tab in ("Medicines", "Requests", "Documents", "History"):
            page.get_by_role("tab", name=tab, exact=True).click()
            panel = page.get_by_role("tabpanel")
            expect(panel).to_be_visible()
            if tab == "Medicines":
                for consent in record["consents"]:
                    expect(panel.locator(".consent-binding")).to_contain_text(
                        "Agreed to Sanad messages on"
                    )
                    expect(panel.locator(".consent-binding")).to_contain_text(
                        f"(consent version {consent['version']})"
                    )
                for binding in record["bindings"]:
                    if binding["confirmed_at"]:
                        expect(page.locator(".record-heading")).to_contain_text("Joined on")

                for hold in record["held_medications"]:
                    card = panel.locator(f'[data-held-order="{hold["order_id"]}"]')
                    expect(card).to_be_visible()
                    expect(card).to_contain_text(hold["drug"] + " is on hold since")
                    expect(card).to_contain_text("of bruising")
                    expect(card).to_contain_text("since")
                for order in record["orders"]:
                    if order["status"] != "active":
                        expect(panel.locator(f'[data-order="{order["id"]}"]')).to_have_count(0)
                        continue
                    card = panel.locator(f'[data-order="{order["id"]}"]')
                    expect(card).to_be_visible()
                    instruction = order["current_version"]["structured_instruction"]
                    for field in ("drug", "dose", "frequency"):
                        if instruction.get(field):
                            expect(card).to_contain_text(str(instruction[field]))
                            if field == "frequency":
                                seen_frequency = True
                    for old in order["history"]:
                        if old["id"] != order["current_version"]["id"]:
                            card.locator("summary").click()
                            for field in ("drug", "dose", "frequency"):
                                if old["structured_instruction"].get(field):
                                    expect(card).to_contain_text(
                                        str(old["structured_instruction"][field])
                                    )
                for fact in record["facts"]:
                    card = panel.locator(f'[data-fact="{fact["id"]}"]')
                    expect(card).to_be_visible()
                    if fact["payload"].get("text"):
                        expect(card).to_contain_text(fact["payload"]["text"])
            elif tab == "Requests":
                for mission in record["missions"]:
                    card = panel.locator(f'[data-mission="{mission["id"]}"]')
                    expect(card).to_be_visible()
                    expect(card).to_contain_text(mission["title"])
                    expect(card.locator("time").first).to_have_attribute(
                        "datetime", mission["due_at"]
                    )
                for follow in record["followups"]:
                    card = panel.locator(f'[data-followup="{follow["id"]}"]')
                    expect(card).to_be_visible()
                    expect(card).to_contain_text("Day-three medication follow-up")
                    if follow["due_at"]:
                        expect(card.locator("time")).to_have_attribute("datetime", follow["due_at"])
                    else:
                        expect(card).to_contain_text("date not recorded")
                        expect(card.locator("time")).to_have_count(0)
                for review in record["reviews"]:
                    card = page.locator(f'[data-obligation="{review["id"]}"]')
                    expect(card).to_be_visible()
                    expect(card.locator("a")).to_have_count(1)
                    expect(panel.locator(f'[data-review="{review["id"]}"]')).to_have_count(0)
                for review in record["review_history"]:
                    card = panel.locator(f'[data-review="{review["id"]}"]')
                    expect(card).to_be_visible()
                    expect(card).to_contain_text("Reviewed on")
            elif tab == "Documents":
                for e in evidence:
                    card = panel.locator(f'[data-evidence="{e["id"]}"]')
                    expect(card).to_be_visible()
                    expect(card.locator(".status")).to_contain_text(
                        "Not used" if e["association_state"] == "detached" else "On file."
                    )
                    expect(card.locator("time").first).to_have_attribute(
                        "datetime", e["provenance"]["received_at"]
                    )
                    for value in e["extracted_values"]:
                        for field in ("name", "dose", "value", "frequency"):
                            if value.get(field):
                                expect(card).to_contain_text(str(value[field]))
                for media in record["media"]:
                    if media["uploaded_by_you"]:
                        expect(
                            panel.locator(f'[data-media][href$="/{media["media_id"]}"]').first
                        ).to_contain_text("Uploaded by you on")

                    expect(
                        panel.locator(f'[data-media][href$="/{media["media_id"]}"]').first
                    ).to_be_visible()
            else:
                for order in record["orders"]:
                    if order["status"] == "stopped":
                        expect(
                            panel.locator(f'[data-stopped-order="{order["id"]}"]')
                        ).to_be_visible()
                for correction in record["corrections"]:
                    card = panel.locator(f'[data-correction="{correction["id"]}"]')
                    expect(card).to_be_visible()
                    expect(card).to_contain_text("changed from")
                    expect(card).to_contain_text("by the doctor on")
                panel.locator("summary").first.click()
                for fact in record["fact_history"]:
                    expect(panel.locator(f'[data-fact-history="{fact["id"]}"]')).to_be_visible()
        counts: dict[str, Any] = {
            k: len(record[k])
            for k in (
                "orders",
                "missions",
                "followups",
                "facts",
                "fact_history",
                "corrections",
                "reviews",
                "review_history",
                "media",
            )
        }
        counts["evidence"] = len(evidence)
        for k, count in counts.items():
            totals[k] = totals.get(k, 0) + count
    assert seen_frequency
    assert all(
        totals[k]
        for k in (
            "orders",
            "missions",
            "followups",
            "evidence",
            "corrections",
            "reviews",
            "review_history",
        )
    )
    assert set(payload_kinds) <= stored_kinds
    report = {
        "rendered": totals,
        "stored_record_kinds": sorted(stored_kinds),
        "unrendered": sorted(stored_kinds - payload_kinds.keys()),
    }
    (tmp_path / "t38-render.json").write_text(json.dumps(report, indent=2))
    print("T38_RENDER " + json.dumps(report, sort_keys=True))
