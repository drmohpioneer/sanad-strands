"""Danger stays outside ordinary fencing; models never decide patient mutations."""

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import uuid4
from zoneinfo import ZoneInfo

from strands.models import Model

from sanad.agents.factory import bedrock_model, make_agent
from sanad.agents.sessions import FencedSessionManager
from sanad.agents.tools import AgentScope
from sanad.concierge import answer, education, plan, preferences, question, reports, templates
from sanad.concierge.policy import DRAFT_CONCIERGE_POLICY as POLICY
from sanad.concierge.records import PatientAction
from sanad.concierge.text import asks_doctor, asks_history, contains, is_question, plan_command
from sanad.domain import FollowUpTask, ObservationRef, PatientScope, Principal, Provenance
from sanad.media.retrieve import MediaRetriever, fetch_telegram_file
from sanad.media.speech import SpeechAdapter, Transcript, transcribe
from sanad.media.telegram import MediaFailure
from sanad.media.vision import VisionAdapter
from sanad.models.io import CallMetadata
from sanad.models.registry import ModelRegistry, ModelRole
from sanad.safety import screen_text, to_incident_facts, wants_treatment_change
from sanad.safety.models import LabVerdict, ScreenVerdict, VitalVerdict
from sanad.scribe.records import CareOrderVersion
from sanad.steward.patient import PatientTurnCommit
from sanad.steward.types import records
from sanad.store import keys
from sanad.store.records import (
    Claim,
    InboundReceipt,
    MediaWork,
    OperationalClock,
    StoredRecord,
    SubjectBinding,
    from_record,
    to_record,
)

if TYPE_CHECKING:
    from sanad.channels.telegram.router import RouteResult, TelegramRuntime


class ConciergeTurn:
    def __init__(
        self,
        runtime: "TelegramRuntime",
        *,
        synthetic: bool = False,
        model_factory: Callable[[ModelRegistry, ModelRole], Model] = bedrock_model,
        speech_factory: Callable[[Provenance], SpeechAdapter] | None = None,
        media_factory: Callable[[InboundReceipt, Principal], MediaRetriever] | None = None,
        vision_factory: Callable[[Provenance], VisionAdapter] | None = None,
        observe: Callable[[CallMetadata], None] = lambda metadata: None,
        checkpoint: Callable[[str], None] = lambda stage: None,
    ):
        self.runtime, self.store = runtime, runtime.store
        self.synthetic, self.model_factory = synthetic, model_factory
        self.speech_factory, self.media_factory = speech_factory, media_factory
        self.observe, self.checkpoint = observe, checkpoint
        self.vision_factory = vision_factory
        from sanad.evidence.turn import EvidenceTurn

        self.evidence = EvidenceTurn(self)

    def __call__(
        self, receipt: InboundReceipt, principal: Principal, binding: SubjectBinding
    ) -> "RouteResult | None":
        if receipt.kind == "callback" and not self.store.get(
            receipt.scope,
            "patient_action",
            str((receipt.payload or {}).get("callback_token_hash", "")),
        ):
            return None
        if receipt.kind not in {"text", "voice", "photo", "document", "callback"}:
            return None
        if receipt.kind == "callback":
            row = self.store.get(
                receipt.scope,
                "patient_action",
                str((receipt.payload or {}).get("callback_token_hash", "")),
            )
            if row and row.body.get("evidence_id"):
                return self.evidence.patient_action(
                    receipt, principal, from_record(row, PatientAction)
                )
        return self.run(receipt, principal, binding)

    def valid(self, principal: Principal, binding: SubjectBinding) -> bool:
        return plan.authorized(self.store, principal, binding, self.runtime.clock()) is not None

    def _claim(self, receipt: InboundReceipt) -> tuple[InboundReceipt, Claim] | None:
        claim = self.store.claim_work(
            to_record(receipt, receipt.scope).scoped_key(receipt.scope),
            receipt.version,
            "concierge",
            self.runtime.clock(),
            self.runtime.accounts.policy.operations.claim_ttl,
        )
        row = self.store.get(receipt.scope, "inbound_receipt", receipt.id) if claim else None
        return (from_record(row, InboundReceipt), claim) if row and claim else None

    def _voice(
        self, receipt: InboundReceipt, principal: Principal, source: Provenance
    ) -> Transcript | None:
        if not self.media_factory or not self.speech_factory:
            return None
        retriever = self.media_factory(receipt, principal)
        retriever.failure_template = "patient_voice_unreadable"
        retriever.failure_text = templates.render("patient_voice_unreadable")
        row = self.store.get(receipt.scope, "media_work", keys.digest(receipt.id))
        work = from_record(row, MediaWork) if row else None
        if work and work.transcript_ref:
            try:
                return Transcript.model_validate_json(
                    retriever.media_store.get(retriever.scope, work.transcript_ref, 200000)
                )
            except Exception:
                return None
        media = fetch_telegram_file(
            receipt.provider_media_handle or "", retriever=retriever, receipt_id=receipt.id
        )
        if isinstance(media, MediaFailure):
            return None
        try:
            audio = retriever.media_store.get(
                retriever.scope, media.normalized_blob_ref, 20 * 1024 * 1024
            )
        except Exception:
            return None
        patient_row = (
            self.store.get(receipt.scope, "patient", receipt.scope.patient_id)
            if isinstance(receipt.scope, PatientScope)
            else None
        )
        from sanad.domain.language import default_language

        language = (
            str(patient_row.body.get("language", default_language))
            if patient_row
            else default_language
        )
        result = asyncio.run(
            transcribe(
                audio, "mp3", adapter=self.speech_factory(source), expected_language=language
            )
        )
        if not isinstance(result, Transcript):
            retriever.extraction_result(receipt.id, failure=result.reason)
            return None
        try:
            ref = retriever.media_store.put(
                retriever.scope, result.model_dump_json().encode(), "application/json"
            )
        except Exception:
            return None
        if not retriever.extraction_result(receipt.id, transcript_ref=ref):
            return None
        self.checkpoint("transcript_persisted")
        return result

    def _incident(
        self, receipt: InboundReceipt, verdict: ScreenVerdict | VitalVerdict | LabVerdict
    ) -> None:
        assert isinstance(receipt.scope, PatientScope)
        facts, severity = to_incident_facts(
            verdict,
            source=ObservationRef(observation_id=receipt.id),
            policy=self.runtime.safety_policy,
        )
        self.runtime.urgent.raise_incident(
            receipt.scope,
            facts.unique_source_key,
            facts.as_payload(),
            severity,
            self.runtime.clock(),
        )

    def run(
        self, receipt: InboundReceipt, principal: Principal, binding: SubjectBinding
    ) -> "RouteResult":
        from sanad.channels.telegram.router import RouteResult

        scope = receipt.scope
        if not isinstance(scope, PatientScope):
            return RouteResult(route="refused", status="patient_scope_required")
        now = self.runtime.clock()
        snapshot = plan.authorized(self.store, principal, binding, now)
        if snapshot is None:
            urgent_verdict = screen_text(
                str((receipt.payload or {}).get("text", "")), policy=self.runtime.safety_policy
            )
            if urgent_verdict.level == "danger":
                self._incident(receipt, urgent_verdict)
            result = self.runtime.inbound.process_inbound(
                to_record(receipt, scope).scoped_key(scope), "concierge-neutral", now
            )
            return RouteResult(
                route="patient",
                status=result.status if result else "busy",
                template_id="patient_safety_ack",
            )
        source = Provenance(
            source_observation_id=receipt.id,
            actor_kind="patient",
            actor_id=principal.subject,
            source_kind="patient_report",
            received_at=receipt.received_at,
        )
        text = str((receipt.payload or {}).get("text", ""))
        claimed = None
        transcript = None
        if receipt.kind == "voice":
            claimed = self._claim(receipt)
            if not claimed:
                return RouteResult(route="busy", status="processing")
            receipt, _ = claimed
            transcript = self._voice(receipt, principal, source)
            if transcript:
                text = transcript.text
                source = transcript.provenance[0]
        verdict = screen_text(text, policy=self.runtime.safety_policy)
        reading = reports.reading(text, self.runtime.safety_policy)
        from sanad.monitor.executor import parse_reading

        if any(m.kind == "MONITOR" for m in snapshot.missions):
            reading = parse_reading(text, self.runtime.safety_policy)
        from sanad.evidence.screen import screen_values

        value_incidents, _ = screen_values(
            self.runtime.urgent,
            scope,
            reading.values,
            receipt.id,
            receipt.received_at,
            self.runtime.safety_policy,
        )
        # Includes newly readable voice and spoken BP separators; no ordinary lease yet.
        danger = verdict if verdict.level == "danger" else reading.danger
        if danger and (
            not value_incidents or verdict.level == "danger" and verdict.rule_family == "phrase"
        ):
            self._incident(receipt, danger)
        elif (
            not danger
            and not value_incidents
            and verdict.level != "none"
            and any(
                f.kind == "MEDICATION_DAY3" and f.state == "waiting_response"
                for f in snapshot.followups
            )
        ):
            self._incident(receipt, verdict)
        lease = self.store.acquire_patient(
            scope,
            "concierge:" + uuid4().hex,
            self.runtime.clock(),
            self.runtime.steward.policy_provider(scope).operations.lease_ttl,
        )
        if not lease:
            return RouteResult(route="busy", status="patient_busy")
        try:
            claimed = claimed or self._claim(receipt)
            if not claimed:
                return RouteResult(route="busy", status="processing")
            receipt, claim = claimed
            snapshot = plan.authorized(self.store, principal, binding, self.runtime.clock())
            if snapshot is None:
                return RouteResult(
                    route="patient", status="authority_changed", template_id="patient_safety_ack"
                )
            tx = PatientTurnCommit(self.runtime.steward, snapshot, receipt, principal, claim, lease)
            session = FencedSessionManager(
                self.store,
                scope,
                "concierge:" + scope.patient_id,
                "concierge",
                lease,
                self.runtime.clock,
                safety_epoch=snapshot.profile.safety_epoch,
                source_order_versions=snapshot.order_refs,
                window=POLICY.window,
            )
            if danger or value_incidents:
                if reading.values:
                    reports.record_reading(tx, text, reading, source=source)
                    from sanad.concierge.monitor_reports import attach_reading

                    monitor_reply = attach_reading(tx, text, reading, source, danger=True)
                    if monitor_reply and tx.buttons:
                        result = tx.finish(*monitor_reply)
                        return RouteResult(
                            route="patient", status=result.status, template_id=monitor_reply[0]
                        )
                if receipt.kind in {"photo", "document"}:
                    self._decide(tx, text, verdict, reading, transcript, source, session)
                result = tx.finish("patient_emergency", "", emit=False)
                return RouteResult(
                    route="patient", status=result.status, template_id="patient_emergency"
                )
            template, reply, status = self._decide(
                tx, text, verdict, reading, transcript, source, session
            )
            result = tx.finish(
                template, reply, emit=not tx.builder.command.payload.get("media_reply_owned", False)
            )
            self.checkpoint("turn_persisted")
            if result.status in {"accepted", "duplicate"}:
                session.commit(text, reply)
            if receipt.kind == "callback":
                self.runtime.transport.answer_callback(
                    str((receipt.payload or {}).get("callback_query_id", "")), ""
                )
            return RouteResult(
                route="patient",
                status=status if result.status in {"accepted", "duplicate"} else result.status,
                template_id=template,
            )
        finally:
            self.store.release_patient(lease)
            if receipt.kind == "voice" and self.media_factory:
                row = self.store.get(scope, "inbound_receipt", receipt.id)
                if row and row.body.get("state") == "completed" and transcript:
                    self.media_factory(receipt, principal).extraction_result(
                        receipt.id, association_ref="patient-receipt:" + receipt.id
                    )

    def _decide(
        self,
        tx: PatientTurnCommit,
        text: str,
        verdict: ScreenVerdict,
        reading: reports.ReadingResult,
        transcript: Transcript | None,
        source: Provenance,
        session: FencedSessionManager,
    ) -> tuple[str, str, str]:
        language = tx.snapshot.patient.language

        def reply(key: str, **fields: str) -> tuple[str, str, str]:
            return key, templates.render(key, language, **fields), key

        if tx.receipt.kind in {"photo", "document"} or (
            tx.receipt.kind == "voice" and not transcript
        ):
            work = self.store.get(tx.snapshot.scope, "media_work", keys.digest(tx.receipt.id))
            if not work:
                tx.put(
                    MediaWork(
                        id=keys.digest(tx.receipt.id),
                        scope=tx.snapshot.scope,
                        receipt_id=tx.receipt.id,
                        provider_handle_ref=tx.receipt.provider_media_handle or "unavailable",
                        pending_mission_ids=tuple(
                            m.id
                            for m in tx.snapshot.missions
                            if m.objective_predicate.kind == "evidence"
                        ),
                        created_at=tx.now,
                        updated_at=tx.now,
                        work_clock=OperationalClock(next_action_at=tx.now, work_lane="media"),
                    )
                )
            if tx.receipt.kind == "voice" and work and work.body.get("resend_intent_id"):
                # The accepted media adapter already owns the single durable resend reply.
                tx.builder.command = tx.builder.command.model_copy(
                    update={"payload": {**tx.builder.command.payload, "media_reply_owned": True}}
                )
            return reply(
                "patient_voice_unreadable"
                if tx.receipt.kind == "voice"
                else "patient_evidence_received_pending"
            )
        if tx.receipt.kind == "callback":
            row = self.store.get(
                tx.snapshot.scope,
                "patient_action",
                str((tx.receipt.payload or {}).get("callback_token_hash", "")),
            )
            token = from_record(row, PatientAction) if row else None
            if (
                not token
                or token.actor_subject != tx.principal.subject
                or token.consumed_at
                or token.expires_at <= tx.now
                or token.delivery_epoch != tx.snapshot.profile.delivery_epoch
                or token.consent_version != tx.snapshot.consent.version
            ):
                return reply("patient_callback_stale")
            tx.consume(token)
            from sanad.concierge.monitor_reports import callback as monitor_callback

            monitor_choice = monitor_callback(tx, token, source)
            if monitor_choice:
                return *monitor_choice, monitor_choice[0]
            medication_choice = reports.medication_callback(tx, token, source)
            if medication_choice:
                key, fields = medication_choice
                return self._start_reply(tx) if key == "_start" else reply(key, **fields)
            if token.action == "resume":
                return reply(
                    preferences.apply(tx, preferences.Preference("resume"), confirmed=True)
                )
            if token.action == "quiet_slot":
                return reply(
                    preferences.apply(
                        tx, preferences.Preference("quiet"), slot_id=token.slot_id, confirmed=True
                    )
                )
            if token.action in {"visit_report", "task_report"}:
                from sanad.concierge import tasks, visits

                selected = next(
                    (
                        m
                        for m in tx.snapshot.missions
                        if to_record(m, tx.snapshot.scope).ref == token.target_ref
                        and all(r in tx.snapshot.order_refs for r in m.order_refs)
                    ),
                    None,
                )
                if not selected or not token.report_text:
                    return reply("patient_callback_stale")
                if token.action == "visit_report" and selected.kind == "VISIT":
                    return reply(
                        visits.record_visit(
                            tx,
                            selected,
                            token.report_text,
                            source=source,
                            original_receipt_id=token.source_receipt_id,
                        )
                    )
                if token.action == "task_report" and selected.kind == "TASK":
                    tasks.record_task_done(
                        tx,
                        selected,
                        token.report_text,
                        source=source,
                        original_receipt_id=token.source_receipt_id,
                    )
                    return reply("patient_task_recorded")
                return reply("patient_callback_stale")
            mission = next(
                (
                    m
                    for m in tx.snapshot.missions
                    if to_record(m, tx.snapshot.scope).ref == token.target_ref
                    and m.order_refs
                    and all(r in tx.snapshot.order_refs for r in m.order_refs)
                ),
                None,
            )
            if not mission:
                return reply("patient_callback_stale")
            reports.record_start(tx, mission, "بدأت الدوا", source=source)
            return self._start_reply(tx)
        if wants_treatment_change(text, policy=self.runtime.safety_policy):
            reports.record_day3(tx, text, source=source, verdict=verdict, treatment_change=True)
            question.open_ticket(tx, text)
            return reply("patient_treatment_change_relay")
        preference = preferences.parse(text)
        if preference:
            template = preferences.apply(tx, preference)
            if template == "patient_snooze_ack":
                assert tx.profile.routine_paused_until
                return reply(
                    template,
                    until=tx.profile.routine_paused_until.astimezone(
                        ZoneInfo(tx.snapshot.patient.timezone)
                    ).isoformat(),
                )
            if preference.action == "quiet" and preference.quiet:
                lines = [templates.render(template, language)]
                for slot, title, instant in preferences.scheduled_slots(tx):
                    time = instant[11:16]
                    start, end = preference.quiet
                    inside = start <= time < end if start < end else time >= start or time < end
                    if inside:
                        tx.button("quiet_slot", "أوافق على التذكير: " + title, slot=slot)
                        lines.append(title + " — " + instant + "؛ التذكير ده محتاج موافقتك لوحده.")
                return template, "\n".join(lines), "preference"
            return reply(template)
        if contains(
            text, "ابني", "بنتي", "زوجتي", "انا دكتور", "انا الدكتور", "I am a doctor", "my son"
        ):
            question.open_ticket(tx, text)
            return reply("patient_safe_fallback")
        if asks_doctor(text):
            question.open_ticket(tx, text)
            return reply("patient_question_forwarded")
        if transcript and transcript.disputed_numbers:
            return reply("patient_voice_unreadable")
        from sanad.concierge import tasks, visits

        if visits.is_booked(text) or visits.is_attended(text) or visits.not_attended(text):
            visit_choices = visits.visit_missions(tx.snapshot, text)
            if len(visit_choices) == 1:
                return reply(visits.record_visit(tx, visit_choices[0], text, source=source))
            if visit_choices:
                visits.choose(tx, visit_choices, text, "visit_report")
                return reply("patient_visit_choose")
            question.open_ticket(tx, text)
            return reply("patient_visit_missing")
        if tasks.is_task_done(text):
            task_choices = tasks.task_missions(tx.snapshot, text)
            if len(task_choices) == 1:
                tasks.record_task_done(tx, task_choices[0], text, source=source)
                return reply("patient_task_recorded")
            if task_choices:
                visits.choose(tx, task_choices, text, "task_report")
                return reply("patient_task_choose")
            question.open_ticket(tx, text)
            return reply("patient_task_missing")
        if not is_question(text) and reports.record_day3(tx, text, source=source, verdict=verdict):
            return reply("patient_day3_recorded")
        if reports.recognize_barrier(text) and reports.record_day3(
            tx, text, source=source, verdict=verdict
        ):
            return reply("patient_day3_recorded")
        medication_reply = reports.medication_reply(tx, text, source=source)
        if medication_reply:
            key, fields = medication_reply
            return self._start_reply(tx) if key == "_start" else reply(key, **fields)
        if reports.is_start(text):
            if reports.ambiguous_start_time(text):
                return reply("patient_start_date")
            missions = reports.start_missions(tx.snapshot, text)
            if len(missions) == 1:
                reports.record_start(tx, missions[0], text, source=source)
                return self._start_reply(tx)
            if missions:
                for mission in missions:
                    tx.button(
                        "start", mission.title, target=to_record(mission, tx.snapshot.scope).ref
                    )
                return reply("patient_start_choose")
            question.open_ticket(tx, text)
            return reply("patient_start_missing")
        if not is_question(text) and reading.incomplete_bp:
            return reply("patient_bp_incomplete")
        if not is_question(text) and reading.values:
            from sanad.concierge.monitor_reports import attach_reading, unsupported_review

            monitor_reply = attach_reading(tx, text, reading, source)
            if monitor_reply:
                return *monitor_reply, monitor_reply[0]
            if unsupported_review(tx, text, reading, source):
                return reply("patient_question_forwarded")
            reports.record_reading(tx, text, reading, source=source)
            # Only the measurement span is echoed, without patient-authored instructions.
            value = "، ".join(r.quoted for r in reading.values)
            if any(r.judgment == "implausible" for r in reading.values):
                body = templates.render("patient_reading_recorded", language, value=value)
                body += " " + templates.render("patient_reading_verify", language)
                return "patient_reading_recorded", body, "reading"
            return reply("patient_reading_recorded", value=value)
        # These are the exact reply words in the held-answer plan-update notice.
        if text.strip().casefold() in {"plan", "الخطة"}:
            return "patient_plan_summary", plan.render_summary(tx.snapshot), "plan"
        if plan_command(text):
            return "patient_plan_summary", plan.render_summary(tx.snapshot), "plan"
        history: tuple[str, ...] = ()
        if asks_history(text):
            history = tuple(
                plan.order_line(
                    from_record(r, CareOrderVersion),
                    language,
                    history=True,
                    names=tx.snapshot.names,
                )
                for r in records(self.store, tx.snapshot.scope, "care_order_version")
                if r.id not in {o.id for o in tx.snapshot.orders}
                and r.body.get("type") == "medication"
            )
        if history:
            return "patient_plan_summary", "\n".join(history), "history"
        entries = education.retrieve(text, synthetic=self.synthetic)
        bundle = answer.build_bundle(
            tx.snapshot, text, entries, tuple(t.model_dump() for t in session.turns), history
        )
        agent_scope = AgentScope(
            principal=tx.principal,
            scope=tx.snapshot.scope,
            source=source,
            policy=self.runtime.safety_policy,
            output_context=bundle.context,
            authority_check=lambda: self._current(tx),
        )
        agent = make_agent(
            "concierge",
            scope=agent_scope,
            tools=(),
            system_prompt=answer.SYSTEM_PROMPT,
            session_key=session.key,
            model_factory=self.model_factory,
            observe=self.observe,
        )
        composed = asyncio.run(answer.compose(text, bundle, agent, self.runtime.safety_policy))
        if composed.ticket:
            question.open_ticket(tx, text)
        if composed.failure:
            self.runtime.count("concierge_" + composed.failure)
        if composed.reply:
            body = composed.reply + (
                "\n" + templates.render("patient_question_forwarded", language)
                if composed.ticket
                else ""
            )
            if len(body) > POLICY.reply_max_chars:
                question.open_ticket(tx, text) if not composed.ticket else None
                return reply("patient_safe_fallback")
            return composed.template, body, composed.kind
        key = composed.template if composed.ticket else "patient_unavailable"
        return composed.template, templates.render(key, language), composed.kind

    def _start_reply(self, tx: PatientTurnCommit) -> tuple[str, str, str]:
        key = "patient_start_recorded"
        language = tx.snapshot.patient.language
        if not tx.profile.routine_contact_enabled:
            return key, templates.render("patient_start_stopped", language), "start"
        tasks = [
            from_record(r, FollowUpTask)
            for r in tx.builder.puts.values()
            if r.entity_type == "followup"
        ]
        parents = {r.id for r in tx.builder.puts.values() if r.entity_type == "mission"}
        task = next(
            (
                f
                for f in (*tasks, *tx.snapshot.followups)
                if f.kind == "MEDICATION_DAY3" and f.parent_mission_id in parents
            ),
            None,
        )
        if task and task.state == "contact_suppressed":
            return key, templates.render("patient_start_review", language), "start"
        text = templates.render(key, language)
        if task and task.prompt_at:
            text += (
                " " + task.prompt_at.astimezone(ZoneInfo(tx.snapshot.patient.timezone)).isoformat()
            )
        return key, text, "start"

    def sweep(self, row: StoredRecord) -> None:
        """Recover persisted media and screen evidence before association."""
        if row.entity_type != "media_work" or not self.media_factory:
            return
        work = from_record(row, MediaWork)
        receipt_row = self.store.get(work.scope, "inbound_receipt", work.receipt_id)
        if not receipt_row:
            return
        receipt = from_record(receipt_row, InboundReceipt)
        auth = self.store.authorize(self.runtime.settings.bot_id, receipt.source_subject)
        if not auth.binding or not self.valid(auth.principal, auth.binding):
            return
        if (
            receipt.kind in {"photo", "document"}
            and self.vision_factory
            and work.state != "needs_attention"
        ):
            self.evidence.run(receipt, auth.principal)
            return
        retriever = self.media_factory(receipt, auth.principal)
        retriever.failure_template = (
            "patient_voice_unreadable" if receipt.kind == "voice" else "patient_evidence_unreadable"
        )
        patient_row = self.store.get(work.scope, "patient", auth.principal.patient_id or "")
        if not patient_row:
            return
        from sanad.store.records import Patient

        retriever.failure_text = templates.render(
            retriever.failure_template, from_record(patient_row, Patient).language
        )
        if receipt.kind == "voice" and receipt.state == "completed" and work.transcript_ref:
            retriever.extraction_result(receipt.id, association_ref="patient-receipt:" + receipt.id)
        else:
            retriever.sweep()

    def _current(self, tx: PatientTurnCommit) -> bool:
        auth = self.store.authorize(tx.snapshot.binding.bot_id, tx.principal.subject)
        current = (
            plan.authorized(self.store, tx.principal, auth.binding, self.runtime.clock())
            if auth.binding
            else None
        )
        return bool(
            current
            and current.profile.safety_epoch == tx.snapshot.profile.safety_epoch
            and current.profile.delivery_epoch == tx.snapshot.profile.delivery_epoch
            and current.order_refs == tx.snapshot.order_refs
            and current.profile.lease_generation == tx.lease.generation
            and current.profile.lease_expires_at
            and current.profile.lease_expires_at > self.runtime.clock()
        )
