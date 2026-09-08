"""One principal reasoning turn, durable reservations, and replay-safe outcomes."""

import asyncio
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from pydantic import Field

from sanad.agents.factory import Proposal, make_agent
from sanad.agents.tools import AgentScope, scoped_tool
from sanad.concierge import plan
from sanad.domain import Mission, Provenance
from sanad.domain.boundaries import _BoundaryValue
from sanad.resolver import templates
from sanad.resolver.attempts import Action, evolve, material, resolved_words
from sanad.resolver.places import STEP_TIMEOUT, OSMPlaces, PlacesProvider, PlacesResult
from sanad.safety.models import OutputContext
from sanad.steward.patient import PatientTurnCommit
from sanad.store.records import Claim, InboundReceipt, from_record, to_record

if TYPE_CHECKING:
    from sanad.concierge.turn import ConciergeTurn


class BarrierProposal(_BoundaryValue):
    step: Literal[
        "ask_patient", "find_places", "hand_to_doctor", "reschedule_visit", "resume_chase"
    ]
    question: str = Field(default="", max_length=180)


class Empty(_BoundaryValue):
    pass


class PermittedStep(_BoundaryValue):
    step: str


class ResolverUnavailable(Exception):
    pass


def pending_reply(tx: PatientTurnCommit, text: str) -> bool:
    choices = [
        m
        for m in tx.snapshot.missions
        if m.barrier_attempts
        and m.barrier_reason
        and (material(m.barrier_attempts[-1], text, tx.now) or resolved_words(text))
    ]
    if len(choices) != 1:
        return False
    tx.builder.command = tx.builder.command.model_copy(
        update={
            "payload": {
                **tx.builder.command.payload,
                "resolver_target": choices[0].id,
                "resolver_words": text,
            }
        }
    )
    tx.kind("RecordPatientReply")
    return True


def keep_blocked(tx: PatientTurnCommit) -> None:
    for mission in tx.snapshot.missions:
        if (
            mission.barrier_attempts
            and mission.barrier_reason
            and mission.state in {"blocked", "overdue"}
            and ("mission", mission.id) not in tx.builder.puts
        ):
            tx.put(
                Mission.model_validate(
                    mission.model_dump() | {"version": mission.version + 1, "updated_at": tx.now}
                )
            )


class ResolverTurn:
    def __init__(self, concierge: "ConciergeTurn", provider: PlacesProvider | None = None):
        self.concierge = concierge
        self.provider = provider or OSMPlaces()

    def _mission(self, tx: PatientTurnCommit, id: str) -> Mission:
        return next(m for m in tx.snapshot.missions if m.id == id)

    def current(self, tx: PatientTurnCommit, mission: Mission) -> bool:
        row = tx.store.get(tx.snapshot.scope, "mission", mission.id)
        return bool(
            self.concierge._current(tx)
            and tx.profile.routine_contact_enabled
            and row
            and row.ref == to_record(mission, tx.snapshot.scope).ref
            and row.body.get("state") in {"blocked", "overdue"}
        )

    def persist(self, tx: PatientTurnCommit, action: Action) -> Mission:
        original = self._mission(tx, action.mission_id)
        row = tx.builder.puts.get(("mission", original.id))
        base = from_record(row, Mission) if row else original
        if action.phase == "begin" and original.barrier_attempts and resolved_words(action.words):
            from sanad.domain import TransitionResult, transition_mission
            from sanad.domain.events import BarrierResolved

            resolved = transition_mission(
                original,
                BarrierResolved(event_id=tx.id + ":resolved"),
                tx.now,
                tx.builder.policy.timing,
            )
            if not isinstance(resolved, TransitionResult) or not isinstance(
                resolved.aggregate, Mission
            ):
                raise ResolverUnavailable("resolution_refused")
            base = resolved.aggregate
        attempts = evolve(
            original, action, tx.receipt.id, tx.now, contact=tx.profile.routine_contact_enabled
        )
        mission = Mission.model_validate(
            base.model_dump()
            | {"version": original.version + 1, "updated_at": tx.now, "barrier_attempts": attempts}
        )
        tx.builder.puts[("mission", mission.id)] = to_record(mission, tx.snapshot.scope)
        tx.kind(
            "RecordPatientReply"
            if action.phase == "begin" and not original.barrier_attempts
            else "ResolverCheckpoint"
        )
        # Unique checkpoint IDs leave the normal final patient-turn key unconsumed.
        tx.builder.command = tx.builder.command.model_copy(
            update={
                "command_id": tx.id + ":resolver:" + str(mission.version),
                "payload": {
                    **tx.builder.command.payload,
                    "resolver_action": action.model_dump(mode="json"),
                },
            }
        )
        from sanad.store.records import AuditEvent

        for event_id, event_row in tuple(tx.builder.events.items()):
            event = from_record(event_row, AuditEvent).model_copy(
                update={"command_id": tx.builder.command.command_id}
            )
            tx.builder.events[event_id] = to_record(event, tx.snapshot.scope)
        saved = InboundReceipt.model_validate(
            tx.receipt.model_dump() | {"version": tx.receipt.version + 1, "updated_at": tx.now}
        )
        tx.put(saved)
        tx.builder.audit(
            "RESOLVER_CHECKPOINT",
            tx.builder.command.command_id,
            (to_record(mission, tx.snapshot.scope).ref,),
        )
        result = tx.store.commit(tx.builder.finish(complete_receipt=False))
        if result.status not in {"accepted", "duplicate"}:
            raise ResolverUnavailable(result.status)
        auth = tx.store.authorize(tx.principal.bot_id or "", tx.principal.subject)
        snapshot = (
            plan.authorized(tx.store, tx.principal, auth.binding, self.concierge.runtime.clock())
            if auth.binding
            else None
        )
        if snapshot is None:
            raise ResolverUnavailable("authority_changed")
        claim = Claim.model_validate(tx.claim.model_dump() | {"version": saved.version})
        # Same lease/processing claim; fresh expected versions for the next stage.
        PatientTurnCommit.__init__(tx, tx.steward, snapshot, saved, tx.principal, claim, tx.lease)
        self.concierge.checkpoint("resolver_" + action.phase + "_persisted")
        return self._mission(tx, mission.id)

    def choose(
        self, tx: PatientTurnCommit, mission: Mission, source: Provenance
    ) -> BarrierProposal:
        attempt = mission.barrier_attempts[-1]
        fallback = BarrierProposal(
            step="find_places" if attempt.area else "ask_patient",
            question=""
            if attempt.area
            else templates.render(attempt.requested_fact or "detail", tx.snapshot.patient.language),
        )
        if attempt.barrier_type == "side_effect_experience" or (
            attempt.answered and not attempt.area
        ):
            fallback = BarrierProposal(step="hand_to_doctor")
        binding = AgentScope(
            principal=tx.principal,
            scope=tx.snapshot.scope,
            source=source,
            policy=self.concierge.runtime.safety_policy,
            authority_check=lambda: self.current(tx, mission),
            output_context=OutputContext(
                language=tx.snapshot.patient.language, mode="plan_explanation"
            ),
        )
        permitted: tuple[str, ...] = (
            ("find_places", "hand_to_doctor") if attempt.area else ("ask_patient", "hand_to_doctor")
        )
        if attempt.barrier_type == "side_effect_experience" or (
            attempt.answered and not attempt.area
        ):
            permitted = ("hand_to_doctor",)

        def handler(name: str) -> Callable[[Empty], PermittedStep]:
            def propose_step(value: Empty) -> PermittedStep:
                return PermittedStep(step=name)

            return propose_step

        tools = tuple(
            scoped_tool(name, Empty, handler(name), binding=binding) for name in permitted
        )
        try:
            agent = make_agent(
                "resolver",
                scope=binding,
                tools=tools,
                system_prompt=(
                    "Choose one permitted practical step in one reasoning turn. "
                    "Tools only propose; code executes. Never classify the barrier. "
                    "Never name a place, price, stock, booking, substitute or date. "
                    "In question copy the supplied safe question for validation; "
                    "code transmits it only for ask_patient. Return the typed JSON value."
                ),
                session_key=f"resolver:{mission.id}:{attempt.sequence}",
                model_factory=self.concierge.model_factory,
                observe=self.concierge.observe,
            )
            # The common factory's patient gate requires a nonempty patient field.
            # A safe question is validated even when choosing a non-question step;
            # it is transmitted only for ask_patient.
            safe_question = templates.render(
                attempt.requested_fact or "detail", tx.snapshot.patient.language
            )
            result = asyncio.run(
                agent.propose(
                    BarrierProposal,
                    json.dumps(
                        {
                            "barrier": attempt.barrier_type,
                            "words": attempt.patient_words[-1],
                            "area": attempt.area,
                            "permitted": permitted,
                            "question": safe_question,
                        }
                    ),
                    patient_fields=("question",),
                    want_spans=False,
                )
            )
            if isinstance(result, Proposal) and result.value.step in permitted:
                value = result.value
                if value.step == "ask_patient" and templates.question_ok(
                    value.question,
                    attempt.requested_fact or "detail",
                    tx.snapshot.patient.language,
                    self.concierge.runtime.safety_policy,
                ):
                    return value
                if value.step != "ask_patient" and value.question in {"", safe_question}:
                    return value.model_copy(update={"question": ""})
        except Exception:
            pass
        return fallback

    def run(self, tx: PatientTurnCommit, source: Provenance) -> str:
        id = str(tx.builder.command.payload["resolver_target"])
        words = str(tx.builder.command.payload["resolver_words"])
        original = self._mission(tx, id)
        already = next(
            (a for a in original.barrier_attempts if a.receipt_id == tx.receipt.id), None
        )
        if already and already.sequence != len(original.barrier_attempts):
            keep_blocked(tx)
            return templates.patient_reply(
                already, tx.snapshot.patient.language, self.concierge.runtime.safety_policy
            )
        mission = self.persist(tx, Action(phase="begin", mission_id=id, words=words))
        attempt = mission.barrier_attempts[-1]
        if attempt.phase == "complete":
            return (
                templates.patient_reply(
                    attempt, tx.snapshot.patient.language, self.concierge.runtime.safety_policy
                )
                if already or attempt.state == "resolved"
                else templates.render("unresolved", tx.snapshot.patient.language)
            )
        if attempt.receipt_id != tx.receipt.id:
            keep_blocked(tx)
            return templates.render("unresolved", tx.snapshot.patient.language)
        if tx.now >= attempt.expires_at:
            mission = self.persist(tx, Action(phase="finish", mission_id=id, outcome="expired"))
        elif not self.current(tx, mission):
            mission = self.persist(
                tx, Action(phase="finish", mission_id=id, outcome="contact_stopped")
            )
        elif already and attempt.phase == "reserved":
            mission = self.persist(tx, Action(phase="finish", mission_id=id, outcome="interrupted"))
        else:
            if attempt.phase == "reserved":
                proposal = self.choose(tx, mission, source)
                mission = self.persist(
                    tx,
                    Action(
                        phase="choose",
                        mission_id=id,
                        choice=proposal.step,  # type: ignore[arg-type]
                        question=proposal.question,
                    ),
                )
            attempt = mission.barrier_attempts[-1]
            if attempt.choice == "ask_patient":
                mission = self.persist(tx, Action(phase="finish", mission_id=id, outcome="asked"))
            elif attempt.choice == "hand_to_doctor":
                mission = self.persist(
                    tx, Action(phase="finish", mission_id=id, outcome="handed_to_doctor")
                )
            elif attempt.choice == "find_places":
                # One durable retry, bounded together to less than ten seconds.
                async def searches() -> None:
                    nonlocal mission
                    async with asyncio.timeout(STEP_TIMEOUT):
                        if mission.barrier_attempts[-1].phase == "search_reserved":
                            mission = self.persist(
                                tx,
                                Action(
                                    phase="result",
                                    mission_id=id,
                                    result=PlacesResult(outcome="places_unavailable"),
                                ),
                            )
                        while mission.barrier_attempts[-1].phase != "complete":
                            mission = self.persist(
                                tx, Action(phase="reserve_search", mission_id=id)
                            )
                            self.concierge.checkpoint("resolver_before_places_call")
                            result = await self.provider.search(
                                mission.barrier_attempts[-1].area or "",
                                authorized=lambda: self.current(tx, self._mission(tx, id)),
                            )
                            if result.places:
                                preview = mission.barrier_attempts[-1].model_copy(
                                    update={"places": result.places, "outcome": "places_offered"}
                                )
                                rendered = templates.patient_reply(
                                    preview,
                                    tx.snapshot.patient.language,
                                    self.concierge.runtime.safety_policy,
                                )
                                if not rendered.startswith(
                                    templates.render("disclosure", tx.snapshot.patient.language)
                                ):
                                    result = PlacesResult(outcome="places_unavailable")
                            mission = self.persist(
                                tx, Action(phase="result", mission_id=id, result=result)
                            )

                try:
                    asyncio.run(searches())
                except TimeoutError:
                    mission = self.persist(
                        tx, Action(phase="finish", mission_id=id, outcome="places_unavailable")
                    )
        keep_blocked(tx)
        return templates.patient_reply(
            mission.barrier_attempts[-1],
            tx.snapshot.patient.language,
            self.concierge.runtime.safety_policy,
        )


def recover(tx: PatientTurnCommit) -> bool:
    for mission in tx.snapshot.missions:
        for attempt in mission.barrier_attempts:
            if attempt.receipt_id == tx.receipt.id:
                tx.builder.command = tx.builder.command.model_copy(
                    update={
                        "payload": {
                            **tx.builder.command.payload,
                            "resolver_target": mission.id,
                            "resolver_words": attempt.patient_words[0],
                        }
                    }
                )
                return True
    return False


def stage_reply(tx: PatientTurnCommit, mission_id: str, template: str, text: str) -> None:
    """Same patient outbox, with the target revision checked again before sending."""
    from sanad.steward.apply import make_intent
    from sanad.store import keys
    from sanad.store.records import canonical_json

    mission = next(m for m in tx.snapshot.missions if m.id == mission_id)
    row = tx.builder.puts.get(("mission", mission_id)) or to_record(mission, tx.snapshot.scope)
    payload = {"text": text}
    intent = make_intent(
        tx.snapshot.scope,
        tx.id,
        (row.ref,),
        "solicited_reply",
        tx.id,
        tx.now,
        tx.builder.policy,
        tx.snapshot.authority,
        tx.profile,
        audience="patient",
        order_refs=mission.order_refs,
        template_id=template,
    ).model_copy(
        update={
            "payload": payload,
            "payload_digest": keys.digest(canonical_json(payload).decode()),
            "recipient_subject": tx.principal.subject,
            "bot_id": tx.snapshot.binding.bot_id,
        }
    )
    tx.builder.intents[intent.id] = to_record(intent, tx.snapshot.scope)
    # The existing composition hook means an adapter has already staged the reply.
    tx.builder.command = tx.builder.command.model_copy(
        update={
            "payload": {
                **tx.builder.command.payload,
                "media_reply_owned": True,
            }
        }
    )
