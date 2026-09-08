"""Closed wording choices over authoritative executor facts, never eligibility."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from sanad.contact.ladder import ContactPlan
from sanad.domain import FollowUpTask, Mission, PatientScope
from sanad.evidence.classify import Category, category_alias, expected
from sanad.evidence.evaluate import evaluate
from sanad.monitor.executor import current_details
from sanad.monitor.slots import coverage
from sanad.steward.types import records
from sanad.store.keys import digest
from sanad.store.protocol import Store
from sanad.store.records import Evidence, EvidenceHead, Patient, from_record


@dataclass(frozen=True)
class Fact:
    id: str
    kind: Literal["title", "due", "slot", "category", "visit", "task"]
    value: str


@dataclass(frozen=True)
class Move:
    id: str
    fact_ids: tuple[str, ...]


@dataclass(frozen=True)
class Permitted:
    source_id: str
    source_version: int
    plan: ContactPlan | None
    template: str
    first_contact: bool
    pre_visit_brief: bool
    facts: tuple[Fact, ...]
    moves: tuple[Move, ...]
    reason: str | None = None

    @property
    def digest(self) -> str:
        # Includes instants, scope-bound IDs and exact current source values.
        return digest(repr(self))


def local_time(instant: datetime, patient: Patient) -> str:
    return instant.astimezone(ZoneInfo(patient.timezone)).strftime("%Y-%m-%d %H:%M")


def compute(
    store: Store,
    source: Mission | FollowUpTask,
    patient: Patient,
    plan: ContactPlan | None,
    template: str,
) -> Permitted:
    scope = PatientScope(doctor_id=source.doctor_id, patient_id=source.patient_id)
    first = isinstance(source, Mission) and source.contact_count == 0
    brief = template == "patient_visit_brief"
    base = (source.id, source.version, plan, template, first, brief)
    if patient.scope != scope:
        return Permitted(*base, (), (), "scope_mismatch")
    if plan is None:
        return Permitted(*base, (), (), "no_contact_plan")
    if isinstance(source, Mission) and source.state not in {"open", "waiting_patient"}:
        return Permitted(*base, (), (), "mission_ineligible")
    if isinstance(source, FollowUpTask):
        if source.state != "scheduled" or source.prompt_at != plan.at:
            return Permitted(*base, (), (), "followup_ineligible")
        return Permitted(*base, (), (Move("schedule_next_contact", ()),))
    if source.kind == "QUESTION" or (
        source.details.kind == "TASK" and source.details.completion_rule == "unsupported_action"
    ):
        return Permitted(*base, (), (), "unsupported_contact")
    if source.kind == "MEDICATION" or brief:
        return Permitted(*base, (), (Move("schedule_next_contact", ()),))
    prefix = digest(f"{scope.doctor_id}:{scope.patient_id}:{source.id}:{source.version}")
    facts: list[Fact] = []

    def add(kind: Literal["title", "due", "slot", "category", "visit", "task"], value: str) -> None:
        facts.append(Fact(f"{prefix}:{kind}:{len(facts)}", kind, value))

    add("title", source.title)
    add("due", local_time(source.due_at, patient))
    summary = Move("schedule_next_contact", tuple(f.id for f in facts))
    if source.details.kind == "MONITOR":
        details = current_details(store, scope, source)
        missing = coverage(details, plan.at).missing
        for gap in missing:
            add("slot", local_time(details.slots[int(gap.removeprefix("slot:"))], patient))
    else:
        for category in missing_categories(store, scope, source, plan.at):
            add("category", category)
        if source.details.kind == "VISIT" and not expected(source):
            add("visit", source.details.objective)
        if source.details.kind == "TASK" and not expected(source):
            add("task", source.details.completion_rule)
    moves = [summary]
    if len(facts) > 2:
        moves.append(Move("request_missing_evidence", tuple(f.id for f in facts)))
    return Permitted(*base, tuple(facts), tuple(moves))


def missing_categories(
    store: Store, scope: PatientScope, mission: Mission, at: datetime
) -> tuple[Category, ...]:
    """Use the accepted evaluator's gaps; never ask the model to assess coverage."""
    categories = expected(mission)
    if not categories:
        return ()
    documents: list[Evidence] = []
    for row in records(store, scope, "evidence_head"):
        head = from_record(row, EvidenceHead)
        if head.mission_id != mission.id or head.status not in {
            "accepted",
            "accepted_pending_identity",
        }:
            continue
        evidence_row = store.get(scope, "evidence", f"{head.id}:{head.current_version}")
        if evidence_row:
            documents.append(from_record(evidence_row, Evidence))
    if not documents:
        return categories
    documents.sort(key=lambda e: (e.updated_at, e.id))
    result = evaluate(mission, documents[-1], documents[:-1], at)
    if result.satisfied or any(
        gap in {"identity", "verification", "doctor_acceptance", "readable_document"}
        for gap in result.missing
    ):
        return ()
    if mission.kind == "SEND_RECORDS":
        missing = {
            category_alias(gap.removeprefix("category:"))
            for gap in result.missing
            if gap.startswith("category:")
        }
        if missing:
            return tuple(c for c in categories if c in missing)
    return categories
