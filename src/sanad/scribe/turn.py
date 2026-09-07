"""Bounded doctor turn: recoverable receipt, one extraction, one saved card."""

import asyncio
import json
from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import BaseModel, JsonValue
from strands.models import Model

from sanad.agents.factory import Proposal as ModelProposal
from sanad.agents.factory import bedrock_model, make_agent, propose
from sanad.agents.tools import AgentScope
from sanad.auth.claim import ClaimService
from sanad.auth.service import read_of, revise
from sanad.auth.tokens import issue_token
from sanad.channels.telegram import wording
from sanad.channels.telegram.router import RouteResult
from sanad.domain import DRAFT_POLICY_2026_09, PatientScope, Principal, Provenance, TenantScope
from sanad.media.retrieve import MediaRetriever, fetch_telegram_file
from sanad.media.speech import SpeechAdapter, Transcript, transcribe
from sanad.media.telegram import MediaFailure
from sanad.models.io import CallMetadata
from sanad.models.registry import ModelRegistry, ModelRole
from sanad.safety import screen_text
from sanad.scribe import amend
from sanad.scribe.card import render_card
from sanad.scribe.commit import ConfirmationResult, ScribeCommit
from sanad.scribe.crosscheck import PhotoReview, review_issues
from sanad.scribe.extract import (
    CORRECTION_PROMPT,
    CORRECTION_PROMPT_VERSION,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    DictationCandidate,
    PatientCandidate,
    ProposalIssue,
    candidate_issues,
    derive_intent,
)
from sanad.scribe.patients import lookup
from sanad.scribe.policy import DRAFT_SCRIBE_POLICY
from sanad.scribe.proposal import InvitationWork, Proposal, ScribeCallback, ScribeState
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


def parse_command(text: str) -> tuple[str | None, str]:
    parts = text.strip().split(maxsplit=1)
    if parts and parts[0] in {"/start", "/help", "/new", "/find", "/qr", "/cancel", "/intake"}:
        return parts[0], parts[1].strip() if len(parts) > 1 else ""
    return None, text


class ScribeTurn:
    def __init__(
        self,
        runtime: "TelegramRuntime",
        claims: ClaimService,
        *,
        model_factory: Callable[[ModelRegistry, ModelRole], Model] = bedrock_model,
        speech_factory: Callable[[Provenance], SpeechAdapter] | None = None,
        vision_factory: Callable[[Provenance], "VisionAdapter"] | None = None,
        media_factory: Callable[[InboundReceipt, Principal], MediaRetriever] | None = None,
        registry: ModelRegistry = DEFAULT_REGISTRY,
        observe: Callable[[CallMetadata], None] = lambda metadata: None,
        checkpoint: Callable[[str], None] = lambda name: None,
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
        from sanad.scribe.photos import PhotoTurn

        self.photos = PhotoTurn(self)

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
            {"text": text if text is not None else wording.render(template)},
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
            retriever.failure_text = wording.render("doctor_voice_unreadable")
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
            speech_result = asyncio.run(
                transcribe(audio, "mp3", adapter=self.speech_factory(source))
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
        command, argument = parse_command(text)
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
                text="مفيش صور مستنية اختيار مريض.",
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
            if len(choices) == 1 and argument:
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
        correction = previous if previous and previous.editing and command is None else None
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
            prompt = (
                json.dumps(
                    {
                        "previous_candidate": correction.candidate.model_dump(mode="json"),
                        "correction_text": text,
                    },
                    ensure_ascii=False,
                )
                if correction
                else text
            )
            scope = AgentScope(
                principal=principal,
                scope=doctor.scope,
                source=source,
                policy=self.runtime.safety_policy,
                authority_check=lambda: self.claims.doctor(principal) is not None,
            )
            agent = make_agent(
                "scribe",
                scope=scope,
                tools=(),
                system_prompt=CORRECTION_PROMPT if correction else SYSTEM_PROMPT,
                session_key="scribe:" + receipt.id,
                registry=self.registry,
                model_factory=self.model_factory,
                observe=self.observe,
            )
            extracted = asyncio.run(
                propose("scribe", DictationCandidate, prompt, agent=agent, want_spans=False)
            )
            if not isinstance(extracted, ModelProposal):
                return self._reply(
                    receipt, principal, claim, "doctor_model_unavailable", "model_unavailable"
                )
            candidate = extracted.value
            provenance = tuple(
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
            )
        original = correction.source_text + "\n" + text if correction else text
        disputed = transcript.disputed_numbers if transcript else ()
        if correction:
            from sanad.media.numbers import numbers_in

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
        )

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
    ) -> RouteResult:
        now = self.repo.clock()
        from sanad.scribe.crosscheck import grade_row, lab_text

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
        if previous and previous.editing and previous.expires_at <= now:
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
        issues = list(candidate_issues(candidate, source_text, disputed))
        lookup_only = intent == "find_patient" and (command in {"/find", "/qr"} or not issues)
        if lookup_only and not choices:
            return self._reply(receipt, actor, claim, "doctor_patient_not_found", "not_found")
        selected = (
            choices[0]
            if len(choices) == 1
            and (candidate.patient.name_as_spoken or candidate.patient.identifiers)
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
                text="المريض: " + selected.display_name,
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
            self.repo, target, candidate, creating=creating
        )
        support = "\n".join(amend.instruction_line(a.old) for a in amendments if a.old)
        issues = [
            i
            for i in issues
            if i.code not in {"unsupported_number", "dose_missing", "unassigned_number"}
        ]
        issues.extend(candidate_issues(candidate, source_text + "\n" + support, disputed))
        issues.extend(amendment_issues)
        timings, timing_issues = candidate_timings(candidate, receipt.received_at, timing_policy)
        issues.extend(timing_issues)
        if photo:
            issues.extend(review_issues(photo))
        # Worst-case record/marker overhead must fit DynamoDB's atomic transaction.
        if (
            len(candidate.facts)
            + 5 * len(candidate.orders)
            + 3 * len(candidate.missions)
            + 3 * len(candidate.alerts)
            > 65
        ):
            issues.append(ProposalIssue(item="all", code="batch_too_large"))
        nonce = issue_token()
        proposal = Proposal(
            id=proposal_id or uuid4().hex,
            scope=doctor.scope,
            doctor_id=doctor.id,
            timezone=doctor.timezone,
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
            source_provenance=provenance,
            disputed_numbers=disputed,
            heard_numbers=heard,
            created_at=now,
            updated_at=now,
            expires_at=now + DRAFT_SCRIBE_POLICY.proposal_ttl,
            review_at=now + DRAFT_SCRIBE_POLICY.proposal_ttl,
            confirmation_nonce_hash=nonce.hash,
            work_clock=OperationalClock(
                next_action_at=now + DRAFT_SCRIBE_POLICY.proposal_ttl, work_lane="scribe"
            ),
            supersedes_id=previous.id if previous else None,
            invitation_requested=invitation,
            prompt_version=CORRECTION_PROMPT_VERSION if correction else PROMPT_VERSION,
            photo=photo,
            amendments=amendments,
        )
        if any(len(part) > DRAFT_SCRIBE_POLICY.card_max_chars for part in render_card(proposal)):
            proposal = Proposal.model_validate(
                proposal.model_dump()
                | {
                    "issues": (*proposal.issues, ProposalIssue(item="all", code="batch_too_large")),
                }
            )
        models: tuple[BaseModel, ...] = (proposal, *extra_models)
        if previous and previous.status == "pending":
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
        cards = render_card(proposal)
        intents = tuple(
            self.repo.intent(
                doctor,
                "scribe_card",
                {"text": card, **({"reply_markup": markup} if i == len(cards) - 1 else {})},
                proposal.id,
                proposal=proposal,
                sequence=i,
            )
            for i, card in enumerate(cards)
        )
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
        return RouteResult(route="doctor", status="proposed", template_id="scribe_card")

    def buttons(
        self, proposal: Proposal, actor: Principal, confirm_raw: str
    ) -> tuple[tuple[ScribeCallback, ...], JsonValue]:
        choices: list[tuple[str, str, str | None]] = []
        if proposal.choices and not proposal.selected_patient_id and not proposal.creating_patient:
            choices.extend(("select", c.display_name, c.patient_id) for c in proposal.choices)
            choices.append(("new", "مريض جديد", None))
        else:
            if (
                not proposal.blocked("all")
                and not proposal.blocked("patient")
                and self.photos.confirmable(proposal)
            ):
                choices.append(("confirm", "✅ تمام", None))
            choices.append(("edit", "✏️ تعديل", None))
        choices.append(("reject", "❌ إلغاء", None))
        tokens: list[ScribeCallback] = []
        buttons: list[JsonValue] = []
        for action, label, patient_id in choices:
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
        photo_tokens, photo_buttons = self.photos.buttons(proposal, actor)
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
            elif token.action == "reject":
                result = self.committer.reject(proposal, actor, id, claim=claim, token=token)
            elif token.action == "edit":
                result = self.committer.edit(proposal, token, actor, id, claim=claim)
            elif token.action == "reading":
                return self.photos.reading(receipt, actor, claim, proposal, token)
            else:
                return self._select(receipt, actor, claim, proposal, token)
        self.runtime.transport.answer_callback(
            str((receipt.payload or {}).get("callback_query_id", "")),
            "" if result.status in {"accepted", "duplicate"} else wording.render(result.template),
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
        return RouteResult(route="callback", status=result.status, template_id=result.template)

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
            self.repo, scope if choice else None, proposal.candidate, creating=token.action == "new"
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
        tokens, markup = self.buttons(changed, actor, nonce.secret.get_secret_value())
        cards = render_card(changed)
        intents = tuple(
            self.repo.intent(
                doctor,
                "scribe_card",
                {"text": card, **({"reply_markup": markup} if i == len(cards) - 1 else {})},
                f"{changed.id}:{changed.version}",
                proposal=changed,
                sequence=i,
            )
            for i, card in enumerate(cards)
        )
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
                    {"text": "المريض: " + display_name},
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
