"""11k: independent mention roles, complete quantities and partition authority."""

import pytest

from sanad.scribe.change_binding import (
    SourcePartition,
    bind_change,
    complete_quantity,
    mentions,
    quantities,
)
from sanad.scribe.changes import previous_instruction
from sanad.scribe.extract import OrderCandidate
from sanad.scribe.names import dictionary

PAIRS = (
    ("Exforge", "Exforge HCT", True),
    ("Atacand", "Atacand Plus", False),
    ("Micardis", "Micardis Plus", False),
    ("Galvus", "Galvus Met", False),
    ("Aspirin", "Aspirin Protect", True),
    ("Ator", "Atorvastatin", True),
    ("Carvid", "Carvedilol", True),
)


@pytest.mark.parametrize("old,new,allowed", PAIRS)
@pytest.mark.parametrize("aliases", [False, True])
def test_all_prefix_pairs_have_independent_roles(
    old: str, new: str, allowed: bool, aliases: bool
) -> None:
    entries = {e.latin: e for e in dictionary()}
    old_spoken = entries[old].arabic_spellings[0] if aliases else old
    new_spoken = entries[new].arabic_spellings[0] if aliases else new
    source = f"Increase {old_spoken} 5/160 to {new_spoken} 5/160/12.5."
    order = OrderCandidate(
        action="change", drug=new, dose="5/160/12.5", previous_drug=old, previous_dose="5/160"
    )
    binding = bind_change(order, source)
    assert binding is not None
    assert source[slice(*binding.previous_name)] == old_spoken
    assert source[slice(*binding.new_name)] == new_spoken
    assert binding.previous_dose and source[slice(*binding.previous_dose)] == "5/160"
    assert binding.new_dose and source[slice(*binding.new_dose)] == "5/160/12.5"
    assert bool(previous_instruction(order, source)) == allowed
    assert len(mentions(source)) == 2


@pytest.mark.parametrize(
    "source",
    [
        "Taking Exforge 5/160. Increase to Exforge HCT 10/160/25.",
        "Taking Exforge 5/160. Request CBC. Upgrade the Exforge to Exforge HCT 10/160/25.",
        "Taking Exforge 5/160.\nRequest CBC.\nUpgrade the Exforge to be Exforge HCT 10/160/25.",
        "Switch to Exforge HCT 10/160/25 from Exforge 5/160.",
        "Increase 5/160 of Exforge to 10/160/25 of Exforge HCT.",
        "زود إكسفورج 5/160 لـ إكسفورج إتش سي تي 10/160/25.",
    ],
)
def test_permitted_cross_clause_and_reverse_bindings(source: str) -> None:
    order = OrderCandidate(
        action="change", drug="Exforge HCT", previous_drug="Exforge", previous_dose="5/160"
    )
    binding = bind_change(order, source)
    assert binding and binding.previous_dose
    assert source[slice(*binding.previous_dose)] == "5/160"
    assert previous_instruction(order, source)
    assert order.dose is None


@pytest.mark.parametrize(
    "source",
    [
        "Do not increase Exforge 5/160 to Exforge HCT 10/160/25.",
        "If needed increase Exforge 5/160 to Exforge HCT 10/160/25.",
        "Mother: increase Exforge 5/160 to Exforge HCT 10/160/25.",
        "Previously increase Exforge 5/160 to Exforge HCT 10/160/25.",
        "Taking Exforge 5/160. Taking Exforge 10/160. Increase Exforge to Exforge HCT 10/160/25.",
        "Taking Exforge 5/160. Stop Exforge. Increase Exforge to Exforge HCT 10/160/25.",
        "Taking Exforge 5/160. Stop Exforge. Increase to Exforge HCT 10/160/25.",
        "Taking Exforge 5/160. Taking Concor 5. Increase to Exforge HCT 10/160/25.",
        "Discuss Exforge 5/160 and Exforge HCT 10/160/25.",
        "Switch to Exforge HCT 5/160/12.5.",
        "Increase Exforge to Exforge HCT 5/160/12.5.",
    ],
)
def test_unproven_or_ambiguous_from_is_never_repaired_by_candidate(source: str) -> None:
    order = OrderCandidate(
        action="change", drug="Exforge HCT", previous_drug="Exforge", previous_dose="5/160"
    )
    assert previous_instruction(order, source) is None


def test_candidate_cannot_offer_target_as_previous() -> None:
    source = "Increase Exforge 5/160 to Exforge HCT 5/160/12.5."
    order = OrderCandidate(
        action="change", drug="Exforge HCT", previous_drug="Exforge HCT", previous_dose="5/160/12.5"
    )
    assert bind_change(order, source) is None


def test_same_brand_shared_name_does_not_supply_from_quantity() -> None:
    source = "Increase Concor to 10."
    order = OrderCandidate(action="change", drug="Concor")
    bound = bind_change(order, source)
    assert bound and bound.previous_name == bound.new_name
    assert bound.previous_dose is None
    assert bound.new_dose and source[slice(*bound.new_dose)] == "10"
    assert (
        bind_change(
            order.model_copy(update={"previous_drug": "Concor", "previous_dose": "10"}), source
        )
        is None
    )


@pytest.mark.parametrize("claim", ["5/160", "160/12.5", "160", "12.5", "5", "5/160/12.5"])
def test_complete_quantity_checks_both_ends(claim: str) -> None:
    source = "Exforge HCT 5/160/12.5 mg."
    start = source.index(claim)
    assert not complete_quantity(claim, source, (start, start + len(claim)))
    span = quantities(source)[0]
    assert complete_quantity("5/160/12.5 mg", source, span)


@pytest.mark.parametrize("claim", ["5", "5/5/5", "5/10", "10/5", "5/5 mg"])
def test_quantity_preserves_multiplicity_order_and_units(claim: str) -> None:
    source = "5/5"
    assert not complete_quantity(claim, source, (0, len(source)))


def test_original_multiline_is_not_a_correction_pool() -> None:
    from sanad.scribe.change_binding import CorrectionExtent

    source = "Taking Exforge 5/160.\nIncrease to Exforge HCT 10/160/25."
    order = OrderCandidate(
        action="change", drug="Exforge HCT", previous_drug="Exforge", previous_dose="5/160"
    )
    assert bind_change(order, source, SourcePartition(original_end=len(source)))
    boundary = source.index("\n")
    partition = SourcePartition(
        original_end=boundary,
        corrections=(
            CorrectionExtent(
                start=boundary + 1, end=len(source), proposal_id="p", proposal_version=1
            ),
        ),
    )
    assert bind_change(order, source, partition) is None


def test_from_correction_requires_exact_item_value_and_version() -> None:
    from sanad.scribe.change_binding import CorrectionExtent

    source = "Increase Exforge to Exforge HCT 10/160/25."
    reply = "The previous Exforge dose was 5/160."
    text = source + "\n" + reply
    order = OrderCandidate(
        action="change", drug="Exforge HCT", previous_drug="Exforge", previous_dose="5/160"
    )
    extent = CorrectionExtent(
        start=len(source) + 1,
        end=len(text),
        proposal_id="p",
        proposal_version=3,
        answers=(("order:0", reply),),
    )
    partition = SourcePartition(original_end=len(source), corrections=(extent,))
    bound = bind_change(order, text, partition, item="order:0")
    assert bound and bound.correction_id == "p" and bound.correction_version == 3
    assert bound.previous_dose and text[slice(*bound.previous_dose)] == "5/160"
    assert bound.previous_dose[0] >= extent.start
    assert bind_change(order, text, partition, item="order:1") is None
    assert (
        bind_change(
            order.model_copy(update={"previous_dose": "10/160/25"}), text, partition, item="order:0"
        )
        is None
    )


def test_alias_collision_is_decided_before_candidate_matching() -> None:
    source = "Increase shared 5/160 to Exforge HCT 10/160/25."
    partition = SourcePartition(
        original_end=len(source), lexicon=(("Exforge", ("shared",)), ("Concor", ("shared",)))
    )
    order = OrderCandidate(
        action="change", drug="Exforge HCT", previous_drug="Exforge", previous_dose="5/160"
    )
    assert bind_change(order, source, partition) is None


@pytest.mark.parametrize(
    "source",
    [
        "Taking Exforge 5/160. Taking Exforge 5/160. Upgrade Exforge to Exforge HCT.",
        "Taking Exforge 5/160. Upgrade Exforge 5/160 to Exforge HCT.",
    ],
)
def test_equal_suppliers_choose_local_then_latest_only_after_agreement(source: str) -> None:
    order = OrderCandidate(
        action="change", drug="Exforge HCT", previous_drug="Exforge", previous_dose="5/160"
    )
    binding = bind_change(order, source)
    assert binding and binding.previous_dose
    assert binding.previous_name[0] == source.rindex("Exforge 5/160")
    assert binding.new_dose is None


@pytest.mark.parametrize(
    "field,statement,prior",
    [
        ("frequency", "Change Concor to twice daily", "daily"),
        ("timing", "Change Concor to the morning", "at night"),
        ("duration", "Change Concor duration to 10 days", "5 days"),
        ("route", "Change Concor route to intravenous", "oral"),
    ],
)
def test_explicit_field_change_cannot_inherit_previous_value(
    field: str, statement: str, prior: str
) -> None:
    from sanad.scribe.change_binding import untouched

    assert not untouched(
        OrderCandidate(action="change", drug="Concor"), field, statement, prior_value=prior
    )


def test_other_drug_change_does_not_touch_continuation_dose() -> None:
    from sanad.scribe.change_binding import untouched

    assert untouched(
        OrderCandidate(action="continue", drug="Forxiga"),
        "dose",
        "Increase Concor to 10 and continue Forxiga",
        prior_value="5",
    )


@pytest.mark.parametrize("raw", ["560 12.5", "560, 12.5", "560.0/12.50", "560 over 12.5"])
def test_complete_raw_observations_preserve_components(raw: str) -> None:
    source = "Exforge HCT " + raw
    span = (len("Exforge HCT "), len(source))
    assert complete_quantity("560/12.5", source, span)
    assert not complete_quantity("560", source, (span[0], span[0] + 3))
    assert not complete_quantity("12.5", source, (len(source) - 4, len(source)))


@pytest.mark.parametrize(
    "field,reply", [("dose", "10"), ("dose", "not sure"), ("frequency", "twice daily")]
)
def test_subjectless_authorized_answer_cannot_inherit_changed_field(field: str, reply: str) -> None:
    from sanad.scribe.change_binding import CorrectionExtent, untouched

    original = "Continue Concor."
    source = original + "\n" + reply
    partition = SourcePartition(
        original_end=len(original),
        corrections=(
            CorrectionExtent(
                start=len(original) + 1,
                end=len(source),
                proposal_id="p",
                proposal_version=1,
                answers=(("order:0", reply),),
            ),
        ),
    )
    assert not untouched(
        OrderCandidate(action="continue", drug="Concor"),
        field,
        source,
        partition,
        item="order:0",
        prior_value="5" if field == "dose" else "once daily",
    )


def test_ambiguous_earlier_current_identity_cannot_be_ignored_for_omitted_from() -> None:
    source = "Taking shared 5. Taking Exforge 5/160. Increase to Exforge HCT 10/160/25."
    partition = SourcePartition(
        original_end=len(source), lexicon=(("Forxiga", ("shared",)), ("Concor", ("shared",)))
    )
    order = OrderCandidate(
        action="change", drug="Exforge HCT", previous_drug="Exforge", previous_dose="5/160"
    )
    assert bind_change(order, source, partition) is None


def test_indexed_inventory_matches_unfiltered_alias_grounding() -> None:
    from sanad.scribe.grounding import grounded
    from sanad.scribe.names import normalize

    aliases = [
        alias
        for entry in dictionary()
        if entry.kind == "drug"
        for alias in (entry.latin, *entry.arabic_spellings, *entry.latin_spellings)
    ]
    source = ". ".join(aliases) + ". والكونكور وإكسفورج وكارفيديلول."
    found: dict[tuple[int, int], set[str]] = {}
    for entry in dictionary():
        if entry.kind == "drug":
            for alias in (entry.latin, *entry.arabic_spellings, *entry.latin_spellings):
                for span in grounded(alias, source):
                    found.setdefault(span, set()).add(entry.latin)
    maximal = {s for s in found if not any(t != s and t[0] <= s[0] and s[1] <= t[1] for t in found)}
    actual = mentions(source)
    assert {m.span for m in actual} == maximal
    assert all(m.ambiguous == (len({normalize(n) for n in found[m.span]}) != 1) for m in actual)
