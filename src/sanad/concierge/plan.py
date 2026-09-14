"""Scoped projections from current heads. Doctor-private facts never enter bundles."""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, JsonValue

from sanad.concierge.records import ReportFactPayload
from sanad.domain import FollowUpTask, Mission, PatientScope, Principal, VersionRef
from sanad.domain.entities import TERMINAL_STATES
from sanad.domain.language import default_language, effective
from sanad.scribe.extract import OrderCandidate
from sanad.scribe.memory import NameVocabulary
from sanad.scribe.records import CareOrderHead, CareOrderVersion, ClinicalFact
from sanad.scribe.resolver import Context, resolve_name
from sanad.steward.types import bounded_records as records
from sanad.store.protocol import Store
from sanad.store.records import (
    Consent,
    Doctor,
    DoctorAuthority,
    Evidence,
    EvidenceHead,
    Patient,
    PatientBinding,
    PatientProfile,
    SubjectBinding,
    from_record,
    model_scope,
    to_record,
)


@dataclass(frozen=True)
class Snapshot:
    patient: Patient
    profile: PatientProfile
    consent: Consent
    binding: PatientBinding
    doctor: Doctor
    authority: DoctorAuthority
    heads: tuple[CareOrderHead, ...]
    orders: tuple[CareOrderVersion, ...]
    missions: tuple[Mission, ...]
    followups: tuple[FollowUpTask, ...]
    facts: tuple[ClinicalFact, ...]
    names: Context = Context()
    stopped_heads: tuple[CareOrderHead, ...] = ()
    stopped_orders: tuple[CareOrderVersion, ...] = ()
    reading_missions: tuple[Mission, ...] = ()
    reading_fact_refs: tuple[tuple[str, VersionRef], ...] = ()

    @property
    def acknowledgment_refs(self) -> tuple[VersionRef, ...]:
        return self.order_refs + tuple(
            VersionRef(entity_type="care_order", id=h.order_id, version=h.current_order_version)
            for h in self.stopped_heads
        )

    @property
    def scope(self) -> PatientScope:
        return self.patient.scope

    @property
    def order_refs(self) -> tuple[VersionRef, ...]:
        return tuple(
            VersionRef(entity_type="care_order", id=h.order_id, version=h.current_order_version)
            for h in self.heads
        )

    @property
    def expected(self) -> tuple[VersionRef, ...]:
        return tuple(
            to_record(v, model_scope(v)).ref
            for v in (
                self.patient,
                self.profile,
                self.consent,
                self.binding,
                self.authority,
                *self.heads,
                *self.orders,
                *self.missions,
                *self.followups,
                *self.stopped_heads,
                *self.stopped_orders,
            )
        )


def load(store: Store, scope: PatientScope, now: datetime) -> Snapshot | None:
    from sanad.monitor.executor import current_details
    from sanad.steward.corrections import current_facts

    def get[T: BaseModel](kind: str, id: str, schema: type[T]) -> T | None:
        row = store.get(scope, kind, id)
        return schema.model_validate(row.body) if row else None

    patient = get("patient", scope.patient_id, Patient)
    profile = store.get_patient_profile(scope)
    doctor = get("doctor", scope.doctor_id, Doctor)
    authority = get("doctor_authority", scope.doctor_id, DoctorAuthority)
    if not patient or not profile or not doctor or not authority:
        return None
    consent = get("consent", patient.consent_id or "", Consent)
    binding = get("patient_binding", patient.active_binding_id or "", PatientBinding)
    if (
        patient.contact_status in {"frozen", "awaiting_link"}
        or doctor.status != "approved"
        or not authority.approved
        or authority.auth_epoch != doctor.auth_epoch
        or not profile.binding_active
        or not profile.consent_active
        or not consent
        or consent.withdrawn_at is not None
        or not binding
        or binding.status != "active"
        or not (
            consent.version
            == patient.consent_version
            == binding.consent_version
            == profile.consent_version
        )
        or consent.binding_id != binding.id
    ):
        return None
    heads, orders = [], []
    stopped_heads, stopped_orders = [], []
    for row in records(store, scope, "care_order_head"):
        h = from_record(row, CareOrderHead)
        if h.status == "stopped" and h.type == "medication":
            stopped = get("care_order_version", h.current_version_id, CareOrderVersion)
            authority_row = store.get(scope, "care_order", h.order_id)
            if (
                stopped
                and isinstance(stopped.structured_instruction, OrderCandidate)
                and stopped.structured_instruction.action == "stop"
                and authority_row
                and authority_row.version == h.current_order_version
                and authority_row.body.get("status") == "stopped"
            ):
                stopped_heads.append(h)
                stopped_orders.append(stopped)
        if h.status != "active" or h.type != "medication":
            continue
        order = get("care_order_version", h.current_version_id, CareOrderVersion)
        active = store.get(scope, "care_order", h.order_id)
        if (
            order
            and isinstance(order.structured_instruction, OrderCandidate)
            and active
            and active.version == h.current_order_version
            and active.body.get("status") == "active"
            and (order.effective_to is None or order.effective_to > now)
        ):
            heads.append(h)
            orders.append(order)
    mission_rows = tuple(records(store, scope, "mission"))
    facts = tuple(
        from_record(r, ClinicalFact)
        for r in current_facts(store, scope, bounded=True)
        if r.body.get("visibility") == "patient_released"
        and r.body.get("category") == "patient_report"
    )
    released_refs = {to_record(f, scope).ref for f in facts}
    # Document reports and monitor observations can describe the same source row.
    # Resolve their current accepted evidence head without exposing its identity.
    document_sources = {
        fact.provenance.source_observation_id
        for fact in facts
        if fact.provenance.source_kind == "document_observation"
        and isinstance(fact.payload, ReportFactPayload)
        and fact.payload.report_kind == "reading"
    }
    evidence_refs = {}
    if document_sources:
        for row in records(store, scope, "evidence"):
            evidence = from_record(row, Evidence)
            if evidence.observation_id not in document_sources:
                continue
            head = get("evidence_head", evidence.evidence_id, EvidenceHead)
            if (
                head
                and head.current_version == evidence.version
                and head.status == "accepted"
                and not evidence.identity_pending
            ):
                evidence_refs[evidence.observation_id] = row.ref
    reading_fact_refs = tuple(
        (fact.id, evidence_refs[fact.provenance.source_observation_id])
        for fact in facts
        if fact.provenance.source_observation_id in evidence_refs
    )
    reading_missions = []
    for row in mission_rows:
        if row.body.get("kind") != "MONITOR" or row.body.get("state") == "proposed":
            continue
        mission = from_record(row, Mission)
        details = current_details(store, scope, mission)
        details = details.model_copy(
            update={
                "readings": tuple(
                    entry
                    for entry in details.readings
                    if entry.source_ref.entity_type == "evidence"
                    or entry.source_ref in released_refs
                )
            }
        )
        reading_missions.append(mission.model_copy(update={"details": details}))
    return Snapshot(
        patient,
        profile,
        consent,
        binding,
        doctor,
        authority,
        tuple(heads),
        tuple(orders),
        tuple(
            from_record(r, Mission)
            for r in mission_rows
            if r.body.get("state") not in TERMINAL_STATES | {"proposed"}
        ),
        tuple(from_record(r, FollowUpTask) for r in records(store, scope, "followup")),
        facts,
        Context(vocabulary=NameVocabulary(store, doctor)),
        tuple(stopped_heads),
        tuple(stopped_orders),
        tuple(reading_missions),
        reading_fact_refs,
    )


def authorized(
    store: Store, principal: Principal, binding: SubjectBinding, now: datetime
) -> Snapshot | None:
    if principal.actor_kind != "patient" or not principal.doctor_id or not principal.patient_id:
        return None
    current = store.authorize(binding.scope.bot_id, principal.subject)
    if current.principal != principal or current.binding != binding or binding.status != "active":
        return None
    snapshot = load(
        store, PatientScope(doctor_id=principal.doctor_id, patient_id=principal.patient_id), now
    )
    if (
        not snapshot
        or snapshot.binding.subject != principal.subject
        or snapshot.profile.recipient_subject != principal.subject
        or snapshot.binding.binding_epoch != binding.binding_epoch
    ):
        return None
    return snapshot


def order_line(
    order: CareOrderVersion,
    language: str = default_language,
    *,
    history: bool = False,
    names: Context | None = None,
) -> str:
    language = effective(language, audience="patient")
    instruction = order.structured_instruction
    assert isinstance(instruction, OrderCandidate)
    resolved = resolve_name(instruction.drug, "drug", instruction.drug, ctx=names)
    fields = [
        resolved.latin or instruction.drug,
        instruction.dose,
        instruction.frequency,
        instruction.timing,
        instruction.route,
        instruction.duration,
    ]
    prefix = (
        ("History (not current): " if history else "Your doctor prescribed: ")
        if language == "en"
        else ("تاريخ سابق، مش الخطة الحالية: " if history else "الدكتور قالك: ")
    )
    return prefix + (", " if language == "en" else "، ").join(x for x in fields if x)


def summary(snapshot: Snapshot) -> dict[str, JsonValue]:
    patient = snapshot.patient
    orders: list[JsonValue] = []
    for order in snapshot.orders:
        instruction = order.structured_instruction
        assert isinstance(instruction, OrderCandidate)
        orders.append(
            {
                "order_id": order.order_id,
                "version": order.order_version,
                "drug": resolve_name(
                    instruction.drug, "drug", instruction.drug, ctx=snapshot.names
                ).latin
                or instruction.drug,
                "dose": instruction.dose,
                "frequency": instruction.frequency,
                "timing": instruction.timing,
                "route": instruction.route,
                "duration": instruction.duration,
                "effective_from": order.effective_from.isoformat()
                if order.effective_from
                else None,
                "line": order_line(order, patient.language, names=snapshot.names),
            }
        )
    missions: list[JsonValue] = [
        {
            "id": m.id,
            "kind": m.kind.value,
            "title": m.title,
            "due_at": m.due_at.astimezone(ZoneInfo(patient.timezone)).isoformat(),
            "state": m.state.value,
        }
        for m in sorted(snapshot.missions, key=lambda m: (m.due_at, m.id))
        if m.kind != "QUESTION"
    ]
    return {
        "doctor_name": snapshot.doctor.name,
        "orders": orders,
        "next_mission": missions[0] if missions else None,
        "next_missions": missions,
    }


def render_summary(snapshot: Snapshot) -> str:
    en = effective(snapshot.patient.language, audience="patient") == "en"
    lines = [("Your doctor: " if en else "دكتورك: ") + snapshot.doctor.name]
    lines.extend(
        order_line(o, snapshot.patient.language, names=snapshot.names) for o in snapshot.orders
    )
    if not snapshot.orders:
        lines.append("No active medication is recorded." if en else "مفيش دوا حالي مسجل في الخطة.")
    if any(
        isinstance(f.payload, ReportFactPayload)
        and f.payload.report_kind
        in {"medication_start", "medication_stop", "medication_change", "day3"}
        for f in snapshot.facts
    ):
        lines.append(
            "Medication reports: self-reported." if en else "بلاغات الدوا: حسب كلام المريض."
        )
    data = summary(snapshot)
    next_mission = data["next_mission"]
    if isinstance(next_mission, dict):
        lines.append(
            ("Next requested task: " if en else "المطلوب بعد كده من الدكتور: ")
            + str(next_mission["title"])
            + ": "
            + str(next_mission["due_at"])
        )
    return "\n".join(lines)


def projection(snapshot: Snapshot) -> dict[str, JsonValue]:
    readings = [
        f
        for f in snapshot.facts
        if isinstance(f.payload, ReportFactPayload) and f.payload.report_kind == "reading"
    ]
    last = max(readings, key=lambda f: f.created_at) if readings else None
    history: dict[tuple[str, str, int, int], JsonValue] = {}
    document_refs = dict(snapshot.reading_fact_refs)
    for fact in readings:
        assert isinstance(fact.payload, ReportFactPayload)
        if fact.provenance.source_kind == "document_observation" and fact.id not in document_refs:
            continue
        ref = document_refs.get(fact.id, to_record(fact, snapshot.scope).ref)
        for index, reading in enumerate(fact.payload.readings):
            history[(ref.entity_type, ref.id, ref.version, index)] = {
                "metric": reading.analyte,
                "unit": reading.raw_unit,
                "value": reading.raw_value,
                "observed_at": None,
                "received_at": None,
                "recorded_at": fact.created_at.isoformat(),
            }
    for mission in snapshot.reading_missions:
        from sanad.domain.entities import MonitorDetails

        assert isinstance(mission.details, MonitorDetails)
        for entry in mission.details.readings:
            ref = entry.source_ref
            history[(ref.entity_type, ref.id, ref.version, entry.reading_index)] = {
                "metric": mission.details.metric,
                "unit": mission.details.unit,
                "value": entry.value,
                "observed_at": entry.observed_at.isoformat(),
                "received_at": entry.received_at.isoformat(),
            }
    return {
        **summary(snapshot),
        "reading_history": list(history.values()),
        "medication_reports": [
            {
                "text": f.payload.text,
                "kind": f.payload.report_kind,
                "label": "Self-reported"
                if effective(snapshot.patient.language, audience="patient") == "en"
                else "حسب كلام المريض",
            }
            for f in snapshot.facts
            if isinstance(f.payload, ReportFactPayload)
            and f.payload.report_kind
            in {"medication_start", "medication_stop", "medication_change", "day3"}
        ],
        "last_reading": last.payload.model_dump(
            mode="json", exclude={"readings": {"__all__": {"judgment", "rule_id"}}}
        )
        if last
        else None,
        "preferences": {
            "contact_status": snapshot.patient.contact_status,
            "routine_contact_enabled": snapshot.consent.routine_contact_enabled,
            "resume_at": snapshot.patient.resume_at.isoformat()
            if snapshot.patient.resume_at
            else None,
            "quiet_hours": list(snapshot.consent.quiet_hours),
            "timezone": snapshot.patient.timezone,
        },
        "open_questions": [
            {"id": m.id, "question": m.details.question_text, "status": "في انتظار الدكتور"}
            for m in snapshot.missions
            if m.kind == "QUESTION" and hasattr(m.details, "question_text")
        ],
    }
