"""Deterministic confirmation compiler and single-use card actions."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, TypedDict
from uuid import uuid4

from pydantic import BaseModel

from sanad.auth.claim import ClaimService
from sanad.auth.commands import CreatePatientStub
from sanad.auth.service import read_of, revise
from sanad.channels.telegram import wording
from sanad.domain import MissionKind, PatientScope, Principal, Provenance, VersionRef
from sanad.domain.entities import (
    MedicationDetails,
    SendRecordsDetails,
    TaskDetails,
    TestDetails,
    VisitDetails,
)
from sanad.domain.events import ProposalCreated
from sanad.domain.predicates import EvidencePredicate, PatientReportPredicate
from sanad.scribe.amend import find_head
from sanad.scribe.amend import order_key as order_key
from sanad.scribe.card import REASONS, plain
from sanad.scribe.proposal import InvitationWork, Proposal, ScribeCallback
from sanad.scribe.records import (
    CareOrderHead,
    CareOrderVersion,
    CarePlan,
    ClinicalFact,
    FactPayload,
    LabFactPayload,
    ValueAlert,
)
from sanad.scribe.repository import ScribeRepository
from sanad.steward.apply import EffectsRejected
from sanad.steward.service import Steward
from sanad.store.records import (
    Claim,
    CommandEnvelope,
    CommitResult,
    Doctor,
    MediaWork,
    OperationalClock,
    OrderAuthority,
    Patient,
    PatientMedia,
    PhotoAssociationWork,
)


class ClinicalMetadata(TypedDict):
    created_at: datetime
    updated_at: datetime
    scope: PatientScope


def value_alert(text: str) -> ValueAlert | None:
    quantities = {
        "potassium": r"potassium|بوتاسيوم|\bk\b",
        "creatinine": r"creatinine|كرياتينين|كرياتنين",
        "systolic": r"systolic|انقباضي|انقباضى",
        "diastolic": r"diastolic|انبساطي|انبساطى",
        "bicarbonate": r"bicarbonate|بيكربونات",
        "glucose": r"glucose|سكر",
        "heart_rate": r"heart rate|نبض",
        "blood_pressure": r"blood pressure|الضغط",
        "sodium": r"sodium|صوديوم",
        "hemoglobin": r"hemoglobin|haemoglobin|هيموجلوبين|هيموغلوبين",
        "platelets": r"platelets|صفائح",
        "oxygen_saturation": r"oxygen saturation|spo2|تشبع الأكسجين|تشبع الاكسجين",
        "temperature": r"temperature|حرارة",
    }
    metric = next(
        (name for name, pattern in quantities.items() if re.search(pattern, text, re.I)), None
    )
    threshold = re.search(
        r"(>=|<=|>|<|يوصل|وصل|reaches|at least|at most|فوق|أعلى من|اعلى من|يتعدى|عدى|"
        r"passes|above|below|أقل من|اقل من|تحت)\s*(\d+(?:\.\d+)?)",
        text,
        re.I,
    )
    if metric is None and threshold:
        from sanad.safety.labs import rule_for

        words = text[: threshold.start()].split()
        supported = next(
            (
                rule
                for start in range(len(words))
                for end in range(start + 1, len(words) + 1)
                if (rule := rule_for(" ".join(words[start:end]))) is not None
            ),
            None,
        )
        if supported:
            metric = supported.analyte
    if metric is None or threshold is None:
        return None
    comparison = threshold[1].lower()
    comparator: Literal["gt", "ge", "lt", "le"] = (
        "le"
        if comparison in {"<=", "at most"}
        else "lt"
        if comparison in {"<", "below", "أقل من", "اقل من", "تحت"}
        else "ge"
        if comparison in {">=", "reaches", "يوصل", "وصل", "at least"}
        else "gt"
    )
    units = re.search(
        r"\s*(mmol/L|mEq/L|mg/dL|umol/L|µmol/L|mmHg|g/dL|x10\^3/uL)\b",
        text[threshold.end() :],
        re.I,
    )
    return ValueAlert(
        text=text,
        metric=metric,
        comparator=comparator,
        threshold=threshold[2],
        unit=units[1] if units else None,
    )


@dataclass(frozen=True)
class ConfirmationResult:
    status: str
    template: str
    patient_id: str | None = None
    invitation_requested: bool = False


class ScribeCommit:
    @staticmethod
    def _wording(template: str, doctor: Doctor) -> str:
        from sanad.scribe.english import render_wording

        return render_wording(template, doctor.language)

    def __init__(self, repository: ScribeRepository, steward: Steward, claims: ClaimService):
        self.repo, self.steward, self.claims = repository, steward, claims

    def _provenance(self, proposal: Proposal, actor: Principal, text: str = "") -> Provenance:
        base = (
            proposal.source_provenance[0]
            if proposal.source_provenance
            else Provenance(
                source_observation_id=proposal.source_receipt_id,
                actor_kind="doctor",
                actor_id=actor.subject,
                source_kind="doctor_statement",
                received_at=proposal.created_at,
            )
        )
        return Provenance.model_validate(
            base.model_dump()
            | {
                "confirmed_by": actor.subject,
                "confirmed_at": self.repo.clock(),
                "prompt_version": base.prompt_version
                if proposal.photo
                else proposal.prompt_version,
                "source_span": None,
            }
        )

    def _compile(
        self,
        proposal: Proposal,
        actor: Principal,
        doctor: Doctor,
    ) -> tuple[tuple[BaseModel, ...], Patient, tuple[str, ...]]:
        now, candidate = self.repo.clock(), proposal.candidate
        if proposal.creating_patient:
            patient, profile = self.claims.prepare_stub(
                CreatePatientStub(
                    command_id="stub:" + proposal.id,
                    actor=actor,
                    display_name=candidate.patient.name_as_spoken or "",
                    timezone=doctor.timezone,
                    language=doctor.language,
                ),
                doctor.id,
                uuid4().hex,
                now,
            )
        else:
            scope = PatientScope(doctor_id=doctor.id, patient_id=proposal.selected_patient_id or "")
            found_patient = self.claims.patient(doctor.id, scope.patient_id)
            found_profile = self.repo.store.get_patient_profile(scope)
            if found_patient is None or found_profile is None:
                raise EffectsRejected("patient_missing")
            patient, profile = revise(found_patient, now), found_profile
        scope, plan_id = patient.scope, uuid4().hex
        metadata: ClinicalMetadata = {"created_at": now, "updated_at": now, "scope": scope}
        provenance = self._provenance(proposal, actor)
        models: list[BaseModel] = []
        accepted: list[str] = []
        if not proposal.blocked("patient"):
            patient = Patient.model_validate(
                patient.model_dump()
                | {
                    "age": candidate.patient.age or patient.age,
                    "sex": candidate.patient.sex or patient.sex,
                    "identifiers": tuple(
                        dict.fromkeys((*patient.identifiers, *candidate.patient.identifiers))
                    ),
                }
            )
            demographics = tuple(
                v
                for v in (
                    "العمر: " + candidate.patient.age if candidate.patient.age else None,
                    "النوع: " + ("ذكر" if candidate.patient.sex == "male" else "أنثى")
                    if candidate.patient.sex
                    else None,
                    "أرقام التعريف: " + "، ".join(candidate.patient.identifiers)
                    if candidate.patient.identifiers
                    else None,
                )
                if v
            )
            if demographics:
                models.append(
                    ClinicalFact(
                        id=uuid4().hex,
                        category="demographic",
                        payload=FactPayload(text="؛ ".join(demographics)),
                        provenance=provenance,
                        **metadata,
                    )
                )
                if doctor.language == "en":
                    accepted.extend(
                        v
                        for v in (
                            "Age: " + candidate.patient.age if candidate.patient.age else None,
                            "Sex: " + candidate.patient.sex if candidate.patient.sex else None,
                            "Identifiers: " + ", ".join(candidate.patient.identifiers)
                            if candidate.patient.identifiers
                            else None,
                        )
                        if v
                    )
                else:
                    accepted.extend(demographics)
        patient = Patient.model_validate(patient.model_dump() | {"current_plan_id": plan_id})
        models.append(patient)
        if proposal.creating_patient:
            models.append(profile)
            accepted.append(
                ("New patient: " if doctor.language == "en" else "مريض جديد: ")
                + patient.display_name
            )
        for i, fact in enumerate(candidate.facts):
            if proposal.blocked(f"fact:{i}"):
                continue
            models.append(
                ClinicalFact(
                    id=uuid4().hex,
                    category=fact.category,
                    payload=LabFactPayload(**fact.lab.model_dump(), text=fact.text)
                    if fact.lab
                    else FactPayload(
                        text=fact.text,
                        clinical_en=fact.clinical_en,
                        clinical_kind=fact.clinical_kind,
                        terms=fact.terms,
                    ),
                    provenance=self._provenance(proposal, actor, fact.text),
                    **metadata,
                )
            )
            from sanad.scribe.crosscheck import lab_text

            accepted.append(
                plain(
                    lab_text(fact.lab, doctor.language)
                    if fact.lab and doctor.language == "en"
                    else fact.text
                )
            )
        order_refs: list[VersionRef] = []
        mission_ids: list[str] = []
        followup_ids: list[str] = []

        def mission(
            item: str,
            kind: MissionKind,
            title: str,
            details: object,
            predicate: object,
            refs: tuple[VersionRef, ...] = (),
        ) -> None:
            timing = next(t.resolved for t in proposal.timings if t.item == item)
            mission_id = uuid4().hex
            command_id = f"confirm:{proposal.id}:{item}"
            proposed = ProposalCreated.model_validate(
                {
                    "event_id": command_id,
                    "mission_id": mission_id,
                    "doctor_id": doctor.id,
                    "patient_id": patient.id,
                    "created_at": proposal.created_at,
                    "kind": kind,
                    "title": title,
                    "details": details,
                    "objective_predicate": predicate,
                    "order_refs": refs,
                    "source_proposal_id": proposal.id,
                    "timing": timing,
                }
            )
            confirmed, followups = self.steward.prepare_confirmation(
                CommandEnvelope(
                    command_id=command_id,
                    principal=actor,
                    scope=scope,
                    requested_at=now,
                    payload={
                        "type": "ConfirmProposal",
                        "source_receipt_id": proposal.source_receipt_id,
                    },
                ),
                proposed,
                profile,
            )
            if item.startswith("mission:"):
                confirmed = confirmed.model_copy(
                    update={"clinical_en": candidate.missions[int(item.split(":")[1])].clinical_en}
                )
            models.extend((confirmed, *followups))
            mission_ids.append(mission_id)
            followup_ids.extend(f.id for f in followups)

        for i, order in enumerate(candidate.orders):
            item = f"order:{i}"
            if proposal.blocked(item):
                continue
            amendment = next((a for a in proposal.amendments if a.item == item), None)
            if amendment and amendment.noop:
                continue
            old = find_head(
                self.repo,
                scope,
                amendment.old.drug
                if amendment and amendment.old and amendment.head_version
                else order.drug,
            )
            id = old.id if old else order_key(order.drug)
            version = old.current_order_version + 1 if old else 1
            status: Literal["stopped", "active"] = "stopped" if order.action == "stop" else "active"
            version_id = f"{id}:{version}"
            models.append(
                CareOrderVersion(
                    id=version_id,
                    order_id=id,
                    order_version=version,
                    type="medication",
                    structured_instruction=order,
                    provenance=self._provenance(proposal, actor, order.drug),
                    confirmed_by=actor.subject,
                    confirmed_at=now,
                    supersedes_version=old.current_order_version if old else None,
                    effective_from=next(
                        (t.resolved.due_at for t in proposal.timings if t.item == f"effective:{i}"),
                        now,
                    ),
                    **metadata,
                )
            )
            head = CareOrderHead(
                id=id,
                order_id=id,
                current_order_version=version,
                current_version_id=version_id,
                type="medication",
                name=order.drug,
                status=status,
                version=old.version + 1 if old else 1,
                created_at=old.created_at if old else now,
                updated_at=now,
                scope=scope,
                delivery_epoch=old.delivery_epoch + 1 if old else 0,
                changed_by=actor.subject,
                changed_at=now,
            )
            authority = self.repo.load(scope, "care_order", id, OrderAuthority)
            authority = (
                revise(authority, now, status=status)
                if authority
                else OrderAuthority(id=id, status=status, **metadata)
            )
            models.extend((head, authority))
            ref = VersionRef(entity_type="care_order", id=id, version=authority.version)
            instruction_ref = VersionRef(entity_type="care_order_version", id=version_id, version=1)
            if old and order.action in {"stop", "change"}:
                from sanad.domain import FollowUpTask, Mission, ReviewObligation
                from sanad.domain import events as medication_events
                from sanad.domain.entities import TERMINAL_STATES, ReviewAction
                from sanad.domain.transitions import (
                    transition_followup,
                    transition_mission,
                    transition_review,
                )
                from sanad.steward.types import records
                from sanad.store.records import from_record

                previous_ref = VersionRef(
                    entity_type="care_order", id=id, version=old.current_order_version
                )
                timing_policy = self.steward.policy_provider(scope).timing
                superseded_ids = set()
                for row in records(self.repo.store, scope, "mission"):
                    previous_mission = from_record(row, Mission)
                    if (
                        previous_mission.kind != MissionKind.MEDICATION
                        or previous_ref not in previous_mission.order_refs
                        or previous_mission.state in TERMINAL_STATES
                    ):
                        continue
                    outcome = transition_mission(
                        previous_mission,
                        medication_events.OrderSuperseded(
                            event_id=f"supersede:{proposal.id}:{previous_mission.id}",
                            successor_order_ref=instruction_ref,
                        ),
                        now,
                        timing_policy,
                    )
                    if isinstance(outcome, medication_events.TransitionRejected):
                        raise EffectsRejected(outcome.reason_code)
                    models.append(outcome.aggregate)
                    superseded_ids.add(previous_mission.id)
                for row in records(self.repo.store, scope, "followup"):
                    task = from_record(row, FollowUpTask)
                    if (
                        task.kind != "MEDICATION_DAY3"
                        or previous_ref not in task.order_refs
                        or task.state in {"fulfilled", "cancelled"}
                    ):
                        continue
                    outcome = transition_followup(
                        task,
                        medication_events.CancelFollowUp(
                            event_id=f"supersede:{proposal.id}:{task.id}",
                            reason="order_superseded",
                        ),
                        now,
                        timing_policy,
                    )
                    if isinstance(outcome, medication_events.TransitionRejected):
                        raise EffectsRejected(outcome.reason_code)
                    models.append(outcome.aggregate)
                for row in records(self.repo.store, scope, "review"):
                    review = from_record(row, ReviewObligation)
                    if (
                        review.source_type != "mission"
                        or review.source_id not in superseded_ids
                        or review.review_kind != "unmet_objective"
                        or review.state == "resolved"
                    ):
                        continue
                    outcome = transition_review(
                        review,
                        medication_events.ResolveReview(
                            event_id=f"supersede:{proposal.id}:{review.id}",
                            action=ReviewAction.extend,
                            expected_source_version=review.source_version,
                            actor_id=actor.subject,
                            reason="superseded",
                        ),
                        now,
                        timing_policy,
                    )
                    if isinstance(outcome, medication_events.TransitionRejected):
                        raise EffectsRejected(outcome.reason_code)
                    models.append(outcome.aggregate)
                # Confirmation writes clinical state atomically. The ordinary
                # Steward/dispatch path owns queue suppression (addendum 2).
            order_refs.append(instruction_ref)
            accepted.append(
                " ".join(v for v in (order.drug, order.dose, order.frequency, order.action) if v)
            )
            if order.action in {"start", "change", "stop"}:
                mission(
                    item,
                    MissionKind.MEDICATION,
                    order.drug,
                    MedicationDetails.model_validate(
                        {"action": order.action.upper(), "order_ref": instruction_ref}
                    ),
                    PatientReportPredicate(report_kind="medication_" + order.action),
                    (ref,),
                )
            checkin = next((t.resolved for t in proposal.timings if t.item == f"checkin:{i}"), None)
            if checkin and order.action in {"change", "stop"}:
                from sanad.domain import CreateFollowUp, create_followup
                from sanad.domain.entities import FollowUpKind
                from sanad.domain.events import TransitionResult

                followup_result = create_followup(
                    CreateFollowUp(
                        event_id=f"checkin:{proposal.id}:{i}",
                        kind=FollowUpKind.CLINICAL_CHECKIN,
                        parent_mission_id=mission_ids[-1],
                        order_refs=(ref,),
                        anchor_kind="doctor_specified_date",
                        anchor_time=checkin.due_at,
                        prompt_at=checkin.due_at,
                        response_predicate=PatientReportPredicate(report_kind="clinical_checkin"),
                        confirmed_by=actor.subject,
                        confirmed_at=now,
                        doctor_id=doctor.id,
                        patient_id=patient.id,
                    ),
                    now,
                    self.steward.policy_provider(scope).timing,
                )
                if not isinstance(followup_result, TransitionResult):
                    raise EffectsRejected("followup_invalid")
                followup = followup_result.aggregate
                models.append(followup)
                followup_ids.append(followup.id)
        amended = any(
            a.head_version and a.old and not a.noop and not proposal.blocked(a.item)
            for a in proposal.amendments
        )
        if amended:
            epoch = max(patient.delivery_epoch, profile.delivery_epoch) + 1
            patient = Patient.model_validate(patient.model_dump() | {"delivery_epoch": epoch})
            models = [patient if isinstance(m, Patient) else m for m in models]
            models.append(revise(profile, now, delivery_epoch=epoch))
        if proposal.photo:
            from sanad.domain import TenantScope

            models.append(
                PhotoAssociationWork(
                    id=proposal.id,
                    proposal_id=proposal.id,
                    scope=TenantScope(doctor_id=doctor.id),
                    patient_id=patient.id,
                    intake_id=proposal.photo.intake_id,
                    created_at=now,
                    updated_at=now,
                    work_clock=OperationalClock(next_action_at=now, work_lane="scribe"),
                )
            )
            from sanad.store.keys import IntakeScope

            media_scope = IntakeScope(doctor_id=doctor.id, intake_id=proposal.photo.intake_id)
            for media_id in proposal.photo.media_work_ids:
                work = self.repo.load(media_scope, "media_work", media_id, MediaWork)
                if work is None or not work.source_blob_ref or not work.mime:
                    raise EffectsRejected("media_unavailable")
                models.append(
                    PatientMedia(
                        id=media_id,
                        media_work_id=media_id,
                        media_scope=media_scope,
                        source_receipt_id=work.receipt_id,
                        kind=proposal.photo.kind,
                        mime=work.mime,
                        **metadata,
                    )
                )
        for i, instruction in enumerate(candidate.missions):
            item = f"mission:{i}"
            if proposal.blocked(item):
                continue
            text = instruction.text
            if instruction.kind == "TEST":
                details: object = TestDetails(analytes=(text,), completeness="all")
                predicate: object = EvidencePredicate(evaluator="test")
            elif instruction.kind == "SEND_RECORDS":
                details = SendRecordsDetails(categories=(text,), required_count=1)
                predicate = EvidencePredicate(evaluator="send_records")
            elif instruction.kind == "VISIT":
                objective = (
                    "report_received"
                    if re.search(r"تقرير|report", text, re.I)
                    else (
                        "booking_reported"
                        if re.search(r"حجز|احجز|book|arrange", text, re.I)
                        else "attendance_reported"
                    )
                )
                details = VisitDetails.model_validate({"objective": objective})
                predicate = PatientReportPredicate(report_kind="visit_" + objective)
            else:
                from sanad.concierge.tasks import unsupported

                details = TaskDetails(
                    category="doctor_request",
                    instruction=text,
                    completion_rule="unsupported_action" if unsupported(text) else "patient_report",
                )
                predicate = PatientReportPredicate(report_kind="doctor_task")
            mission(item, MissionKind(instruction.kind), text, details, predicate)
            accepted.append(plain(text))
        for i, text in enumerate(candidate.alerts):
            if proposal.blocked(f"alert:{i}"):
                continue
            alert = value_alert(text)
            if alert:
                from sanad.safety.alerts import refused_at_write
                from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT

                if refused_at_write(alert, SAFETY_POLICY_V1_CARDIOLOGY_DRAFT):
                    raise ValueError("alert_would_lower_kernel_floor")
                id = uuid4().hex
                version_id = id + ":1"
                models.extend(
                    (
                        CareOrderVersion(
                            id=version_id,
                            order_id=id,
                            order_version=1,
                            type="value_alert",
                            structured_instruction=alert,
                            provenance=self._provenance(proposal, actor, text),
                            confirmed_by=actor.subject,
                            confirmed_at=now,
                            **metadata,
                        ),
                        CareOrderHead(
                            id=id,
                            order_id=id,
                            current_order_version=1,
                            current_version_id=version_id,
                            type="value_alert",
                            name=alert.metric,
                            status="active",
                            changed_by=actor.subject,
                            changed_at=now,
                            **metadata,
                        ),
                    )
                )
                order_refs.append(
                    VersionRef(entity_type="care_order_version", id=version_id, version=1)
                )
            else:
                models.append(
                    ClinicalFact(
                        id=uuid4().hex,
                        category="condition",
                        payload=FactPayload(text=text, tags=("alert_text",)),
                        provenance=self._provenance(proposal, actor, text),
                        **metadata,
                    )
                )
            accepted.append(
                ("Notify me if: " if doctor.language == "en" else "بلّغني لو: ") + plain(text)
            )
        if (
            not accepted
            and not proposal.creating_patient
            and any(a.noop for a in proposal.amendments)
        ):
            return (
                tuple(m for m in models if isinstance(m, (PatientMedia, PhotoAssociationWork))),
                patient,
                tuple(
                    a.new.drug + wording.label("unchanged", doctor.language)
                    for a in proposal.amendments
                    if a.noop
                ),
            )
        models.append(
            CarePlan(
                id=plan_id,
                plan_id=plan_id,
                order_refs=tuple(order_refs),
                mission_ids=tuple(mission_ids),
                followup_ids=tuple(followup_ids),
                confirmed_by=actor.subject,
                confirmed_at=now,
                source_proposal_id=proposal.id,
                accepted_items=tuple(accepted),
                blocked_items=tuple(i for i in proposal.issues if i.blocked),
                **metadata,
            )
        )
        return tuple(models), patient, tuple(accepted)

    def confirm(
        self,
        proposal: Proposal,
        token: ScribeCallback,
        actor: Principal,
        command_id: str,
        *,
        claim: Claim | None = None,
    ) -> ConfirmationResult:
        now = self.repo.clock()
        doctor = self.claims.doctor(actor)
        if doctor is None:
            if proposal.doctor_id == actor.doctor_id:
                self.invalidate(proposal)
            return ConfirmationResult("forbidden", "scribe_stale")
        if proposal.expires_at <= now:
            self.expire(proposal)
            return ConfirmationResult("expired", "scribe_expired")
        if (
            proposal.status != "pending"
            or proposal.editing
            or proposal.pending_reply
            or token.consumed_at
            or token.id != proposal.confirmation_nonce_hash
            or token.proposal_id != proposal.id
            or token.action != "confirm"
            or token.expires_at <= now
            or token.proposal_version != proposal.version
            or token.actor_subject != actor.subject
            or proposal.doctor_id != doctor.id
        ):
            return ConfirmationResult("stale", "scribe_stale")
        if (proposal.creating_patient and proposal.blocked("patient")) or not (
            proposal.creating_patient or proposal.selected_patient_id
        ):
            return ConfirmationResult("clarification", "scribe_stale")
        if proposal.photo:
            from sanad.scribe.photos import PhotoTurn

            if not PhotoTurn.confirmable(proposal):
                return ConfirmationResult("clarification", "scribe_stale")
        if proposal.blocked("all"):
            return ConfirmationResult("clarification", "scribe_stale")
        lease = None
        try:
            if not proposal.creating_patient:
                scope = PatientScope(
                    doctor_id=doctor.id, patient_id=proposal.selected_patient_id or ""
                )
                lease = self.repo.store.acquire_patient(
                    scope, "scribe:" + command_id, now, timedelta(minutes=5)
                )
                if lease is None:
                    return self.reject(
                        proposal, actor, command_id, reason="patient_busy", claim=claim
                    )
                for ref in proposal.base_versions:
                    row = self.repo.store.get(scope, ref.entity_type, ref.id)
                    if row is None or row.version != ref.version:
                        return self.reject(
                            proposal, actor, command_id, reason="stale_version", claim=claim
                        )
            models, patient, accepted = self._compile(proposal, actor, doctor)
            from sanad.concierge.answer_command import flag_amended_answers

            models += flag_amended_answers(self.repo.store, proposal, models, now)
            from sanad.scribe.resolver import learn

            learned = learn(self.repo.store, doctor, proposal, lambda: now)
            models += learned
            if proposal.creating_patient or proposal.invitation_requested:
                models += (
                    InvitationWork(
                        id=proposal.id,
                        scope=proposal.scope,
                        patient_id=patient.id,
                        proposal_id=proposal.id,
                        created_at=now,
                        updated_at=now,
                        work_clock=OperationalClock(next_action_at=now, work_lane="scribe"),
                    ),
                )
            changed = revise(proposal, now, status="confirmed", work_clock=None)
            state = self.repo.state(proposal.scope)
            if state is None or state.pending_proposal_id != proposal.id:
                return ConfirmationResult("stale", "scribe_stale")
            item_limit = min(160, 2600 // max(1, len(accepted)))
            body = "\n".join(
                "• " + (line if len(line) <= item_limit else line[:item_limit] + "…")
                for line in accepted
            ) or (
                "No items could be recorded."
                if doctor.language == "en"
                else "مفيش بنود صالحة للتسجيل."
            )
            from sanad.scribe.english import REASONS as ENGLISH_REASONS
            from sanad.scribe.english import render_wording

            english = doctor.language == "en"
            if any(i.blocked for i in proposal.issues):
                body += ("\nNot recorded:\n" if english else "\nمش هيتسجل:\n") + "\n".join(
                    "• " + (ENGLISH_REASONS if english else REASONS)[code]
                    for code in dict.fromkeys(i.code for i in proposal.issues if i.blocked)
                )
            intent = self.repo.intent(
                doctor,
                "scribe_confirmed",
                {"text": render_wording("scribe_confirmed", "en" if english else "ar", body=body)},
                command_id,
                proposal=changed,
            )
            result = self.repo.commit(
                actor,
                "ScribeConfirm",
                command_id,
                (
                    *models,
                    changed,
                    revise(token, now, consumed_at=now),
                    revise(state, now, pending_proposal_id=None),
                ),
                (intent,),
                reads=(read_of(state),),
                claim=claim,
                fence=lease,
                payload={"proposal_id": proposal.id, "nonce_hash": token.id},
            )
            if result.status in {"accepted", "duplicate"}:
                return ConfirmationResult(
                    result.status,
                    "scribe_confirmed",
                    patient.id,
                    proposal.creating_patient or proposal.invitation_requested,
                )
            return self.reject(
                proposal, actor, command_id + ":stale", reason="stale_version", claim=claim
            )
        except EffectsRejected:
            return self.reject(
                proposal, actor, command_id + ":invalid", reason="stale_version", claim=claim
            )
        finally:
            if lease:
                self.repo.store.release_patient(lease)

    def reject(
        self,
        proposal: Proposal,
        actor: Principal,
        command_id: str,
        *,
        reason: str = "doctor",
        claim: Claim | None = None,
        token: ScribeCallback | None = None,
    ) -> ConfirmationResult:
        doctor = self.claims.doctor(actor)
        if doctor is None or proposal.doctor_id != doctor.id or proposal.status != "pending":
            if doctor is None and proposal.doctor_id == actor.doctor_id:
                self.invalidate(proposal)
            return ConfirmationResult("stale", "scribe_stale")
        now = self.repo.clock()
        if proposal.expires_at <= now:
            self.expire(proposal)
            return ConfirmationResult("expired", "scribe_expired")
        changed = revise(proposal, now, status="rejected", reason=reason, work_clock=None)
        models: tuple[BaseModel, ...] = (changed,)
        state = self.repo.state(proposal.scope)
        if state and state.pending_proposal_id == proposal.id:
            models += (revise(state, now, pending_proposal_id=None),)
        if token:
            models += (revise(token, now, consumed_at=now),)
        template = "scribe_discarded" if reason == "doctor" else "scribe_stale"
        intent = self.repo.intent(
            doctor,
            template,
            {"text": self._wording(template, doctor)},
            command_id,
            proposal=changed,
        )
        result = self.repo.commit(
            actor,
            "ScribeAction",
            command_id,
            models,
            (intent,),
            claim=claim,
            reason=reason,
            payload={"proposal_id": proposal.id, **({"nonce_hash": token.id} if token else {})},
        )
        return ConfirmationResult(result.status, template)

    def edit(
        self,
        proposal: Proposal,
        token: ScribeCallback,
        actor: Principal,
        command_id: str,
        *,
        claim: Claim | None = None,
    ) -> ConfirmationResult:
        doctor = self.claims.doctor(actor)
        if doctor is None:
            return ConfirmationResult("forbidden", "scribe_stale")
        now = self.repo.clock()
        if proposal.expires_at <= now:
            self.expire(proposal)
            return ConfirmationResult("expired", "scribe_expired")
        if (
            proposal.doctor_id != doctor.id
            or proposal.status != "pending"
            or token.action != "edit"
            or token.proposal_id != proposal.id
            or token.proposal_version != proposal.version
            or token.actor_subject != actor.subject
            or token.consumed_at
            or token.expires_at <= now
        ):
            return ConfirmationResult("stale", "scribe_stale")
        changed = revise(proposal, now, editing=True)
        from sanad.scribe.card import clinical_line, split_card
        from sanad.scribe.english import render_wording
        from sanad.scribe.policy import DRAFT_SCRIBE_POLICY

        text = render_wording("scribe_edit", doctor.language)
        if (
            not proposal.photo
            and len(proposal.candidate.facts) > DRAFT_SCRIBE_POLICY.history_lines_max
        ):
            text += "\n" + (
                "Full history:\n" if doctor.language == "en" else "التاريخ المرضي كامل:\n"
            )
            text += "\n".join(
                clinical_line(
                    proposal.model_copy(update={"language": doctor.language}), f"fact:{i}", f.text
                )
                for i, f in enumerate(proposal.candidate.facts)
                if not any(
                    x.item == f"fact:{i}" and x.code == "unsafe_text" for x in proposal.issues
                )
            )
        intents = tuple(
            self.repo.intent(
                doctor, "scribe_edit", {"text": part}, command_id, proposal=changed, sequence=i
            )
            for i, part in enumerate(split_card(text))
        )
        result = self.repo.commit(
            actor,
            "ScribeAction",
            command_id,
            (changed, revise(token, now, consumed_at=now)),
            intents,
            claim=claim,
            payload={"proposal_id": proposal.id, "nonce_hash": token.id},
        )
        return ConfirmationResult(result.status, "scribe_edit")

    def invalidate(self, proposal: Proposal) -> CommitResult | None:
        if proposal.status != "pending":
            return None
        now = self.repo.clock()
        actor = Principal(
            subject="scribe-invalidate", actor_kind="system", doctor_id=proposal.doctor_id
        )
        models: tuple[BaseModel, ...] = (
            revise(proposal, now, status="rejected", reason="authority_changed", work_clock=None),
        )
        state = self.repo.state(proposal.scope)
        if state and state.pending_proposal_id == proposal.id:
            models += (revise(state, now, pending_proposal_id=None),)
        return self.repo.commit(actor, "ScribeInvalidate", "invalidate:" + proposal.id, models)

    def expire(self, proposal: Proposal) -> CommitResult | None:
        now = self.repo.clock()
        if proposal.status != "pending" or proposal.expires_at > now:
            return None
        actor = Principal(
            subject="scribe-expiry", actor_kind="system", doctor_id=proposal.doctor_id
        )
        models: tuple[BaseModel, ...] = (revise(proposal, now, status="expired", work_clock=None),)
        state = self.repo.state(proposal.scope)
        if state and state.pending_proposal_id == proposal.id:
            models += (revise(state, now, pending_proposal_id=None),)
        return self.repo.commit(actor, "ScribeExpire", "expire:" + proposal.id, models)
