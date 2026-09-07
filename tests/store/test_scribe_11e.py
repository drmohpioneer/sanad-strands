"""Two-run persistence, independent recovery and doctor/clinic scope on both stores."""

import asyncio
from typing import Any

import pytest
from harness import FakeClock
from providers.fixtures import SOURCE, ScriptedModel, candidate, response

from sanad.agents.factory import Proposal as ModelProposal
from sanad.domain import CandidateRef, TenantScope
from sanad.scribe.card import dictation_questions, render_card
from sanad.scribe.memory import NameVocabulary
from sanad.scribe.names import normalize
from sanad.scribe.proposal import Proposal
from sanad.scribe.resolver import Context, NameKind, hint_names, resolve_name
from sanad.store import keys
from sanad.store._base import StoreBase, Write
from sanad.store.keys import AccountScope
from sanad.store.records import NameMemory, from_record, record_item, to_record
from store.account_fixtures import APPLICANT, update
from store.scribe_fixtures import ScribeWorld


@pytest.fixture
def world(store: StoreBase, clock: FakeClock) -> ScribeWorld:
    world = ScribeWorld.create(store, clock)
    world.approve()
    return world


VALUE: dict[str, Any] = {
    "patient": {"name_as_spoken": "سامي اختبار"},
    "orders": [{"action": "start", "drug": "كونكور", "dose": "5"}],
}


@pytest.mark.parametrize("failed", [0, 1, 2])
@pytest.mark.parametrize("failure", ["crash", "schema"])
def test_each_reading_retries_once_and_survives_peer_failure(
    world: ScribeWorld, failed: int, failure: str
) -> None:
    scripts = [
        RuntimeError("private failure") if failure == "crash" else response("invalid json")
        for _ in range(4)
    ]
    if failed < 2:
        scripts[1] = candidate(VALUE)
    if failed == 0:
        scripts[0] = candidate(VALUE)
    models: list[ScriptedModel] = []

    def factory(*args: Any) -> ScriptedModel:
        model = ScriptedModel(scripts[len(models)])
        models.append(model)
        return model

    world.scribe.model_factory = factory
    world.post(update(APPLICANT, "سامي اختبار ابدأ كونكور 5", 10))
    assert world.receipt(10).state == "completed"
    assert len(models) == 2 + failed
    assert all(len(m.script.calls) == 1 for m in models)
    assert all(m.script.calls[0] == models[0].script.calls[0] for m in models)
    p = world.scribe.repo.pending(world.doctor.scope)
    if failed == 2:
        assert p is None
        assert any(i.template_id == "doctor_model_unavailable" for i in world.cards())
    else:
        assert p and p.single_source == (("order:0",) if failed else ())
        assert not dictation_questions(p)
        assert "سمعتها مرة واحدة" not in render_card(p)[0]


def test_single_source_question_persists_and_conflict_blocks_only_its_item(
    world: ScribeWorld,
) -> None:
    left = {"patient": VALUE["patient"], "orders": [{"action": "start", "drug": "فورسيجا"}]}
    right = {"patient": VALUE["patient"]}
    model = ScriptedModel(candidate(left), candidate(right))
    world.scribe.model_factory = lambda *args: model
    world.post(update(APPLICANT, "سامي اختبار ابدأ فورسيجا", 10))
    p = world.proposal
    assert p.single_source == ("order:0",)
    assert dictation_questions(p) == ('جرعة "Forxiga" إيه؟ سمعتها مرة واحدة',)
    row = world.store.get(p.scope, "scribe_proposal", p.id)
    assert row and render_card(from_record(row, Proposal)) == render_card(p)
    corrected = p.candidate.model_dump()
    corrected["orders"][0]["dose"] = "10"
    next_card = world.dictate("فورسيجا 10", corrected, id=11)
    assert not next_card.single_source and not dictation_questions(next_card)
    assert next_card.id == p.id and next_card.expires_at == p.expires_at
    assert "عدّلت الكارت حسب كلامك" in render_card(next_card)[0]


def test_unanswered_merge_conflict_survives_reload_and_unrelated_correction(
    world: ScribeWorld,
) -> None:
    second = {
        "patient": VALUE["patient"],
        "orders": [{"action": "start", "drug": "كونكور", "dose": "10"}],
    }
    model = ScriptedModel(candidate(VALUE), candidate(second))
    world.scribe.model_factory = lambda *args: model
    world.post(update(APPLICANT, "سامي اختبار ابدأ كونكور 5 أو 10", 10))
    p = world.proposal
    assert p.blocked("order:0")
    assert len([q for q in p.issues if q.code == "extraction_conflict"]) == 1
    assert (
        len(dictation_questions(p)) == 1
    )  # Both dose alternatives are quoted in the one field question.
    next_card = world.dictate("المريض هو سامي اختبار", p.candidate.model_dump(), id=11)
    assert next_card.blocked("order:0")
    assert len([q for q in next_card.issues if q.code == "extraction_conflict"]) == 1


def test_action_conflict_cannot_hide_an_independent_missing_dose(world: ScribeWorld) -> None:
    left = {"patient": VALUE["patient"], "orders": [{"action": "start", "drug": "فورسيجا"}]}
    right = {"patient": VALUE["patient"], "orders": [{"action": "continue", "drug": "فورسيجا"}]}
    model = ScriptedModel(candidate(left), candidate(right))
    world.scribe.model_factory = lambda *args: model
    world.post(update(APPLICANT, "سامي اختبار فورسيجا", 10))
    p = world.proposal
    assert p.blocked("order:0")
    assert len(dictation_questions(p)) == 2
    assert 'جرعة "Forxiga" إيه؟' in dictation_questions(p)
    assert (
        len([q for q in p.issues if q.code == "extraction_conflict" and q.field == "action"]) == 1
    )


def test_calls_are_concurrent_fresh_and_share_fifteen_second_budget(
    world: ScribeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sanad.scribe.turn as turn
    from sanad.scribe.extract import DictationCandidate

    entered = 0
    requests: list[str] = []
    timeouts: list[float] = []
    agents: list[object] = []

    class Agent:
        async def propose(
            self, schema: Any, request: str, **kwargs: Any
        ) -> ModelProposal[DictationCandidate]:
            nonlocal entered
            entered += 1
            requests.append(request)
            timeouts.append(kwargs["timeout"])
            for _ in range(10):
                if entered == 2:
                    break
                await asyncio.sleep(0)
            assert entered == 2
            return ModelProposal(
                candidate=CandidateRef(candidate_id="synthetic", version=1),
                unsupported_spans=(),
                value=DictationCandidate.model_validate(VALUE),
                provenance=(),
                metadata=(),
            )

    def make(*args: Any, **kwargs: Any) -> Agent:
        agent = Agent()
        agents.append(agent)
        return agent

    monkeypatch.setattr(turn, "make_agent", make)
    service = world.scribe.name_lookup(world.doctor, world.actor(APPLICANT))
    result = world.scribe.extract_candidate(
        "synthetic",
        world.actor(APPLICANT),
        world.doctor,
        SOURCE,
        "سامي اختبار ابدأ كونكور 5",
        None,
        service,
    )
    assert isinstance(result, ModelProposal)
    assert len(agents) == 2 and agents[0] is not agents[1]
    assert requests[0] == requests[1] and all(0 < t <= 15 for t in timeouts)


def test_doctor_clinic_seed_precedence_kind_isolation_and_hint_order(world: ScribeWorld) -> None:
    rows: tuple[tuple[TenantScope | AccountScope, NameKind, str], ...] = (
        (AccountScope(bot_id=world.doctor.telegram_bot_id), "test", "ClinicLab"),
        (world.doctor.scope, "test", "DoctorLab"),
        (world.doctor.scope, "finding", "DoctorFinding"),
    )
    for scope, kind, latin in rows:
        row = NameMemory(
            id=keys.digest(keys.partition(scope) + ":" + kind + ":" + normalize(latin)),
            scope=scope,
            kind=kind,
            latin=latin,
            spoken_forms=("سوديوم",),
            source="doctor_confirmation",
            created_at=world.clock(),
            updated_at=world.clock(),
            last_confirmed_at=world.clock(),
        )
        assert world.store._atomic([Write(record_item(to_record(row, scope)), None)], [])
    vocabulary = NameVocabulary(world.store, world.doctor)
    ctx = Context(vocabulary=vocabulary)
    assert resolve_name("سوديوم", "test", "سوديوم", ctx=ctx).latin == "DoctorLab"
    assert resolve_name("سوديوم", "finding", "سوديوم", ctx=ctx).latin == "DoctorFinding"
    assert resolve_name("سوديوم", "drug", "سوديوم", ctx=ctx).latin is None
    other = world.approve("40004")
    result = resolve_name(
        "سوديوم", "test", "سوديوم", ctx=Context(vocabulary=NameVocabulary(world.store, other))
    )
    assert (result.latin, result.tier) == ("ClinicLab", "clinic")
    names = hint_names(vocabulary).split(", ")
    assert names.index("DoctorLab") < names.index("ClinicLab") < names.index("Na")


def test_other_field_of_same_order_cannot_clear_dose_conflict(world: ScribeWorld) -> None:
    right = {
        "patient": VALUE["patient"],
        "orders": [{"action": "start", "drug": "كونكور", "dose": "10"}],
    }
    model = ScriptedModel(candidate(VALUE), candidate(right))
    world.scribe.model_factory = lambda *args: model
    world.post(update(APPLICANT, "سامي اختبار ابدأ كونكور 5 أو 10", 10))
    p = world.proposal
    changed = p.candidate.model_dump()
    changed["orders"][0]["timing"] = "الصبح"
    second = world.dictate("كونكور الصبح", changed, id=11)
    assert any(i.code == "extraction_conflict" and i.field == "dose" for i in second.issues)
    assert second.blocked("order:0")
    third = world.dictate("كونكور الجرعة 5", second.candidate.model_dump(), id=12)
    assert not any(i.code == "extraction_conflict" for i in third.issues)
    assert not dictation_questions(third)


def test_patient_plan_uses_the_doctors_confirmed_spelling(world: ScribeWorld) -> None:
    from sanad.concierge.plan import load, render_summary, summary
    from sanad.steward.types import records
    from sanad.store.records import OutboundIntent
    from store.account_fixtures import PATIENT

    row = NameMemory(
        id=keys.digest(keys.partition(world.doctor.scope) + ":drug:concorr"),
        scope=world.doctor.scope,
        kind="drug",
        latin="Concorr",
        generic="bisoprolol",
        spoken_forms=("كونكور",),
        source="doctor_confirmation",
        created_at=world.clock(),
        updated_at=world.clock(),
        last_confirmed_at=world.clock(),
    )
    assert world.store._atomic([Write(record_item(to_record(row, row.scope)), None)], [])
    patient = world.bound()
    p = world.dictate(
        "Synthetic Patient كونكور 5",
        {
            "patient": {"name_as_spoken": "Synthetic Patient"},
            "orders": [{"action": "continue", "drug": "كونكور", "dose": "5"}],
        },
    )
    assert "Concorr 5" in render_card(p)[0]
    world.tap()
    snapshot = load(world.store, patient.scope, world.clock())
    assert snapshot and "Concorr، 5" in render_summary(snapshot)
    orders = summary(snapshot)["orders"]
    assert isinstance(orders, list) and isinstance(orders[0], dict)
    assert orders[0]["drug"] == "Concorr"
    world.dictate(
        "Synthetic Patient وقفت كونكور",
        {
            "patient": {"name_as_spoken": "Synthetic Patient"},
            "orders": [{"action": "stop", "drug": "كونكور"}],
        },
        id=30,
    )
    world.tap(id=31)
    world.post(update(PATIENT, "كنت باخد ايه قبل كده", 32))
    receipt = world.receipt(32)
    assert receipt.state == "completed"
    intents = [
        from_record(r, OutboundIntent)
        for r in records(world.store, patient.scope, "outbound_intent")
    ]
    replies = [i for i in intents if "patient-turn:" + receipt.id in i.source_event_ids]
    assert replies and replies[-1].template_id == "patient_plan_summary"
    assert replies[-1].payload
    history = str(replies[-1].payload["text"])
    assert "Concorr، 5" in history
    assert all(
        line.startswith("تاريخ سابق، مش الخطة الحالية: Concorr") for line in history.splitlines()
    )
