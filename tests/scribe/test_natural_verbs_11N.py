"""Citation-grounded English instructions through the real offline turn."""

import hashlib
import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from store.scribe_fixtures import ScribeWorld
from store.test_scribe_11L import confirmation

from sanad.scribe.card import dictation_questions
from sanad.scribe.extract import (
    ENGLISH_SYSTEM_PROMPT,
    DictationCandidate,
    EnglishDictationCandidate,
    EnglishOrderCandidate,
    OrderCandidate,
    candidate_issues,
    extracted_numbers,
)
from sanad.scribe.grounding import (
    Claim,
    _cited_action,
    _fingerprint,
    _instruction_span,
    deduplicate_instructions,
    ensure_evidence,
    grounded,
    invalid_claims,
    inventory,
    permits,
    valid_record,
)
from sanad.scribe.merge import merge_candidates
from sanad.scribe.proposal import Proposal
from sanad.scribe.resolver import Context
from scribe.test_grounding_invariant import world_for

# Contract addendum 1: fresh selected patient with four confirmed prior orders.
PHRASES = (
    ("put him on Exforge 5/160", "Exforge", "start", "5/160", "put him on"),
    ("let's give him Jardiance 10", "Jardiance", "start", "10", "let's give him"),
    ("I'll add Lasix 40", "Lasix", "start", "40", "I'll add"),
    ("we can hold the aspirin", "Aspirin", "stop", None, "we can hold"),
    ("take him off Nebilet", "Nebilet", "stop", None, "take him off"),
    ("he stays on Concor 5", "Concor", "continue", "5", "he stays on"),
    ("he is already taking Forxiga 10, keep it", "Forxiga", "continue", "10", "keep it"),
    ("bump the Concor up to 10", "Concor", "change", "10", "bump"),
    ("cut the Nebilet down to 2.5", "Nebilet", "change", "2.5", "cut"),
    ("Forxiga, we can keep it", "Forxiga", "continue", None, "we can keep it"),
)
NEGATIVES = (
    ("no need to put him on Coversyl", "Coversyl", "start", None, "put him on"),
    ("his mother is on Amlodipine", "Amlodipine", "continue", None, "is on"),
    ("he used to take Aspocid", "Aspocid", "continue", None, "take"),
)


def seeded_world() -> ScribeWorld:
    world = world_for("en")
    world.scribe.rxnorm_client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))
    )
    world.named_stub("Synthetic Person")
    orders = [
        {"action": "continue", "drug": drug, "dose": dose, "action_quote": "Taking"}
        for drug, dose in (("Concor", "5"), ("Nebilet", "5"), ("Aspirin", "75"), ("Forxiga", "10"))
    ]
    p = world.dictate(
        "Synthetic Person. " + ". ".join(f"Taking {o['drug']} {o['dose']}" for o in orders),
        {"patient": {"name_as_spoken": "Synthetic Person"}, "orders": orders},
        id=8,
    )
    assert not any(p.blocked(f"order:{i}") for i in range(4))
    world.tap("✅ Confirm", id=9)
    assert world.scribe.repo.pending(world.doctor.scope) is None
    return world


def dictate_selected(world: ScribeWorld, source: str, value: dict[str, Any]) -> Proposal:
    world.dictate(source, value)
    world.tap("Synthetic Person", id=11)
    return world.proposal


def value_for(row: tuple[str, str, str, str | None, str]) -> dict[str, Any]:
    _, drug, action, dose, quote = row
    return {"orders": [{"action": action, "drug": drug, "dose": dose, "action_quote": quote}]}


@pytest.mark.parametrize("row", PHRASES)
def test_natural_phrase_confirms_with_citation(row: tuple[str, str, str, str | None, str]) -> None:
    world = seeded_world()
    value = EnglishDictationCandidate.model_validate(value_for(row))
    assert isinstance(value.orders[0], EnglishOrderCandidate)
    p = dictate_selected(world, row[0], value.model_dump())
    assert len(p.candidate.orders) == 1
    order = p.candidate.orders[0]
    assert (order.drug.casefold(), order.action) == (row[1].casefold(), row[2])
    assert not p.blocked("order:0") and not p.blocked("all"), (
        p.issues,
        p.evidence,
        p.selected_patient_id,
    )
    record = next(e for e in p.evidence if e.item == "order:0" and e.field == "action")
    assert record.transformation == "instruction_clause"
    assert record.offsets[0] in grounded(row[4], p.source_text)
    assert valid_record(record, Claim("order:0", "action", row[2]), p)
    assert permits(p, "order:0", "action")
    assert _fingerprint(p) == p.evidence_fingerprint
    world.tap("✅ Confirm", id=20)
    assert world.scribe.repo.pending(world.doctor.scope) is None
    assert _fingerprint(Proposal.model_validate_json(p.model_dump_json())) == p.evidence_fingerprint


@pytest.mark.parametrize("row", NEGATIVES)
def test_negative_never_confirms(row: tuple[str, str, str, str | None, str]) -> None:
    world = seeded_world()
    p = dictate_selected(world, row[0], value_for(row))
    assert not any(e.field == "action" for e in p.evidence)
    assert not p.candidate.orders or p.blocked("order:0")
    assert p.selected_patient_id
    patient = world.claims.patient(world.doctor.id, p.selected_patient_id)
    assert patient
    before = world.store.list_records(patient.scope, "care_order_version")[0]
    confirmation(world, p)
    assert world.store.list_records(patient.scope, "care_order_version")[0] == before


@pytest.mark.parametrize(
    "source,quote",
    [
        ("let's give him Jardiance 10", "start"),
        ("Start Lasix. Give Jardiance 10", "Start"),
        ("Start Jardiance 10", "invented citation"),
    ],
)
def test_unverified_quote_has_no_regex_or_reconciliation_fallback(source: str, quote: str) -> None:
    world = seeded_world()
    p = dictate_selected(
        world,
        source,
        {"orders": [{"action": "start", "drug": "Jardiance", "dose": "10", "action_quote": quote}]},
    )
    assert not permits(p, "order:0", "action")
    assert 'I heard "Jardiance"; please clarify the action.' in dictation_questions(p)
    assert p.selected_patient_id
    patient = world.claims.patient(world.doctor.id, p.selected_patient_id)
    assert patient
    before = world.store.list_records(patient.scope, "care_order_version")[0]
    confirmation(world, p)
    assert world.store.list_records(patient.scope, "care_order_version")[0] == before


@pytest.mark.parametrize(
    "source,quote,valid",
    [
        ("He was tired. Put him on Forxiga", "Put him on", True),
        ("Forxiga, we can add it", "we can add it", True),
        ("Start Forxiga 10, actually stop Forxiga", "Start", False),
        ("Put him on Forxiga 10. Discuss Forxiga tomorrow", "Put him on", True),
        ("Put him on Forxiga. Do not start Forxiga", "Put him on", False),
        ("no need to put him on Forxiga", "no need to put him on", False),
        ("his mother is on Forxiga", "his mother is on", False),
        ("he used to take Forxiga", "he used to take", False),
        ("Forxiga is for his mother, give it", "give it", False),
    ],
)
def test_instruction_span_polarity_and_later_mentions(source: str, quote: str, valid: bool) -> None:
    order = OrderCandidate(action="start", drug="Forxiga", action_quote=quote)
    assert bool(_instruction_span(order, source, Context(), order.drug)) == valid


def test_citation_nearest_gap_tie_and_overlap() -> None:
    source = "give Forxiga give"
    order = OrderCandidate(action="start", drug="Forxiga", action_quote="give")
    assert _cited_action(order, source, (5, 12)) == ((0, 4), False)
    order = order.model_copy(update={"action_quote": "give Forxiga"})
    assert _cited_action(order, source, (5, 12)) == ((0, 12), False)


@pytest.mark.parametrize("quote", [None, "", "null", " none "])
def test_absent_citation_preserves_legacy(quote: str | None) -> None:
    order = OrderCandidate(action="start", drug="Forxiga", action_quote=quote)
    assert order.action_quote is None
    assert _instruction_span(order, "Start Forxiga", Context(), "Forxiga")


def test_quote_cap_strip_and_numeric_metadata() -> None:
    order = OrderCandidate(action="start", drug="Forxiga", action_quote="  " + "a" * 200 + "  ")
    assert len(order.action_quote or "") == 200
    with pytest.raises(ValidationError):
        OrderCandidate(action="start", drug="Forxiga", action_quote="a" * 201)
    c = DictationCandidate(orders=(order.model_copy(update={"action_quote": "give 999"}),))
    assert extracted_numbers(c) == ()
    assert not any(i.code == "unsupported_number" for i in candidate_issues(c, "Forxiga"))
    other = c.orders[0].model_copy(update={"action_quote": "give"})
    deduped = deduplicate_instructions(
        c.model_copy(update={"orders": (*c.orders, other)}), "give Forxiga", Context()
    )
    assert len(deduped.orders) == 1


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("action", ["start", "stop"])
def test_merge_carries_kept_action_citation(reverse: bool, action: str) -> None:
    left = DictationCandidate(orders=(OrderCandidate(action="start", drug="Lasix", dose="40"),))
    right = DictationCandidate.model_validate(
        {"orders": [{"action": action, "drug": "Lasix", "dose": "40", "action_quote": "give"}]}
    )
    first, second = (right, left) if reverse else (left, right)
    result = merge_candidates(first, second, "give Lasix 40")
    assert result
    assert bool(result.issues) == (action != "start")
    expected = "give" if action == "start" or reverse else None
    assert result.candidate.orders[0].action_quote == expected
    assert all(i.code == "extraction_conflict" and i.field == "action" for i in result.issues)


def test_citation_metadata_fingerprint_and_anchor_revalidation() -> None:
    world = seeded_world()
    p = dictate_selected(world, PHRASES[7][0], value_for(PHRASES[7]))
    assert p.amendments
    assert not any(c.field == "action_quote" for c in inventory(p))
    changed = p.model_copy(
        update={
            "candidate": p.candidate.model_copy(
                update={
                    "orders": (
                        p.candidate.orders[0].model_copy(update={"action_quote": "invented"}),
                    )
                }
            )
        }
    )
    assert _fingerprint(changed) == p.evidence_fingerprint
    assert not permits(changed, "order:0", "action")
    raw = json.loads(p.model_dump_json())

    def strip(value: Any) -> None:
        if isinstance(value, dict):
            value.pop("action_quote", None)
            for part in value.values():
                strip(part)
        elif isinstance(value, list):
            for part in value:
                strip(part)

    strip(raw)
    legacy = Proposal.model_validate_json(json.dumps(raw))
    assert _fingerprint(legacy) == p.evidence_fingerprint
    assert (
        _fingerprint(Proposal.model_validate_json(legacy.model_dump_json()))
        == p.evidence_fingerprint
    )


def test_prompt_preserves_other_rules() -> None:
    from sanad.scribe.extract import REMOVAL_PROMPT_EN

    assert REMOVAL_PROMPT_EN in ENGLISH_SYSTEM_PROMPT
    # Full baseline prompt hash after reversing only the released substitutions.
    new = (
        "Decide each order's action from the meaning of the sentence, whatever the wording. "
        "start: the patient begins a drug they are not on. "
        "continue: a drug the patient is already on stays as spoken. "
        "change: a current drug's dose, frequency or product changes. stop: a current drug ends. "
        "action_quote: copy verbatim, from the same sentence as the drug, the words "
        "(at most twelve) that told you the action; never paraphrase. "
        "If the sentence does not let you decide, do not guess: leave the order out and put "
        "the sentence in ambiguities. One order per drug. "
    )
    old = (
        "Express add, begin, put on, hold, discontinue, increase, decrease or switch "
        "using one of these four values. "
        "One order per drug. Taking/on means continue; add/start/I can add means start; "
        "stop means stop; increase/decrease/change the dose means change. "
    )
    assert new in ENGLISH_SYSTEM_PROMPT
    assert "Express add, begin" not in ENGLISH_SYSTEM_PROMPT
    assert "Taking/on means continue" not in ENGLISH_SYSTEM_PROMPT
    restored = (
        ENGLISH_SYSTEM_PROMPT.replace("scribe-v10", "scribe-v9", 1)
        .replace(new, old)
        .replace(REMOVAL_PROMPT_EN, "", 1)
    )
    assert (
        hashlib.sha256(restored.encode()).hexdigest()
        == "d3c0b00598efd64a2bd20ae8f58cdfb37bbea33c1a9b9e7fa6e88ed23f3975c0"
    )


# Serialized by the untouched pre-11N tree at 39d279e1312cac1ee3d87c926f46a98622776b97.
# Synthetic selected patient, confirmed Concor 5, then Increase Concor to 10.
# Frozen JSON includes amendment old/new and the original evidence fingerprint.
LEGACY_PROPOSAL_JSON = (
    r"""
{
  "id": "e5a4fb1e2f9b4535839862b9ff8926ff",
  "version": 1,
  "created_at": "2026-09-06T12:00:00Z",
  "updated_at": "2026-09-06T12:00:00Z",
  "entity_type": "scribe_proposal",
  "scope": {
    "doctor_id": "6db97509d83d4ffbb8e55c760857939f"
  },
  "doctor_id": "6db97509d83d4ffbb8e55c760857939f",
  "timezone": "Africa/Cairo",
  "language": "en",
  "selected_patient_id": "1fac8ef55a0049d7b9d708f85497c65f",
  "selected_display_name": "Synthetic Person",
  "creating_patient": false,
  "candidate": {
    "patient": {
      "name_as_spoken": "Synthetic Person",
      "identifiers": [],
      "age": null,
      "sex": null
    },
    "facts": [],
    "orders": [
      {
        "action": "change",
        "drug": "Concor",
        "name_latin": null,
        "generic": null,
        "dose": "10",
        "frequency": null,
        "route": null,
        "timing": null,
        "duration": null,
        "effective_expression": null,
        "checkin_expression": null,
        "previous_drug": null,
        "previous_dose": null
      }
    ],
    "missions": [],
    "alerts": [],
    "ambiguities": [],
    "correction_edits": []
  },
  "intent": "update_record",
  "issues": [],
  "base_versions": [
    {
      "entity_type": "patient",
      "id": "1fac8ef55a0049d7b9d708f85497c65f",
      "version": 2
    },
    {
      "entity_type": "care_order_head",
      "id": "337efe4c3e9132d11d019f2eef881a6c8f246f00f2990d9dfd9c3c9df3cab584",
      "version": 1
    },
    {
      "entity_type": "care_order",
      "id": "337efe4c3e9132d11d019f2eef881a6c8f246f00f2990d9dfd9c3c9df3cab584",
      "version": 1
    }
  ],
  "choices": [],
  "timings": [
    {
      "item": "order:0",
      "resolved": {
        "due_at": "2026-09-09T07:00:00Z",
        "due_source": "default",
        "due_reason": "MEDICATION default under policy draft-2026-09.",
        "timing_anchor": {
          "kind": "confirmation",
          "instant": "2026-09-06T12:00:00Z"
        },
        "original_time_expression": null,
        "timezone": "Africa/Cairo",
        "grace_seconds": 0,
        "escalation_at": "2026-09-09T07:00:00Z",
        "review_at": "2026-09-08T12:00:00Z",
        "policy_version": "draft-2026-09"
      }
    }
  ],
"""
    '  "source_receipt_id": "IN#telegram#235042613aefef6cb3c97f17'
    '2c2fbc6ab54035665becb2951cda80321b709261",\n'
    r"""  "source_transcript_ref": null,
  "source_text": "Synthetic Person. Increase Concor to 10.",
  "source_partition": {
    "original_end": 40,
    "corrections": [],
    "lexicon": [
      [
        "Concor",
        [
          "Concor"
        ]
      ]
    ]
  },
  "source_provenance": [
    {
"""
    '      "source_observation_id": "IN#telegram#235042613aefef6c'
    'b3c97f172c2fbc6ab54035665becb2951cda80321b709261",\n'
    r"""      "actor_kind": "doctor",
      "actor_id": "20002",
      "source_kind": "doctor_statement",
      "received_at": "2026-09-06T12:00:00Z",
      "model_id": "us.amazon.nova-lite-v1:0",
      "prompt_version": "scribe-v9",
      "extraction_version": "08-v2",
      "validation_rule_ids": []
    },
    {
"""
    '      "source_observation_id": "IN#telegram#235042613aefef6c'
    'b3c97f172c2fbc6ab54035665becb2951cda80321b709261",\n'
    r"""      "actor_kind": "doctor",
      "actor_id": "20002",
      "source_kind": "doctor_statement",
      "received_at": "2026-09-06T12:00:00Z",
      "model_id": "us.amazon.nova-lite-v1:0",
      "prompt_version": "scribe-v9",
      "extraction_version": "08-v2",
      "validation_rule_ids": []
    }
  ],
  "disputed_numbers": [],
  "heard_numbers": [],
  "expires_at": "2026-09-06T12:30:00Z",
  "confirmation_nonce_hash": "4a0bc0435af4587d6701d73009e5e5db46f3fc0ebbacdc1b09c007cfd9a8eb59",
  "status": "pending",
  "reason": null,
  "review_at": "2026-09-06T12:30:00Z",
  "work_clock": {
    "next_action_at": "2026-09-06T12:30:00Z",
    "work_lane": "scribe",
    "work_shard": "0",
    "work_generation": 1,
    "attempt_count": 0,
    "last_error_code": null
  },
  "editing": false,
  "supersedes_id": null,
  "invitation_requested": false,
  "prompt_version": "scribe-v9",
  "photo": null,
  "amendments": [
    {
      "item": "order:0",
      "old": {
        "action": "continue",
        "drug": "Concor",
        "name_latin": null,
        "generic": null,
        "dose": "5",
        "frequency": null,
        "route": null,
        "timing": null,
        "duration": null,
        "effective_expression": null,
        "checkin_expression": null,
        "previous_drug": null,
        "previous_dose": null
      },
      "new": {
        "action": "change",
        "drug": "Concor",
        "name_latin": null,
        "generic": null,
        "dose": "10",
        "frequency": null,
        "route": null,
        "timing": null,
        "duration": null,
        "effective_expression": null,
        "checkin_expression": null,
        "previous_drug": null,
        "previous_dose": null
      },
      "head_version": 1,
      "noop": false,
      "note": null
    }
  ],
  "names": [
    {
      "item": "order:0",
      "kind": "drug",
      "spoken": "Concor",
      "latin": "Concor",
      "generic": "bisoprolol",
      "strengths": [
        "10"
      ],
      "verified": true,
      "learnable": true,
      "source": "memory"
    }
  ],
  "rxnorm_calls": 0,
  "corrected": false,
  "resolved_numbers": [],
  "single_source": [],
  "pending_reply": null,
  "evidence": [
    {
      "item": "order:0",
      "field": "action",
      "origin": "transcript_span",
      "source_ref": "IN#telegram#235042613aefef6cb3c97f172c2fbc6ab54035665becb2951cda80321b709261",
      "offsets": [
        [
          18,
          26
        ],
        [
          27,
          33
        ]
      ],
      "transformation": "instruction_clause",
      "value": "change",
      "correction_version": null,
      "correction_id": null
    },
    {
      "item": "order:0",
      "field": "drug",
      "origin": "transcript_span",
      "source_ref": "IN#telegram#235042613aefef6cb3c97f172c2fbc6ab54035665becb2951cda80321b709261",
      "offsets": [
        [
          27,
          33
        ]
      ],
      "transformation": "drug_name",
      "value": "Concor",
      "correction_version": null,
      "correction_id": null
    },
    {
      "item": "order:0",
      "field": "dose",
      "origin": "transcript_span",
      "source_ref": "IN#telegram#235042613aefef6cb3c97f172c2fbc6ab54035665becb2951cda80321b709261",
      "offsets": [
        [
          27,
          33
        ],
        [
          37,
          39
        ]
      ],
      "transformation": "instruction_attachment",
      "value": "10",
      "correction_version": null,
      "correction_id": null
    },
    {
      "item": "patient",
      "field": "name_as_spoken",
      "origin": "transcript_span",
      "source_ref": "IN#telegram#235042613aefef6cb3c97f172c2fbc6ab54035665becb2951cda80321b709261",
      "offsets": [
        [
          0,
          16
        ]
      ],
      "transformation": "normalized_words",
      "value": "Synthetic Person",
      "correction_version": null,
      "correction_id": null
    },
    {
      "item": "order:0",
      "field": "name:0",
      "origin": "transcript_span",
      "source_ref": "IN#telegram#235042613aefef6cb3c97f172c2fbc6ab54035665becb2951cda80321b709261",
      "offsets": [
        [
          27,
          33
        ]
      ],
      "transformation": "name_resolver:memory",
      "value": "Concor",
      "correction_version": null,
      "correction_id": null
    },
    {
      "item": "order:0",
      "field": "prior:0:drug",
      "origin": "stored_prior_order",
"""
    '      "source_ref": "doctor:6db97509d83d4ffbb8e55c760857939f'
    ":patient:1fac8ef55a0049d7b9d708f85497c65f:order:337efe4c3e91"
    '32d11d019f2eef881a6c8f246f00f2990d9dfd9c3c9df3cab584:head:1"'
    ",\n"
    r"""      "offsets": [],
      "transformation": "scoped_order_version",
      "value": "Concor",
      "correction_version": null,
      "correction_id": null
    },
    {
      "item": "order:0",
      "field": "prior:0:dose",
      "origin": "stored_prior_order",
"""
    '      "source_ref": "doctor:6db97509d83d4ffbb8e55c760857939f'
    ":patient:1fac8ef55a0049d7b9d708f85497c65f:order:337efe4c3e91"
    '32d11d019f2eef881a6c8f246f00f2990d9dfd9c3c9df3cab584:head:1"'
    ",\n"
    r"""      "offsets": [],
      "transformation": "scoped_order_version",
      "value": "5",
      "correction_version": null,
      "correction_id": null
    },
    {
      "item": "order:0",
      "field": "deadline",
      "origin": "code_computed",
      "source_ref": "timing:order:0",
      "offsets": [],
      "transformation": "resolved_timing",
"""
    '      "value": "{\\"due_at\\":\\"2026-09-09T07:00:00Z\\",\\"due_s'
    'ource\\":\\"default\\",\\"due_reason\\":\\"MEDICATION default unde'
    'r policy draft-2026-09.\\",\\"timing_anchor\\":{\\"kind\\":\\"conf'
    'irmation\\",\\"instant\\":\\"2026-09-06T12:00:00Z\\"},\\"original_'
    'time_expression\\":null,\\"timezone\\":\\"Africa/Cairo\\",\\"grace'
    '_seconds\\":0,\\"escalation_at\\":\\"2026-09-09T07:00:00Z\\",\\"re'
    'view_at\\":\\"2026-09-08T12:00:00Z\\",\\"policy_version\\":\\"draf'
    't-2026-09\\"}",\n'
    r"""      "correction_version": null,
      "correction_id": null
    }
  ],
  "evidence_fingerprint": "e28c2e7584369c3aa6f0034e0b76223f5cb4f0e64b4e850973bd282fbbc29772"
}
"""
)


def test_pre_slice_amendment_json_revalidates_without_resealing() -> None:
    assert '"action_quote"' not in LEGACY_PROPOSAL_JSON
    legacy = Proposal.model_validate_json(LEGACY_PROPOSAL_JSON)
    assert legacy.amendments and legacy.amendments[0].old and legacy.amendments[0].new
    assert _fingerprint(legacy) == legacy.evidence_fingerprint
    assert not invalid_claims(legacy)
    assert ensure_evidence(legacy) is legacy
    raw = json.loads(LEGACY_PROPOSAL_JSON)
    for order in raw["candidate"]["orders"]:
        order["action_quote"] = None
    for amendment in raw["amendments"]:
        for name in ("old", "new"):
            if amendment[name]:
                amendment[name]["action_quote"] = None
    explicit_null = Proposal.model_validate_json(json.dumps(raw))
    assert _fingerprint(explicit_null) == legacy.evidence_fingerprint
    assert not invalid_claims(explicit_null)
    assert ensure_evidence(explicit_null) is explicit_null
