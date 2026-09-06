"""Already-classified incidents bypass ordinary leases. Screening belongs to 04."""

from datetime import datetime

from pydantic import JsonValue

from sanad.domain import CreateReview, PatientScope, ReviewKind, create_review
from sanad.steward.apply import CommitBuilder, make_intent
from sanad.steward.service import Steward, system_command
from sanad.store import keys
from sanad.store.records import DoctorAuthority, Incident, PatientProfile, from_record, to_record


class UrgentService:
    def __init__(self, steward: Steward, *, patient_template_id: str):
        # Supplied by the safety-policy adapter. This slice invents no clinical prose.
        if not patient_template_id.strip():
            raise ValueError("template ID required")
        self.steward, self.store, self.patient_template_id = (
            steward,
            steward.store,
            patient_template_id,
        )

    def raise_incident(
        self,
        scope: PatientScope,
        unique_source_key: str,
        facts: dict[str, JsonValue],
        severity: str,
        now: datetime,
    ) -> Incident:
        id = "incident:" + keys.digest(unique_source_key)
        existing = self.store.get(scope, "incident", id)
        if existing is not None:
            return from_record(existing, Incident)
        policy = self.steward.policy_provider(scope)
        for _ in range(5):
            profile = self.store.get_patient_profile(scope)
            doctor_row = self.store.get(scope, "doctor_authority", scope.doctor_id)
            if profile is None or doctor_row is None:
                raise ValueError("incident requires scoped recipient authority facts")
            doctor = from_record(doctor_row, DoctorAuthority)
            command = system_command(
                scope, id, {"source_key_digest": keys.digest(unique_source_key)}, now, lane="urgent"
            )
            builder = CommitBuilder(scope, command, now, policy, self.store)
            bumped = PatientProfile.model_validate(
                profile.model_dump()
                | {
                    "version": profile.version + 1,
                    "updated_at": now,
                    "safety_epoch": profile.safety_epoch + 1,
                }
            )
            review_result = create_review(
                CreateReview(
                    event_id=id,
                    source_type="incident",
                    source_id=id,
                    source_version=1,
                    review_kind=ReviewKind.incident_response,
                    owner_doctor_id=scope.doctor_id,
                    patient_id=scope.patient_id,
                    review_at=now,
                ),
                now,
                policy.timing,
            )
            from sanad.domain import VersionRef

            refs = (VersionRef(entity_type="incident", id=id, version=1),)
            outgoing = [
                make_intent(
                    scope,
                    id,
                    refs,
                    purpose,
                    id,
                    now,
                    policy,
                    doctor,
                    bumped,
                    audience=audience,
                    template_id=self.patient_template_id
                    if audience == "patient"
                    else "liaison:DANGER",
                )
                for audience, purpose in (
                    ("patient", "patient_safety_response"),
                    ("doctor", "DANGER"),
                )
            ]
            incident = Incident(
                id=id,
                scope=scope,
                created_at=now,
                updated_at=now,
                unique_source_key=unique_source_key,
                facts=facts,
                severity=severity,
                raised_at=now,
                review_obligation_id=review_result.aggregate.id,
                alert_intent_ids=tuple(i.id for i in outgoing),
                template_id=self.patient_template_id,
            )
            builder.put(to_record(bumped, scope))
            builder.put(to_record(incident, scope))
            builder.add(review_result)
            builder.audit("INCIDENT_RAISED", keys.digest(id + ":raised"), refs)
            builder.intents.update({i.id: to_record(i, scope) for i in outgoing})
            result = self.store.commit_incident(builder.finish())
            if result.status in {"accepted", "duplicate"}:
                saved = self.store.get(scope, "incident", id)
                assert saved is not None
                return from_record(saved, Incident)
            if result.status != "stale_version":
                raise ValueError("incident transaction rejected: " + result.status)
            existing = self.store.get(scope, "incident", id)
            if existing is not None:
                return from_record(existing, Incident)
        raise RuntimeError("incident contention; retry the same source key")
