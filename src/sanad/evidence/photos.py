"""Add-only alert adapter around the accepted, sibling-owned photo turn."""

from sanad.channels.telegram.router import RouteResult
from sanad.domain import PatientScope, Principal
from sanad.evidence.screen import read_values, screen_values
from sanad.media.vision import DocumentRead
from sanad.scribe.crosscheck import PhotoReview, two_readers, unreadable_read
from sanad.scribe.extract import DictationCandidate, LabRowCandidate
from sanad.scribe.photos import PhotoTurn
from sanad.store import keys
from sanad.store.records import Claim, Doctor, InboundReceipt, IntakeConcern, IntakeDraft


def corroborated_values(read: DocumentRead) -> tuple[LabRowCandidate, ...]:
    """Rows slice 11d allows a danger decision to rest on.

    A document that failed the agreement gate reaches the doctor as the honest
    card, and a lone reader must not corroborate itself into a danger card. On
    such a document only a row both readers read identically may be screened,
    the same condition `scribe.photos.danger_facts` applies before it raises an
    incident. An editable document keeps 09b's behaviour, where either reader's
    row is screened.
    """
    if not two_readers(read):
        return ()
    if not unreadable_read(read):
        return read_values(read)
    rows: list[LabRowCandidate] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    for reader in read.readers:
        for row in reader.items:
            name = (row.item.name or "").strip()
            if not name or "[unreadable]" in name or row.item.value is None or not row.item.unit:
                continue
            if not all(
                any(
                    (other.item.name or "").strip().casefold() == name.casefold()
                    and other.item.value == row.item.value
                    and other.item.unit == row.item.unit
                    for other in reading.items
                )
                for reading in read.readers
            ):
                continue
            key = (name.casefold(), row.item.value, row.item.unit)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                LabRowCandidate(
                    analyte=name,
                    value=row.item.value,
                    unit=row.item.unit,
                    flag=row.item.flag,
                )
            )
    return tuple(rows)


class AlertPhotoTurn(PhotoTurn):
    def raise_patient(self, reads: DocumentRead, patient_id: str, actor: Principal) -> bool:
        values = corroborated_values(reads)
        if not values:
            return False
        if (
            self.turn.claims.doctor(actor) is None
            or self.turn.claims.patient(actor.doctor_id or "", patient_id) is None
        ):
            raise ValueError("photo_patient_authority")
        scope = PatientScope(doctor_id=actor.doctor_id or "", patient_id=patient_id)
        intake_id = keys.digest(reads.first.provenance.source_observation_id)
        concern = self.repo.load(
            keys.IntakeScope(doctor_id=scope.doctor_id, intake_id=intake_id),
            "intake_concern",
            keys.digest("intake-lab:" + intake_id),
            IntakeConcern,
        )
        incidents, _ = screen_values(
            self.turn.runtime.urgent,
            scope,
            values,
            reads.first.provenance.source_observation_id,
            reads.first.provenance.received_at,
            self.turn.runtime.safety_policy,
            prior=concern.prior_delivery_refs if concern else (),
        )
        if incidents:
            self.turn.checkpoint("photo_danger_persisted")
        return bool(incidents)

    def unreadable_card(
        self,
        receipt: InboundReceipt,
        actor: Principal,
        claim: Claim,
        doctor: Doctor,
        draft: IntakeDraft,
        selected: str | None,
    ) -> RouteResult:
        if selected:
            self.raise_patient(draft.reads, selected, actor)
        return super().unreadable_card(receipt, actor, claim, doctor, draft, selected)

    def screen_correction(
        self,
        candidate: DictationCandidate,
        photo: PhotoReview,
        receipt: InboundReceipt,
        actor: Principal,
        patient_id: str | None,
    ) -> PhotoReview:
        if not patient_id:
            return super().screen_correction(candidate, photo, receipt, actor, patient_id)
        if (
            self.turn.claims.doctor(actor) is None
            or self.turn.claims.patient(actor.doctor_id or "", patient_id) is None
        ):
            raise ValueError("photo_patient_authority")
        incidents, _ = screen_values(
            self.turn.runtime.urgent,
            PatientScope(doctor_id=actor.doctor_id or "", patient_id=patient_id),
            tuple(f.lab for f in candidate.facts if f.lab),
            receipt.id,
            receipt.received_at,
            self.turn.runtime.safety_policy,
        )
        if incidents:
            self.turn.checkpoint("photo_danger_persisted")
        return photo.model_copy(update={"danger_raised": photo.danger_raised or bool(incidents)})
