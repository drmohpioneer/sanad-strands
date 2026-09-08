"""Deterministic task reports and explicit doctor acceptance; no executors."""

import json
from functools import lru_cache
from importlib.resources import files
from secrets import token_urlsafe

from sanad.concierge.plan import Snapshot
from sanad.concierge.records import ReportFactPayload
from sanad.concierge.reports import fact
from sanad.concierge.text import contains, is_question
from sanad.concierge.visits import matches, original_time
from sanad.domain import Mission, Provenance, transition_mission
from sanad.domain import events as ev
from sanad.domain.predicates import PredicateResult
from sanad.steward.patient import PatientTurnCommit
from sanad.store import keys
from sanad.store.records import OutboundIntent, canonical_json, from_record, to_record


@lru_cache(maxsize=1)
def unsupported_verbs() -> tuple[str, ...]:
    source = files("sanad.scribe").joinpath("unsupported_tasks.yaml").read_text()
    return tuple(json.loads("\n".join(s for s in source.splitlines() if not s.startswith("#"))))


def unsupported(text: str) -> bool:
    return contains(text, *unsupported_verbs())


def marker(text: str, language: str) -> str:
    if not unsupported(text):
        return ""
    return (
        " (not supported: recorded as a request only)"
        if language == "en"
        else " (مش مدعوم: هيتسجل كطلب بس)"
    )


def is_task_done(text: str) -> bool:
    return (
        not is_question(text)
        and contains(text, "done", "finished", "completed", "عملت", "خلصت", "نفذت")
        and not contains(text, "not", "never", "haven't", "didn't", "مش", "لسه", "لم", "ما")
        and not contains(text, "مخلصتش", "معملتش", "مانفذتش")
    )


def task_missions(snapshot: Snapshot, text: str) -> tuple[Mission, ...]:
    return matches(snapshot, text, "TASK")


def record_task_done(
    tx: PatientTurnCommit,
    mission: Mission,
    text: str,
    *,
    source: Provenance | None = None,
    original_receipt_id: str | None = None,
) -> None:
    note = "self-reported; pending doctor acceptance"
    fact(
        tx,
        text,
        ReportFactPayload(
            report_kind="task_done",
            text=text,
            detail=note,
            target_ref=to_record(mission, tx.snapshot.scope).ref,
            original_receipt_id=original_receipt_id,
        ),
        source=source,
    )
    tx.builder.add(
        transition_mission(
            mission,
            ev.ObjectiveFulfilled(
                event_id=tx.id,
                fulfillment_event_id=tx.id,
                actor_kind="patient",
                objective_received_at=original_time(tx, original_receipt_id),
                danger_flag=False,
                predicate_result=PredicateResult(satisfied=True, detail=note, evaluated_at=tx.now),
            ),
            tx.now,
            tx.builder.policy.timing,
        )
    )
    from sanad.contact.delivery import task_done_payload
    from sanad.scribe.policy import DRAFT_SCRIBE_POLICY

    for id, row in tuple(tx.builder.intents.items()):
        intent = from_record(row, OutboundIntent)
        if intent.notification_purpose == "DONE:FULFILLMENT":
            accept, reopen = token_urlsafe(32), token_urlsafe(32)
            payload = task_done_payload(
                mission,
                tx.snapshot.doctor.language,
                accept,
                reopen,
                tx.snapshot.patient.display_name,
            )
            intent = intent.model_copy(
                update={
                    "task_accept_token_hash": keys.digest(accept),
                    "task_reopen_token_hash": keys.digest(reopen),
                    "task_action_expires_at": tx.now + DRAFT_SCRIBE_POLICY.proposal_ttl,
                    "payload": payload,
                    "payload_digest": keys.digest(canonical_json(payload).decode()),
                }
            )
            tx.builder.intents[id] = to_record(intent, tx.snapshot.scope)
    tx.kind("RecordPatientReply")
