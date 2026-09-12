"""Walkthrough regressions: observed instructions still require doctor confirmation."""

from pathlib import Path

import pytest
from store.account_fixtures import APPLICANT, update
from store.photo_fixtures import photo, providers
from store.test_scribe_11L import confirmation

from sanad.scribe.card import render_card
from sanad.scribe.extract import DictationCandidate, OrderCandidate
from sanad.scribe.grounding import invalid_claims
from sanad.scribe.merge import merge_candidates
from sanad.scribe.monitoring import compile_schedule
from scribe.test_grounding_invariant import dictate, world_for


@pytest.mark.parametrize(
    "frequency,count",
    [
        ("once a day", 1),
        ("twice a day", 2),
        ("thrice a day", 3),
        ("every morning and evening", 2),
        ("2 times daily", 2),
        ("مرة في اليوم", 1),
        ("مرتين يوميا", 2),
        ("تلات مرات في اليوم", 3),
    ],
)
@pytest.mark.parametrize("metric", ["pressure", "bp", "sugar", "glucose", "ضغطه"])
def test_frequency_and_metric(metric: str, frequency: str, count: int) -> None:
    schedule = compile_schedule(f"{metric} {frequency} for three days")
    assert schedule and schedule.times_per_day == count and schedule.days == 3
    assert schedule.metric == (
        "blood glucose" if metric in {"sugar", "glucose"} else "blood pressure"
    )


@pytest.mark.parametrize(
    "text",
    [
        "check pressure 1-2 times a day for three days",
        "check pressure about twice a day for three days",
        "check pressure twice a day or thrice a day for three days",
        "check pressure twice a day and once a day for three days",
        "check pressure twice a day for 2-3 days",
        "check pressure twice a day for about three days",
    ],
)
def test_ambiguous_monitoring_refused(text: str) -> None:
    assert compile_schedule(text) is None


@pytest.mark.parametrize("verb", ["check", "watch", "follow", "track", "keep an eye on"])
def test_monitor_omission_is_grounded_and_confirms(verb: str) -> None:
    world = world_for("en")
    patient = world.named_stub("Synthetic Person")
    source = f"Synthetic Person and {verb} his pressure twice a day for three days"
    p = dictate(world, source, {"patient": {"name_as_spoken": "Synthetic Person"}})
    assert len(p.candidate.missions) == 1
    mission = p.candidate.missions[0]
    assert mission.kind == "MONITOR" and mission.text == source.split(" and ")[1]
    assert mission.timing_expression == "for three days"
    assert not invalid_claims(p)
    assert not any(i.code in {"request_missing", "unassigned_number"} for i in p.issues)
    assert "which item" not in "\n".join(render_card(p))
    assert not world.store.list_records(patient.scope, "mission")[0]
    assert confirmation(world, p) == "accepted"
    saved = world.store.list_records(patient.scope, "mission")[0]
    assert len(saved) == 1
    assert saved[0].body["kind"] == "MONITOR"
    details = saved[0].body["details"]
    assert isinstance(details, dict) and isinstance(details["slots"], list)
    assert len(details["slots"]) == 6


@pytest.mark.parametrize(
    "placeholder", [None, "", "غير مذكور", "not supplied", "none", "n/a", "unknown"]
)
@pytest.mark.parametrize("reverse", [False, True])
def test_present_grounded_forxiga_timing_wins(placeholder: str | None, reverse: bool) -> None:
    a = DictationCandidate(
        orders=(OrderCandidate(action="start", drug="Forxiga", dose="10", timing=placeholder),)
    )
    b = DictationCandidate(
        orders=(OrderCandidate(action="start", drug="Forxiga", dose="10", timing="in the morning"),)
    )
    result = merge_candidates(*((b, a) if reverse else (a, b)), "Start Forxiga 10 in the morning")
    assert result and not result.issues
    assert result.candidate.orders[0].timing == "in the morning"


def test_qr_guess_requires_selection_and_exact_name_issues() -> None:
    world = world_for("en")
    world.named_stub("Ahmed Test")
    world.post(update(APPLICANT, "/qr ahmed nest", 10))
    p = world.proposal
    assert p.selected_patient_id is None
    assert "Did you mean Ahmed Test?" in "\n".join(render_card(p))
    assert not any(c.template_id == "scribe_invitation" for c in world.intents())
    world.tap("Ahmed Test", id=11)
    assert world.receipt(11).state == "completed"
    assert any(c.template_id == "scribe_invitation" for c in world.intents())


def test_two_candidate_headlines_are_scoped() -> None:
    world = world_for("en")
    world.named_stub("Ahmed Test")
    world.named_stub("Ahmed Other")
    world.post(update(APPLICANT, "/find Ahmed", 10))
    p = world.proposal
    assert len(p.choices) == 2
    assert all(c.headline == "no plan yet" for c in p.choices)
    assert "\n".join(render_card(p)).count("no plan yet") == 2


@pytest.mark.parametrize(
    "phrase,action,dose,quote",
    [
        ("we can hold the aspirin for now", "stop", None, "we can hold"),
        ("stop Aspirin", "stop", None, "stop"),
        ("change Aspirin to 100 mg", "change", "100 mg", "change"),
    ],
)
def test_home_instruction_is_history_until_confirm(
    phrase: str, action: str, dose: str | None, quote: str
) -> None:
    world = world_for("en")
    patient = world.named_stub("Synthetic Person")
    p = dictate(
        world,
        "Synthetic Person. " + phrase,
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [{"action": action, "drug": "Aspirin", "dose": dose, "action_quote": quote}],
        },
    )
    assert not any(i.code == "order_missing" for i in p.issues)
    assert not invalid_claims(p)
    assert p.amendments[0].old is None
    assert p.candidate.orders[0].action_quote == quote
    assert p.candidate.orders[0].action == ("start" if action == "change" else "stop")
    history = p.candidate.facts[0]
    assert history.category == "medication_history"
    assert "no prior order on file; dose unknown" in history.text
    assert (
        "doctor instructed hold" if "hold" in phrase else "doctor instructed " + action
    ) in history.text
    card = "\n".join(render_card(p))
    assert "was taking" not in card and "no prior order on file" in card
    assert not world.store.list_records(patient.scope, "clinical_fact")[0]
    assert not world.store.list_records(patient.scope, "care_order_head")[0]
    assert confirmation(world, p) == "accepted"
    fact = world.store.list_records(patient.scope, "clinical_fact")[0][0]
    provenance = fact.body["provenance"]
    assert isinstance(provenance, dict)
    assert provenance["source_observation_id"] == p.source_receipt_id
    order = world.store.list_records(patient.scope, "care_order_head")[0][0]
    assert order.body["status"] == ("active" if action == "change" else "stopped")


@pytest.mark.parametrize("dose", [None, "100 mg"])
def test_home_change_without_spoken_new_dose_stays_blocked(dose: str | None) -> None:
    world = world_for("en")
    world.named_stub("Synthetic Person")
    p = dictate(
        world,
        "Synthetic Person. change Aspirin",
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
            "orders": [
                {"action": "change", "drug": "Aspirin", "dose": dose, "action_quote": "change"}
            ],
        },
    )
    assert p.blocked("order:0")
    assert not any(i.code == "order_missing" for i in p.issues)


@pytest.mark.parametrize("counterpart", ["unreadable", "absent", "mixed"])
def test_exact_seven_row_prescription_pair(counterpart: str) -> None:
    from providers.fixtures import document

    world = world_for("en")
    patient = world.named_stub("Synthetic Person")
    names = ("Bisoprolol", "Atorvastatin", "Apixaban", "Spironolactone")
    doses = ("5 mg tab, 1 tab", "40 mg tab, 1 tab", "5 mg tab, 1 tab", "25 mg, 1 tab")
    frequencies = ("once daily", "at night", "twice daily", "daily")
    first = document(
        document_type="prescription",
        printed_name=None,
        items=[
            *[
                {
                    "name": name,
                    "dose": dose,
                    "frequency": frequency,
                    "timing": frequency if frequency == "at night" else None,
                }
                for name, dose, frequency in zip(names, doses, frequencies, strict=True)
            ],
            *[{"name": name} for name in ("echo", "K", "creatinine")],
        ],
    )
    second = document(
        document_type="prescription",
        printed_name=None,
        items=[
            *[
                {"name": name, "dose": dose.split(" tab")[0].split(",")[0]}
                for name, dose in zip(names, doses, strict=True)
            ],
            *[
                {"name": "[unreadable]"}
                for _ in range(0 if counterpart == "absent" else 1 if counterpart == "mixed" else 3)
            ],
        ],
    )
    providers(world, first, second, data=Path("tests/data/09b/rx_synthetic.png").read_bytes())
    world.post(photo("Synthetic Person"))
    p = world.proposal
    assert len(p.candidate.orders) == 4
    assert len(p.candidate.missions) == 3
    assert {m.text for m in p.candidate.missions} == {"echo", "K", "creatinine"}
    assert all(m.kind == "TEST" for m in p.candidate.missions)
    assert not p.issues
    assert p.photo and not p.photo.reads.disagreements
    assert "items.0.dose" in p.photo.resolved_fields
    assert p.candidate.orders[0].dose == doses[0]
    assert p.candidate.orders[0].frequency == frequencies[0]
    card = "\n".join(render_card(p))
    assert card.count("(one reader)") == 3
    assert card.count("The day-three check-in") == 1
    assert "Requested:" in card and "Two different readings" not in card
    assert "at night, at night" not in card
    assert "Please clarify" not in card
    assert not world.store.list_records(patient.scope, "care_order_head")[0]
    world.tap("✅ Confirm")
    assert len(world.store.list_records(patient.scope, "care_order_head")[0]) == 4
    assert len(world.store.list_records(patient.scope, "mission")[0]) == 7


def test_qr_exact_name_can_issue_without_guess() -> None:
    world = world_for("en")
    world.named_stub("Ahmed Test")
    world.post(update(APPLICANT, "/qr AHMED TEST", 10))
    assert world.scribe.repo.pending(world.doctor.scope) is None
    assert sum(i.template_id == "scribe_invitation" for i in world.intents()) == 1


def test_headline_counts_current_plan_and_preserves_scope() -> None:
    from sanad.scribe.extract import PatientCandidate
    from sanad.scribe.patients import lookup

    world = world_for("en")
    world.named_stub("Ahmed Test")
    p = dictate(
        world,
        "Ahmed Test. Start Aspirin 75 mg. Request ECG",
        {
            "patient": {"name_as_spoken": "Ahmed Test"},
            "orders": [
                {"action": "start", "action_quote": "Start", "drug": "Aspirin", "dose": "75 mg"}
            ],
            "missions": [{"kind": "TEST", "text": "ECG"}],
        },
    )
    assert confirmation(world, p) == "accepted"
    world.named_stub("Ahmed Other")
    choices = lookup(world.store, world.doctor.scope, PatientCandidate(name_as_spoken="Ahmed"))
    assert {c.display_name: c.headline for c in choices} == {
        "Ahmed Test": "1 active medications, 2 open requests",
        "Ahmed Other": "no plan yet",
    }


@pytest.mark.parametrize(
    "name", ["echo", "ECG", "X-ray", "ultrasound", "CT", "MRI", "Holter", "K", "creatinine"]
)
def test_recognized_test_never_becomes_medication(name: str) -> None:
    from sanad.media.vision import DocumentItem
    from sanad.scribe.crosscheck import prescription_projection

    candidate, targets, unknown = prescription_projection((DocumentItem(name=name),))
    assert not candidate.orders and not unknown
    assert targets == ("mission:0",) and candidate.missions[0].kind == "TEST"


def test_unrecognized_photo_row_is_unresolved_not_a_start() -> None:
    from sanad.media.vision import DocumentItem
    from sanad.scribe.crosscheck import prescription_projection

    candidate, targets, unknown = prescription_projection(
        (DocumentItem(name="InventedDrug", dose="10 mg"),)
    )
    assert not candidate.orders and not candidate.missions
    assert targets == ("fact:0",) and unknown == (0,)


def test_reordered_reader_uses_name_alignment_and_keeps_dose_conflict() -> None:
    import asyncio

    from providers.fixtures import SOURCE, ScriptedVision, document, png

    from sanad.media.vision import DocumentRead, VisionAdapter
    from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT
    from sanad.scribe.crosscheck import projected_items

    first = document(
        document_type="prescription",
        items=[
            {"name": "Bisoprolol", "dose": "5 mg tab, 1 tab", "frequency": "daily"},
            {"name": "Atorvastatin", "dose": "40 mg"},
            {"name": "echo"},
        ],
    )
    second = document(
        document_type="prescription",
        items=[
            {"name": "Atorvastatin", "dose": "20 mg"},
            {"name": "[unreadable]"},
            {"name": "Bisoprolol", "dose": "5 mg"},
        ],
    )
    read = asyncio.run(
        VisionAdapter(
            ScriptedVision(first, second), SOURCE, SAFETY_POLICY_V1_CARDIOLOGY_DRAFT
        ).read_document(png(), "png", kind_hint="prescription")
    )
    assert isinstance(read, DocumentRead)
    assert [(d.field, d.first, d.second) for d in read.disagreements] == [
        ("items.1.dose", "40 mg", "20 mg")
    ]
    rows, fields, single = projected_items(read)
    assert rows[0].dose == "5 mg tab, 1 tab" and rows[0].frequency == "daily"
    assert single == (2,) and "items.0.dose" in fields


@pytest.mark.parametrize(
    "field,first_value,second_value",
    [
        ("quantity", "5 mg tab, 1 tab", "5 mg tab, 2 tab"),
        ("frequency", "daily", "twice daily"),
    ],
)
def test_equal_strength_retains_other_conflicting_fields(
    field: str, first_value: str, second_value: str
) -> None:
    import asyncio

    from providers.fixtures import SOURCE, ScriptedVision, document, png

    from sanad.media.vision import DocumentRead, VisionAdapter
    from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT

    key = "dose" if field == "quantity" else field
    payloads = [
        document(
            document_type="prescription", items=[{"name": "Bisoprolol", "dose": "5 mg", key: value}]
        )
        for value in (first_value, second_value)
    ]
    read = asyncio.run(
        VisionAdapter(
            ScriptedVision(*payloads), SOURCE, SAFETY_POLICY_V1_CARDIOLOGY_DRAFT
        ).read_document(png(), "png", kind_hint="prescription")
    )
    assert isinstance(read, DocumentRead)
    assert [d.field for d in read.disagreements] == ["items.0." + field]


def test_mixed_photo_callback_edits_original_row_not_order_index() -> None:
    from providers.fixtures import document

    world = world_for("en")
    world.named_stub("Synthetic Person")
    providers(
        world,
        *[
            document(
                document_type="prescription",
                items=[
                    {"name": "echo"},
                    {"name": "Concor", "dose": dose},
                ],
            )
            for dose in ("5 mg", "10 mg")
        ],
    )
    world.post(photo("Synthetic Person"))
    before = world.proposal
    assert before.photo and before.photo.row_targets == ("mission:0", "order:0")
    assert before.blocked("order:0") and not before.blocked("mission:0")
    world.tap("Reading 2: 10 mg")
    after = world.proposal
    assert after.candidate.orders[0].dose == "10 mg"
    assert after.candidate.missions[0] == before.candidate.missions[0]
    assert not after.blocked("order:0")
    assert after.photo and after.photo.reads == before.photo.reads


def test_mixed_photo_correction_keeps_source_rows_for_later_callback() -> None:
    from providers.fixtures import document

    world = world_for("en")
    world.named_stub("Synthetic Person")
    read = document(
        document_type="prescription",
        items=[{"name": "echo"}, {"name": "Concor", "dose": "5 mg"}],
    )
    providers(world, read, read)
    world.post(photo("Synthetic Person"))
    before = world.proposal
    changed = before.candidate.model_copy(
        update={"orders": (OrderCandidate(action="start", drug="Bisoprolol", dose="10 mg"),)}
    )
    review = world.scribe.photos.corrected_review(before, changed, "Bisoprolol 10 mg")
    assert review.edited_rows == (1,)
    assert {"items.1.name", "items.1.dose"} <= set(review.resolved_fields)
    later, review = world.scribe.photos.set_field(changed, review, 1, "frequency", "daily")
    assert later.orders[0].drug == "Bisoprolol" and later.orders[0].dose == "10 mg"
    assert later.orders[0].frequency == "daily"
    assert later.missions == before.candidate.missions
    assert before.photo and review.reads == before.photo.reads


@pytest.mark.parametrize("verb,action", [("hold", "stop"), ("stop", "stop"), ("change", "change")])
def test_legacy_home_instruction_copies_literal_action_quote(verb: str, action: str) -> None:
    from sanad.scribe.amend import prepare

    world = world_for("en")
    patient = world.named_stub("Synthetic Person")
    source = f"{verb} Aspirin" + (" to 100 mg" if action == "change" else "")
    candidate = DictationCandidate(
        orders=(OrderCandidate.model_validate({"action": action, "drug": "Aspirin"}),)
    )
    result, changes, issues = prepare(world.scribe.repo, patient.scope, candidate, source=source)
    assert not issues and changes[0].old is None
    assert result.orders[0].action_quote == verb
    assert f"doctor instructed {verb}" in result.facts[0].text


@pytest.mark.parametrize(
    "first,second",
    [
        ("5/160/12.5 mg", "10/160/12.5 mg"),
        ("5-10 mg", "10 mg"),
        ("5 mg", "50 mg"),
        ("5 mg/ml", "5 mg"),
        ("-5 mg", "5 mg"),
        ("5e2 mg", "2 mg"),
        ("5 mg, 1/2 tab", "5 mg, 2 tab"),
        ("5 mg, one tablet", "5 mg, two tablets"),
    ],
)
def test_strength_normalization_cannot_hide_compound_or_range(first: str, second: str) -> None:
    from sanad.media.vision import compatible_dose

    assert not compatible_dose(first, second)


def test_unmatched_row_is_single_reader_and_keeps_original_reads() -> None:
    from providers.fixtures import document

    world = world_for("en")
    world.named_stub("Synthetic Person")
    shared = {"name": "Concor", "dose": "5 mg"}
    providers(
        world,
        document(document_type="prescription", items=[shared]),
        document(
            document_type="prescription", items=[shared, {"name": "Aspirin", "dose": "75 mg"}]
        ),
    )
    world.post(photo("Synthetic Person"))
    before = world.proposal
    assert before.photo and before.photo.row_targets == ("order:0", "order:1")
    assert before.photo.single_rows == (1,)
    assert len(before.candidate.orders) == 2 and not before.candidate.facts
    assert before.candidate.orders[1].drug == "Aspirin"
    assert before.candidate.orders[1].dose == "75 mg"
    assert not before.blocked("order:1")
    original = before.photo.reads
    world.tap("✅ Confirm", id=21)
    saved = world.scribe.repo.load(before.scope, "scribe_proposal", before.id, type(before))
    assert saved and saved.photo and saved.photo.reads == original


def test_monitoring_claim_owns_duration_number_but_not_unrelated_number() -> None:
    world = world_for("en")
    world.named_stub("Synthetic Person")
    p = dictate(
        world,
        "Synthetic Person. check his pressure twice a day for 3 days. 9",
        {
            "patient": {"name_as_spoken": "Synthetic Person"},
        },
    )
    assert len(p.candidate.missions) == 1
    unassigned = {
        n for issue in p.issues if issue.code == "unassigned_number" for n in issue.numbers
    }
    assert unassigned == {"9"}
