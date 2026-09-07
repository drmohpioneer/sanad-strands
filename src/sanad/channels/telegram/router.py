"""Recoverable processing step, separately callable by a worker after receipt ACK."""

from collections.abc import Callable
from datetime import datetime
from typing import Literal

from pydantic import JsonValue

from sanad.accounts.commands import ApplyAsDoctor
from sanad.accounts.service import AccountService
from sanad.channels.telegram import wording
from sanad.channels.telegram.settings import TelegramSettings
from sanad.channels.transport import Transport
from sanad.domain import DRAFT_POLICY_2026_09, ObservationRef, PatientScope
from sanad.domain.boundaries import _BoundaryValue
from sanad.safety import render_urgent, to_incident_facts
from sanad.safety.models import IncidentFacts, ScreenVerdict
from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT, SafetyPolicy
from sanad.steward.dispatch import Dispatcher
from sanad.steward.inbound import InboundProcessor
from sanad.steward.service import Steward
from sanad.steward.types import StewardPolicy
from sanad.steward.urgent import UrgentService
from sanad.store.keys import ScopedKey
from sanad.store.protocol import Store
from sanad.store.records import Authorization, InboundReceipt, Incident, OutboundIntent, from_record

LANGUAGES: tuple[Literal["ar", "en"], ...] = ("ar", "en")


class RouteResult(_BoundaryValue):
    route: Literal[
        "patient", "doctor", "admin", "unknown", "callback", "duplicate", "busy", "refused"
    ]
    status: str
    template_id: str | None = None


class TelegramRuntime:
    def __init__(
        self,
        settings: TelegramSettings,
        store: Store,
        clock: Callable[[], datetime],
        transport: Transport,
        *,
        safety_policy: SafetyPolicy = SAFETY_POLICY_V1_CARDIOLOGY_DRAFT,
    ):
        self.settings, self.store, self.clock, self.transport = settings, store, clock, transport
        self.safety_policy = safety_policy
        self.accounts = AccountService(
            store,
            clock,
            settings.identity,
            lambda key, fields: wording.render(key, **fields),
            approve_label=wording.APPROVE_BUTTON,
            reject_label=wording.REJECT_BUTTON,
        )
        self.steward = Steward(store, clock, lambda scope: StewardPolicy(DRAFT_POLICY_2026_09))
        self.inbound = InboundProcessor(self.steward, transport="telegram")
        self.urgent = UrgentService(self.steward, patient_template_id="patient_emergency")
        self.dispatcher = Dispatcher(
            self.steward,
            transport,
            settings=settings.identity,
            payload_resolver=self.patient_payload,
        )
        self.counters: dict[str, int] = {}
        self.identity_route: (
            Callable[[InboundReceipt, Authorization], RouteResult | None] | None
        ) = None
        self.scribe_route: Callable[[InboundReceipt, Authorization], RouteResult | None] | None = (
            None
        )

    def count(self, reason: str) -> None:
        self.counters[reason] = self.counters.get(reason, 0) + 1

    def general(self, template: Literal["patient_emergency", "patient_safety_ack"]) -> str:
        return "\n".join(
            render_urgent(template, language=language, gender="u", policy=self.safety_policy)
            for language in LANGUAGES
        )

    def patient_payload(self, intent: OutboundIntent) -> dict[str, JsonValue]:
        if intent.template_id == "patient_emergency":
            return {"text": self.general("patient_emergency")}
        if intent.template_id == "liaison:DANGER" and isinstance(intent.scope, PatientScope):
            sources = [r for r in intent.source_versions if r.entity_type == "incident"]
            if len(sources) == 1:
                row = self.store.get(intent.scope, "incident", sources[0].id)
                if row:
                    incident = from_record(row, Incident)
                    facts = IncidentFacts.model_validate(incident.facts)
                    return {
                        "text": (
                            "الصورة اللي اتنبهت لها قبل كده اتربطت بالمريض.\n"
                            if incident.prior_delivery_refs
                            else ""
                        )
                        + "\n".join(
                            render_urgent(
                                "doctor_danger",
                                language=language,
                                gender="u",
                                policy=self.safety_policy,
                                patient=intent.scope.patient_id,
                                concept=(getattr(facts.verdict, "concept", None) or facts.rule_id),
                                source=facts.source.observation_id,
                                uncertainty="unverified / غير متحقق",
                            )
                            for language in LANGUAGES
                        )
                    }
        # This slice has no renderer for ordinary clinical guidance.
        raise ValueError("unreleased_patient_payload")


def route_receipt(
    runtime: TelegramRuntime, key: ScopedKey, *, owner: str = "telegram"
) -> RouteResult:
    store, now = runtime.store, runtime.clock()
    row = store.get(key.scope, "inbound_receipt", key.pk)
    if row is None or row.key != key.key:
        return RouteResult(route="refused", status="missing_receipt")
    receipt = from_record(row, InboundReceipt)
    if receipt.state == "completed":
        return RouteResult(route="duplicate", status="completed")
    if receipt.state == "needs_attention":
        return RouteResult(route="refused", status="needs_attention")
    if receipt.principal is None or receipt.safety_result is None:
        return RouteResult(route="refused", status="unverified_receipt")
    verdict = ScreenVerdict.model_validate(receipt.safety_result)
    # Screening was done before authorize at ingress. Revalidate roles for every action.
    auth = store.authorize(runtime.settings.bot_id, receipt.source_subject)
    if verdict.level != "danger" and runtime.identity_route is not None:
        identity_result = runtime.identity_route(receipt, auth)
        if identity_result is not None:
            return identity_result
    if verdict.level != "danger" and runtime.scribe_route is not None:
        scribe_result = runtime.scribe_route(receipt, auth)
        if scribe_result is not None:
            return scribe_result
    active_patient = (
        auth.binding is not None
        and auth.binding.status == "active"
        and auth.principal.actor_kind == "patient"
        and isinstance(receipt.scope, PatientScope)
        and auth.principal.doctor_id == receipt.scope.doctor_id
        and auth.principal.patient_id == receipt.scope.patient_id
        and auth.private_chat_id == receipt.source_chat
        and store.get_patient_profile(receipt.scope) is not None
    )
    if active_patient:
        assert isinstance(receipt.scope, PatientScope)
        if receipt.kind == "callback":
            runtime.transport.answer_callback(
                str((receipt.payload or {}).get("callback_query_id", "")),
                wording.render("callback_refused"),
            )
            runtime.inbound.process_inbound(key, owner, now)
            return RouteResult(
                route="callback", status="callback_refused", template_id="callback_refused"
            )
        if verdict.level == "danger":
            facts, severity = to_incident_facts(
                verdict,
                source=ObservationRef(observation_id=receipt.id),
                policy=runtime.safety_policy,
            )
            runtime.urgent.raise_incident(
                receipt.scope, facts.unique_source_key, facts.as_payload(), severity, now
            )
        patient_result = runtime.inbound.process_inbound(key, owner, now)
        return RouteResult(
            route="patient", status=patient_result.status if patient_result else "busy"
        )
    if isinstance(receipt.scope, PatientScope):
        # An identity change cannot turn an old patient receipt into an account action.
        return RouteResult(route="refused", status="binding_changed")
    claim = store.claim_work(
        key, receipt.version, owner, now, runtime.accounts.policy.operations.claim_ttl
    )
    if claim is None:
        return RouteResult(route="busy", status="processing")
    current = store.get(key.scope, "inbound_receipt", receipt.id)
    assert current is not None
    receipt = from_record(current, InboundReceipt)
    if (
        verdict.level != "danger"
        and receipt.work_clock is not None
        and receipt.work_clock.attempt_count
        > runtime.accounts.policy.operations.max_inbound_attempts
    ):
        attention = runtime.accounts.fail_receipt(receipt, claim)
        return RouteResult(
            route="refused",
            status="needs_attention"
            if attention.status in {"accepted", "duplicate"}
            else attention.status,
        )
    # Keep the receipt-time principal immutable; fresh authority is passed to commands.
    payload = receipt.payload or {}
    template: str | None = None
    text = None
    status = "deferred_capability"
    route: Literal["doctor", "admin", "unknown", "callback"] = (
        "doctor"
        if auth.principal.actor_kind == "doctor"
        else "admin"
        if auth.principal.actor_kind == "admin"
        else "unknown"
    )
    if verdict.level == "danger":
        template, text, status = (
            "patient_emergency",
            runtime.general("patient_emergency"),
            "safety_response",
        )
    elif receipt.kind == "callback":
        result = runtime.accounts.callback_by_hash(
            str(payload.get("callback_token_hash", "")), auth.principal, "callback:" + receipt.id
        )
        refused = result.status not in {"accepted", "duplicate"}
        callback_text = wording.render("callback_refused") if refused else ""
        outcome = runtime.transport.answer_callback(
            str(payload.get("callback_query_id", "")), callback_text
        )
        runtime.count("callback_ack_" + outcome.status)
        route, status = "callback", result.status
        completion = runtime.accounts.finish_receipt(receipt, claim, result_code=status)
        return RouteResult(
            route=route,
            status=status if completion.status in {"accepted", "duplicate"} else completion.status,
            template_id="callback_refused" if refused else None,
        )
    elif receipt.kind != "text":
        if route == "doctor":
            template = "doctor_capability_pending"
        else:
            template, text = "patient_safety_ack", runtime.general("patient_safety_ack")
    elif route == "doctor":
        template = (
            "doctor_welcome_back"
            if str(payload.get("text", "")).strip() == "/start"
            else "doctor_capability_pending"
        )
        status = "welcome_back" if template == "doctor_welcome_back" else "deferred_capability"
    elif route in {"unknown", "admin"} and auth.binding is None:
        raw_text = payload.get("text")
        result = runtime.accounts.apply(
            ApplyAsDoctor(
                command_id="apply:" + receipt.id,
                actor=auth.principal,
                private_chat_id=receipt.source_chat,
                restart_rejected=isinstance(raw_text, str) and raw_text.strip() == "/start",
            )
        )
        if result.status not in {"accepted", "duplicate", "already_in_state"}:
            return RouteResult(route=route, status=result.status)
        status = result.status
    else:
        template, text = "patient_safety_ack", runtime.general("patient_safety_ack")
    completed = runtime.accounts.finish_receipt(
        receipt,
        claim,
        result_code=status,
        template_id=template,
        text=text,
        safety=verdict.level == "danger",
    )
    return RouteResult(
        route=route,
        status=status if completed.status in {"accepted", "duplicate"} else completed.status,
        template_id=template,
    )
