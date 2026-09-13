"""Bounded doctor turn: recoverable receipt, one extraction, one saved card."""

import asyncio
import logging
from collections.abc import Callable
from time import monotonic
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx
from pydantic import BaseModel, JsonValue
from strands.models import Model

from sanad.agents.factory import Proposal as ModelProposal
from sanad.agents.factory import ProposalFailure, bedrock_model, make_agent
from sanad.agents.tools import AgentScope, drug_lookup_tool
from sanad.auth.claim import ClaimService
from sanad.auth.service import read_of, revise
from sanad.auth.tokens import issue_token
from sanad.channels.telegram import wording
from sanad.channels.telegram.router import RouteResult
from sanad.domain import DRAFT_POLICY_2026_09, PatientScope, Principal, Provenance, TenantScope
from sanad.domain.language import effective as contest_language
from sanad.media.audio import ConvertedAudio
from sanad.media.numbers import numbers_in
from sanad.media.retrieve import MediaRetriever, fetch_telegram_file, invoked
from sanad.media.speech import SpeechAdapter, Transcript
from sanad.media.telegram import MediaFailure
from sanad.models.io import CallMetadata, ModelUnavailable
from sanad.models.registry import ModelRegistry, ModelRole
from sanad.models.timeouts import EXTRACTION_TIMEOUT as EXTRACTION_TIMEOUT
from sanad.safety import screen_text
from sanad.scribe import amend
from sanad.scribe.change_binding import SourcePartition, append_reply, partition_for
from sanad.scribe.commit import ConfirmationResult, ScribeCommit
from sanad.scribe.corrections import (
    NEW_PATIENT,
    correction_request,
    merge_correction,
    patient_answer,
    reply_mode,
)
from sanad.scribe.crosscheck import PhotoReview, render_card, review_issues
from sanad.scribe.extract import (
    CORRECTION_PROMPT_VERSION,
    PROMPT_VERSION,
    DictationCandidate,
    EnglishDictationCandidate,
    PatientCandidate,
    ProposalIssue,
    candidate_issues,
    derive_intent,
    missing_request,
    scribe_prompt,
)
from sanad.scribe.lookup import DrugLookupService, LookupBudget
from sanad.scribe.memory import NameVocabulary
from sanad.scribe.patients import lookup
from sanad.scribe.policy import DRAFT_SCRIBE_POLICY
from sanad.scribe.proposal import (
    InvitationWork,
    PendingReply,
    Proposal,
    ScribeCallback,
    ScribeState,
)
from sanad.scribe.qr import issue_qr
from sanad.scribe.repository import ScribeRepository
from sanad.scribe.timing import candidate_timings
from sanad.steward.types import records
from sanad.store.records import (
    Authorization,
    Claim,
    Doctor,
    IdentityRead,
    InboundReceipt,
    MediaWork,
    OperationalClock,
    PhotoAssociationWork,
    StoredRecord,
    from_record,
    to_record,
)

if TYPE_CHECKING:
    from sanad.channels.telegram.router import TelegramRuntime
    from sanad.media.vision import VisionAdapter

DEFAULT_REGISTRY = ModelRegistry()
logger = logging.getLogger(__name__)


def dictation_model(registry: ModelRegistry, role: ModelRole) -> Model:
    return bedrock_model(registry, role, timeout=EXTRACTION_TIMEOUT)


def parse_command(text: str) -> tuple[str | None, str]:
    parts = text.strip().split(maxsplit=1)
    if parts and parts[0] in {
        "/start",
        "/help",
        "/new",
        "/find",
        "/qr",
        "/cancel",
        "/intake",
        "/lang",
        "/digest",
        "/inbox",
        "/resolve",
        "/questions",
        "/send",
        "/defer",
        "/reuse",
        "/answer",
        "/close",
    }:
        return parts[0], parts[1].strip() if len(parts) > 1 else ""
    return None, text


class ScribeTurn:
    def __init__(
        self,
        runtime: "TelegramRuntime",
        claims: ClaimService,
        *,
        model_factory: Callable[[ModelRegistry, ModelRole], Model] = dictation_model,
        speech_factory: Callable[[Provenance], SpeechAdapter] | None = None,
        vision_factory: Callable[[Provenance], "VisionAdapter"] | None = None,
        media_factory: Callable[[InboundReceipt, Principal], MediaRetriever] | None = None,
        registry: ModelRegistry = DEFAULT_REGISTRY,
        observe: Callable[[CallMetadata], None] = lambda metadata: None,
        checkpoint: Callable[[str], None] = lambda name: None,
        rxnorm_client: httpx.Client | None = None,
        observe_retry: Callable[[str], None] = lambda reason: None,
    ):
        self.runtime, self.claims = runtime, claims
        self.repo = ScribeRepository(runtime.store, runtime.clock)
        self.committer = ScribeCommit(self.repo, runtime.steward, claims)
        self.model_factory, self.speech_factory, self.media_factory = (
            model_factory,
            speech_factory,
            media_factory,
        )
        self.registry, self.observe, self.checkpoint = registry, observe, checkpoint
        self.vision_factory = vision_factory
        self.rxnorm_client = rxnorm_client
        self.observe_retry = observe_retry
        from sanad.evidence.photos import AlertPhotoTurn

        self.photos = AlertPhotoTurn(self)

    def __call__(self, receipt: InboundReceipt, auth: Authorization) -> RouteResult | None:
        callback_hash = str((receipt.payload or {}).get("callback_token_hash", ""))
        doctor_id = auth.principal.doctor_id or (auth.binding.doctor_id if auth.binding else None)
        token = (
            self.repo.load(
                TenantScope(doctor_id=doctor_id), "scribe_callback", callback_hash, ScribeCallback
            )
            if doctor_id and callback_hash
            else None
        )
        from sanad.concierge.answer_command import task_route
        from sanad.evidence.doctor import route as evidence_route

        task_result = task_route(self, receipt, auth)
        if task_result is not None:
            return task_result

        from sanad.evidence.correction_doctor import route as correction_route

        correction_result = correction_route(self, receipt, auth)
        if correction_result is not None:
            return correction_result
        evidence_result = evidence_route(self, receipt, auth)
        if evidence_result is not None:
            return evidence_result
        photo_result = self.photos.route(receipt, auth)
        if photo_result is not None:
            return photo_result
        if receipt.kind == "callback" and token:
            return self._callback(receipt, auth.principal, token)
        if auth.principal.actor_kind == "doctor" and receipt.kind in {"text", "voice"}:
            return self.run(receipt, auth.principal)
        return None

    def _claim(self, receipt: InboundReceipt) -> tuple[InboundReceipt, Claim] | None:
        claim = self.repo.store.claim_work(
            to_record(receipt, receipt.scope).scoped_key(receipt.scope),
            receipt.version,
            "scribe",
            self.repo.clock(),
            self.runtime.accounts.policy.operations.claim_ttl,
        )
        if claim is None:
            return None
        current = self.repo.store.get(receipt.scope, "inbound_receipt", receipt.id)
        assert current is not None
        return from_record(current, InboundReceipt), claim

    def _reply(
        self,
        receipt: InboundReceipt,
        actor: Principal,
        claim: Claim,
        template: str,
        status: str,
        *,
        text: str | None = None,
    ) -> RouteResult:
        doctor = self.claims.doctor(actor)
        if doctor is None:
            self.runtime.accounts.finish_receipt(receipt, claim, result_code=status)
            return RouteResult(route="doctor", status=status, template_id=template)
        intent = self.repo.intent(
            doctor,
            template,
            {"text": text if text is not None else self.wording(template, doctor)},
            "reply:" + receipt.id,
        )
        result = self.repo.commit(
            actor,
            "ScribeReply",
            "reply:" + receipt.id,
            intents=(intent,),
            claim=claim,
            reason=status,
        )
        return RouteResult(
            route="doctor",
            status=status if result.status in {"accepted", "duplicate"} else result.status,
            template_id=template,
        )

    def _preference_refused(
        self, receipt: InboundReceipt, claim: Claim, setting: str
    ) -> RouteResult:
        # A refused preference is a completed observation, even if authority changed.
        # Account delivery revalidates the recipient and latest doctor epoch.
        doctor = self.runtime.accounts.doctor(
            receipt.principal.doctor_id or "" if receipt.principal else ""
        )
        template = (
            "doctor_suspended_notice"
            if doctor and doctor.status == "suspended"
            else "scribe_" + setting
            if doctor
            else "claim_refused"
        )
        result = self.runtime.accounts.finish_receipt(
            receipt,
            claim,
            result_code="forbidden",
            template_id=template,
            text=f"The {setting} setting was refused because your account authority "
            "or setting changed. Refresh and try again.",
        )
        return RouteResult(route="doctor", status=result.status, template_id=template)

    @staticmethod
    def wording(template: str, doctor: Doctor) -> str:
        from sanad.scribe.english import render_wording

        return render_wording(template, contest_language(doctor.language))

    def run(self, receipt: InboundReceipt, principal: Principal) -> RouteResult:
        try:
            return self._run(receipt, principal)
        finally:
            self._finish_media(receipt, principal)

    def _finish_media(self, receipt: InboundReceipt, actor: Principal) -> None:
        if receipt.kind not in {"voice", "photo", "document"} or self.media_factory is None:
            return
        row = self.repo.store.get(receipt.scope, "inbound_receipt", receipt.id)
        if row is None or row.body.get("state") != "completed":
            return
        retriever = self.media_factory(receipt, actor)
        retriever.extraction_result(receipt.id, association_ref="doctor-receipt:" + receipt.id)

    def _run(self, receipt: InboundReceipt, principal: Principal) -> RouteResult:
        claimed = self._claim(receipt)
        if claimed is None:
            return RouteResult(route="busy", status="processing")
        receipt, claim = claimed
        doctor = self.claims.doctor(principal)
        if doctor is None:
            return self._reply(receipt, principal, claim, "scribe_stale", "forbidden")
        vocabulary = NameVocabulary(self.repo.store, doctor)
        state = self.repo.state(doctor.scope)
        previous = self.repo.pending(doctor.scope)
        if previous and previous.expires_at <= self.repo.clock():
            editing_expired = previous.editing
            self.committer.expire(previous)
            previous, state = None, self.repo.state(doctor.scope)
            if editing_expired:
                return self._reply(receipt, principal, claim, "scribe_expired", "expired")
        text = str((receipt.payload or {}).get("text", ""))
        source = Provenance(
            source_observation_id=receipt.id,
            actor_kind="doctor",
            actor_id=principal.subject,
            source_kind="doctor_statement",
            received_at=receipt.received_at,
        )
        transcript: Transcript | None = None
        transcript_ref = None
        retriever = None
        if receipt.kind == "voice":
            if self.media_factory is None or self.speech_factory is None:
                return self._reply(
                    receipt, principal, claim, "doctor_voice_unreadable", "media_unavailable"
                )
            retriever = self.media_factory(receipt, principal)
            retriever.failure_template = "doctor_voice_unreadable"
            retriever.failure_text = wording.render("doctor_voice_unreadable", doctor.language)
            media = fetch_telegram_file(
                receipt.provider_media_handle or "", retriever=retriever, receipt_id=receipt.id
            )
            if isinstance(media, MediaFailure):
                if media.durable:
                    self.runtime.accounts.finish_receipt(receipt, claim, result_code=media.reason)
                    return RouteResult(
                        route="doctor", status=media.reason, template_id="doctor_voice_unreadable"
                    )
                return self._reply(
                    receipt, principal, claim, "doctor_voice_unreadable", media.reason
                )
            try:
                audio = retriever.media_store.get(
                    retriever.scope, media.normalized_blob_ref, 20 * 1024 * 1024
                )
            except Exception:
                return self._reply(
                    receipt, principal, claim, "doctor_voice_unreadable", "storage_unavailable"
                )
            speech = self.speech_factory(source)
            speech.vocabulary_hint = vocabulary.hint()
            speech_result = asyncio.run(
                speech.transcribe_converted(
                    ConvertedAudio(data=audio, duration=media.duration or 0),
                    expected_language=contest_language(doctor.language),
                )
            )
            if not isinstance(speech_result, Transcript):
                failure = retriever.extraction_result(receipt.id, failure=speech_result.reason)
                if isinstance(failure, MediaFailure) and failure.durable:
                    self.runtime.accounts.finish_receipt(
                        receipt, claim, result_code=speech_result.reason
                    )
                    return RouteResult(
                        route="doctor",
                        status=speech_result.reason,
                        template_id="doctor_voice_unreadable",
                    )
                return self._reply(
                    receipt, principal, claim, "doctor_voice_unreadable", speech_result.reason
                )
            transcript, text = speech_result, speech_result.text
            try:
                transcript_ref = retriever.media_store.put(
                    retriever.scope, transcript.model_dump_json().encode(), "application/json"
                )
            except Exception:
                return self._reply(
                    receipt, principal, claim, "doctor_voice_unreadable", "storage_unavailable"
                )
            if not retriever.extraction_result(receipt.id, transcript_ref=transcript_ref):
                return self._reply(
                    receipt, principal, claim, "doctor_voice_unreadable", "stale_work"
                )
            if screen_text(text, policy=self.runtime.safety_policy).level == "danger":
                return self._reply(
                    receipt,
                    principal,
                    claim,
                    "doctor_voice_unreadable",
                    "safety_response",
                    text=self.runtime.general("patient_emergency"),
                )
        from sanad.media.speech import normalize_transcript

        text = normalize_transcript(text, contest_language(doctor.language))
        command, argument = parse_command(text)
        if command in {
            "/questions",
            "/answer",
            "/close",
            "/send",
            "/defer",
            "/reuse",
            "/inbox",
            "/resolve",
        }:
            from sanad.concierge.answer_command import doctor_command

            return doctor_command(self, receipt, principal, claim, doctor, command, argument)
        if command == "/digest":
            from sanad.contact.question_digest import (
                LIMIT,
                due,
                next_instant,
                parse_setting,
                setting_schedule,
            )

            values = parse_setting(argument, doctor)
            if values is None:
                return self._reply(
                    receipt,
                    principal,
                    claim,
                    "scribe_digest_usage",
                    "invalid_input",
                    text=wording.render("scribe_digest_usage", doctor.language),
                )
            now = self.repo.clock()
            changed = revise(doctor, now, **values) if argument else doctor
            from zoneinfo import ZoneInfo

            zone = ZoneInfo("Africa/Cairo")
            count = len(due(self.repo.store, doctor.scope)[:LIMIT])
            english = contest_language(doctor.language) == "en"
            waiting = (
                (f"{count} questions are waiting." if count else "Nothing is waiting right now.")
                if english
                else (f"{count} أسئلة في الانتظار." if count else "مفيش أسئلة في الانتظار دلوقتي.")
            )
            if argument:
                next_at = next_instant(changed, now).astimezone(zone)
                today = next_at.date() == now.astimezone(zone).date()
                when = (
                    ("today" if today else "tomorrow")
                    if english
                    else ("النهارده" if today else "بكرة")
                )
                text = (
                    f"Next digest: {when} at {next_at:%H:%M} Cairo. {waiting}"
                    if english
                    else f"الملخص الجاي: {when} الساعة {next_at:%H:%M} بتوقيت القاهرة. {waiting}"
                )
            else:
                text = wording.render(
                    "scribe_digest",
                    contest_language(doctor.language),
                    time=next_instant(changed, now).astimezone(zone).strftime("%H:%M"),
                    packing=("one message" if changed.digest_packing == "one" else "each question")
                    if english
                    else ("رسالة واحدة" if changed.digest_packing == "one" else "كل سؤال لوحده"),
                    waiting=waiting,
                )
            if not argument:
                return self._reply(receipt, principal, claim, "scribe_digest", "setting", text=text)
            schedule = setting_schedule(self.repo.store, changed, now)
            intent = self.repo.intent(
                changed, "scribe_digest", {"text": text}, "digest:" + receipt.id
            )
            digest_result = self.repo.commit(
                principal,
                "ScribeDigest",
                "digest:" + receipt.id,
                (changed,) + ((schedule,) if schedule else ()),
                (intent,),
                claim=claim,
            )
            if digest_result.status == "forbidden":
                return self._preference_refused(receipt, claim, "digest")
            return RouteResult(
                route="doctor", status=digest_result.status, template_id="scribe_digest"
            )
        if command == "/lang":
            if argument not in {"en", "ar"}:
                return self._reply(
                    receipt,
                    principal,
                    claim,
                    "scribe_help",
                    "invalid_language",
                    text="Use /lang en or /lang ar.",
                )
            changed = revise(doctor, self.repo.clock(), language=argument)
            intent = self.repo.intent(
                changed,
                "scribe_language",
                {
                    "text": (
                        "The app currently shows English. "
                        "Your Arabic preference is saved for later."
                        if argument == "ar" and contest_language(changed.language) == "en"
                        else "Language set to English."
                        if contest_language(changed.language) == "en"
                        else "تم اختيار العربية."
                    )
                },
                "language:" + receipt.id,
            )
            language_result = self.repo.commit(
                principal,
                "ScribeLanguage",
                "language:" + receipt.id,
                (changed,),
                (intent,),
                claim=claim,
            )
            if language_result.status == "forbidden":
                return self._preference_refused(receipt, claim, "language")
            return RouteResult(
                route="doctor", status=language_result.status, template_id="scribe_language"
            )
        if command is None and text.lstrip().startswith("/"):
            return self._reply(receipt, principal, claim, "scribe_help", "unknown_command")
        if command == "/intake":
            from sanad.store.records import IntakeDraft

            drafts = [
                from_record(r, IntakeDraft)
                for r in records(self.repo.store, doctor.scope, "intake_draft")
                if r.body.get("state") == "pending"
            ]
            if drafts:
                return self.photos.pending(receipt, principal, claim, doctor, drafts[0])
            return self._reply(
                receipt,
                principal,
                claim,
                "scribe_card",
                "no_intake",
                text=wording.label("no_intake", doctor.language),
            )
        if command in {"/start", "/help"}:
            return self._reply(
                receipt,
                principal,
                claim,
                "doctor_welcome_back" if command == "/start" else "doctor_help",
                "command",
            )
        if command == "/cancel":
            if previous:
                result = self.committer.reject(
                    previous, principal, "cancel:" + receipt.id, claim=claim
                )
                return RouteResult(
                    route="doctor", status=result.status, template_id=result.template
                )
            return self._reply(receipt, principal, claim, "scribe_discarded", "cancelled")
        if command == "/qr":
            choices = lookup(
                self.repo.store, doctor.scope, PatientCandidate(name_as_spoken=argument or None)
            )
            if len(choices) == 1 and argument and choices[0].score == 3:
                qr_result = issue_qr(
                    self.claims, principal, choices[0].patient_id, "qr:" + receipt.id
                )
                if qr_result.status in {"issued", "duplicate"}:
                    self.runtime.accounts.finish_receipt(
                        receipt, claim, result_code="invitation_issued"
                    )
                    return RouteResult(
                        route="doctor", status="invitation_issued", template_id="scribe_invitation"
                    )
            # Ambiguous QR requests use the same selection card and never guess an id.
            if not choices:
                return self._reply(
                    receipt, principal, claim, "doctor_patient_not_found", "not_found"
                )
        mode = (
            reply_mode(text, previous, self.repo.store) if previous and command is None else "new"
        )
        if mode == "choose" and previous:
            return self._choose_reply(
                receipt,
                principal,
                claim,
                doctor,
                previous,
                PendingReply(
                    text=text,
                    source=source,
                    disputed=transcript.disputed_numbers if transcript else (),
                    heard=transcript.heard_numbers if transcript else (),
                    transcript_ref=transcript_ref,
                ),
            )
        correction = previous if previous and command is None and mode == "correction" else None
        service = self.name_lookup(doctor, principal, vocabulary, correction)
        correction_issues: tuple[ProposalIssue, ...] = ()
        resolved_numbers: tuple[str, ...] = ()
        manual_photo = (
            self.photos.manual_edit(correction, text) if correction and correction.photo else None
        )
        if manual_photo is not None:
            candidate = manual_photo
            provenance = (*correction.source_provenance, source) if correction else (source,)
        elif command in {"/new", "/find", "/qr"}:
            candidate = DictationCandidate(
                patient=PatientCandidate(name_as_spoken=argument or None),
            )
            provenance = (source,)
        else:
            extracted = self.extract_candidate(
                receipt.id, principal, doctor, source, text, correction, service
            )
            if not isinstance(extracted, ModelProposal):
                return self._reply(
                    receipt, principal, claim, "doctor_model_unavailable", "model_unavailable"
                )
            candidate = extracted.value
            if correction and not correction.photo:
                merged = merge_correction(correction, candidate, text)
                candidate, correction_issues, resolved_numbers = (
                    merged.candidate,
                    merged.issues,
                    merged.resolved_numbers,
                )
            provenance = (
                *(correction.source_provenance if correction else ()),
                *tuple(
                    Provenance.model_validate(
                        p.model_dump()
                        | {
                            "prompt_version": CORRECTION_PROMPT_VERSION
                            if correction
                            else PROMPT_VERSION,
                            "source_span": None,
                        }
                    )
                    for p in extracted.provenance
                ),
            )
        source_partition = (
            append_reply(correction, text)
            if correction
            else SourcePartition(original_end=len(text))
        )
        original = correction.source_text + "\n" + text if correction else text
        disputed = transcript.disputed_numbers if transcript else ()
        if correction:
            explicitly_corrected = set(numbers_in(text)) - set(disputed)
            disputed = tuple(
                dict.fromkeys(
                    (
                        *disputed,
                        *(n for n in correction.disputed_numbers if n not in explicitly_corrected),
                    )
                )
            )
        return self._propose(
            receipt,
            principal,
            claim,
            doctor,
            candidate,
            original,
            state,
            previous,
            provenance,
            disputed,
            transcript.heard_numbers if transcript else (),
            transcript_ref,
            command == "/qr" or "عايز أبعت له اللينك" in text,
            correction is not None,
            command,
            photo=self.photos.corrected_review(correction, candidate, text)
            if correction and correction.photo
            else None,
            names_service=service,
            correction_issues=correction_issues,
            resolved_numbers=resolved_numbers,
            source_partition=source_partition,
        )

    def name_lookup(
        self,
        doctor: Doctor,
        actor: Principal,
        vocabulary: NameVocabulary | None = None,
        previous: Proposal | None = None,
    ) -> DrugLookupService:
        from sanad.scribe.patients import panel

        names = tuple(p.display_name for p in panel(self.repo.store, doctor.scope))
        if previous and previous.candidate.patient.name_as_spoken:
            names += (previous.candidate.patient.name_as_spoken,)
        return DrugLookupService(
            self.repo,
            vocabulary or NameVocabulary(self.repo.store, doctor),
            actor,
            client=self.rxnorm_client,
            budget=LookupBudget(previous.rxnorm_calls if previous else 0),
            forbidden_names=names,
        )

    def extract_candidate(
        self,
        receipt_id: str,
        actor: Principal,
        doctor: Doctor,
        source: Provenance,
        text: str,
        correction: Proposal | None,
        service: DrugLookupService,
    ) -> object:
        scope = AgentScope(
            principal=actor,
            scope=doctor.scope,
            source=source,
            policy=self.runtime.safety_policy,
            authority_check=lambda: self.claims.doctor(actor) is not None,
        )
        service.patient_identity_pending = True
        request = correction_request(correction, text) if correction else text
        prompt = scribe_prompt(
            service.vocabulary.hint(),
            correction=correction is not None,
            language=contest_language(doctor.language),
        )
        from sanad.scribe.merge import merge_candidates
        from sanad.scribe.resolver import context

        async def extract_twice() -> object:
            deadline = monotonic() + EXTRACTION_TIMEOUT
            attempts = [0, 0]

            async def reading(
                index: int, pending: ModelProposal[DictationCandidate] | None = None
            ) -> object:
                reason = "request_missing" if pending else ""
                for attempt in range(attempts[index], 2):
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        return pending or ModelUnavailable(reason="timeout")
                    if attempt:
                        logger.info("scribe_extraction_retry attempt=2 reason=%s", reason)
                        self.observe_retry(reason)
                    attempts[index] += 1
                    agent = make_agent(
                        "scribe",
                        scope=scope,
                        tools=(drug_lookup_tool(scope, service.lookup_drug),),
                        system_prompt=prompt,
                        session_key="scribe:" + receipt_id,
                        registry=self.registry,
                        model_factory=self.model_factory,
                        observe=self.observe,
                    )
                    result = await agent.propose(
                        EnglishDictationCandidate
                        if contest_language(doctor.language) == "en"
                        and not (correction and correction.photo)
                        else DictationCandidate,
                        request,
                        want_spans=False,
                        timeout=remaining,
                    )
                    transient = (
                        isinstance(result, ProposalFailure) and result.reason == "schema_validation"
                    ) or (isinstance(result, ModelUnavailable) and result.reason == "unavailable")
                    if not transient or attempt:
                        return (
                            pending if pending and not isinstance(result, ModelProposal) else result
                        )
                    reason = (
                        "schema_validation"
                        if isinstance(result, ProposalFailure)
                        else "model_unavailable"
                    )
                raise AssertionError("bounded extraction must return")

            # Two fresh agents, identical request and independent timeouts inside
            # one unchanged worker deadline. A peer failure never cancels a survivor.
            if correction and correction.photo:
                # Photo correction keeps its accepted single-extraction path.
                return await reading(0)
            results = list(await asyncio.gather(reading(0), reading(1), return_exceptions=True))
            for primary in range(len(results)):
                if not isinstance(results[primary], ModelProposal):
                    continue
                before = results[primary]
                assert isinstance(before, ModelProposal)
                peer_requests = any(
                    isinstance(peer, ModelProposal)
                    and any(m.kind == "TEST" for m in peer.value.missions)
                    for peer in results
                ) and not any(m.kind == "TEST" for m in before.value.missions)
                if attempts[primary] < 2 and (missing_request(before.value, text) or peer_requests):
                    try:
                        retried = await reading(primary, before)
                    except Exception:
                        # Keep the durable clarification card on a retry setup or
                        # adapter failure; never log the provider's raw exception.
                        logger.warning("scribe_request_retry_failed")
                        retried = before
                    if isinstance(retried, ModelProposal):
                        from sanad.scribe.extract import extracted_numbers

                        represented = set(extracted_numbers(retried.value))
                        retried.value._dropped_numbers = tuple(
                            dict.fromkeys(
                                (
                                    *before.value._dropped_numbers,
                                    *retried.value._dropped_numbers,
                                    *(
                                        n
                                        for n in extracted_numbers(before.value)
                                        if n not in represented
                                    ),
                                )
                            )
                        )
                    results[primary] = retried
            good = [r for r in results if isinstance(r, ModelProposal)]
            if not good:
                return next(
                    (r for r in results if isinstance(r, (ProposalFailure, ModelUnavailable))),
                    ModelUnavailable(reason="unavailable"),
                )
            values = [r.value if isinstance(r, ModelProposal) else None for r in results]
            from sanad.scribe.patients import panel

            merged = merge_candidates(
                values[0],
                values[1],
                text,
                context(service),
                panel_names=tuple(p.display_name for p in panel(self.repo.store, doctor.scope)),
            )
            assert merged is not None
            return good[0].model_copy(
                update={
                    "value": merged.candidate,
                    "provenance": tuple(p for r in good for p in r.provenance),
                    "metadata": tuple(m for r in good for m in r.metadata),
                }
            )

        return asyncio.run(extract_twice())

    def _choose_reply(
        self,
        receipt: InboundReceipt,
        actor: Principal,
        claim: Claim,
        doctor: Doctor,
        previous: Proposal,
        reply: PendingReply,
    ) -> RouteResult:
        changed = revise(previous, self.repo.clock(), pending_reply=reply)
        tokens, markup = self.buttons(changed, actor, "")
        intent = self.repo.intent(
            doctor,
            "scribe_card",
            {"text": wording.label("choose_reply", doctor.language), "reply_markup": markup},
            f"{changed.id}:{changed.version}",
            proposal=changed,
        )
        state = self.repo.state(doctor.scope)
        assert state is not None
        result = self.repo.commit(
            actor,
            "ScribePropose",
            "reply-choice:" + receipt.id,
            (changed, revise(state, self.repo.clock()), *tokens),
            (intent,),
            claim=claim,
        )
        return RouteResult(route="doctor", status=result.status, template_id="scribe_card")

    def _propose(
        self,
        receipt: InboundReceipt,
        actor: Principal,
        claim: Claim,
        doctor: Doctor,
        candidate: DictationCandidate,
        source_text: str,
        state: ScribeState | None,
        previous: Proposal | None,
        provenance: tuple[Provenance, ...],
        disputed: tuple[str, ...],
        heard: tuple[str, ...],
        transcript_ref: str | None,
        invitation: bool,
        correction: bool,
        command: str | None,
        *,
        photo: PhotoReview | None = None,
        selected_id: str | None = None,
        extra_models: tuple[BaseModel, ...] = (),
        extra_reads: tuple[IdentityRead, ...] = (),
        proposal_id: str | None = None,
        intake_fence: Claim | None = None,
        names_service: DrugLookupService | None = None,
        correction_issues: tuple[ProposalIssue, ...] = (),
        resolved_numbers: tuple[str, ...] = (),
        source_partition: SourcePartition | None = None,
    ) -> RouteResult:
        now = self.repo.clock()
        from sanad.scribe.crosscheck import grade_row, lab_text
        from sanad.scribe.names import NameReading, prepare_names

        source_partition = source_partition or SourcePartition(original_end=len(source_text))
        name_issues: tuple[ProposalIssue, ...] = ()
        clinical_issues: tuple[ProposalIssue, ...] = ()
        names: list[NameReading] = []
        if not photo:
            candidate._merge_issues = tuple(
                dict.fromkeys(
                    (
                        *candidate._merge_issues,
                        *(i for i in correction_issues if i.code == "extraction_conflict"),
                    )
                )
            )
            correction_issues = tuple(
                i for i in correction_issues if i.code != "extraction_conflict"
            )
            from sanad.scribe.clinical import prepare_clinical

            service = names_service or self.name_lookup(
                doctor, actor, previous=previous if correction else None
            )
            if candidate.patient.name_as_spoken:
                from sanad.scribe.names import normalize

                service.forbidden_names += (normalize(candidate.patient.name_as_spoken),)
            service.patient_identity_pending = False
            clarified_tests: frozenset[str] = frozenset()
            if correction and previous:
                from sanad.scribe.resolver import context, test_reply_resolves

                # Item-level correction authority does not settle its analyte:
                # a timing answer may also cause the model to reword the list.
                prefix = previous.source_text + "\n"
                reply = source_text[len(prefix) :] if source_text.startswith(prefix) else ""
                settled = set()
                for i, (before_test, after_test) in enumerate(
                    zip(previous.candidate.missions, candidate.missions, strict=False)
                ):
                    if before_test.kind != "TEST" or after_test.kind != "TEST":
                        continue
                    item = f"mission:{i}"
                    prior_names = tuple(
                        n.latin for n in previous.names if n.item == item and n.kind == "test"
                    )
                    unanswered = any(
                        q.item == item and q.field == "analyte" for q in previous.issues
                    )
                    if (prior_names and not unanswered) or (
                        before_test.text != after_test.text
                        and test_reply_resolves(
                            after_test.text, prior_names, reply, source_text, context(service)
                        )
                    ):
                        settled.add(item)
                clarified_tests = frozenset(settled)
            from sanad.scribe.change_binding import with_context
            from sanad.scribe.resolver import context

            source_partition = with_context(source_partition, source_text, context(service))
            candidate, clinical_issues = prepare_clinical(
                candidate,
                source_text,
                service,
                names,
                language=contest_language(doctor.language),
                clarified_tests=clarified_tests,
                partition=source_partition,
                previous=previous if correction else None,
            )
            verified_names = {}
            if correction and previous:
                for i, (before, after) in enumerate(
                    zip(previous.candidate.orders, candidate.orders, strict=False)
                ):
                    if any(
                        getattr(before, field) != getattr(after, field)
                        for field in ("drug", "name_latin", "generic")
                    ):
                        continue
                    reading = next(
                        (
                            n
                            for n in previous.names
                            if n.item == f"order:{i}" and n.kind == "drug" and n.verified
                        ),
                        None,
                    )
                    if reading:
                        verified_names[f"order:{i}"] = reading
            from sanad.scribe.names import split_drug_dose
            from sanad.scribe.resolver import context, resolve_name

            # Fetch after identity screening; the pure resolver only consumes
            # these already-fetched results. Both readings share the same cap.
            for i, order in enumerate(candidate.orders):
                if f"order:{i}" in verified_names:
                    continue
                resolved = resolve_name(
                    split_drug_dose(order.drug)[0],
                    "drug",
                    source_text,
                    order.name_latin,
                    context(service, order.generic),
                )
                if resolved.latin:
                    service.lookup_drug(resolved.latin)
            candidate, name_issues = prepare_names(
                candidate,
                source_text,
                resolve_name=service.resolve,
                readings=names,
                verified_names=verified_names,
            )

        candidate = candidate.model_copy(
            update={
                "facts": tuple(
                    fact.model_copy(
                        update={
                            "lab": grade_row(fact.lab, self.runtime.safety_policy),
                            "text": lab_text(grade_row(fact.lab, self.runtime.safety_policy)),
                        }
                    )
                    if fact.lab
                    else fact
                    for fact in candidate.facts
                )
            }
        )
        if correction and previous and previous.expires_at <= now:
            self.committer.expire(previous)
            return self._reply(receipt, actor, claim, "scribe_expired", "expired")
        force_new = command == "/new" or bool(correction and previous and previous.creating_patient)
        choices = () if force_new else lookup(self.repo.store, doctor.scope, candidate.patient)
        intent = derive_intent(
            candidate,
            has_match=bool(
                choices and (candidate.patient.name_as_spoken or candidate.patient.identifiers)
            ),
        )
        if force_new:
            intent = "create_patient"
        elif command in {"/find", "/qr"}:
            intent = "find_patient"
        issues = [
            *candidate_issues(candidate, source_text, disputed),
            *name_issues,
            *clinical_issues,
            *correction_issues,
            *candidate._merge_issues,
        ]
        if candidate._malformed_items:
            # A rejected nonnumeric item still blocks confirmation; the model's
            # generic placeholder is never promoted into a doctor question.
            issues.append(ProposalIssue(item="all", code="clarification"))
        if not photo and command is None and missing_request(candidate, source_text):
            from sanad.scribe.extract import MissionCandidate
            from sanad.scribe.monitoring import (
                compile_schedule,
                duration_expression,
                spoken_clause,
                task_request,
            )

            schedule = compile_schedule(source_text)
            if schedule and not any(m.kind == "MONITOR" for m in candidate.missions):
                instruction = spoken_clause(source_text)
                candidate = candidate.model_copy(
                    update={
                        "missions": (
                            *candidate.missions,
                            MissionCandidate(
                                kind="MONITOR",
                                text=instruction,
                                timing_expression=duration_expression(instruction),
                            ),
                        )
                    }
                )
                intent = derive_intent(candidate, has_match=bool(choices))
                if force_new:
                    intent = "create_patient"
            else:
                issues.append(
                    ProposalIssue(
                        item="all",
                        code="request_missing",
                        field="task" if task_request(source_text) else None,
                    )
                )
        lookup_only = intent == "find_patient" and (command in {"/find", "/qr"} or not issues)
        if lookup_only and not choices:
            return self._reply(receipt, actor, claim, "doctor_patient_not_found", "not_found")
        selected = (
            choices[0]
            if len(choices) == 1
            and (candidate.patient.name_as_spoken or candidate.patient.identifiers)
            and (command != "/qr" or choices[0].score == 3)
            else None
        )
        if (
            correction
            and previous
            and previous.selected_patient_id
            and (candidate.patient == previous.candidate.patient)
        ):
            selected = next(
                (
                    p
                    for p in lookup(self.repo.store, doctor.scope, PatientCandidate())
                    if p.patient_id == previous.selected_patient_id
                ),
                selected,
            )
            if selected is None:
                from sanad.scribe.patients import choice_of, panel

                selected = next(
                    (
                        choice_of(p)
                        for p in panel(self.repo.store, doctor.scope)
                        if p.id == previous.selected_patient_id
                    ),
                    None,
                )
        if selected_id:
            from sanad.scribe.patients import choice_of

            explicit = self.claims.patient(doctor.id, selected_id)
            if explicit is None:
                return self._reply(receipt, actor, claim, "scribe_stale", "stale_version")
            selected = choice_of(explicit)
            intent, lookup_only = "update_record", False
        if lookup_only and selected and not invitation:
            return self._reply(
                receipt,
                actor,
                claim,
                "scribe_card",
                "found",
                text=wording.label("patient", doctor.language) + selected.display_name,
            )
        creating = force_new or (
            intent == "create_patient" and not choices and bool(candidate.patient.name_as_spoken)
        )
        if (not selected and not creating) or (creating and not candidate.patient.name_as_spoken):
            issues.append(ProposalIssue(item="patient", code="patient_missing"))
        if candidate.patient.name_as_spoken and len(candidate.patient.name_as_spoken) > 160:
            issues.append(ProposalIssue(item="patient", code="patient_missing"))
        for family, values in (
            ("fact", tuple(f.text for f in candidate.facts)),
            ("alert", candidate.alerts),
            ("ambiguity", candidate.ambiguities),
        ):
            for i, text in enumerate(values):
                if (
                    not (photo and family == "fact")
                    and screen_text(text, policy=self.runtime.safety_policy).level == "danger"
                ):
                    issues.append(ProposalIssue(item=f"{family}:{i}", code="unsafe_text"))
        timing_policy = DRAFT_POLICY_2026_09.model_copy(update={"timezone": doctor.timezone})
        timings, timing_issues = candidate_timings(candidate, receipt.received_at, timing_policy)
        issues.extend(timing_issues)
        versions = []
        if selected:
            scope = PatientScope(doctor_id=doctor.id, patient_id=selected.patient_id)
            patient = self.repo.store.get(scope, "patient", selected.patient_id)
            if patient:
                versions.append(patient.ref)
            heads = tuple(records(self.repo.store, scope, "care_order_head"))
            versions.extend(row.ref for row in heads)
            versions.extend(row.ref for row in records(self.repo.store, scope, "care_order"))
        target = (
            PatientScope(doctor_id=doctor.id, patient_id=selected.patient_id) if selected else None
        )
        if photo and correction:
            photo = self.photos.screen_correction(
                candidate, photo, receipt, actor, selected.patient_id if selected else None
            )
        candidate, amendments, amendment_issues = amend.prepare(
            self.repo,
            target,
            candidate,
            creating=creating,
            source=source_text,
            partition=source_partition,
            previous=previous if correction else None,
        )
        support = "\n".join(amend.instruction_line(a.old) for a in amendments if a.old)
        issues = [
            i
            for i in issues
            if i.code not in {"unsupported_number", "dose_missing", "unassigned_number"}
        ]
        for issue in candidate_issues(candidate, source_text + "\n" + support, disputed):
            if issue.code == "unassigned_number":
                # Old order values support the displayed diff; they were not newly heard.
                heard = tuple(n for n in issue.numbers if n in numbers_in(source_text))
                if not heard:
                    continue
                issue = issue.model_copy(update={"numbers": heard})
            issues.append(issue)
        issues.extend(amendment_issues)
        timings, timing_issues = candidate_timings(candidate, receipt.received_at, timing_policy)
        issues.extend(timing_issues)
        if correction and previous:
            unchanged: set[str] = set()
            for i, (before, after) in enumerate(
                zip(previous.candidate.orders, candidate.orders, strict=False)
            ):
                if before.action == after.action:
                    unchanged.add(f"order:{i}")
                for label in ("effective", "checkin"):
                    field = label + "_expression"
                    if getattr(before, field) == getattr(after, field):
                        unchanged.add(f"{label}:{i}")
            unchanged.update(
                f"mission:{i}"
                for i, (before_mission, after_mission) in enumerate(
                    zip(previous.candidate.missions, candidate.missions, strict=False)
                )
                if before_mission.kind == after_mission.kind
                and before_mission.timing_expression == after_mission.timing_expression
            )
            prior_timings = {t.item: t for t in previous.timings}
            timings = tuple(
                prior_timings[t.item] if t.item in unchanged and t.item in prior_timings else t
                for t in timings
            )
        issues = [
            i
            for i in issues
            if not (i.code == "unassigned_number" and set(i.numbers) <= set(resolved_numbers))
        ]
        from sanad.evidence.templates import render as render_evidence
        from sanad.safety.alerts import refused_at_write
        from sanad.scribe.commit import value_alert

        for index, text in enumerate(candidate.alerts):
            alert = value_alert(text)
            if alert and refused_at_write(alert, self.runtime.safety_policy):
                issues.append(
                    ProposalIssue(
                        item=f"alert:{index}",
                        code="clinical_unclear",
                        question=render_evidence("alert_refused", doctor.language),
                    )
                )
        if photo:
            issues.extend(review_issues(photo, candidate))
        # Worst-case record/marker overhead must fit DynamoDB's atomic transaction.
        if (
            len(candidate.facts)
            + 5 * len(candidate.orders)
            + 3 * len(candidate.missions)
            + 3 * len(candidate.alerts)
            + 2 * len({(n.kind, n.latin) for n in names})
            > 65
        ):
            issues.append(ProposalIssue(item="all", code="batch_too_large"))
        # Choosing the other offered identity creates a replacement proposal;
        # an existing proposal's selected identity remains immutable in the store.
        rebind = bool(
            correction
            and previous
            and patient_answer(previous, source_text[len(previous.source_text) :])
            and candidate.patient != previous.candidate.patient
            and (selected.patient_id if selected else None) != previous.selected_patient_id
        )
        nonce = issue_token()
        proposal = Proposal(
            id=previous.id
            if correction and previous and not rebind
            else proposal_id or uuid4().hex,
            version=previous.version + 1 if correction and previous and not rebind else 1,
            scope=doctor.scope,
            doctor_id=doctor.id,
            timezone=doctor.timezone,
            language=contest_language(doctor.language),
            selected_patient_id=selected.patient_id if selected else None,
            selected_display_name=selected.display_name if selected else None,
            creating_patient=creating,
            candidate=candidate,
            intent=intent,
            issues=tuple(dict.fromkeys(issues)),
            base_versions=tuple(versions),
            choices=choices if not selected else (),
            timings=timings,
            source_receipt_id=photo.reads.first.provenance.source_observation_id
            if photo
            else receipt.id,
            source_transcript_ref=transcript_ref,
            source_text=source_text,
            source_partition=source_partition,
            source_provenance=provenance,
            disputed_numbers=disputed,
            heard_numbers=heard,
            created_at=previous.created_at if correction and previous else now,
            updated_at=now,
            expires_at=previous.expires_at
            if correction and previous
            else now + DRAFT_SCRIBE_POLICY.proposal_ttl,
            review_at=previous.expires_at
            if correction and previous
            else now + DRAFT_SCRIBE_POLICY.proposal_ttl,
            confirmation_nonce_hash=nonce.hash,
            work_clock=OperationalClock(
                next_action_at=previous.expires_at
                if correction and previous
                else now + DRAFT_SCRIBE_POLICY.proposal_ttl,
                work_lane="scribe",
            ),
            supersedes_id=previous.supersedes_id
            if correction and previous and not rebind
            else previous.id
            if previous
            else None,
            invitation_requested=invitation
            or bool(correction and previous and previous.invitation_requested),
            prompt_version=CORRECTION_PROMPT_VERSION if correction else PROMPT_VERSION,
            photo=photo,
            amendments=amendments,
            names=tuple(names),
            rxnorm_calls=service.budget.calls if not photo else 0,
            corrected=correction,
            single_source=candidate._single_source,
            resolved_numbers=resolved_numbers,
        )
        if candidate.ambiguities:
            from sanad.scribe.extract import anchored_patient_name
            from sanad.scribe.grounding import normalize
            from sanad.store.records import Patient

            other_names = []
            patient_cursor = None
            while True:
                profiles, patient_cursor = self.repo.store.list_patients(
                    doctor.scope, cursor=patient_cursor
                )
                for profile in profiles:
                    if profile.id == proposal.selected_patient_id:
                        continue
                    patient_row = self.repo.store.get(
                        PatientScope(doctor_id=doctor.id, patient_id=profile.id),
                        "patient",
                        profile.id,
                    )
                    if patient_row:
                        name = from_record(patient_row, Patient).display_name
                        if normalize(name) != normalize(candidate.patient.name_as_spoken or ""):
                            other_names.append(name)
                if patient_cursor is None:
                    break
            multiple = any(
                anchored_patient_name(name, ambiguity, source_text)
                for name in other_names
                for ambiguity in candidate.ambiguities
            )
            if multiple:
                proposal = proposal.model_copy(
                    update={
                        "issues": (
                            *proposal.issues,
                            ProposalIssue(item="all", code="multiple_patients"),
                        )
                    }
                )
        if not photo:
            from sanad.scribe.grounding import seal
            from sanad.scribe.resolver import context

            proposal = seal(proposal, context(service), previous if correction else None)
            logger.info("scribe_turn dropped_facts=%d", len(proposal.candidate._dropped_facts))
        if any(
            len(part) > DRAFT_SCRIBE_POLICY.card_max_chars
            for part in render_card(proposal, contest_language(doctor.language))
        ):
            proposal = Proposal.model_validate(
                proposal.model_dump()
                | {
                    "issues": (*proposal.issues, ProposalIssue(item="all", code="batch_too_large")),
                }
            )
        models: tuple[BaseModel, ...] = (proposal, *extra_models)
        if previous and previous.status == "pending" and (not correction or rebind):
            models += (revise(previous, now, status="superseded", work_clock=None),)
        models += (
            (
                revise(state, now, pending_proposal_id=proposal.id)
                if state
                else ScribeState(
                    id="current",
                    scope=doctor.scope,
                    pending_proposal_id=proposal.id,
                    created_at=now,
                    updated_at=now,
                )
            ),
        )
        callback_models, markup = self.buttons(proposal, actor, nonce.secret.get_secret_value())
        intents = self.photos.card_intents(proposal, doctor, markup)
        condition = (
            read_of(state)
            if state
            else IdentityRead(
                scope=doctor.scope, entity_type="scribe_state", id="current", version=None
            )
        )
        result = self.repo.commit(
            actor,
            "ScribePropose",
            "propose:" + receipt.id,
            (*models, *callback_models),
            intents,
            reads=(condition, *extra_reads),
            claim=claim,
            payload={"intake_fence": intake_fence.model_dump(mode="json")}
            if intake_fence
            else None,
        )
        self.checkpoint("proposal_persisted")
        if result.status not in {"accepted", "duplicate"}:
            return self._reply(receipt, actor, claim, "scribe_stale", "stale_version")
        return RouteResult(
            route="doctor", status="proposed", template_id="scribe_card", delivery_patient=target
        )

    def buttons(
        self, proposal: Proposal, actor: Principal, confirm_raw: str
    ) -> tuple[tuple[ScribeCallback, ...], JsonValue]:
        from sanad.scribe.crosscheck import unreadable_read

        if proposal.photo and unreadable_read(proposal.photo.reads):
            return (), {"inline_keyboard": []}
        doctor = self.claims.doctor(actor)
        from sanad.domain.language import default_language

        language = contest_language(doctor.language) if doctor else default_language
        from sanad.scribe.proposal import card_actions

        choices = [
            (
                action,
                next((c.display_name for c in proposal.choices if c.patient_id == patient_id), ""),
                patient_id,
            )
            for action, patient_id in card_actions(proposal)
        ]
        tokens: list[ScribeCallback] = []
        buttons: list[JsonValue] = []
        for action, label, patient_id in choices:
            if action in wording.BUTTONS:
                label = wording.button(action, language)
            token = issue_token()
            raw, hash = (
                (confirm_raw, proposal.confirmation_nonce_hash)
                if action == "confirm"
                else (token.secret.get_secret_value(), token.hash)
            )
            tokens.append(
                ScribeCallback.model_validate(
                    {
                        "id": hash,
                        "scope": proposal.scope,
                        "proposal_id": proposal.id,
                        "proposal_version": proposal.version,
                        "actor_subject": actor.subject,
                        "action": action,
                        "patient_id": patient_id,
                        "expires_at": proposal.expires_at,
                        "created_at": self.repo.clock(),
                        "updated_at": self.repo.clock(),
                    }
                )
            )
            buttons.append([{"text": label, "callback_data": raw}])
        photo_tokens, photo_buttons = self.photos.buttons(proposal, actor, language)
        return (*tokens, *photo_tokens), {"inline_keyboard": [*photo_buttons, *buttons]}

    def _callback(
        self, receipt: InboundReceipt, actor: Principal, token: ScribeCallback
    ) -> RouteResult:
        claimed = self._claim(receipt)
        if claimed is None:
            return RouteResult(route="busy", status="processing")
        receipt, claim = claimed
        proposal = self.repo.load(token.scope, "scribe_proposal", token.proposal_id, Proposal)
        result = ConfirmationResult("stale", "scribe_stale")
        now = self.repo.clock()
        if proposal and token.actor_subject == actor.subject and not self.claims.doctor(actor):
            self.committer.invalidate(proposal)
        if proposal and proposal.expires_at <= now:
            self.committer.expire(proposal)
            result = ConfirmationResult("expired", "scribe_expired")
        elif (
            proposal
            and token.actor_subject == actor.subject
            and proposal.doctor_id == actor.doctor_id
            and not token.consumed_at
            and token.expires_at > now
            and proposal.status == "pending"
            and token.proposal_version == proposal.version
            and self.claims.doctor(actor)
        ):
            id = "callback:" + receipt.id
            if token.action == "confirm":
                result = self.committer.confirm(proposal, token, actor, id, claim=claim)
                if result.status == "busy":
                    self.repo.store.defer_inbound(claim, self.repo.clock())
                    self.runtime.transport.answer_callback(
                        str((receipt.payload or {}).get("callback_query_id", "")), ""
                    )
                    return RouteResult(route="busy", status="patient_busy")
            elif token.action == "reject":
                result = self.committer.reject(proposal, actor, id, claim=claim, token=token)
            elif token.action == "edit":
                result = self.committer.edit(proposal, token, actor, id, claim=claim)
            elif token.action == "reading":
                return self.photos.reading(receipt, actor, claim, proposal, token)
            elif token.action in {"correct_reply", "new_reply"}:
                return self._resolve_reply(receipt, actor, claim, proposal, token)
            else:
                return self._select(receipt, actor, claim, proposal, token)
        self.runtime.transport.answer_callback(
            str((receipt.payload or {}).get("callback_query_id", "")),
            ""
            if result.status in {"accepted", "duplicate"}
            else wording.render(result.template, self.runtime.accounts.language(actor.subject)),
        )
        current = self.repo.store.get(receipt.scope, "inbound_receipt", receipt.id)
        if current and from_record(current, InboundReceipt).state != "completed":
            self.runtime.accounts.finish_receipt(receipt, claim, result_code=result.status)
        if result.patient_id and proposal and proposal.photo:
            photo_work = self.repo.load(
                proposal.scope, "photo_association_work", proposal.id, PhotoAssociationWork
            )
            if photo_work:
                self.photos.finish_association(photo_work)
        if result.patient_id and result.invitation_requested:
            assert proposal is not None
            work = self.repo.load(
                proposal.scope, "scribe_invitation_work", proposal.id, InvitationWork
            )
            if work:
                self.invitation_work(work)
        return RouteResult(
            route="callback",
            status=result.status,
            template_id=result.template,
            delivery_patient=PatientScope(
                doctor_id=actor.doctor_id or "", patient_id=result.patient_id
            )
            if result.patient_id
            else None,
        )

    def _resolve_reply(
        self,
        receipt: InboundReceipt,
        actor: Principal,
        claim: Claim,
        previous: Proposal,
        token: ScribeCallback,
    ) -> RouteResult:
        reply = previous.pending_reply
        doctor = self.claims.doctor(actor)
        if reply is None or doctor is None:
            return self._reply(receipt, actor, claim, "scribe_stale", "stale_version")
        correction = previous if token.action == "correct_reply" else None
        text = reply.text
        if not correction and (marker := NEW_PATIENT.search(text)):
            text = text[marker.start() :]
        service = self.name_lookup(doctor, actor, previous=correction)
        extracted = self.extract_candidate(
            receipt.id, actor, doctor, reply.source, text, correction, service
        )
        if not isinstance(extracted, ModelProposal):
            return self._reply(
                receipt, actor, claim, "doctor_model_unavailable", "model_unavailable"
            )
        candidate = extracted.value
        issues: tuple[ProposalIssue, ...] = ()
        resolved: tuple[str, ...] = ()
        if correction:
            merged = merge_correction(correction, candidate, text)
            candidate, issues, resolved = merged.candidate, merged.issues, merged.resolved_numbers
        disputed = (
            tuple(
                dict.fromkeys(
                    (
                        *reply.disputed,
                        *(n for n in previous.disputed_numbers if n not in numbers_in(text)),
                    )
                )
            )
            if correction
            else reply.disputed
        )
        result = self._propose(
            receipt,
            actor,
            claim,
            doctor,
            candidate,
            previous.source_text + "\n" + text if correction else text,
            self.repo.state(doctor.scope),
            previous,
            (*(previous.source_provenance if correction else ()), *extracted.provenance),
            disputed,
            reply.heard,
            reply.transcript_ref,
            False,
            correction is not None,
            None,
            names_service=service,
            correction_issues=issues,
            resolved_numbers=resolved,
            extra_models=(revise(token, self.repo.clock(), consumed_at=self.repo.clock()),),
            source_partition=append_reply(correction, text)
            if correction
            else SourcePartition(original_end=len(text)),
        )
        self.runtime.transport.answer_callback(
            str((receipt.payload or {}).get("callback_query_id", "")), ""
        )
        return result

    def _select(
        self,
        receipt: InboundReceipt,
        actor: Principal,
        claim: Claim,
        proposal: Proposal,
        token: ScribeCallback,
    ) -> RouteResult:
        doctor = self.claims.doctor(actor)
        assert doctor is not None
        choice = next((c for c in proposal.choices if c.patient_id == token.patient_id), None)
        if token.action == "select" and choice is None:
            return self._reply(receipt, actor, claim, "scribe_stale", "stale_version")
        if (
            choice
            and proposal.intent == "find_patient"
            and not any(i.code not in {"patient_missing"} for i in proposal.issues)
        ):
            return self._finish_lookup(
                receipt, actor, claim, proposal, token, choice.patient_id, choice.display_name
            )
        now, nonce = self.repo.clock(), issue_token()
        versions = []
        if choice:
            scope = PatientScope(doctor_id=doctor.id, patient_id=choice.patient_id)
            row = self.repo.store.get(scope, "patient", choice.patient_id)
            if row is None:
                return self._reply(receipt, actor, claim, "scribe_stale", "stale_version")
            versions.append(row.ref)
            versions.extend(r.ref for r in records(self.repo.store, scope, "care_order_head"))
            versions.extend(r.ref for r in records(self.repo.store, scope, "care_order"))
        issues = [
            i for i in proposal.issues if i.code not in {"patient_missing", "amendment_pending_09b"}
        ]
        candidate, amendments, amendment_issues = amend.prepare(
            self.repo,
            scope if choice else None,
            proposal.candidate,
            creating=token.action == "new",
            source=proposal.source_text,
            partition=partition_for(proposal),
        )
        issues = [i for i in issues if i.code != "order_missing"]
        issues.extend(amendment_issues)
        if not choice and not proposal.candidate.patient.name_as_spoken:
            issues.append(ProposalIssue(item="patient", code="patient_missing"))
        timings, timing_issues = candidate_timings(
            candidate,
            proposal.created_at,
            DRAFT_POLICY_2026_09.model_copy(update={"timezone": doctor.timezone}),
        )
        issues.extend(timing_issues)
        if proposal.photo and choice:
            self.photos.raise_patient(proposal.photo.reads, choice.patient_id, actor)
        changed = revise(
            proposal,
            now,
            candidate=candidate,
            amendments=amendments,
            timings=timings,
            selected_patient_id=choice.patient_id if choice else None,
            selected_display_name=choice.display_name if choice else None,
            creating_patient=token.action == "new",
            choices=(),
            base_versions=tuple(versions),
            confirmation_nonce_hash=nonce.hash,
            issues=tuple(issues),
            intent="create_patient" if token.action == "new" else "update_record",
        )
        if not changed.photo:
            from sanad.scribe.grounding import seal
            from sanad.scribe.resolver import context

            changed = seal(changed, context(self.name_lookup(doctor, actor)))
        tokens, markup = self.buttons(changed, actor, nonce.secret.get_secret_value())
        intents = self.photos.card_intents(changed, doctor, markup)
        result = self.repo.commit(
            actor,
            "ScribeAction",
            "select:" + receipt.id,
            (changed, revise(token, now, consumed_at=now), *tokens),
            intents,
            claim=claim,
            payload={"proposal_id": proposal.id, "nonce_hash": token.id},
        )
        self.runtime.transport.answer_callback(
            str((receipt.payload or {}).get("callback_query_id", "")), ""
        )
        return RouteResult(route="callback", status=result.status, template_id="scribe_card")

    def _finish_lookup(
        self,
        receipt: InboundReceipt,
        actor: Principal,
        claim: Claim,
        proposal: Proposal,
        token: ScribeCallback,
        patient_id: str,
        display_name: str,
    ) -> RouteResult:
        if proposal.invitation_requested:
            qr_result = issue_qr(self.claims, actor, patient_id, "qr-selection:" + proposal.id)
            if qr_result.status not in {"issued", "duplicate"}:
                return self._reply(receipt, actor, claim, "scribe_stale", "invitation_unavailable")
        doctor = self.claims.doctor(actor)
        if doctor is None:
            return self._reply(receipt, actor, claim, "scribe_stale", "forbidden")
        now = self.repo.clock()
        state = self.repo.state(proposal.scope)
        if state is None or state.pending_proposal_id != proposal.id:
            return self._reply(receipt, actor, claim, "scribe_stale", "stale_version")
        changed = revise(
            proposal, now, status="rejected", reason="lookup_complete", work_clock=None
        )
        intents = (
            ()
            if proposal.invitation_requested
            else (
                self.repo.intent(
                    doctor,
                    "scribe_card",
                    {"text": wording.label("patient", doctor.language) + display_name},
                    "found:" + proposal.id,
                ),
            )
        )
        result = self.repo.commit(
            actor,
            "ScribeAction",
            "select:" + receipt.id,
            (
                changed,
                revise(token, now, consumed_at=now),
                revise(state, now, pending_proposal_id=None),
            ),
            intents,
            claim=claim,
            payload={"proposal_id": proposal.id, "nonce_hash": token.id},
        )
        self.runtime.transport.answer_callback(
            str((receipt.payload or {}).get("callback_query_id", "")), ""
        )
        return RouteResult(route="callback", status=result.status, template_id="scribe_card")

    def invitation_work(self, work: InvitationWork) -> None:
        if work.status != "pending":
            return
        row = self.repo.store.get(work.scope, "doctor", work.scope.doctor_id)
        if row is None:
            return
        doctor = from_record(row, Doctor)
        auth = self.repo.store.authorize(doctor.telegram_bot_id, doctor.telegram_user_id)
        if self.claims.doctor(auth.principal):
            result = issue_qr(
                self.claims, auth.principal, work.patient_id, "qr-after:" + work.proposal_id
            )
            if result.status not in {"issued", "duplicate", "forbidden"}:
                return
            status = "issued" if result.status in {"issued", "duplicate"} else "suppressed"
        else:
            status = "suppressed"
        actor = Principal(subject="scribe-work", actor_kind="system", doctor_id=doctor.id)
        self.repo.commit(
            actor,
            "ScribeWork",
            "qr-work:" + work.id,
            (revise(work, self.repo.clock(), status=status, work_clock=None),),
        )

    @invoked("tick")
    def sweep(self, row: StoredRecord) -> None:
        if row.entity_type == "photo_association_work":
            self.photos.finish_association(from_record(row, PhotoAssociationWork))
        elif row.entity_type == "intake_draft":
            from sanad.store.records import IntakeDraft

            self.photos.intake.sweep(from_record(row, IntakeDraft))
        elif row.entity_type == "scribe_proposal":
            self.committer.expire(from_record(row, Proposal))
        elif row.entity_type == "scribe_invitation_work":
            self.invitation_work(from_record(row, InvitationWork))
        elif row.entity_type == "media_work" and self.media_factory:
            work = from_record(row, MediaWork)
            source = self.repo.store.get(
                TenantScope(doctor_id=work.scope.doctor_id), "inbound_receipt", work.receipt_id
            )
            doctor_row = self.repo.store.get(
                TenantScope(doctor_id=work.scope.doctor_id), "doctor", work.scope.doctor_id
            )
            if source is None or doctor_row is None:
                return
            receipt, doctor = from_record(source, InboundReceipt), from_record(doctor_row, Doctor)
            actor = self.repo.store.authorize(
                doctor.telegram_bot_id, doctor.telegram_user_id
            ).principal
            if receipt.state == "completed" and work.transcript_ref:
                self._finish_media(receipt, actor)
            elif receipt.kind in {"photo", "document"} and receipt.state != "completed":
                self.photos.run(receipt, actor)
            else:
                self.media_factory(receipt, actor).sweep()
