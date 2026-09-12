import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from live.check08 import SpendGuard, SpendLimit
from live.check11d import (
    BoundedClient,
    recovered_rows,
    run_check,
    score_name_fields,
    score_names,
    within_two_edits,
)

from providers.fixtures import SOURCE, ScriptedConverse, ScriptedVision, document, response
from providers.photo11d_fixtures import CONTROL_PHRASES, paper
from providers.test_photo11d import Readers
from sanad.media.images import normalize_document
from sanad.media.limits import image_info
from sanad.media.vision import DocumentRead, VisionAdapter
from sanad.models.registry import ModelRegistry
from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT as POLICY


def test_guard_counts_concurrent_requests_and_reserves_spend_before_io() -> None:
    provider = ScriptedConverse(*(response("{}") for _ in range(12)))
    guard = BoundedClient(provider, SpendGuard(cap=0.20))
    kwargs: dict[str, Any] = {
        "modelId": ModelRegistry().vision,
        "messages": [{"content": [{"image": {"source": {"bytes": normalize_document(paper())}}}]}],
        "inferenceConfig": {"maxTokens": 2048},
    }
    for _ in range(12):
        guard.converse(**kwargs)
    with pytest.raises(ValueError, match="request_limit"):
        guard.converse(**kwargs)
    assert len(provider.calls) == 12
    cheap = BoundedClient(provider, SpendGuard(cap=0.00001))
    with pytest.raises(SpendLimit):
        cheap.converse(**kwargs)
    assert cheap.count == 0 and len(provider.calls) == 12


def test_full_pipeline_live_harness_with_scripted_rows_and_private_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANAD_LIVE", "1")
    med = [f"PrivateMedicine{i}" for i in range(3)]
    labs = [f"PrivateLab{i}" for i in range(5)]
    form = [f"PrivateFormMedicine{i}" for i in range(11)]

    def reply(names: list[str]) -> str:
        return document(
            document_type="prescription",
            printed_name=None,
            items=[{"name": n, "dose": "5 mg", "frequency": "اليوم"} for n in names],
        )

    control = document(
        printed_name=None,
        items=[{"name": f"Control{i}", "frequency": p} for i, p in enumerate(CONTROL_PHRASES)],
    )
    reader = ScriptedVision(
        reply(med + labs), reply(med + labs), reply(form), reply(form), control, control
    )
    destination = tmp_path / "live-11d-test.json"
    previous = tmp_path / "live-11d-2026-09-07.json"
    previous.write_text('{"state":"failed","attempt":2}\n')
    ground_truth = tmp_path / "private-ground-truth.json"
    ground_truth.write_text(
        json.dumps(
            {
                "handwritten_note": {
                    "medications": [n + " 5/10 mg 1x2" for n in med],
                    "labs": labs,
                    "followup": [],
                },
                "opd_form": {
                    "rows_present": 11,
                    "architect_firm_rows": {
                        str(i): n + " 10 mg" for i, n in enumerate(form[:5], 1)
                    },
                    "architect_uncertain_rows": {
                        str(i): n + "?? 5/10" for i, n in enumerate(form[5:], 6)
                    },
                },
            }
        )
    )
    monkeypatch.setattr("live.check11d.GROUND_TRUTH", ground_truth)
    result = run_check(
        destination,
        caller=reader,
        fixtures={"handwritten_note": paper(), "opd_form": paper()},
    )
    assert result["state"] == "passed" and len(reader.calls) == 6
    assert [p["medication_rows_recovered"] for p in result["papers"]] == [3, 11]
    assert all(p["invented_items"] == 0 and p["column_photo"] for p in result["papers"])
    assert all(
        p["invented_items_reaching_card"] == 0 and not p["honest_card"] for p in result["papers"]
    )
    sent = normalize_document(paper())
    info = image_info(sent)
    assert all(
        p["normalized_inputs"] == [{"width": info.width, "height": info.height, "bytes": len(sent)}]
        for p in result["papers"]
    )
    assert previous.read_text() == '{"state":"failed","attempt":2}\n'
    assert set(result["control"]["reader_scores"].values()) == {6}
    assert (
        "PrivateMedicine" not in destination.read_text() and "اليوم" not in destination.read_text()
    )
    assert "PrivateMedicine" in Path(result["private_readings_path"]).read_text()
    with pytest.raises(RuntimeError, match="already recorded"):
        run_check(destination, caller=reader)
    assert len(reader.calls) == 6


def test_missing_ground_truth_never_claims_zero_inventions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SANAD_LIVE", "1")
    monkeypatch.setattr("live.check11d.GROUND_TRUTH", tmp_path / "absent-ground-truth.json")
    reader = ScriptedVision(*(document(printed_name=None) for _ in range(6)))
    result = run_check(
        tmp_path / "live-11d-no-oracle.json",
        caller=reader,
        fixtures={"handwritten_note": paper(), "opd_form": paper()},
    )
    assert result["state"] == "failed" and len(reader.calls) == 6
    assert all(p["invented_items"] is None for p in result["papers"])
    assert result["scoring_limit"]


@pytest.mark.parametrize(
    ("name", "reference", "matches"),
    [
        ("abc", "abc", True),
        ("ab", "abc", True),
        ("abc", "axcd", True),
        ("abc", "axyz", False),
        ("", "abc", False),
        ("abcd", "ab", True),
    ],
)
def test_exact_two_edit_scoring_boundary(name: str, reference: str, matches: bool) -> None:
    assert within_two_edits(name, reference) is matches


def test_recovery_counts_each_returned_name_at_most_once() -> None:
    assert recovered_rows({"synthetic"}, [["synthetic"], ["synthetix"]]) == 1
    assert recovered_rows({"synthetic", "synthetix"}, [["synthetic"], ["synthetix"]]) == 2


def test_private_reference_scores_names_separately_from_strengths_and_annotations() -> None:
    names = [["SyntheticAlpha", "SyntheticBeta MR", "Synthetic With Words", "ImaginaryTablet"]]
    oracle = {
        "rows_present": 4,
        "architect_firm_rows": {"1": "SyntheticAlpha 5/160/12.5 1x1", "2": "SyntheticBeta MR 35"},
        "architect_uncertain_rows": {
            "3": "Synthetic With Words (hand-written brand, unclear strength)",
            "4": "?? 1g",
        },
    }
    result = score_name_fields(
        names,
        oracle,
        card_text="SyntheticAlpha / SyntheticBeta MR / Synthetic With Words / ImaginaryTablet",
    )
    assert result["medication_reference_rows_scorable"] == 3
    assert result["medication_rows_recovered"] == 3
    assert result["invented_items"] == result["invented_items_reaching_card"] == 1


def test_medication_reference_parsing_preserves_digits_within_names_and_lab_labels() -> None:
    result = score_name_fields(
        [["SyntheticB12", "5-HTSynthetic", "Synthetic 2hPP"]],
        {
            "medications": ["SyntheticB12 100 mg", "5-HTSynthetic 50 1x2"],
            "labs": ["Synthetic 2hPP"],
        },
    )
    assert result["medication_rows_recovered"] == 2 and result["lab_names_recovered"] == 1
    assert result["invented_items"] == 0


def test_scoring_counts_inventions_before_resolution_and_exposure_separately() -> None:
    reply = document(
        items=[{"name": "SyntheticMedic"}, {"name": "ImaginaryTablet"}, {"name": "ControlTest"}]
    )
    read = asyncio.run(
        VisionAdapter(ScriptedVision(reply, reply), SOURCE, POLICY).read_document(
            normalize_document(paper()), "jpeg", kind_hint="prescription"
        )
    )
    assert isinstance(read, DocumentRead)
    oracle = {"medications": ["SyntheticMedicine"], "labs": ["ControlTest"]}
    result = score_names(read, oracle, card_text="SyntheticMedic / ImaginaryTablet / ControlTest")
    assert result["medication_rows_recovered"] == 0  # three missing characters
    assert result["lab_names_recovered"] == 1
    assert result["invented_items"] == 2 and result["invented_items_reaching_card"] == 2
    result = score_names(read, oracle)
    assert result["invented_items"] == 2 and result["invented_items_reaching_card"] == 0


@pytest.mark.parametrize("single", [True, False])
def test_live_gate_counts_invented_names_even_when_disagreement_blocks_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, single: bool
) -> None:
    monkeypatch.setenv("SANAD_LIVE", "1")
    shared = {"name": "SyntheticAlpha", "dose": "5 mg"}
    fabricated = document(
        document_type="prescription",
        printed_name=None,
        items=[shared, {"name": "InventedMedicine", "dose": "10 mg"}],
    )
    honest = document(
        document_type="prescription",
        printed_name=None,
        items=[shared, {"name": "SyntheticBeta", "dose": "10 mg"}],
    )
    control = document(printed_name=None, items=[])
    reader = Readers(
        [fabricated, honest, control],
        ["bad", "bad", honest, control] if single else [honest, honest, control],
    )
    result = run_check(
        tmp_path / "live-11d-attempt3.json",
        caller=reader,
        fixtures={"handwritten_note": paper(), "opd_form": paper()},
        oracle={
            name: {"medications": ["SyntheticAlpha", "SyntheticBeta"]}
            for name in ("handwritten_note", "opd_form")
        },
    )
    note, form = result["papers"]
    assert note["invented_items"] == 1 and note["receipt_completed"]
    assert note["invented_items_reaching_card"] == (0 if single else 1)
    assert note["honest_card"] is single and note["card"] is not single
    assert note["proposed_orders"] == 0
    assert note["proposed_facts"] == (0 if single else 3)
    assert form["medication_rows_recovered"] == 2 and form["passed"]
    assert result["state"] == ("passed" if single else "failed")
