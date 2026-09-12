"""Pure, reproducible attempt transitions; models cannot set counters or clocks."""

import re
from datetime import datetime
from typing import Literal

from sanad.concierge.reports import recognize_barrier
from sanad.concierge.text import is_question, normalized
from sanad.domain.boundaries import _BoundaryValue
from sanad.domain.entities import BarrierAttempt, BarrierStep, Mission
from sanad.resolver.places import PlacesResult
from sanad.resolver.policy import POLICY


class Action(_BoundaryValue):
    phase: Literal["begin", "choose", "reserve_search", "result", "finish", "hold"]
    mission_id: str
    words: str = ""
    choice: Literal["ask_patient", "find_places", "hand_to_doctor", "resume_chase"] | None = None
    question: str = ""
    outcome: Literal[
        "asked",
        "handed_to_doctor",
        "interrupted",
        "budget_exhausted",
        "expired",
        "contact_stopped",
        "places_unavailable",
        "resolved",
        "repeated",
    ] = "interrupted"
    result: PlacesResult | None = None


def area_in(text: str, *, answering: bool = False) -> str | None:
    from unicodedata import name

    # Explicit location framing, or the answer to this attempt's area question only.
    match = re.search(
        r"(?:\b(?:in|near|area:|neighbourhood:|neighborhood:)\s+|(?:في|منطقة|حي)\s+)([^.!?؟;\n]{2,120})",
        text,
        re.I,
    )
    area = match[1].strip() if match else text.strip() if answering else None
    if not match and area:
        letters = sum(c.isalpha() and name(c, "").startswith(("LATIN ", "ARABIC ")) for c in area)
        if letters < 2 and not any(c.isdigit() for c in area):
            return None
    if area:
        # A geocoding query contains only the stated area clause, never the
        # rest of a patient's report. No clinical source text goes to OSM.
        area = re.split(
            r"\s+(?:and|but|because|with|where|عشان|علشان|بس|وعندي|وباخد|ومحتاج)\b|[؛;]",
            area,
            maxsplit=1,
            flags=re.I,
        )[0].strip()
        if re.search(
            r"\b(?:I|my|mine|have|take|need|diagnosed|symptom|dose|medicine|medication|diabetes)\b"
            r"|(?:عندي|باخد|دوا|مريض|جرعة|سكري)",
            area,
            re.I,
        ):
            return None
    if not area or len(area) > 120 or is_question(text) or recognize_barrier(area):
        return None
    if re.search(r"[<>\[\]{}\d]|https?://", area) or len(area.split()) > 12:
        return None
    if normalized(area) in {
        "yes",
        "no",
        "ok",
        "okay",
        "plan",
        "الخطة",
        "thanks",
        "thank you",
        "نعم",
        "لا",
        "تمام",
        "شكرا",
    }:
        return None
    return area


def resolved_words(text: str) -> bool:
    return normalized(text) in {
        normalized(s)
        for s in (
            "the barrier is resolved",
            "I can do it now",
            "العائق اتحل",
        )
    }


def material(previous: BarrierAttempt, text: str, now: datetime) -> bool:
    kind = recognize_barrier(text)
    if kind and kind != previous.barrier_type:
        return True
    if previous.expires_at <= now or previous.phase != "complete" or previous.outcome != "asked":
        return False
    if previous.requested_fact == "area":
        return area_in(text, answering=True) is not None
    return bool(
        text.strip()
        and not is_question(text)
        and not kind
        and not resolved_words(text)
        and normalized(text) not in {normalized(t) for t in previous.patient_words}
        and normalized(text) not in {"ok", "thanks", "تمام", "شكرا"}
    )


def evolve(
    mission: Mission, action: Action, receipt_id: str, now: datetime, *, contact: bool
) -> tuple[BarrierAttempt, ...]:
    history = mission.barrier_attempts
    previous = history[-1] if history else None
    if action.phase == "hold":
        if not history:
            raise ValueError("attempt_missing")
        return history
    if action.phase == "begin":
        if any(receipt_id in attempt.receipt_ids for attempt in history):
            return history
        if previous and resolved_words(action.words):
            step = BarrierStep(
                receipt_id=receipt_id, at=now, action="patient_reply", outcome="resolved"
            )
            return (
                *history[:-1],
                BarrierAttempt.model_validate(
                    previous.model_dump()
                    | {
                        "version": previous.version + 1,
                        "updated_at": now,
                        "phase": "complete",
                        "outcome": "resolved",
                        "state": "resolved",
                        "patient_words": (*previous.patient_words, action.words),
                        "receipt_ids": (*previous.receipt_ids, receipt_id),
                        "steps": (*previous.steps, step),
                    }
                ),
            )
        kind = recognize_barrier(action.words)
        new = previous is None or material(previous, action.words, now)
        if not new:
            assert previous
            step = BarrierStep(
                receipt_id=receipt_id,
                at=now,
                action="patient_reply",
                outcome="expired" if now >= previous.expires_at else "budget_exhausted",
            )
            return (
                *history[:-1],
                BarrierAttempt.model_validate(
                    previous.model_dump()
                    | {
                        "version": previous.version + 1,
                        "updated_at": now,
                        "patient_words": (*previous.patient_words, action.words),
                        "receipt_ids": (*previous.receipt_ids, receipt_id),
                        "steps": (*previous.steps, step),
                    }
                ),
            )
        if previous and previous.phase != "complete":
            closed = BarrierAttempt.model_validate(
                previous.model_dump()
                | {
                    "version": previous.version + 1,
                    "updated_at": now,
                    "phase": "complete",
                    "outcome": "interrupted",
                    "steps": (
                        *previous.steps,
                        BarrierStep(
                            receipt_id=receipt_id,
                            at=now,
                            action="finish",
                            outcome="superseded_by_material_reply",
                        ),
                    ),
                }
            )
            history = (*history[:-1], closed)
        if not kind and not previous:
            raise ValueError("barrier_required")
        answering = bool(
            previous and previous.requested_fact == "area" and previous.outcome == "asked"
        )
        area = area_in(action.words, answering=answering)
        requested: Literal["area", "detail"] = (
            "area"
            if (kind or previous and previous.barrier_type) in {"cost", "availability", "other"}
            else "detail"
        )
        attempt = BarrierAttempt(
            doctor_id=mission.doctor_id,
            patient_id=mission.patient_id,
            mission_id=mission.id,
            sequence=len(history) + 1,
            receipt_id=receipt_id,
            created_at=now,
            updated_at=now,
            expires_at=now + POLICY.attempt_ttl,
            barrier_type=kind or previous.barrier_type,  # type: ignore[union-attr]
            patient_words=(action.words,),
            receipt_ids=(receipt_id,),
            area=area,
            area_receipt_id=receipt_id if area else None,
            requested_fact=requested
            if not area and not (previous and not kind and previous.requested_fact == "detail")
            else None,
            answered=bool(previous and not kind),
            reasoning_spent=int(contact),
            questions_spent=int(
                contact
                and area is None
                and not (previous and not kind and previous.requested_fact == "detail")
            ),
            steps=(
                BarrierStep(
                    receipt_id=receipt_id,
                    at=now,
                    action="reason",
                    outcome="reserved" if contact else "contact_stopped",
                ),
            ),
        )
        return (*history, attempt)
    if not previous or previous.receipt_id != receipt_id or previous.phase == "complete":
        raise ValueError("attempt_turn_closed")
    changes: dict[str, object] = {"version": previous.version + 1, "updated_at": now}
    step_action: Literal["reason", "ask_patient", "find_places", "patient_reply", "finish"] = (
        "finish"
    )
    outcome = action.outcome
    if action.phase == "choose":
        if previous.phase != "reserved" or not previous.reasoning_spent or not contact:
            raise ValueError("reasoning_not_reserved")
        if action.choice == "ask_patient" and (
            previous.questions_spent != 1 or previous.area is not None
        ):
            raise ValueError("question_budget")
        if action.choice == "find_places" and (
            not previous.area or previous.searches_spent >= POLICY.search_budget
        ):
            raise ValueError("search_budget")
        if action.choice not in {"ask_patient", "find_places", "hand_to_doctor"}:
            raise ValueError("unsupported_step")
        changes.update(choice=action.choice, question=action.question, phase="chosen")
        outcome = "interrupted"
        step_action = "reason"
    elif action.phase == "reserve_search":
        if (
            previous.choice != "find_places"
            or previous.phase != "chosen"
            or previous.searches_spent >= POLICY.search_budget
            or not contact
            or now >= previous.expires_at
        ):
            raise ValueError("search_budget")
        changes.update(searches_spent=previous.searches_spent + 1, phase="search_reserved")
        step_action = "find_places"
    elif action.phase == "result":
        if previous.phase != "search_reserved" or not action.result:
            raise ValueError("search_not_reserved")
        changes.update(
            places=action.result.places,
            outcome=action.result.outcome,
            phase="complete"
            if action.result.places
            or previous.searches_spent >= POLICY.search_budget
            or action.result.outcome == "area_ambiguous"
            else "chosen",
        )
        step_action = "find_places"
    elif action.phase == "finish":
        if action.outcome == "asked" and (
            previous.choice != "ask_patient" or not previous.question
        ):
            raise ValueError("question_not_chosen")
        if action.outcome == "resolved":
            raise ValueError("resolution_requires_new_patient_reply")
        changes.update(
            phase="complete",
            outcome=outcome,
            state="handed_to_doctor"
            if outcome == "handed_to_doctor"
            else "resolved"
            if outcome == "resolved"
            else "unresolved",
        )
        step_action = "ask_patient" if outcome == "asked" else "finish"
    else:
        raise ValueError("attempt_action")
    step = BarrierStep(
        receipt_id=receipt_id,
        at=now,
        action=step_action,
        outcome=action.result.outcome
        if action.result
        else "chosen"
        if action.phase == "choose"
        else "reserved"
        if action.phase == "reserve_search"
        else outcome,
    )
    changes["steps"] = (*previous.steps, step)
    return (*history[:-1], BarrierAttempt.model_validate(previous.model_dump() | changes))
