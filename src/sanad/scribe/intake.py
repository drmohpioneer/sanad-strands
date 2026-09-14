"""Doctor-private intake, independently timed clarification and urgent concerns."""

from typing import TYPE_CHECKING

from pydantic import BaseModel, JsonValue

from sanad.auth.service import read_of, revise
from sanad.auth.tokens import issue_token
from sanad.channels.telegram import wording
from sanad.domain import DRAFT_POLICY_2026_09, CreateReview, Principal, ReviewKind, create_review
from sanad.media.vision import DocumentRead
from sanad.scribe.patients import panel
from sanad.scribe.policy import DRAFT_SCRIBE_POLICY
from sanad.steward.types import records
from sanad.store import keys
from sanad.store.keys import IntakeScope
from sanad.store.records import (
    Doctor,
    IntakeCallback,
    IntakeConcern,
    IntakeDraft,
    OperationalClock,
    OutboundIntent,
)

if TYPE_CHECKING:
    from sanad.media.retrieve import MediaRetriever
    from sanad.scribe.turn import ScribeTurn


class IntakeService:
    def __init__(self, turn: "ScribeTurn"):
        self.turn, self.repo = turn, turn.repo

    def create(
        self,
        actor: Principal,
        receipt_id: str,
        reads: DocumentRead,
        kind: str,
        *,
        pdf_retriever: "MediaRetriever | None" = None,
    ) -> IntakeDraft:
        doctor = self.turn.claims.doctor(actor)
        if doctor is None:
            raise ValueError("intake_authority")
        id, now = keys.digest(receipt_id), self.repo.clock()
        existing = self.repo.load(doctor.scope, "intake_draft", id, IntakeDraft)
        if existing and pdf_retriever is None:
            return existing
        draft = IntakeDraft.model_validate(
            {
                "id": id,
                "scope": doctor.scope,
                "owner_doctor_id": doctor.id,
                "source_receipt_ids": (receipt_id,),
                "media_work_ids": (id,),
                "reads": reads,
                "kind": kind,
                "reader_policy_version": self.turn.runtime.safety_policy.policy_version,
                "created_at": now,
                "updated_at": now,
                "review_at": now + DRAFT_SCRIBE_POLICY.intake_review_interval,
                "work_clock": OperationalClock(
                    next_action_at=now + DRAFT_SCRIBE_POLICY.intake_review_interval,
                    work_lane="scribe",
                ),
            }
        )
        if pdf_retriever:
            from sanad.media.documents import (
                check_item_size,
                commit_pdf_intake,
                failed_document_read,
            )
            from sanad.store.records import MediaWork, to_record

            parent = self.repo.load(pdf_retriever.scope, "media_work", id, MediaWork)
            assert parent
            draft = draft.model_copy(
                update={"document_page_refs": tuple(p.work_id for p in parent.document_pages)}
            )
            try:
                check_item_size(to_record(draft, draft.scope))
                # The proposal also carries its candidate, printable source and row
                # projection. Bound that combined core before finalizing the parent;
                # the 50 KiB difference reserves its authority/lifecycle envelope.
                import json

                from sanad.media.documents import AGGREGATE_BYTES
                from sanad.media.limits import MediaInvalid
                from sanad.scribe.crosscheck import candidate_from, projected_items, source_text

                projected = candidate_from(reads, kind, self.turn.runtime.safety_policy)
                core = {
                    "reads": reads.model_dump(mode="json"),
                    "candidate": projected.model_dump(mode="json"),
                    "source_text": source_text(reads),
                    "rows": [r.model_dump(mode="json") for r in projected_items(reads)[0]]
                    if kind == "prescription"
                    else [],
                }
                if len(json.dumps(core, ensure_ascii=False).encode()) > AGGREGATE_BYTES:
                    raise MediaInvalid("document_too_detailed")
            except ValueError:
                reads = failed_document_read(
                    "document_too_detailed", reads.first.provenance, parent.document_pages
                )
                draft = draft.model_copy(update={"reads": reads, "kind": "other"})

            if existing:
                draft = revise(
                    existing,
                    now,
                    reads=reads,
                    kind=draft.kind,
                    document_page_refs=draft.document_page_refs,
                )
            result = commit_pdf_intake(self.repo, actor, draft, pdf_retriever, existing is not None)
        else:
            result = self.repo.commit(actor, "IntakeCreate", "intake:" + id, (draft,))
        if result.status not in {"accepted", "duplicate"}:
            raise RuntimeError("intake_create_retry")
        saved = self.repo.load(doctor.scope, "intake_draft", id, IntakeDraft)
        assert saved is not None
        return saved

    def buttons(
        self, draft: IntakeDraft, doctor: Doctor, actor: Principal, language: str
    ) -> tuple[tuple[BaseModel, ...], JsonValue]:
        now = self.repo.clock()
        choices = [
            ("select", p.display_name, p.id)
            for p in sorted(
                panel(self.repo.store, doctor.scope),
                key=lambda p: (p.updated_at, p.id),
                reverse=True,
            )[:5]
        ] + [
            ("new", wording.button("new", language), None),
            ("later", wording.button("later", language), None),
        ]
        models: list[BaseModel] = []
        rows: list[JsonValue] = []
        for action, label, patient_id in choices:
            token = issue_token()
            models.append(
                IntakeCallback.model_validate(
                    {
                        "id": token.hash,
                        "scope": doctor.scope,
                        "intake_id": draft.id,
                        "intake_version": draft.version,
                        "actor_subject": actor.subject,
                        "action": action,
                        "patient_id": patient_id,
                        "expires_at": now + DRAFT_SCRIBE_POLICY.intake_review_interval,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
            )
            rows.append([{"text": label, "callback_data": token.secret.get_secret_value()}])
        return tuple(models), {"inline_keyboard": rows}

    def concern(
        self,
        draft: IntakeDraft,
        actor: Principal,
        facts: dict[str, JsonValue],
        *,
        source_key: str | None = None,
    ) -> IntakeDraft:
        scope = IntakeScope(doctor_id=draft.owner_doctor_id, intake_id=draft.id)
        unique = "intake-lab:" + draft.id + (":" + source_key if source_key else "")
        id = keys.digest(unique)
        for _ in range(5):
            old = self.repo.load(scope, "intake_concern", id, IntakeConcern)
            if old:
                current = self.repo.load(draft.scope, "intake_draft", draft.id, IntakeDraft)
                assert current is not None
                return current
            doctor = self.turn.claims.doctor(actor)
            if doctor is None:
                raise ValueError("intake_authority")
            now = self.repo.clock()
            review = create_review(
                CreateReview(
                    event_id=id,
                    source_type="intake",
                    source_id=draft.id,
                    source_version=draft.safety_epoch + 1,
                    review_kind=ReviewKind.incident_response,
                    owner_doctor_id=doctor.id,
                    review_at=now,
                ),
                now,
                DRAFT_POLICY_2026_09,
            ).aggregate
            alert = self.repo.intent(
                doctor,
                "liaison:DANGER",
                {
                    "text": wording.label("intake_danger", doctor.language),
                    "facts": facts,
                },
                id,
            )
            alert = OutboundIntent.model_validate(
                alert.model_dump()
                | {
                    "scope": scope,
                    "notification_purpose": "DANGER",
                    "eligibility_class": "urgent",
                    "source_versions": (),
                }
            )
            concern = IntakeConcern(
                id=id,
                scope=scope,
                intake_id=draft.id,
                owner_doctor_id=doctor.id,
                unique_source_key=unique,
                source_receipt_ids=tuple(dict.fromkeys((*draft.source_receipt_ids, source_key)))
                if source_key
                else draft.source_receipt_ids,
                rule_version=self.turn.runtime.safety_policy.policy_version,
                severity="danger",
                facts=facts,
                review_obligation_id=review.id,
                prior_delivery_refs=(alert.id,),
                created_at=now,
                updated_at=now,
            )
            changed = revise(draft, now, safety_epoch=draft.safety_epoch + 1)
            result = self.repo.commit(
                actor,
                "IntakeDanger",
                "danger:" + id,
                (changed, concern, review),
                (alert,),
                reads=(read_of(draft),),
            )
            if result.status in {"accepted", "duplicate"}:
                return changed
            current = self.repo.load(draft.scope, "intake_draft", draft.id, IntakeDraft)
            if current is None:
                break
            draft = current
        raise RuntimeError("intake_concern_retry")

    def sweep(self, draft: IntakeDraft) -> None:
        now = self.repo.clock()
        if draft.state != "pending" or draft.review_at > now or draft.review_obligation_id:
            return
        review = create_review(
            CreateReview(
                event_id="intake-review:" + draft.id,
                source_type="intake",
                source_id=draft.id,
                source_version=1,
                review_kind=ReviewKind.intake_clarification,
                owner_doctor_id=draft.owner_doctor_id,
                review_at=draft.review_at,
            ),
            now,
            DRAFT_POLICY_2026_09,
        ).aggregate
        actor = Principal(
            subject="intake-review", actor_kind="system", doctor_id=draft.owner_doctor_id
        )
        self.repo.commit(
            actor,
            "IntakeReview",
            "intake-review:" + draft.id,
            (revise(draft, now, review_obligation_id=review.id, work_clock=None), review),
            reads=(read_of(draft),),
        )

    def associated_concern(self, draft: IntakeDraft, patient_id: str) -> tuple[BaseModel, ...]:
        scope = IntakeScope(doctor_id=draft.owner_doctor_id, intake_id=draft.id)
        from sanad.store.records import from_record

        return tuple(
            revise(
                concern,
                self.repo.clock(),
                state="associated",
                associated_patient_id=patient_id,
                associated_event_id="photo-lab:" + draft.id,
            )
            for row in records(self.repo.store, scope, "intake_concern")
            if (concern := from_record(row, IntakeConcern)).state == "open"
        )
