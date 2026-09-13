"""Rendered evidence, completeness and confirmation exclusion on adversarial dictations."""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from domain_fixtures import NOW
from harness import FakeClock
from providers.fixtures import ScriptedModel, candidate
from store.account_fixtures import APPLICANT, update
from store.scribe_fixtures import ScribeWorld

from sanad.domain.language import Language
from sanad.scribe.card import clinical_line, dictation_questions, medication_line, render_card
from sanad.scribe.grounding import Claim, invalid_claims, inventory, permitted_origins, valid_record
from sanad.scribe.proposal import Proposal
from sanad.store import keys
from sanad.store._base import Write
from sanad.store.memory import MemoryStore
from sanad.store.records import NameMemory, record_item, to_record
from scribe.dictations import TABLE
from scribe.english_dictations import SOURCE, VALUE

CORPUS = tuple(
    json.loads(p.read_text()) for p in sorted(Path(__file__).with_name("dictations").glob("*.json"))
)


def world_for(language: Language) -> ScribeWorld:
    clock = FakeClock(NOW)
    world = ScribeWorld.create(MemoryStore(clock=clock), clock)
    world.approve(language=language)
    return world


def dictate(world: ScribeWorld, source: str, value: dict[str, Any], id: int = 10) -> Proposal:
    # The existing omission rail may use one identical retry per reader.
    model = ScriptedModel(*(candidate(value) for _ in range(4)))
    world.scribe.model_factory = lambda *_: model
    world.post(update(APPLICANT, source, id))
    assert world.receipt(id).state == "completed"
    assert len(model.script.calls) in {2, 3, 4}
    return world.proposal


def prepare(
    row: dict[str, Any], language: Language, world: ScribeWorld | None = None
) -> tuple[ScribeWorld, Proposal]:
    world = world or world_for(language)
    if memory := row.get("memory"):
        row_memory = NameMemory(
            id=keys.digest(
                keys.partition(world.doctor.scope) + ":drug:" + memory["latin"].casefold()
            ),
            scope=world.doctor.scope,
            kind="drug",
            source="doctor_confirmation",
            latin=memory["latin"],
            generic=memory["generic"],
            spoken_forms=(memory["spoken"],),
            created_at=world.clock(),
            updated_at=world.clock(),
            last_confirmed_at=world.clock(),
        )
        assert world.store._atomic(
            [Write(record_item(to_record(row_memory, row_memory.scope)), None)], []
        )
    if prior := row.get("prior_order"):
        dictate(
            world,
            "New patient Synthetic Person. Taking Concor 5.",
            {"patient": {"name_as_spoken": "Synthetic Person"}, "orders": [prior]},
            id=8,
        )
        world.tap("✅ Confirm" if language == "en" else "✅ تمام", id=9)
    proposal = dictate(world, row["source"], row["extraction"])
    if reply := row.get("reply"):
        value = proposal.candidate.model_dump()
        value["orders"] = reply["orders"]
        proposal = dictate(world, reply["text"], value, id=11)
    return world, proposal


def fact_surface(proposal: Proposal, language: Language) -> str:
    text = "\n".join(render_card(proposal, language))
    return re.split(r"Needs confirmation:|محتاج تأكيد:", text)[0]


def assert_rendered_evidence(proposal: Proposal, fixture: str) -> None:
    """Inspect actual clinical lines, with field/fixture diagnostics, in both languages."""
    # Check the schema too: an optional new field cannot escape just because all
    # existing fixtures happen to leave it empty. Exemptions are explicit metadata.
    metadata_fields = {
        "action_quote",
        "name_latin",
        "generic",
        "clinical_en",
        "clinical_kind",
        "terms",
        "drug_mentions",
        "lab",
    }
    for family, values in (
        ("order", proposal.candidate.orders),
        ("fact", proposal.candidate.facts),
        ("mission", proposal.candidate.missions),
        ("patient", (proposal.candidate.patient,)),
    ):
        for index, value in enumerate(values):
            item = "patient" if family == "patient" else f"{family}:{index}"
            for field in type(value).model_fields:
                if field in metadata_fields:
                    continue
                field = "identifiers:0" if field == "identifiers" else field
                assert permitted_origins(Claim(item, field, "")), (
                    f"{fixture}: {item}.{field}: unregistered clinical field"
                )
    claims = inventory(proposal)
    for claim in claims:
        assert permitted_origins(claim), (
            f"{fixture}: {claim.item}.{claim.field}: unregistered clinical field"
        )
    invalid = invalid_claims(proposal)
    assert proposal.evidence_fingerprint, f"{fixture}: missing card evidence"
    for language in ("en", "ar"):
        shown = proposal.model_copy(update={"language": language})
        surface = fact_surface(shown, language)
        assert_final_surface_evidence(shown, surface, fixture)
        # Inspect the emitted clinical text too. A newly printed value with no
        # inventory field must fail, even if every older field still has evidence.
        for family, values in (
            ("order", shown.candidate.orders),
            ("fact", shown.candidate.facts),
            ("mission", shown.candidate.missions),
        ):
            for index, value in enumerate(values):
                item = f"{family}:{index}"
                rendered = (
                    medication_line(shown, index)
                    if family == "order"
                    else clinical_line(shown, item, getattr(value, "text", ""))
                )
                allowed = " ".join(c.value for c in claims if c.item == item and c not in invalid)
                # Display grammar and unit spellings are fixed code transformations.
                allowed += (
                    " start stop change continue بداية إيقاف تغيير استمرار mg مج مجم مليجرام"
                    " unverified غير متحقق Dx ECG Echo Complaint History Finding"
                )
                for change in proposal.amendments:
                    if (
                        change.item == item
                        and change.old is None
                        and change.note
                        in {"doctor instructed, not on file before", "previous dose not on file"}
                    ):
                        allowed += " " + change.note
                permitted = set(re.findall(r"\w+|[%]", allowed.casefold()))
                for token in re.findall(r"\w+|[%]", rendered.casefold()):
                    assert token in permitted, (
                        f"{fixture}/{language}: {item}.rendered: token without evidence: {token!r}"
                    )
        for claim in claims:
            emitted = False
            if claim.item.startswith("order:") and claim.field in {
                "action",
                "drug",
                "dose",
                "frequency",
                "route",
                "timing",
                "duration",
            }:
                line = medication_line(shown, int(claim.item.split(":")[1]))
                emitted = (
                    bool(line)
                    if claim.field in {"action", "drug"}
                    else bool(claim.value and claim.value in line)
                )
            elif claim.field.startswith("name:"):
                if not claim.item.startswith("order:"):
                    line = clinical_line(shown, claim.item, "")
                    emitted = bool(claim.value and claim.value in line)
            elif claim.item.startswith("fact:") and claim.field == "text":
                emitted = bool(clinical_line(shown, claim.item, claim.value))
            elif claim.item.startswith("mission:") and claim.field == "text":
                emitted = bool(clinical_line(shown, claim.item, claim.value))
            elif claim.item.startswith("alert:") or claim.item == "patient":
                emitted = claim.value in surface
            if emitted:
                assert claim not in invalid and any(
                    valid_record(e, claim, proposal) for e in proposal.evidence
                ), (
                    f"{fixture}/{language}: {claim.item}.{claim.field}: "
                    f"printed without valid evidence: {claim.value!r}"
                )


def assert_final_surface_evidence(proposal: Proposal, surface: str, fixture: str) -> None:
    """Inspect the final card, so a newly printed field cannot evade the field walk."""
    from sanad.scribe.card import arabic_datetime
    from sanad.scribe.english import date

    invalid = invalid_claims(proposal)
    allowed = " ".join(c.value for c in inventory(proposal) if c not in invalid)
    # These are fixed UI words, units and labels, not medical vocabulary.
    allowed += (
        " new patient medications requested history dx complaint finding ecg echo test task visit"
        " who is me"
        " monitor send_records age sex male female start stop change continue mg unverified"
        " confirm cancel edit valid minutes due default days if not done i will notify you at"
        " the same time unconfirmed of from reported date day three follow up runs by here does"
        " establish that treatment started readings every slot total expected extra"
        " مريض جديد الأدوية أدوية قديمة المطلوب التاريخ المرضي المريض بداية إيقاف تغيير استمرار"
        " مين بل غني"
        " مج مجم مليجرام غير متحقق ذكر أنثى سنة يوم افتراضي صريح الموعد صالح دقيقة"
        " تمام تعديل إلغاء تأكيد بداية متابعة اليوم الثالث من تاريخ البداية اللي"
        " يبل غنا بيه التأكيد هنا مش دليل إنه بدأ لو متأكدش متعملش هبل غك في نفس"
    )
    for change in proposal.amendments:
        if change.old is None and change.note in {
            "doctor instructed, not on file before",
            "previous dose not on file",
        }:
            allowed += " " + change.note
    # The code-owned deadline record authorizes its actual calendar rendering and defaults.
    for timing in proposal.timings:
        if any(c.item == timing.item and c.field == "deadline" for c in invalid):
            continue
        for instant in (timing.resolved.due_at, timing.resolved.escalation_at):
            allowed += " " + (
                date(instant, proposal)
                if proposal.language == "en"
                else arabic_datetime(instant, proposal.timezone, reference=proposal.created_at)
            )
        allowed += " " + str((timing.resolved.due_at - proposal.created_at).days)
    # Card expiry and the fixed draft-policy day intervals are code-owned UI values.
    allowed += " 1 3 7 14 30"
    tokens = set(re.findall(r"\w+|[%]", allowed.casefold()))
    for token in re.findall(r"\w+|[%]", surface.casefold()):
        assert token in tokens, (
            f"{fixture}/{proposal.language}: card.rendered: token without evidence: {token!r}"
        )


def required_in_language(text: str, language: Language) -> tuple[str, ...]:
    if language == "en":
        return (text,)
    for action, label in (("start", "بداية"), ("stop", "إيقاف"), ("change", "تغيير")):
        text = text.replace(f"({action})", f"({label})").replace(
            f"({action}; doctor instructed, not on file before)", f"({label})"
        )
    if " → " in text:
        previous, current = text.split(" → ")
        from sanad.scribe.names import split_drug_dose

        old_name, old_dose = split_drug_dose(previous)
        new_name, new_dose = split_drug_dose(current.removesuffix(" (تغيير)"))
        line = (
            old_name + ": " + old_dose + " ← " + new_dose
            if old_name == new_name
            else previous + " ← " + current.removesuffix(" (تغيير)")
        )
        return (line, current)
    if text.startswith("MONITOR:"):
        return ("MONITOR:", "15")
    return (text,)


@pytest.mark.parametrize("row", CORPUS, ids=lambda r: r["id"])
@pytest.mark.parametrize("language", ["en", "ar"])
@pytest.mark.usefixtures("legacy_dictation_schema")
def test_contrasting_corpus_evidence_completeness_and_confirmation(
    row: dict[str, Any], language: Language
) -> None:
    exercise_corpus(row, language)


def exercise_corpus(
    row: dict[str, Any], language: Language, world: ScribeWorld | None = None
) -> None:
    world, proposal = prepare(row, language, world)
    assert_rendered_evidence(proposal, row["id"])
    surface = fact_surface(proposal, language)
    questions = "\n".join(dictation_questions(proposal))
    for required in row["required"]:
        for value in required_in_language(required, language):
            assert value in "\n".join(render_card(proposal, language)), (
                f"{row['id']}: item must remain visible, including under "
                f"Needs confirmation: {value!r}"
            )
    for forbidden in row["forbidden"]:
        assert forbidden not in surface, f"{row['id']}: forbidden fact {forbidden!r}\n{surface}"
    for question in row["questions"]:
        expected_question = (
            {"dose": "جرع", "dose of Forxiga": 'جرعة "Forxiga"'}.get(question, question)
            if language == "ar"
            else question
        )
        assert expected_question in questions, (
            f"{row['id']}: missing question {expected_question!r}\n{questions}"
        )
    if row.get("blocked_all"):
        assert proposal.blocked("all")
    # Exercise the real confirmation compiler, even for a blocked card, using
    # a server-shaped callback. No Telegram transport or model provider is used.
    from sanad.scribe.proposal import ScribeCallback

    token = ScribeCallback(
        id=proposal.confirmation_nonce_hash,
        scope=proposal.scope,
        proposal_id=proposal.id,
        proposal_version=proposal.version,
        actor_subject=world.actor(APPLICANT).subject,
        action="confirm",
        expires_at=proposal.expires_at,
        created_at=world.clock(),
        updated_at=world.clock(),
    )
    from sanad.scribe.patients import panel

    prior_records = {
        (family, r.id)
        for patient in panel(world.store, world.doctor.scope)
        for family in ("care_order_version", "clinical_fact", "mission")
        for r in world.store.list_records(patient.scope, family)[0]
    }
    outcome = world.scribe.committer.confirm(
        proposal, token, world.actor(APPLICANT), "grounding-confirm"
    )
    assert outcome.status in {"accepted", "clarification"}
    if not proposal.blocked("all") and not proposal.blocked("patient"):
        assert outcome.status == "accepted", (row["id"], outcome)
    confirmed_orders = []
    for patient in panel(world.store, world.doctor.scope):
        for family in ("care_order_version", "clinical_fact", "mission"):
            records, _ = world.store.list_records(patient.scope, family)
            for record in records:
                if (family, record.id) in prior_records:
                    continue
                if family == "care_order_version":
                    instruction = record.body["structured_instruction"]
                    assert isinstance(instruction, dict)
                    confirmed_orders.append(instruction)
                body = str(
                    record.body.get(
                        "structured_instruction",
                        record.body.get("payload", record.body.get("title", "")),
                    )
                )
                if family == "care_order_version":
                    assert isinstance(instruction, dict)
                    body += (
                        " "
                        + " ".join(str(instruction.get(k) or "") for k in ("drug", "dose")).strip()
                    )
                elif family == "clinical_fact":
                    payload = record.body.get("payload", {})
                    if isinstance(payload, dict):
                        body += (
                            " "
                            + (
                                "Dx: "
                                if record.body.get("category") == "condition"
                                else "Complaint: "
                                if record.body.get("category") == "complaint"
                                else ""
                            )
                            + str(payload.get("text", ""))
                        )
                for forbidden in row["forbidden"]:
                    assert forbidden not in body, (
                        f"{row['id']}: forbidden after confirmation: {forbidden}"
                    )
    expected_orders = [
        o.model_dump(mode="json")
        for i, o in enumerate(proposal.candidate.orders)
        if not proposal.blocked("all")
        and not proposal.blocked("patient")
        and not proposal.blocked(f"order:{i}")
        and not any(a.item == f"order:{i}" and a.noop for a in proposal.amendments)
    ]

    def key(order: dict[str, Any]) -> tuple[str, str]:
        return str(order["drug"]), str(order["action"])

    assert sorted(confirmed_orders, key=key) == sorted(expected_orders, key=key), (
        row["id"],
        confirmed_orders,
        expected_orders,
    )


@pytest.mark.parametrize("example", TABLE, ids=[f"existing-{i + 1}" for i in range(len(TABLE))])
def test_existing_dictations_share_rendered_invariant(example: Any) -> None:
    world = world_for("ar")
    proposal = dictate(world, example.input, example.candidate.model_dump())
    assert_rendered_evidence(proposal, example.input[:35])


@pytest.mark.usefixtures("legacy_dictation_schema")
def test_existing_english_fixture_shares_rendered_invariant() -> None:
    world = world_for("en")
    assert_rendered_evidence(dictate(world, SOURCE, VALUE), "existing-english")


@pytest.mark.usefixtures("legacy_dictation_schema")
def test_removed_evidence_and_late_candidate_changes_are_rejected() -> None:
    _, proposal = prepare(CORPUS[0], "en")
    without = proposal.model_copy(
        update={"evidence": tuple(e for e in proposal.evidence if e.field != "dose")}
    )
    assert any(c.field == "dose" for c in invalid_claims(without))
    assert "Concor 5" not in fact_surface(without, "en")
    injected = proposal.candidate.orders[0].model_copy(update={"dose": "900"})
    changed = proposal.model_copy(
        update={
            "candidate": proposal.candidate.model_copy(
                update={"orders": (injected, *proposal.candidate.orders[1:])}
            )
        }
    )
    assert invalid_claims(changed)
    assert "900" not in fact_surface(changed, "en")


def test_corpus_is_data_and_has_eight_contrasting_pairs() -> None:
    assert len(CORPUS) >= 20
    assert all(
        {"source", "extraction", "required", "forbidden", "questions"} <= row.keys()
        for row in CORPUS
    )
    assert [row["id"][:2] for row in CORPUS[:16]] == [f"{n:02}" for n in range(1, 17)]


@pytest.mark.usefixtures("legacy_dictation_schema")
def test_invariant_catches_a_new_unguarded_rendered_field(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanad.scribe import card

    _, proposal = prepare(CORPUS[0], "en")
    real = card.medication_line
    monkeypatch.setattr(card, "medication_line", lambda p, i: real(p, i) + " invented-treatment")
    with pytest.raises(AssertionError, match="card.rendered: token without evidence: 'invented'"):
        assert_rendered_evidence(proposal, "scratch-unguarded-field")


@pytest.mark.parametrize("language", ["en", "ar"])
@pytest.mark.usefixtures("legacy_dictation_schema")
def test_question_key_keeps_independent_occurrences_and_reasons(language: Language) -> None:
    from sanad.scribe.extract import ProposalIssue
    from sanad.scribe.grounding import deduplicate_questions

    _, proposal = prepare(CORPUS[0], language)
    first = ProposalIssue(
        item="order:0",
        field="dose",
        code="clinical_unclear",
        question='I heard "5"; please clarify.',
        occurrence=(1, 2),
    )
    second = first.model_copy(update={"occurrence": (20, 21)})
    action = first.model_copy(update={"field": "action", "question": "Clarify the action."})
    issues = deduplicate_questions((first, first, second, action))
    assert issues == (first, second, action)
    shown = proposal.model_copy(update={"issues": issues})
    assert dictation_questions(shown).count(first.question) == 2
    assert len(dictation_questions(shown)) == 3


@pytest.mark.parametrize(
    "alteration",
    [
        {"source_ref": "another-doctor-receipt"},
        {"offsets": ((-1, 99999),)},
        {"origin": "vocabulary_alias"},
        {"origin": "code_computed", "transformation": "candidate_classification"},
    ],
)
@pytest.mark.usefixtures("legacy_dictation_schema")
def test_invalid_dose_evidence_is_neither_printable_nor_confirmable(
    alteration: dict[str, Any],
) -> None:
    from sanad.scribe.grounding import confirmable_evidence

    _, proposal = prepare(CORPUS[0], "en")
    changed = proposal.model_copy(
        update={
            "evidence": tuple(
                e.model_copy(update=alteration) if e.field == "dose" else e
                for e in proposal.evidence
            )
        }
    )
    assert any(c.field == "dose" for c in invalid_claims(changed))
    assert "Concor 5" not in fact_surface(changed, "en")
    assert not confirmable_evidence(changed)


@pytest.mark.usefixtures("legacy_dictation_schema")
def test_pending_dose_edit_retains_start_action_and_correction_evidence() -> None:
    world, before = prepare(CORPUS[0], "en")
    value = before.candidate.model_dump()
    value["orders"][0]["dose"] = "10"
    changed = dictate(world, "Change Concor to 10", value, id=11)
    assert not changed.blocked("order:0")
    assert changed.candidate.orders[0].action == "start"
    assert "Concor 10 (start)" in fact_surface(changed, "en")
    assert any(
        e.item == "order:0" and e.field == "dose" and e.origin == "authorized_correction"
        for e in changed.evidence
    )


@pytest.mark.parametrize(
    "prefix,action",
    [
        ("He isn't taking", "continue"),
        ("He was taking", "continue"),
        ("Don't start", "start"),
        ("The starter mentions", "start"),
    ],
)
@pytest.mark.usefixtures("legacy_dictation_schema")
def test_contractions_history_and_keyword_prefixes_do_not_authorize_an_order(
    prefix: str,
    action: str,
) -> None:
    import copy

    row = copy.deepcopy(CORPUS[0])
    row["id"] = "clause-boundary-" + prefix
    row["source"] = f"New patient Synthetic Person. {prefix} Concor 5; start Forxiga 10."
    row["extraction"]["orders"][0]["action"] = action
    row["required"] = ["Forxiga 10 (start)"]
    row["forbidden"] = ["Concor 5"]
    exercise_corpus(row, "en")


@pytest.mark.usefixtures("legacy_dictation_schema")
def test_a_new_clinical_field_requires_an_explicit_origin_rule() -> None:
    from sanad.scribe.extract import OrderCandidate
    from sanad.scribe.grounding import seal
    from sanad.scribe.resolver import Context

    class FutureOrder(OrderCandidate):
        new_clinical_field: str | None = None

    _, proposal = prepare(CORPUS[0], "en")
    original = proposal.candidate.orders[0]
    future = FutureOrder(**original.model_dump(), new_clinical_field="Concor")
    candidate_value = proposal.candidate.model_copy(
        update={"orders": (future, *proposal.candidate.orders[1:])}
    )
    changed = seal(proposal.model_copy(update={"candidate": candidate_value}), Context())
    assert any(c.field == "new_clinical_field" for c in invalid_claims(changed))
    assert changed.blocked("order:0")
    assert any(i.field == "new_clinical_field" and i.blocked for i in changed.issues)
    with pytest.raises(AssertionError, match="new_clinical_field: unregistered clinical field"):
        assert_rendered_evidence(changed, "future-field-negative-control")
    empty_future = future.model_copy(update={"new_clinical_field": None})
    empty_candidate = proposal.candidate.model_copy(
        update={"orders": (empty_future, *proposal.candidate.orders[1:])}
    )
    empty = seal(proposal.model_copy(update={"candidate": empty_candidate}), Context())
    with pytest.raises(AssertionError, match="new_clinical_field: unregistered clinical field"):
        assert_rendered_evidence(empty, "optional-future-field-negative-control")
