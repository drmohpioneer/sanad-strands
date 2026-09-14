"""Doctor-scoped panel lookup; provider output never carries a selected id."""

from sanad.domain import PatientScope, TenantScope
from sanad.scribe.extract import PatientCandidate
from sanad.scribe.policy import DRAFT_SCRIBE_POLICY
from sanad.scribe.proposal import PatientChoice
from sanad.store.protocol import Store
from sanad.store.records import Patient, PatientProfile, from_record


def normalized_name(name: str) -> str:
    """Reuse the accepted stub/index normalization without introducing fuzzy identity."""
    return " ".join(name.casefold().split())


def choice_of(patient: Patient) -> PatientChoice:
    return PatientChoice(
        patient_id=patient.id,
        display_name=patient.display_name,
        age=patient.age,
        sex=patient.sex,
        updated_at=patient.updated_at,
    )


def panel(store: Store, scope: TenantScope) -> tuple[Patient, ...]:
    patients = []
    rows, _ = store.list_patients(scope, limit=199)
    profiles = [from_record(row, PatientProfile) for row in rows]
    state = store.get(scope, "scribe_state", "current")
    pin = state.recent_patient_id if state else None
    if pin and not any(p.patient_id == pin for p in profiles):
        profile = store.get_patient_profile(PatientScope(doctor_id=scope.doctor_id, patient_id=pin))
        if profile:
            profiles.append(profile)
    for profile in profiles:
        own = PatientScope(doctor_id=scope.doctor_id, patient_id=profile.patient_id)
        patient = store.get(own, "patient", profile.patient_id)
        if patient:
            patients.append(from_record(patient, Patient))
    return tuple(patients)


def lookup(
    store: Store, scope: TenantScope, candidate: PatientCandidate
) -> tuple[PatientChoice, ...]:
    query = normalized_name(candidate.name_as_spoken or "").strip()
    # A spoken honorific is not a demographic or an identifier.
    for title in ("الحاج ", "الحاجة ", "الأستاذ ", "الاستاذ ", "أستاذ ", "استاذ "):
        if query.startswith(title):
            query = query.removeprefix(title)
            break
    ranked = []
    for patient in panel(store, scope):
        profile = store.get_patient_profile(patient.scope)
        if profile and profile.removed_at:
            continue
        name = normalized_name(patient.display_name)
        identifiers = set(patient.identifiers)
        exact_identifier = bool(identifiers.intersection(candidate.identifiers))
        if candidate.identifiers and not exact_identifier:
            continue
        score = (
            4
            if exact_identifier
            else 3
            if name == query
            else 2
            if query and name.startswith(query)
            else 1
            if query and set(query.split()) & set(name.split())
            else 0
        )
        if score or not query:
            ranked.append((score, patient))
    ranked.sort(key=lambda pair: (pair[0], pair[1].updated_at, pair[1].id), reverse=True)
    if ranked and query:
        ranked = [pair for pair in ranked if pair[0] == ranked[0][0]]
    return tuple(
        choice_of(p).model_copy(update={"score": score, "headline": headline(store, p)})
        for score, p in ranked[: DRAFT_SCRIBE_POLICY.max_candidates]
    )


def headline(store: Store, patient: Patient) -> str:
    from sanad.steward.types import bounded_records as records

    active = sum(
        row.body.get("status") == "active"
        for row in records(store, patient.scope, "care_order_head")
    )
    opened = sum(
        row.body.get("state")
        not in {"fulfilled", "cancelled", "expired", "closed_unfulfilled", "superseded"}
        for row in records(store, patient.scope, "mission")
    )
    return (
        f"{active} active medications, {opened} open requests"
        if active or opened
        else "no plan yet"
    )
