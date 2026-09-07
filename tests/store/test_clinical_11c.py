import asyncio
from typing import Any

import pytest
from harness import FakeClock
from providers.fixtures import ScriptedModel, response
from providers.rxnorm_fixture import RxNormFixture
from providers.test_agents import binding

from sanad.agents.factory import make_agent
from sanad.agents.tools import ALLOWLIST, AgentRole, drug_lookup_tool
from sanad.scribe.card import render_card
from sanad.scribe.lookup import DrugLookup
from sanad.scribe.memory import memory_rows
from sanad.scribe.proposal import ScribeCallback
from sanad.store import keys
from sanad.store._base import StoreBase
from sanad.store.records import from_record
from store.account_fixtures import APPLICANT, update
from store.scribe_fixtures import ScribeWorld


@pytest.fixture
def world(store: StoreBase, clock: FakeClock) -> ScribeWorld:
    value = ScribeWorld.create(store, clock)
    value.approve()
    return value


def test_clinical_english_preserves_spoken_fields_and_persists_both(world: ScribeWorld) -> None:
    spoken = (
        "تي ويف انفريجن في لاترال ليدز",
        "الأكو فانكشن 45%",
        "أنجينا",
        "ضغط وسكر",
    )
    english = (
        "T wave inversion in lateral leads",
        "EF 45% %",
        "angina",
        "hypertension, diabetes",
    )
    text = "مريض جديد سامي اختبار " + "، ".join(spoken) + "، وتحاليل بانو كريات وسوديوم وبوتاسيوم"
    p = world.dictate(
        text,
        {
            "patient": {"name_as_spoken": "سامي اختبار"},
            "facts": [
                {"category": "history", "text": raw, "terms": [{"spoken": raw, "english": en}]}
                for raw, en in zip(spoken, english, strict=True)
            ],
            "missions": [
                {
                    "kind": "TEST",
                    "text": "بانو كريات وسوديوم وبوتاسيوم",
                    "clinical_en": "BUN, creatinine, sodium, potassium",
                }
            ],
        },
    )
    card = render_card(p)[0]
    assert all(en.replace("% %", "%") in card for en in english)
    assert "TEST: BUN, creatinine, Na, K" in card
    assert tuple(f.text for f in p.candidate.facts) == spoken
    assert not any(i.code == "clinical_unclear" for i in p.issues)
    world.tap()
    from sanad.scribe.patients import panel

    patient = panel(world.store, world.doctor.scope)[0]
    facts = world.store.list_records(patient.scope, "clinical_fact")[0]
    payloads = [payload for f in facts if isinstance(payload := f.body.get("payload"), dict)]
    assert len(payloads) == len(spoken)
    assert {p["text"] for p in payloads} == set(spoken)
    assert all(p["terms"] for p in payloads)
    assert all(p["clinical_en"] is None for p in payloads)
    mission = world.store.list_records(patient.scope, "mission")[0][0]
    assert mission.body["title"] == "بانو كريات وسوديوم وبوتاسيوم"
    assert mission.body["clinical_en"] == "BUN, creatinine, Na, K"
    assert all(
        "45" not in r.latin and "45" not in " ".join(r.spoken_forms)
        for r in memory_rows(world.store, world.doctor.scope)
    )


@pytest.mark.parametrize(
    "english", ["EF 99%", "EF خمسة", "x" * 121, "EF 45% <script>", None, "angina on aspirin"]
)
def test_invalid_english_uses_unchanged_spoken_form_and_question(
    world: ScribeWorld, english: str | None
) -> None:
    p = world.dictate(
        "سامي اختبار عنده كفاءة 45",
        {
            "patient": {"name_as_spoken": "سامي اختبار"},
            "facts": [{"category": "history", "text": "كفاءة 45", "clinical_en": english}],
        },
    )
    assert p.candidate.facts[0].clinical_en is None
    assert "كفاءة 45" in render_card(p)[0]
    assert any(i.code == "clinical_unclear" for i in p.issues)
    assert "99" not in render_card(p)[0] and "aspirin" not in render_card(p)[0]


def test_disguised_current_medication_becomes_continue_question(world: ScribeWorld) -> None:
    p = world.dictate(
        "سامي اختبار واخد أسبرين",
        {
            "patient": {"name_as_spoken": "سامي اختبار"},
            "facts": [
                {"category": "history", "text": "واخد أسبرين", "clinical_en": "taking aspirin"}
            ],
        },
    )
    assert not p.candidate.facts
    assert p.candidate.orders[0].drug == "Aspirin" and p.candidate.orders[0].action == "continue"
    assert any(i.code == "fact_medication" for i in p.issues)
    assert not any(i.code == "order_missing" for i in p.issues)


def test_explicit_brand_correction_learns_edited_value(world: ScribeWorld) -> None:
    first = world.dictate(
        "سامي اختبار إكسفورج 5/160/12.5",
        {
            "patient": {"name_as_spoken": "سامي اختبار"},
            "orders": [
                {
                    "action": "continue",
                    "drug": "إكسفورج",
                    "name_latin": "Exforge",
                    "dose": "5/160/12.5",
                }
            ],
        },
    )
    changed = first.candidate.model_dump()
    changed["orders"][0].update(
        drug="إكسفورج إتش سي تي",
        name_latin="Exforge HCT",
        generic="amlodipine/valsartan/hydrochlorothiazide",
    )
    second = world.dictate("لا، مش إكسفورج، ده إكسفورج إتش سي تي", changed, id=11)
    assert second.candidate.orders[0].drug == "Exforge HCT"
    world.tap()
    assert [r.latin for r in memory_rows(world.store, world.doctor.scope)] == ["Exforge HCT"]


def test_ordinary_reply_adds_to_name_only_card_and_unknown_command_keeps_it(
    world: ScribeWorld,
) -> None:
    world.post(update(APPLICANT, "/new سامي اختبار", 10))
    first = world.proposal
    second = world.dictate(
        "ضغط مزمن وعايز CBC",
        {
            "patient": {},
            "facts": [
                {"category": "condition", "text": "ضغط مزمن", "clinical_en": "chronic hypertension"}
            ],
            "missions": [{"kind": "TEST", "text": "CBC", "clinical_en": "CBC"}],
        },
        id=11,
    )
    assert second.id == first.id and second.candidate.patient == first.candidate.patient
    assert second.candidate.facts and second.candidate.missions
    world.scribe.model_factory = lambda registry, role: ScriptedModel()
    world.post(update(APPLICANT, "/unknown", 12))
    assert world.proposal.version == second.version


def test_quoted_non_seed_finding_correction_keeps_unanswered_fact(world: ScribeWorld) -> None:
    first = world.dictate(
        "سامي اختبار عنده طنين ودوخة",
        {
            "patient": {"name_as_spoken": "سامي اختبار"},
            "facts": [
                {"category": "history", "text": "طنين", "clinical_en": "tinnitus"},
                {"category": "history", "text": "دوخة", "clinical_en": "dizziness"},
            ],
        },
    )
    changed = first.candidate.model_dump()
    changed["facts"][0].update(text="مفيش طنين", clinical_en="no tinnitus")
    changed["facts"][1].update(text="مفيش دوخة", clinical_en="no dizziness")
    changed["correction_edits"] = [
        {"item": "fact:0", "proposal_index": 0, "source_quote": "مفيش طنين"}
    ]
    second = world.dictate("مفيش طنين", changed, id=11)
    assert second.candidate.facts[0].text == "مفيش طنين"
    assert "no tinnitus" not in render_card(second)[0]
    assert second.candidate.facts[1] == first.candidate.facts[1]


def test_name_memory_cannot_be_removed_from_confirmation_batch(
    world: ScribeWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = world.dictate(
        "سامي اختبار كونكور 5",
        {
            "patient": {"name_as_spoken": "سامي اختبار"},
            "orders": [{"action": "continue", "drug": "كونكور", "dose": "5"}],
        },
    )
    row = world.store.get(p.scope, "scribe_callback", keys.digest(world.button("✅ تمام")))
    assert row
    token = from_record(row, ScribeCallback)
    original = world.scribe.repo.commit

    def omitted(
        actor: Any, command: Any, command_id: Any, models: Any, *args: Any, **kwargs: Any
    ) -> Any:
        return original(
            actor,
            command,
            command_id,
            tuple(m for m in models if m.entity_type != "name_memory"),
            *args,
            **kwargs,
        )

    monkeypatch.setattr(world.scribe.repo, "commit", omitted)
    assert (
        world.scribe.committer.confirm(p, token, world.owner, "missing-memory").template
        == "scribe_stale"
    )
    assert not memory_rows(world.store, world.doctor.scope)
    from sanad.scribe.patients import panel

    assert not panel(world.store, world.doctor.scope)


def test_lookup_refuses_identity_sentences_and_defers_novel_agent_query(world: ScribeWorld) -> None:
    world.named_stub("Private Patient")
    fixture = RxNormFixture()
    world.scribe.rxnorm_client = fixture.client
    service = world.scribe.name_lookup(world.doctor, world.owner)
    for value in ("Private Patient", "https://example.org", "Forxiga 10 mg", "patient id 123"):
        assert not service.lookup_drug(value).found
    service.patient_identity_pending = True
    assert not service.lookup_drug("NovelPatientName").found
    assert service.resolve("Private Patient", "Private Patient", "Private Patient", None).conflict
    assert not fixture.calls


@pytest.mark.parametrize("role", [r for r in ALLOWLIST if r != "scribe"])
def test_lookup_drug_is_refused_by_every_other_role(role: AgentRole) -> None:
    context = binding()
    called: list[str] = []

    def body(name: str) -> DrugLookup:
        called.append(name)
        return DrugLookup()

    tool = drug_lookup_tool(context, body)
    model = ScriptedModel(response(calls=[("lookup_drug", {"name": "Amlodipine"})]))
    agent = make_agent(
        role,
        scope=context,
        tools=(tool,),
        system_prompt="Synthetic",
        session_key="role-lookup",
        model_factory=lambda registry, role: model,
    )
    try:
        asyncio.run(
            agent.sdk.invoke_async(
                "Synthetic", invocation_state={"sanad_scope": context}, limits={"turns": 1}
            )
        )
    except Exception:
        pass
    assert agent.guard.refusals[0].reason == "tool_not_allowed"
    assert not called
