"""18g's binding sentences, navigation, counts and human-facing language rails."""

import json
import re
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from playwright.sync_api import Dialog, Page, Route, expect
from store.account_fixtures import ADMIN, PATIENT

from browser.conftest import RenderedApp
from browser.depth18e import identity
from sanad.presentation.patient_browser import CATALOG
from sanad.web.browser import surface

NOW = datetime.fromisoformat("2026-09-12T12:00:00+00:00")
PAST = "2026-09-10T12:00:00Z"
FUTURE = "2026-09-14T12:00:00Z"
TODAY = "2026-09-12T20:00:00Z"
QUIET = "All quiet. Nothing is waiting on you or on the patient."
REVIEW_WORDS = {
    "incident_response": "Respond to the danger report. Nobody has answered it yet.",
    "result_review": "Read the result and tell the patient what it means.",
    "evidence_association": (
        "A document arrived. Say which request it belongs to, or that it is not this patient's."
    ),
    "question_answer": "The patient asked a question. Answer it.",
    "correction_disposition": "A correction to the record is waiting for your yes or no.",
    "unmet_objective": (
        'The patient missed "the request". Decide: chase again, extend, or close it.'
    ),
    "followup_disposition": (
        "The follow-up on the new medicine needs your decision "
        "(it came back, ran late, or could not be sent)."
    ),
    "media_failure": "A photo the patient sent could not be read. Ask them to send it again.",
    "intake_clarification": "A new patient's file needs one clarification before it is complete.",
    "delivery_failure": "A message to this patient did not arrive. Check how to reach them.",
    "binding_review": (
        "Check this patient's link: who joined, or a conflict in what they asked for "
        "(stop, quiet hours)."
    ),
    "coverage_review": "Check who is covering these patients.",
}
ACK = "You have seen this. It stays open until you reply."
CHANGED = " Something changed since you last looked."
OLD_WORDS = (
    "No patients recorded yet. Add a patient through Telegram.",
    "Acknowledgment does not resolve a review. Actions remain in Telegram.",
    "Sanad writes to you on Telegram when your doctor is waiting for",
    "Change or cancel the request in Telegram.",
    "Needs you now",
    "Reinstate",
    "Reinstated.",
    "cover arranged",
    "Handled from the intake message in Telegram.",
    "Handled from the review message in Telegram.",
    "Check your Telegram.",
    "confirmation in Telegram.",
    "Confirm in Telegram",
    "until handled in Telegram.",
    "Needs review",
    "Pending review",
    "Awaiting link",
    "Danger response",
    "Material change",
    "Missing",
    "Not yet due",
    "Blocked",
    "Unverifiable",
    "Received but processing",
    "Fulfillment under review",
    "Deadline follow-up",
    "Document association",
    "Intake clarification",
    "Correction review",
    "coverage",
    "admin_rejected",
    "unverified",
)


def old_word_walk(page: Page) -> None:
    texts = page.evaluate("""() => {
      const walk=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT),out=[];
      while(walk.nextNode()){
        const node=walk.currentNode, element=node.parentElement;
        if(element.closest('script,style,details.support'))continue;
        out.push(node.textContent);
      }
      for(const element of document.querySelectorAll('[aria-label],[title]'))
        if(!element.closest('details.support'))out.push(element.getAttribute('aria-label')||'',
          element.getAttribute('title')||'');
      return out.join(' ');
    }""")
    for word in OLD_WORDS:
        assert word not in texts, word
    assert not re.search(r"\b[a-zA-Z]+_[a-zA-Z_]+\b", texts)
    assert "Due today" not in page.locator(".tile-label").all_text_contents()
    identity(page)


def record(app: RenderedApp, **changes: Any) -> dict[str, Any]:
    return {
        "patient_id": app.world.patient_scope.patient_id,
        "display_name": "Synthetic Human Words",
        "age": "61",
        "timezone": "UTC",
        "contact_status": "active",
        "bindings": [{"confirmed_at": PAST, "status": "active"}],
        "missions": [],
        "reviews": [],
        "review_history": [],
        "orders": [],
        "facts": [],
        "followups": [],
        "corrections": [],
        "consents": [],
        "media": [],
        "last_activity_at": PAST,
        **changes,
    }


def fulfill_projection(payload: Any, route: Route) -> None:
    route.fulfill(json=payload)


def projected(app: RenderedApp, data: dict[str, Any], evidence: list[Any] | None = None) -> None:
    """Project a synthetic source state through the shipped shell and browser script."""
    page = app.page
    id = app.world.patient_scope.patient_id
    page.clock.set_fixed_time(NOW)
    for r in data["reviews"] + data["review_history"]:
        r["patient_id"] = id
    for path, payload in (
        ("/api/patients", [{"patient_id": id}]),
        (f"/api/patients/{id}", data),
        (f"/api/patients/{id}/evidence", evidence or data.get("evidence", [])),
        ("/api/browser/reviews", data["reviews"]),
        ("/api/browser/reviews?history=true", data["review_history"]),
    ):
        page.unroute(app.origin + path)
        page.route(app.origin + path, partial(fulfill_projection, payload))


def goto(app: RenderedApp, path: str) -> Page:
    page = app.page
    page.goto(app.origin + path)
    page.locator('#content[aria-busy="false"]').wait_for()
    expect(page.locator("#feedback")).to_be_empty()
    return page


def mission(state: str, **changes: Any) -> dict[str, Any]:
    return {
        "id": "human-request",
        "title": "Send the reading",
        "state": state,
        "due_at": PAST,
        "review_status": "not_required",
        "details": {},
        **changes,
    }


def review(kind: str, **changes: Any) -> dict[str, Any]:
    return {
        "id": "human-review",
        "review_kind": kind,
        "state": "open",
        "source_type": "patient",
        "source_id": "not-projected",
        "source_version": 1,
        "last_material_change_version": 1,
        "review_at": PAST,
        "first_notice_at": "2026-09-01T00:00:00Z",
        **changes,
    }


def test_18g_every_outstanding_sentence(rendered: RenderedApp) -> None:
    app, page = rendered, rendered.page
    cases: list[tuple[dict[str, Any], str, str | None]] = []
    for kind, text in REVIEW_WORDS.items():
        cases.append((record(app, reviews=[review(kind)]), text, None))
    for kind in ("incident_response", "result_review"):
        cases.append((record(app, reviews=[review(kind, state="acknowledged")]), ACK, None))
        cases.append(
            (
                record(app, reviews=[review(kind, last_material_change_version=2)]),
                REVIEW_WORDS[kind] + CHANGED,
                None,
            )
        )
    for state, extra, text, phrase in [
        (
            "proposed",
            {},
            '"Send the reading" is proposed and waits for your confirmation.',
            "Proposed",
        ),
        (
            "awaiting_link",
            {},
            '"Send the reading" starts once the patient joins.',
            "Starts when the patient joins",
        ),
        (
            "open",
            {"due_at": FUTURE},
            'Nothing to do yet: "Send the reading" is due Sep 14.',
            "Not due yet",
        ),
        ("open", {}, '"Send the reading" was due Sep 10 and has not arrived.', "Late"),
        ("overdue", {}, '"Send the reading" was due Sep 10 and has not arrived.', "Late"),
        (
            "open",
            {"due_at": None},
            '"Send the reading" is in progress; no due date was set.',
            "No due date",
        ),
        (
            "waiting_patient",
            {},
            'Sanad asked the patient about "Send the reading" and is waiting for their answer.',
            "Waiting for the patient",
        ),
        (
            "blocked",
            {},
            'The patient cannot do "Send the reading" yet.',
            "Patient cannot do it yet",
        ),
        (
            "unreachable",
            {},
            '"Send the reading" cannot reach the patient. Check how to contact them.',
            "Unreachable",
        ),
        (
            "processing",
            {},
            '"Send the reading": something arrived and is being checked.',
            "Being checked",
        ),
        (
            "fulfilled",
            {"fulfillment_validity": "invalidated_pending_review"},
            '"Send the reading" was marked done, then something changed. Confirm it is still done.',
            "Confirm still done",
        ),
    ]:
        cases.append(
            (record(app, missions=[mission(state, **cast(dict[str, Any], extra))]), text, phrase)
        )
    for contact in ("paused", "opted_out"):
        cases.append(
            (
                record(app, contact_status=contact, missions=[mission("overdue")]),
                '"Send the reading" was due Sep 10 and has not arrived. '
                "Reminders are paused, so nobody is chasing it.",
                "Late",
            )
        )
    for kind, state, date, text, phrase in [
        (
            "MEDICATION_DAY3",
            "awaiting_anchor",
            None,
            "Day-three check on the new medicine will be scheduled once the start date is known.",
            "Waiting for the start date",
        ),
        (
            "MEDICATION_DAY3",
            "scheduled",
            FUTURE,
            "Day-three check on the new medicine is on Sep 14. Nothing to do until then.",
            "Scheduled",
        ),
        (
            "CLINICAL_CHECKIN",
            "scheduled",
            FUTURE,
            "Check-in with the patient is on Sep 14.",
            "Scheduled",
        ),
        (
            "MEDICATION_DAY3",
            "waiting_response",
            PAST,
            "Sanad asked how the new medicine is going and is waiting for the patient's answer.",
            "Waiting for an answer",
        ),
        (
            "CLINICAL_CHECKIN",
            "waiting_response",
            PAST,
            "Sanad asked the patient how they are doing and is waiting for their answer.",
            "Waiting for an answer",
        ),
        (
            "MEDICATION_DAY3",
            "overdue",
            PAST,
            "The follow-up was due Sep 10 and has no answer yet.",
            "Late",
        ),
        (
            "MEDICATION_DAY3",
            "contact_suppressed",
            PAST,
            "The follow-up cannot be sent: contact with this patient is paused.",
            "Cannot be sent",
        ),
        (
            "CLINICAL_CHECKIN",
            "overdue",
            None,
            "The follow-up was due date not recorded and has no answer yet.",
            "Late",
        ),
    ]:
        cases.append(
            (
                record(
                    app,
                    followups=[
                        {"id": "human-follow", "kind": kind, "state": state, "due_at": date}
                    ],
                ),
                text,
                phrase,
            )
        )
    for data, text, expected_phrase in cases:
        projected(app, data)
        goto(app, "/a?filter=all")
        expect(page.locator(".patient-sentence")).to_have_text(text)
        old_word_walk(page)
        goto(app, f"/a/patients/{data['patient_id']}")
        expect(page.locator("#what-to-do a")).to_have_text(text)
        expect(page.locator("#content").get_by_text(text, exact=True)).to_have_count(1)
        page.get_by_role("tab", name="Requests", exact=True).click()
        if expected_phrase:
            expect(page.locator("[data-mission] .status,[data-followup] .status")).to_have_text(
                expected_phrase
            )
            expect(page.locator(".tab-panel").get_by_text(text, exact=True)).to_have_count(0)
        old_word_walk(page)
        if data["reviews"]:
            goto(app, "/a/inbox")
            expect(page.locator(".review-line")).to_have_text(text)
            expect(page.locator(".inbox-item .status")).to_have_count(0)
            old_word_walk(page)


def test_18g_dates_targets_and_no_repeats(rendered: RenderedApp) -> None:
    app, page = rendered, rendered.page
    data = record(
        app,
        missions=[mission("overdue", created_at=PAST)],
        followups=[{"id": "f", "kind": "CLINICAL_CHECKIN", "state": "scheduled", "due_at": FUTURE}],
        reviews=[
            review("unmet_objective", source_type="mission", source_id="human-request"),
            review("question_answer", id="q", source_type="mission", source_id="human-request"),
            review("result_review", id="e", source_type="evidence", source_id="doc"),
            review(
                "correction_disposition", id="c", source_type="correction", source_id="correction"
            ),
            review("media_failure", id="absent", source_type="evidence"),
        ],
        corrections=[{"id": "correction", "created_at": PAST}],
    )
    evidence = [
        {
            "id": "doc",
            "evidence_id": "doc",
            "association_state": "accepted",
            "category": "lab_result",
            "provenance": {"received_at": PAST},
        }
    ]
    projected(app, data, evidence)
    goto(app, "/a?filter=all")
    page.locator(".patient-row").click()
    expect(page.locator(".detail")).to_contain_text("6 more on the full record")
    list_sentence = page.locator(".patient-sentence").inner_text()
    goto(app, f"/a/patients/{data['patient_id']}")
    expect(page.locator("#what-to-do [data-obligation]")).to_have_count(7)
    expect(page.locator("#what-to-do").get_by_text(list_sentence, exact=True)).to_have_count(1)
    for id, tab, target in [
        ("human-request", "Requests", "human-request"),
        ("f", "Requests", "f"),
        ("human-review", "Requests", "human-request"),
        ("q", "Requests", "human-request"),
        ("e", "Documents", "doc"),
        ("c", "History", "correction"),
        ("absent", "Documents", "record-panel-2"),
    ]:
        link = page.locator(f'[data-obligation="{id}"] a')
        link.click()
        expect(page.get_by_role("tab", name=tab, exact=True)).to_have_attribute(
            "aria-selected", "true"
        )
        expect(page.locator('[id="' + target + '"]')).to_be_focused()
        assert page.locator('[id="' + target + '"]').is_visible()
    # The evidence endpoint's date is not in the record API: no arrival date is invented.
    expect(page.locator('[data-obligation="e"]')).to_have_text(REVIEW_WORDS["result_review"])
    expect(page.locator('[data-obligation="q"]')).to_contain_text("asked a question Sep 10")
    expect(page.locator('[data-obligation="human-review"]')).to_contain_text("(due Sep 10)")
    old_word_walk(page)


def test_18g_history_labels(rendered: RenderedApp) -> None:
    app, page = rendered, rendered.page
    earlier = {
        "id": "instruction-v1",
        "structured_instruction": {"drug": "Atorvastatin", "dose": "10 mg"},
    }
    current = {
        "id": "instruction-v2",
        "structured_instruction": {"drug": "Atorvastatin", "dose": "20 mg"},
    }
    data = record(
        app,
        orders=[
            {
                "id": "medicine",
                "status": "active",
                "current_version": current,
                "history": [earlier, current],
            }
        ],
        review_history=[review("result_review", state="resolved", resolved_at=PAST)],
    )
    projected(app, data)
    goto(app, f"/a/patients/{data['patient_id']}")
    medicine = page.locator('[data-order="medicine"]')
    summary = medicine.locator("details summary")
    expect(summary).to_have_text("Earlier versions of this instruction")
    summary.click()
    expect(medicine.locator("details")).to_contain_text("10 mg")
    expect(medicine.locator("details")).not_to_contain_text("20 mg")
    expect(page.locator('#navigation a[href="/a/history"]')).to_have_text("Past reviews")
    page.get_by_role("tab", name="Requests", exact=True).click()
    requests = page.get_by_role("tabpanel", name="Requests", exact=True)
    expect(requests.get_by_role("heading", name="Past reviews", exact=True)).to_be_visible()
    expect(requests.locator("[data-review] p")).to_have_text("Reviewed on Sep 10: result closed.")
    old_word_walk(page)
    goto(app, "/a/history")
    expect(page.get_by_role("heading", name="Past reviews", exact=True)).to_be_visible()
    expect(page.locator('#navigation a[href="/a/history"]')).to_have_text("Past reviews")
    expect(page.locator(".clinical caption")).to_have_text(
        "Past reviews. Use the column buttons to sort."
    )
    expect(page.locator(".patient-sentence")).to_have_text("Reviewed on Sep 10: result closed.")
    old_word_walk(page)


def test_18g_terminal_states_history_and_contact(rendered: RenderedApp) -> None:
    app, page = rendered, rendered.page
    terminal = [
        mission(state, id=state)
        for state in ("fulfilled", "cancelled", "closed_unfulfilled", "superseded")
    ]
    followups = [
        {"id": "f-" + state, "kind": "MEDICATION_DAY3", "state": state}
        for state in ("fulfilled", "cancelled")
    ]
    evidence = [
        {"id": "d-" + state, "category": "lab_result", "association_state": state}
        for state in (
            "candidate",
            "unmatched",
            "accepted",
            "accepted_pending_identity",
            "rejected",
            "detached",
        )
    ]
    data = record(
        app,
        missions=terminal,
        followups=followups,
        held_medications=[{"drug": "Aspirin", "order_id": "held", "since": None, "reason": None}],
        consents=[{"version": 2, "accepted_at": PAST, "withdrawn_at": PAST}],
        review_history=[
            review(kind, id=kind, state="resolved", resolved_at=PAST) for kind in REVIEW_WORDS
        ],
    )
    projected(app, data, evidence)
    goto(app, "/a?filter=all")
    expect(page.locator(".patient-sentence")).to_have_text(QUIET)
    goto(app, f"/a/patients/{data['patient_id']}")
    expect(page.locator("#what-to-do")).to_contain_text(QUIET)
    expect(page.locator("[data-held-order]")).to_contain_text(
        "Aspirin is on hold since date not recorded: no reason given."
    )
    expect(page.locator(".consent-binding")).to_contain_text(
        "Agreed to Sanad messages on Sep 10 (consent version 2)"
    )
    expect(page.locator(".consent-binding")).to_contain_text("Withdrew consent on Sep 10")
    page.get_by_role("tab", name="Requests", exact=True).click()
    expect(page.locator("[data-mission] .status")).to_have_text(
        ["Done", "Cancelled", "Closed", "Replaced"]
    )
    expect(page.locator("[data-followup] .status")).to_have_text(["Done", "Cancelled"])
    for id, ending in [
        ("fulfilled", "done"),
        ("cancelled", "cancelled"),
        ("closed_unfulfilled", "closed without being done"),
        ("superseded", "replaced"),
    ]:
        expect(page.locator(f'[data-mission="{id}"]')).to_contain_text(
            f'"Send the reading": {ending} on date not recorded.'
        )
    expect(page.locator('[data-followup="f-fulfilled"]')).to_contain_text(
        "Follow-up done on date not recorded."
    )
    expect(page.locator('[data-followup="f-cancelled"]')).to_contain_text(
        "Follow-up cancelled on date not recorded."
    )
    nouns = [
        "danger report",
        "result",
        "document",
        "question",
        "correction",
        "missed deadline",
        "follow-up",
        "unreadable photo",
        "new patient's file",
        "undelivered message",
        "patient link",
        "cover",
    ]
    expect(page.locator("[data-review] p")).to_have_text(
        [f"Reviewed on Sep 10: {noun} closed." for noun in nouns]
    )
    page.get_by_role("tab", name="Documents", exact=True).click()
    expect(page.locator("[data-evidence] .status")).to_have_text(
        [
            "Waiting for you to say whose it is.",
            "Not matched to any request yet.",
            "On file.",
            "On file, but the patient's identity is not confirmed.",
            "Not used.",
            "Not used.",
        ]
    )
    old_word_walk(page)
    goto(app, "/a/history")
    expect(page.locator(".patient-sentence")).to_have_count(12)
    for noun in nouns:
        expect(
            page.locator(".patient-sentence").filter(has_text=f": {noun} closed.")
        ).to_have_count(1)
    old_word_walk(page)
    for reason, words in {
        "doctor_reviewed": "reviewed by you",
        "answered": "answered",
        "closed": "closed",
        "corrected": "corrected",
        "patient_stopped": "closed because the patient stopped reminders",
        "order_superseded": "closed because the instruction was replaced",
    }.items():
        data = record(
            app, review_history=[review("media_failure", state="resolved", resolved_reason=reason)]
        )
        projected(app, data)
        goto(app, "/a/history")
        expect(page.locator(".patient-sentence")).to_have_text(
            f"Reviewed on date not recorded: unreadable photo {words}."
        )
    for contact, line, heading in [
        ("active", QUIET, "Joined on Sep 10"),
        (
            "awaiting_link",
            "Has not joined Sanad yet. Nothing reaches them until they open the invitation.",
            "Has not joined yet: nothing reaches them until they open the invitation",
        ),
        (
            "paused",
            "All quiet. Reminders are paused by the patient.",
            "Reminders paused by the patient",
        ),
        (
            "opted_out",
            "All quiet. The patient turned routine messages off.",
            "The patient turned routine messages off",
        ),
        (
            "unreachable",
            "Sanad cannot reach this patient. Check how to contact them.",
            "Sanad cannot reach this patient",
        ),
        ("frozen", "Contact with this patient is frozen.", "Contact frozen"),
    ]:
        data = record(app, contact_status=contact)
        projected(app, data)
        goto(app, "/a?filter=all")
        expect(page.locator(".patient-sentence")).to_have_text(line)
        goto(app, f"/a/patients/{data['patient_id']}")
        expect(page.locator(".record-heading")).to_contain_text(heading)
        expect(page.locator(".record-heading")).to_contain_text("age 61")


def test_18g_click_anywhere_back_and_counts(rendered: RenderedApp) -> None:
    app, page = rendered, rendered.page
    data = record(
        app,
        missions=[mission("overdue"), mission("open", id="today", due_at=TODAY)],
        followups=[{"id": "f", "kind": "MEDICATION_DAY3", "state": "overdue", "due_at": PAST}],
        reviews=[
            review("incident_response", state="acknowledged"),
            review("result_review", id="r", state="acknowledged"),
        ],
    )
    projected(app, data)
    goto(app, "/a")
    expect(page.locator(".tile-label")).to_have_text(
        ["Emergency", "Waiting on you", "Patient is late", "Patient tasks due today"]
    )
    expect(page.locator(".summary-tile strong")).to_have_text(["1", "1", "2", "1"])
    expect(page.locator('[data-summary="danger"] .tile-clause')).to_have_text("1 needs a response")
    expect(page.locator('[data-summary="overdue"] .tile-clause')).to_have_text(
        "oldest has waited 2 days"
    )
    for key in ("danger", "pending_review", "overdue", "due_today"):
        tile = page.locator(f'[data-summary="{key}"]')
        tile.click()
        expect(tile).to_have_attribute("aria-pressed", "true")
        expect(page.locator('#filter [aria-pressed="true"]')).to_have_count(0)
        expect(page.locator(".patient-row")).to_have_count(1)
    page.locator('#filter [data-filter="all"]').click()
    for selector in (".who2 small", ".need", ".who2 b"):
        row = page.locator(".patient-row").first
        row.locator(selector).click()
        expect(row).to_have_attribute("aria-expanded", "true")
        row.locator(selector).click()
        expect(row).to_have_attribute("aria-expanded", "false")
    row.focus()
    page.keyboard.press("Enter")
    expect(row).to_have_attribute("aria-expanded", "true")
    page.keyboard.press("Enter")
    expect(row).to_have_attribute("aria-expanded", "false")
    row.click()
    page.locator(".patient-row.open + .detail a.primary").click()
    expect(page).to_have_url(app.origin + f"/a/patients/{data['patient_id']}")
    expect(page.locator("#what-to-do")).to_be_visible()
    expect(page.locator("#patient-drawer")).to_have_count(0)
    page.go_back()
    page.locator('#content[aria-busy="false"]').wait_for()
    page.locator(".patient-row").first.click()
    with page.context.expect_page() as opened:
        page.locator(".patient-row.open + .detail a.primary").click(modifiers=["Meta"])
    expect(opened.value).to_have_url(app.origin + "/a/patients/" + data["patient_id"])
    opened.value.close()
    page.unroute(app.origin + "/api/patients")
    # Restore the list position on the default Needs me tab.
    goto(app, "/demo")
    old_word_walk(page)
    page.locator('#filter [data-filter="all"]').click()
    expect(page.locator(".patient-row")).to_have_count(18)
    expect(page.locator("#next")).to_have_count(0)
    page.locator('#filter [data-filter="needs"]').click()
    page.locator(".patient-row").last.scroll_into_view_if_needed()
    position = page.evaluate("scrollY")
    page.locator(".patient-row").last.click()
    page.locator(".patient-row.open + .detail a.primary").click()
    expect(page.locator("#what-to-do")).to_be_visible()
    page.locator("#back").click()
    page.locator('#content[aria-busy="false"]').wait_for()
    assert abs(page.evaluate("scrollY") - position) < 5


@pytest.mark.parametrize("locale", ["en", "ar"])
def test_18g_patient_links_words_and_uploads(
    rendered: RenderedApp, locale: Literal["en", "ar"], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANAD_CONTEST_ENGLISH", "0")
    app, page = rendered, rendered.page
    app.login(PATIENT)
    # Exercise both locales through the actual server shell and its shipped catalog.
    page.route(
        app.origin + "/pp",
        lambda r: r.fulfill(
            content_type="text/html", body=surface("Synthetic Patient", locale, audience="patient")
        ),
    )
    uploads = [
        {"received_at": PAST, "state": state}
        for state in (
            "received",
            "processing",
            "accepted",
            "needs_doctor_review",
            "not_used",
            "rejected",
        )
    ]
    page.route(app.origin + "/api/patient/uploads", lambda r: r.fulfill(json=uploads))
    page.clock.set_fixed_time(NOW)
    goto(app, "/pp")
    expect(page.locator("#patient-uploads p")).to_have_count(5)
    page.locator("#patient-uploads-toggle").click()
    expect(page.locator("#patient-uploads p")).to_have_count(6)
    expect(page.locator("#patient-uploads h3")).to_have_text(
        CATALOG["patient_browser.documents_sent"][locale]
    )
    expect(page.locator("#patient-reminder-explanation")).to_have_text(
        CATALOG["patient_browser.reminder_explanation"][locale]
    )
    explanation = page.locator("#patient-reminder-explanation").inner_text()
    assert "dose reminder" not in explanation and "nothing arrives" not in explanation
    for tile in page.locator("#content .summary-tile").all():
        assert tile.evaluate("e=>e.tagName") == "A"
        id = tile.get_attribute("href")
        assert id
        tile.click()
        expect(page.locator(id)).to_be_focused()
        expect(page.locator(id)).to_be_in_viewport()
    selectors = {
        "conversation": "#patient-controls section:first-child h2",
        "message": "#patient-message-form label",
        "send": "#patient-message-form button",
        "upload": "#patient-controls section:nth-child(2) h2",
        "file": "#patient-upload-form label:first-child",
        "caption": "#patient-upload-form label:nth-child(2)",
        "limits": "#patient-file-help",
        "preferences": "#patient-controls section:nth-child(3) h2",
        "quiet_start": "#patient-quiet-form label:first-child",
        "save": "#patient-quiet-form button",
    }
    for key, selector in selectors.items():
        assert (
            page.locator(selector).evaluate(
                "e=>[...e.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent).join('').trim()"
            )
            == CATALOG["patient_browser." + key][locale]
        )
    for i, file in enumerate(uploads):
        expect(page.locator("#patient-uploads p").nth(i)).to_contain_text(
            " · " + CATALOG["patient_browser." + file["state"]][locale]
        )
    expect(page.locator("html")).to_have_attribute("dir", "rtl" if locale == "ar" else "ltr")
    identity(page)
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    uploads.clear()
    page.locator("#refresh").click()
    expect(page.locator("#patient-uploads h3")).to_have_count(0)
    expect(page.locator("#patient-uploads p")).to_have_count(0)
    assert CATALOG["patient_browser.no_messages"]["ar"] == "لسه مفيش رسايل."


def test_18g_admin_counts_reasons_and_results(rendered: RenderedApp) -> None:
    app, page = rendered, rendered.page
    app.login(ADMIN)
    page.goto(app.origin + "/admin")
    page.locator("#admin-applications .summary-strip").wait_for()
    cards = page.locator(".application-card")
    page.wait_for_function(
        "() => [...document.querySelectorAll('.summary-tile strong')]"
        ".every(e=>e.textContent===e.dataset.count)"
    )
    assert sum(map(int, page.locator(".summary-tile strong").all_text_contents())) == cards.count()
    identity(page)
    page.goto(app.origin + "/demo/admin")
    expect(cards).to_have_count(4)
    expect(page.locator(".summary-tile strong")).to_have_text(["1", "1", "1", "1"])
    page.wait_for_function(
        "() => [...document.querySelectorAll('.summary-tile strong')]"
        ".every(e=>e.textContent===e.dataset.count)"
    )
    assert (
        sum(map(int, page.locator(".summary-tile strong").all_text_contents()))
        == cards.count()
        == 4
    )
    expect(page.locator(".summary-tile > span")).to_have_text(
        ["Waiting for your decision", "Approved", "Suspended", "Rejected"]
    )
    for i, actions in enumerate((["Approve", "Reject"], ["Suspend"], ["Restore access"], [])):
        expect(cards.nth(i).locator("button")).to_have_text(actions)
    # Submit both reason choices to the real transport boundary without account mutations.
    fixture = json.loads(
        (Path(__file__).parents[2] / "src/sanad/web/static/demo-admin.json").read_text()
    )
    fixture.append({**fixture[3], "id": "revoked", "status": "revoked"})
    captured: list[dict[str, Any]] = []

    def serve(route: Route) -> None:
        if route.request.method == "GET":
            route.fulfill(json=fixture)
            return
        body = route.request.post_data_json
        assert isinstance(body, dict)
        assert body["command_id"] and route.request.headers["x-csrf-token"]
        captured.append(body)
        route.fulfill(json={"status": "accepted"})

    page.route("**/api/admin/**", serve)
    page.goto(app.origin + "/admin")
    expect(cards).to_have_count(5)
    expect(cards.nth(4).locator("button")).to_have_count(0)
    page.wait_for_function(
        "() => [...document.querySelectorAll('.summary-tile strong')]"
        ".every(e=>e.textContent===e.dataset.count)"
    )
    assert sum(map(int, page.locator(".summary-tile strong").all_text_contents())) == 5
    for code, label in [
        ("unverified", "Identity could not be verified"),
        ("admin_rejected", "Declined by the administrator"),
    ]:
        cards.nth(0).get_by_role("button", name="Reject", exact=True).click()
        cards.nth(0).get_by_label(label).check()
        cards.nth(0).get_by_role("button", name="Confirm rejection").click()
        expect(page.locator("#admin-result")).to_have_text("Rejected.")
        expect(cards.nth(0).locator("form")).to_have_count(0)
        assert captured[-1]["reason_code"] == code
    dialogs: list[str] = []

    def confirm_dialog(dialog: Dialog) -> None:
        dialogs.append(dialog.message)
        dialog.accept()

    page.on("dialog", confirm_dialog)
    cards.nth(1).get_by_role("button", name="Suspend", exact=True).click()
    expect(page.locator("#admin-result")).to_have_text("Suspended.")
    assert captured[-1]["reason_code"] == "coverage"
    assert dialogs[-1] == (
        "Suspending closes this doctor's access and opens an item for you to "
        "find another doctor to take over their patients (it does not do this by itself). "
        "Restore access gives the account back."
    )
    cards.nth(2).get_by_role("button", name="Restore access", exact=True).click()
    expect(page.locator("#admin-result")).to_have_text("Access restored.")
    assert (
        dialogs[-1]
        == "Restoring access lets this doctor sign in again. Their patients' records were kept."
    )
    cards.nth(0).get_by_role("button", name="Approve", exact=True).click()
    expect(page.locator("#admin-result")).to_have_text("Approved.")
    old_word_walk(page)


def test_18g_demo_catalog(rendered: RenderedApp) -> None:
    app, page = rendered, rendered.page
    fixture_path = Path(__file__).parents[2] / "src/sanad/web/static/demo.json"
    fixture = json.loads(fixture_path.read_text())
    assert len(fixture) == 18
    assert not re.search(r"[\u0600-\u06ff]", json.dumps(fixture, ensure_ascii=False))
    page.clock.set_fixed_time(NOW)
    goto(app, "/demo")
    page.locator('#filter [data-filter="all"]').click()
    sentences = page.locator(".patient-sentence").all_text_contents()
    old_word_walk(page)
    assert len(sentences) == 18
    for patient in fixture:
        goto(app, "/demo#" + patient["patient_id"])
        sentences += page.locator("#what-to-do a").all_text_contents()
    for wording in (
        "Respond to the danger report.",
        "Read the result that arrived",
        "A document arrived",
        "The patient asked a question",
        "The patient missed",
        "Day-three check on the new medicine",
        "has not arrived.",
        "Nothing to do yet:",
        "cannot do",
        "is waiting for their answer.",
        "is proposed",
        "starts once the patient joins.",
        QUIET,
        "Reminders are paused by the patient.",
        "turned routine messages off.",
        "Sanad cannot reach this patient.",
        "Contact with this patient is frozen.",
        "Something changed since you last looked.",
        ACK,
        "is in progress; no due date was set.",
        "cannot reach the patient.",
        "Confirm it is still done.",
        "something arrived and is being checked.",
        "once the start date is known.",
        "Check-in with the patient is on",
        "Sanad asked how the new medicine is going",
        "Sanad asked the patient how they are doing",
        "The follow-up was due",
        "The follow-up cannot be sent:",
    ):
        assert any(wording in sentence for sentence in sentences), wording
    goto(app, "/demo#" + next(r["patient_id"] for r in fixture if r["held_medications"]))
    expect(page.locator("[data-held-order]")).to_contain_text(
        "Aspirin is on hold since yesterday: bruising reported."
    )
    old_word_walk(page)
    for patient in (r["patient_id"] for r in fixture):
        goto(app, "/demo#" + patient)
        for tab in ("Medicines", "Requests", "Documents", "History"):
            page.get_by_role("tab", name=tab, exact=True).click()
            old_word_walk(page)


def test_18g_urgency_uses_source_wait(rendered: RenderedApp) -> None:
    app, page = rendered, rendered.page
    page.clock.set_fixed_time(NOW)
    records = [record(app, patient_id="quiet", display_name="Quiet Patient")]
    for id, asked, deadline in [
        ("newer", PAST, "2026-09-01T00:00:00Z"),
        ("older", "2026-09-01T00:00:00Z", FUTURE),
    ]:
        records.append(
            record(
                app,
                patient_id=id,
                display_name=id.title() + " Patient",
                missions=[mission("fulfilled", id="question-" + id, created_at=asked)],
                reviews=[
                    review(
                        "question_answer",
                        id=id,
                        patient_id=id,
                        source_type="mission",
                        source_id="question-" + id,
                        review_at=deadline,
                    )
                ],
            )
        )
    records.append(
        record(
            app,
            patient_id="danger",
            display_name="Danger Patient",
            reviews=[review("incident_response", patient_id="danger")],
        )
    )
    for path, payload in [
        ("/api/patients", [{"patient_id": r["patient_id"]} for r in records]),
        *[("/api/patients/" + r["patient_id"], r) for r in records],
        *[("/api/patients/" + r["patient_id"] + "/evidence", []) for r in records],
    ]:
        page.route(app.origin + path, partial(fulfill_projection, payload))
    goto(app, "/a?filter=all")
    expect(page.locator(".patient-row .who2 b")).to_have_text(
        ["Danger Patient", "Older Patient", "Newer Patient", "Quiet Patient"]
    )
    expect(page.locator('[data-summary="pending_review"] .tile-clause')).to_have_text(
        "oldest has waited 11 days"
    )
    old_word_walk(page)
