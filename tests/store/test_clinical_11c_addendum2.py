"""Term alignment, visible learning and transient recovery through real receipts."""

import copy
from typing import Any

import pytest
from harness import FakeClock, SimulatedCrash
from providers.fixtures import ScriptedModel, candidate, response
from providers.rxnorm_fixture import RxNormFixture

from sanad.scribe.card import dictation_questions, render_card
from sanad.scribe.memory import NameVocabulary, memory_rows
from sanad.scribe.patients import panel
from sanad.store._base import Check, StoreBase, Write
from sanad.store.keys import AccountScope
from store.account_fixtures import APPLICANT, update
from store.scribe_fixtures import ScribeWorld


@pytest.fixture
def world(store: StoreBase, clock: FakeClock) -> ScribeWorld:
    value = ScribeWorld.create(store, clock)
    value.approve()
    return value


def fact(spoken: str, english: str | None, kind: str = "History") -> dict[str, Any]:
    return {
        "category": "history",
        "kind": kind,
        "text": spoken,
        "terms": [{"spoken": spoken, "english": english}],
    }


def propose(
    world: ScribeWorld, facts: list[dict[str, Any]], *, source: str = "", id: int = 10
) -> Any:
    return world.dictate(
        "سامي اختبار " + (source or "، ".join(f["text"] for f in facts)),
        {"patient": {"name_as_spoken": "سامي اختبار"}, "facts": facts},
        id=id,
    )


def test_required_terms_are_aligned_ordered_and_confirmed(world: ScribeWorld) -> None:
    pairs = [
        ("ضغطه سكر", "hypertension, diabetes", "History"),
        ("تي أوف إنفرجين", "T wave inversion", "ECG"),
        ("في اللاترال", "lateral leads", "ECG"),
        ("الأكو فانكشن 45%", "EF 45% %", "Echo"),
        ("سيجمنتال إنفروبوسترو لاترال", "segmental hypokinesia inferoposterolateral", "Echo"),
        ("أنجينا", "angina", "Complaint"),
    ]
    p = propose(world, [fact(*p) for p in pairs])
    lines = render_card(p)[0].splitlines()
    assert [
        line for line in lines if line.startswith(("ECG:", "Echo:", "History:", "Complaint:"))
    ] == [f"{kind}: {en.replace('% %', '%')}" for _, en, kind in pairs]
    assert not dictation_questions(p)
    assert all(f.clinical_en is None for f in p.candidate.facts)
    world.tap()
    patient = panel(world.store, world.doctor.scope)[0]
    saved = world.store.list_records(patient.scope, "clinical_fact")[0]
    assert len(saved) == 6
    for row in saved:
        payload = row.body["payload"]
        assert isinstance(payload, dict) and payload["terms"]
    learned = memory_rows(world.store, world.doctor.scope)
    assert len(learned) == 6 and all(r.kind == "finding" for r in learned)
    assert all("45" not in str((r.latin, r.spoken_forms)) for r in learned)


@pytest.mark.parametrize(
    "invented", ["J wave", "ST depression", "LVH", "RBBB", "grade", "New disease"]
)
def test_unanchored_invented_term_never_replaces_spoken_identity(
    world: ScribeWorld,
    invented: str,
) -> None:
    f = fact("أنجينا", invented, "Complaint")
    f["clinical_en"] = invented
    f["terms"][0]["spoken"] = "عبارة مش في الكلام"
    p = propose(world, [f])
    assert "Complaint: أنجينا (؟)" in render_card(p)[0]
    assert invented not in render_card(p)[0]
    assert len(dictation_questions(p)) == 1
    world.tap()
    rows = memory_rows(world.store, world.doctor.scope)
    assert [r.latin for r in rows] == ["أنجينا"]
    assert invented not in str(rows)


@pytest.mark.parametrize(
    ("spoken", "english", "source"),
    [
        ("الأكو فانكشن 45%", "EF 99%", "الأكو فانكشن 45% عمره 99"),
        ("الأكو فانكشن 45", "EF 45%", "الأكو فانكشن 45"),
        ("أنجينا", "angina" + "x" * 121, "أنجينا"),
        ("أنجينا", "angina <script>", "أنجينا"),
    ],
)
def test_each_term_has_its_own_number_percent_and_text_gate(
    world: ScribeWorld,
    spoken: str,
    english: str,
    source: str,
) -> None:
    p = propose(world, [fact(spoken, english)], source=source)
    assert spoken + " (؟)" in render_card(p)[0]
    assert english not in render_card(p)[0]
    assert all(not n.verified for n in p.names)


def test_unmatched_overlap_reversed_terms_and_uncovered_negation(world: ScribeWorld) -> None:
    f = fact("مفيش أنجينا، في اللاترال", None, "ECG")
    f["terms"] = [
        {"spoken": "في اللاترال", "english": "lateral leads"},
        {"spoken": "أنجينا", "english": "angina"},
        {"spoken": "مش موجود", "english": "LVH"},
        {"spoken": "أنجينا", "english": "ST depression"},
    ]
    p = propose(world, [f])
    assert "ECG: مفيش (؟), angina, lateral leads" in render_card(p)[0]
    assert "LVH" not in render_card(p)[0] and "ST depression" not in render_card(p)[0]
    assert len(dictation_questions(p)) == 1


def test_normalized_substrings_keep_original_fallback_text(world: ScribeWorld) -> None:
    f = fact("أَنْجِينَا وشيء غريب", None, "Complaint")
    f["terms"] = [{"spoken": "انجينا", "english": "angina"}]
    p = propose(world, [f])
    assert "Complaint: angina, وشيء غريب (؟)" in render_card(p)[0]


def test_mixed_kinds_split_and_new_patient_marker_is_not_a_fact(world: ScribeWorld) -> None:
    f = fact("تي أوف إنفرجين، الأكو فانكشن 45%، أنجينا", None)
    f["terms"] = [
        {"spoken": "الأكو فانكشن 45%", "english": "EF 45%", "kind": "Echo"},
        {"spoken": "تي أوف إنفرجين", "english": "T wave inversion", "kind": "ECG"},
        {"spoken": "أنجينا", "english": "angina", "kind": "Complaint"},
    ]
    p = propose(world, [fact("مريض جديد", "New disease"), f])
    assert len(p.candidate.facts) == 3
    assert [
        line
        for line in render_card(p)[0].splitlines()
        if line.startswith(("ECG:", "Echo:", "Complaint:"))
    ] == [
        "ECG: T wave inversion",
        "Echo: EF 45%",
        "Complaint: angina",
    ]
    assert not dictation_questions(p)
    assert "New disease" not in render_card(p)[0]


def test_no_free_english_string_is_rendered_even_on_legacy_candidates(world: ScribeWorld) -> None:
    p = world.dictate(
        "سامي اختبار عنده دوخة وعايز يراجع العيادة",
        {
            "patient": {"name_as_spoken": "سامي اختبار"},
            "facts": [{"category": "history", "text": "دوخة", "clinical_en": "ST depression"}],
            "missions": [{"kind": "VISIT", "text": "يراجع العيادة", "clinical_en": "LVH"}],
        },
    )
    assert "ST depression" not in render_card(p)[0] and "LVH" not in render_card(p)[0]
    assert "دوخة (؟)" in render_card(p)[0] and "VISIT: يراجع العيادة" in render_card(p)[0]


def test_unchanged_fallback_teaches_only_visible_words_and_resolves_next_time(
    world: ScribeWorld,
) -> None:
    value = fact("طنين", None, "Complaint")
    first = propose(world, [value])
    assert "طنين (؟)" in render_card(first)[0]
    world.tap()
    assert [r.latin for r in memory_rows(world.store, world.doctor.scope)] == ["طنين"]
    next_card = propose(world, [value], id=30)
    assert "Complaint: طنين" in render_card(next_card)[0]
    assert not dictation_questions(next_card)
    assert "tinnitus" not in render_card(next_card)[0]
    other = world.approve("40004")
    assert not memory_rows(world.store, other.scope)
    assert NameVocabulary(world.store, other).find("طنين", "finding") is not None


@pytest.mark.parametrize("after_commit", [False, True])
def test_finding_memory_and_fact_have_one_crash_boundary(
    world: ScribeWorld,
    monkeypatch: pytest.MonkeyPatch,
    after_commit: bool,
) -> None:
    propose(world, [fact("أنجينا", "angina", "Complaint"), fact("طنين", "tinnitus")])
    original = world.store._atomic

    def crash(writes: list[Write], checks: list[Check]) -> bool:
        if any(w.item.get("entity_type") == "name_memory" for w in writes):
            if after_commit:
                assert original(writes, checks)
            raise SimulatedCrash()
        return original(writes, checks)

    monkeypatch.setattr(world.store, "_atomic", crash)
    with pytest.raises(SimulatedCrash):
        world.tap()
    assert bool(panel(world.store, world.doctor.scope)) == after_commit
    for scope in (world.doctor.scope, AccountScope(bot_id=world.doctor.telegram_bot_id)):
        assert bool(memory_rows(world.store, scope)) == after_commit


@pytest.mark.parametrize("failure", ["schema_validation", "model_unavailable"])
@pytest.mark.parametrize("succeeds", [False, True])
def test_transient_extraction_retries_identical_request_once_before_reply(
    world: ScribeWorld,
    failure: str,
    succeeds: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    script = (
        response("private malformed reply")
        if failure == "schema_validation"
        else RuntimeError("private provider exception")
    )
    valid = candidate(
        {"patient": {"name_as_spoken": "سامي اختبار"}, "facts": [fact("أنجينا", "angina")]}
    )
    model = ScriptedModel(script, valid if succeeds else copy.deepcopy(script))
    world.scribe.model_factory = lambda registry, role: model
    retry: list[str] = []
    world.scribe.observe_retry = retry.append
    world.post(update(APPLICANT, "سامي اختبار أنجينا", 10))
    assert len(model.script.calls) == 2
    assert model.script.calls[0] == model.script.calls[1]
    assert retry == [failure]
    assert bool(world.scribe.repo.pending(world.doctor.scope)) == succeeds
    assert "private malformed" not in caplog.text and "private provider" not in caplog.text


def test_retry_shares_rxnorm_budget_and_does_not_repeat_success(world: ScribeWorld) -> None:
    fixture = RxNormFixture()
    world.scribe.rxnorm_client = fixture.client
    model = ScriptedModel(
        response(calls=[("lookup_drug", {"name": "Bisoprolol"})]),
        response("private malformed"),
        response(calls=[("lookup_drug", {"name": "Amlodipine"})]),
        candidate(
            {"patient": {"name_as_spoken": "سامي اختبار"}, "facts": [fact("أنجينا", "angina")]}
        ),
    )
    world.scribe.model_factory = lambda registry, role: model
    world.post(update(APPLICANT, "سامي اختبار أنجينا", 10))
    assert len(model.script.calls) == 4 and len(fixture.calls) == 6
    assert world.proposal.rxnorm_calls == 6


@pytest.mark.parametrize(
    ("spoken", "english", "accepted"),
    [
        ("ليبوما", "lipoma", True),
        ("نوفيل", "novel", True),
        ("novelterm", "novelterp", True),
        ("novelterm", "noveltexx", True),
        ("novelterm", "novelxxxm", False),
    ],
)
def test_non_seed_phonetic_verifier_keeps_the_two_edit_bound(
    world: ScribeWorld,
    spoken: str,
    english: str,
    accepted: bool,
) -> None:
    p = propose(world, [fact(spoken, english)])
    assert p.names[0].verified == accepted
    assert (" (؟)" in render_card(p)[0]) != accepted


def test_provider_cannot_forge_verification_or_teach_a_patient_identity(world: ScribeWorld) -> None:
    f = fact("سامي اختبار", "LVH")
    f["terms"][0]["verified"] = True
    p = propose(world, [f, fact("أنجينا", "angina")])
    assert not p.names[0].verified and not p.names[0].learnable
    world.tap()
    for scope in (world.doctor.scope, AccountScope(bot_id=world.doctor.telegram_bot_id)):
        rows = memory_rows(world.store, scope)
        assert [r.latin for r in rows] == ["angina"]
        assert "سامي" not in str([r.spoken_forms for r in rows])


def test_repeated_fragments_preserve_spoken_order(world: ScribeWorld) -> None:
    f = fact("أنجينا، أنجينا", None)
    f["terms"] = [{"spoken": "أنجينا", "english": "angina"}] * 2
    p = propose(world, [f])
    assert "History: angina, angina" in render_card(p)[0]


def test_empty_model_terms_cannot_erase_source_or_produce_new_disease(world: ScribeWorld) -> None:
    f = fact("مريض جديد سامي اختبار 53 سنة ضغطه سكر", "New disease")
    f["terms"] = [{"spoken": "ضغطه سكر", "english": "hypertension, diabetes"}]
    p = propose(world, [f])
    assert "History: hypertension, diabetes" in render_card(p)[0]
    assert "New disease" not in render_card(p)[0]
    assert all("سامي" not in n.spoken for n in p.names)


def test_transient_retry_cannot_extend_worker_budget(
    world: ScribeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sanad.scribe.turn as turn

    clock = iter([0.0, 0.0, 15.0])
    monkeypatch.setattr(turn, "monotonic", lambda: next(clock))
    model = ScriptedModel(response("malformed"))
    world.scribe.model_factory = lambda registry, role: model
    retries: list[str] = []
    world.scribe.observe_retry = retries.append
    world.post(update(APPLICANT, "سامي اختبار أنجينا", 10))
    assert len(model.script.calls) == 1 and not retries
    assert world.scribe.repo.pending(world.doctor.scope) is None


def test_ef_answer_uses_aligned_terms_when_model_reorders_facts(world: ScribeWorld) -> None:
    first = propose(
        world, [fact("الأكو فانكشن 45%", "EF 45%", "Echo"), fact("أنجينا", "angina", "Complaint")]
    )
    changed = first.candidate.model_dump()
    changed["facts"] = [changed["facts"][1], fact("الأكو فانكشن 50%", "EF 50%", "Echo")]
    second = world.dictate("الأكو فانكشن 50%", changed, id=11)
    assert second.candidate.facts[0].text == "الأكو فانكشن 50%"
    assert second.candidate.facts[1] == first.candidate.facts[1]
    assert "Echo: EF 50%" in render_card(second)[0]
    assert not dictation_questions(second)


def test_english_limit_applies_to_whole_fact_not_each_pair(world: ScribeWorld) -> None:
    one, two = "a" * 61, "b" * 61
    f = fact(one + ", " + two, None)
    f["terms"] = [{"spoken": one, "english": one}, {"spoken": two, "english": two}]
    p = propose(world, [f])
    assert all(not n.verified for n in p.names)
    assert len(dictation_questions(p)) == 1


def test_arabic_digits_and_percent_preserve_fragment_number_support(world: ScribeWorld) -> None:
    p = propose(world, [fact("الأكو فانكشن ٤٥%", "EF 45%", "Echo")])
    assert "Echo: EF 45%" in render_card(p)[0]
    assert not dictation_questions(p)
