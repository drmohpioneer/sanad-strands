"""Reply-derived accounting without relaxing sentence-level grounding."""

import asyncio
import json
from dataclasses import replace
from typing import Literal

import pytest
from domain_fixtures import NOW
from harness import FakeClock
from providers.fixtures import SOURCE, ScriptedModel, candidate
from store.concierge_fixtures import PatientWorld
from store.fixtures import ACTOR, SCOPE

from sanad.agents.factory import make_agent
from sanad.agents.tools import AgentScope
from sanad.concierge import answer, education, plan
from sanad.media.numbers import numbers_in
from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT as POLICY
from sanad.store.memory import MemoryStore


@pytest.fixture
def bundle() -> answer.Bundle:
    clock = FakeClock(NOW)
    world = PatientWorld.create(MemoryStore(clock=clock), clock)
    assert isinstance(world, PatientWorld)
    world.enroll()
    snapshot = plan.load(world.store, world.patient_scope, clock())
    assert snapshot
    return answer.build_bundle(
        snapshot,
        "هو الدكتور قال 40 ولا 20؟",
        education.retrieve("يعني إيه ارتفاع ضغط الدم؟", synthetic=True),
    )


@pytest.mark.parametrize("selected", ["plan", "education", "mixed"])
@pytest.mark.parametrize("model_kind", ["plan", "education", "mixed"])
def test_compose_derives_kind_and_accounting_from_reply(
    bundle: answer.Bundle,
    selected: str,
    model_kind: Literal["plan", "education", "mixed"],
) -> None:
    plan_line = next(line for line in bundle.plan_lines if "40 مج" in line)
    education_line = bundle.education_lines[0]
    reply = {
        "plan": plan_line,
        "education": education_line,
        "mixed": plan_line + "\n" + education_line,
    }[selected]
    # Education can contain no number despite an active plan containing 40.
    assert numbers_in(education_line) == () and "40" in bundle.numbers
    model = ScriptedModel(candidate({"reply": reply, "kind": model_kind, "needs_doctor": False}))
    agent = make_agent(
        "concierge",
        scope=AgentScope(ACTOR, SCOPE, SOURCE, POLICY, lambda: True, output_context=bundle.context),
        tools=(),
        system_prompt=answer.SYSTEM_PROMPT,
        session_key="synthetic-accounting",
        model_factory=lambda registry, role: model,
    )
    result = asyncio.run(answer.compose("هو الدكتور قال؟", bundle, agent, POLICY))
    assert result.template == "patient_answer" and result.reply == reply
    assert result.kind == selected and not result.ticket and result.failure is None
    request = json.dumps(model.script.calls, ensure_ascii=False)
    assert "sources_used" not in request and "numbers_used" not in request
    assert set(answer.ConciergeAnswer.model_fields) == {"reply", "kind", "needs_doctor"}


def test_repeated_source_markers_are_a_set(bundle: answer.Bundle) -> None:
    reply = "\n".join(bundle.education_lines[:3])
    assert reply.count("(مصدر:") == 3
    assert answer.sources_in(reply, bundle) == {bundle.labels[0]}
    value = answer.ConciergeAnswer(reply=reply, kind="plan", needs_doctor=False)
    assert answer.gate(value, bundle, POLICY) is None
    assert answer.answer_kind(reply, bundle) == "education"


def test_each_education_sentence_requires_its_own_marker(bundle: answer.Bundle) -> None:
    first, second = bundle.education_lines[:2]
    reply = first.split(" (مصدر:")[0] + "\n" + second
    value = answer.ConciergeAnswer(reply=reply, kind="education", needs_doctor=False)
    assert answer.gate(value, bundle, POLICY) == "sentence_not_grounded"


def test_retrieved_but_wrong_source_cannot_license_a_sentence(bundle: answer.Bundle) -> None:
    reply = bundle.education_lines[0].replace(bundle.labels[0], "مصدر تجريبي")
    value = answer.ConciergeAnswer(reply=reply, kind="education", needs_doctor=False)
    assert answer.gate(value, bundle, POLICY) == "source_not_retrieved"
    bundle = replace(bundle, labels=(*bundle.labels, "مصدر تجريبي"))
    assert answer.gate(value, bundle, POLICY) == "sentence_not_grounded"


def test_english_marker_and_kind_are_derived(bundle: answer.Bundle) -> None:
    entry = education.retrieve("يعني إيه ارتفاع ضغط الدم؟", synthetic=True)[0]
    line = entry.lines("en")[0]
    bundle = replace(
        bundle,
        context=bundle.context.model_copy(update={"language": "en"}),
        education_lines=(line,),
        labels=(entry.label("en"),),
    )
    value = answer.ConciergeAnswer(reply=line, kind="mixed", needs_doctor=False)
    assert answer.sources_in(line, bundle) == {entry.label("en")}
    assert answer.gate(value, bundle, POLICY) is None
    assert answer.answer_kind(line, bundle) == "education"


@pytest.mark.parametrize("needs_doctor", [False, True])
def test_cannot_answer_remains_a_fallback(bundle: answer.Bundle, needs_doctor: bool) -> None:
    model = ScriptedModel(
        candidate(
            {
                "reply": bundle.education_lines[0],
                "kind": "cannot_answer",
                "needs_doctor": needs_doctor,
            }
        )
    )
    agent = make_agent(
        "concierge",
        scope=AgentScope(ACTOR, SCOPE, SOURCE, POLICY, lambda: True, output_context=bundle.context),
        tools=(),
        system_prompt=answer.SYSTEM_PROMPT,
        session_key="synthetic-cannot-answer",
        model_factory=lambda registry, role: model,
    )
    result = asyncio.run(answer.compose("يعني إيه؟", bundle, agent, POLICY))
    assert result.template == "patient_safe_fallback" and result.ticket
    assert result.kind == "cannot_answer" and result.reply == ""
