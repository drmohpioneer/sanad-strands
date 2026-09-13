"""Contract 24: literal calendar outcomes and persisted acceptance boundaries."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from store import evidence_fixtures as f
from store.account_fixtures import APPLICANT
from store.concierge_fixtures import PatientWorld
from store.conftest import clock as clock
from store.conftest import ddb_server as ddb_server
from store.conftest import pytest_generate_tests as pytest_generate_tests
from store.conftest import store as store
from store.test_corrections_19 import fact_change
from store.test_monitor import current, monitor
from store.test_monitor import world as world

from sanad.concierge.reports import reading
from sanad.domain import DRAFT_POLICY_2026_09, Mission, VersionRef
from sanad.domain.deadlines import NeedsClarification
from sanad.domain.entities import MonitorDetails
from sanad.monitor.executor import current_details
from sanad.monitor.report import assignment_ack, patient_reply
from sanad.monitor.slots import attach, filled, generate, generate_legacy, slot_for
from sanad.safety.policy import SAFETY_POLICY_V1_CARDIOLOGY_DRAFT as SAFETY
from sanad.safety.validator import wants_treatment_change
from sanad.scribe.card import render_card
from sanad.scribe.extract import DictationCandidate, FactCandidate, MissionCandidate
from sanad.scribe.monitoring import card_line, compile_schedule, timing
from sanad.scribe.proposal import Proposal, ScribeCallback
from sanad.scribe.timing import candidate_timings
from sanad.store.records import to_record

ZONE = "Africa/Cairo"
TEXT = "Measure blood pressure twice a day for five days"


def local(value: str, zone: str = ZONE) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(zone)).astimezone(UTC)


def plan(anchor: str = "2026-09-13T07:00", zone: str = ZONE) -> MonitorDetails:
    schedule = compile_schedule(TEXT)
    assert schedule is not None
    return schedule.details(local(anchor, zone), zone)


def add(d: MonitorDetails, at: datetime, id: str, value: str = "120/80") -> MonitorDetails:
    return attach(
        d,
        reading(value, SAFETY).values,
        VersionRef(entity_type="clinical_fact", id=id, version=1),
        at,
        at,
    )


@pytest.mark.parametrize("zone", [ZONE, "America/New_York"])
@pytest.mark.parametrize(
    "count,hours", [(1, [8]), (2, [10, 22]), (3, [8, 14, 20]), (4, [8, 12, 16, 20])]
)
def test_cadences(zone: str, count: int, hours: list[int]) -> None:
    slots = generate(local("2026-09-13T07:00", zone), zone, count, 2)
    assert [s.astimezone(ZoneInfo(zone)).hour for s in slots] == hours * 2
    assert len(slots) == 2 * count
    assert DRAFT_POLICY_2026_09.default_deadline_local_time == "10:00"
    assert DRAFT_POLICY_2026_09.default_grace_seconds == 0


@pytest.mark.parametrize(
    "hour,first,last",
    [
        (7, "2026-09-13T10:00", "2026-09-17T22:00"),
        (10, "2026-09-13T10:00", "2026-09-17T22:00"),
        (21, "2026-09-13T22:00", "2026-09-18T10:00"),
        (22, "2026-09-13T22:00", "2026-09-18T10:00"),
        (23, "2026-09-14T10:00", "2026-09-18T22:00"),
    ],
)
def test_full_count_from_next_instant(hour: int, first: str, last: str) -> None:
    slots = plan(f"2026-09-13T{hour:02}:00").slots
    assert len(slots) == 10
    assert (slots[0], slots[-1]) == (local(first), local(last))


@pytest.mark.parametrize(
    "suffix,first",
    [
        ("starting today", "2026-09-13T22:00"),
        ("starting tomorrow", "2026-09-14T10:00"),
        ("starting 2026-09-15", "2026-09-15T10:00"),
    ],
)
def test_explicit_starts(suffix: str, first: str) -> None:
    schedule = compile_schedule(TEXT + " " + suffix)
    assert schedule is not None
    d = schedule.details(local("2026-09-13T21:00"), ZONE)
    assert len(d.slots) == 10 and d.slots[0] == local(first)


@pytest.mark.parametrize("deadline", [None, "in 4 hours"])
def test_past_start_question_even_with_explicit_deadline(deadline: str | None) -> None:
    text = TEXT + " starting 2026-09-12"
    at = local("2026-09-13T21:00")
    outcome = timing(text, deadline, at, DRAFT_POLICY_2026_09)
    assert isinstance(outcome, NeedsClarification) and outcome.reason_code == "monitor_start_past"
    candidate = DictationCandidate(
        missions=(MissionCandidate(kind="MONITOR", text=text, timing_expression=deadline),)
    )
    _, issues = candidate_timings(candidate, at, DRAFT_POLICY_2026_09)
    assert [i.code for i in issues] == ["monitor_start_past"]


@pytest.mark.parametrize("language,weekday", [("en", "Sun"), ("ar", "الأحد")])
def test_weekday_independent_of_process_locale(language: str, weekday: str) -> None:
    with patch("time.strftime", return_value="WRONG"):
        text = card_line(TEXT, local("2026-09-13T21:00"), ZONE, language)
    assert weekday + " 22:00" in text and "WRONG" not in text


def test_exact_window_sequence_replay_and_out_of_order() -> None:
    d = plan()
    for i, (stamp, slot) in enumerate(
        [
            ("2026-09-13T10:00", 0),
            ("2026-09-13T21:59", 1),
            ("2026-09-13T23:30", 2),
            ("2026-09-14T09:45", None),
        ]
    ):
        old = d.readings
        at = local(stamp)
        d = add(d, at, str(i))
        assert d.readings[:-1] == old
        assert d.readings[-1].slot == slot
        assert add(d, at, str(i)) == d
        ack = assignment_ack(
            d, d.readings[-1].source_ref, "en", at.astimezone(ZoneInfo(ZONE)).date()
        )
        expected = (
            "Recorded as an additional reading."
            if slot is None
            else (
                "Recorded as your 10:00 reading."
                if slot == 0
                else "Recorded as your 22:00 reading."
                if slot == 1
                else "Recorded as your Mon 2026-09-14 10:00 reading."
            )
        )
        assert ack == expected
    original = d.readings
    d = add(d, local("2026-09-13T11:00"), "late")
    assert d.readings[:-1] == original and d.readings[-1].slot is None
    assert set(filled(d)) == {0, 1, 2}


def test_empty_previous_slot_before_start_and_final_boundary() -> None:
    d = add(plan(), local("2026-09-14T09:45"), "previous")
    assert d.readings[0].slot == 1
    d = add(d, local("2026-09-14T09:45"), "next")
    assert d.readings[-1].slot == 2
    early = add(plan(), local("2026-09-12T09:45"), "early")
    assert early.readings[0].slot == 0
    assert slot_for(plan(), local("2026-09-18T09:59")) == 9
    assert slot_for(plan(), local("2026-09-18T10:00")) is None
    late = plan("2026-09-13T21:00")
    assert slot_for(late, local("2026-09-18T21:59")) == 9
    assert slot_for(late, local("2026-09-18T22:00")) is None


@pytest.mark.parametrize(
    "first,end",
    [("2026-03-07T22:00", "2026-03-08T10:00"), ("2026-10-31T22:00", "2026-11-01T10:00")],
)
def test_final_window_uses_local_pattern_across_dst(first: str, end: str) -> None:
    zone = "America/New_York"
    d = MonitorDetails(
        metric="blood pressure",
        unit="mmHg",
        slots=(local(first, zone),),
        required_coverage=1,
        timezone=zone,
        times_per_day=2,
        slot_rule="window-next-v1",
    )
    assert slot_for(d, local(end, zone) - timedelta(microseconds=1)) == 0
    assert slot_for(d, local(end, zone)) is None


@pytest.mark.parametrize("field", ["timezone", "times_per_day"])
def test_new_rule_requires_metadata(field: str) -> None:
    data = plan().model_dump()
    data.pop(field)
    with pytest.raises(ValidationError):
        MonitorDetails.model_validate(data)


@pytest.mark.parametrize("hours", [(8,), (8, 20), (8, 13, 19)])
def test_legacy_missing_metadata_keeps_tolerance(hours: tuple[int, ...]) -> None:
    slots = tuple(local(f"2026-09-13T{h:02}:00") for h in hours)
    d = MonitorDetails.model_validate(
        dict(metric="blood pressure", unit="mmHg", slots=slots, required_coverage=len(slots))
    )
    assert d.slot_rule == "tolerance-3h" and d.timezone is None
    d = add(d, slots[0], "first")
    d = add(d, slots[0] + timedelta(hours=1), "duplicate", "130/85")
    assert d.slots == slots and filled(d)[0].value == "130/85"
    assert len(filled(d)) == 1
    assert slot_for(d, slots[-1] + timedelta(hours=3, microseconds=1)) is None
    assert generate_legacy(local("2026-09-13T07:00"), ZONE, 2, 1) == (
        local("2026-09-14T08:00"),
        local("2026-09-14T20:00"),
    )


def current_d(w: PatientWorld) -> MonitorDetails:
    details = current(w).details
    assert isinstance(details, MonitorDetails)
    return details


def seed_new(w: PatientWorld) -> Mission:
    w.clock.now = local("2026-09-13T10:00")
    old = monitor(w)
    new = old.model_copy(update={"details": plan()})
    w.seed(new)
    return new


def test_text_photo_and_batch_assignments_persist(world: PatientWorld) -> None:
    seed_new(world)
    _, first = world.send("BP 120/80", id=3100)
    assert "Recorded as your 10:00 reading." in str(first.payload)
    world.clock.advance(timedelta(hours=11, minutes=59))
    doc = f.lab(
        items=[
            {"name": "BP", "value": "130/85", "unit": "mmHg"},
            {"name": "BP", "value": "135/85", "unit": "mmHg"},
        ],
        document_type="other",
    )
    f.providers(world, doc, doc)
    assert f.upload(world, caption="BP monitor screen") == "accepted"
    d = current_d(world)
    assert isinstance(d, MonitorDetails)
    assert [r.slot for r in d.readings] == [0, 1, None]
    assert current_details(world.store, world.patient_scope, current(world)) == d
    replies = [
        str(i.payload) for i in world.patient_intents() if i.template_id == "patient_evidence_kept"
    ]
    assert len(replies) == 1
    assert "Recorded as your 22:00 reading." in replies[0]
    assert "Recorded as an additional reading." in replies[0]


@pytest.mark.parametrize(
    "state",
    [
        "open",
        "waiting_patient",
        "blocked",
        "unreachable",
        "overdue",
        "awaiting_link",
        "fulfilled",
        "cancelled",
        "closed_unfulfilled",
        "superseded",
    ],
)
def test_mission_state_gates_unchanged(world: PatientWorld, state: str) -> None:
    m = seed_new(world)
    from sanad.domain import MissionState

    # Reuse accepted factory validation for states with independent lifecycle metadata.
    terminal = state in {"fulfilled", "cancelled", "closed_unfulfilled", "superseded"}
    changes: dict[str, object] = {"state": MissionState(state)}
    if terminal:
        changes["work_clock"] = None
    if state == "fulfilled":
        changes.update(
            fulfilled_at=world.clock(),
            objective_received_at=world.clock(),
            fulfillment_event_id="historical",
            fulfillment_validity="valid",
            timeliness="on_time",
        )
    if state == "blocked":
        changes.update(
            barrier_type="other",
            barrier_reason="Synthetic barrier",
            resume_at=world.clock() + timedelta(hours=1),
        )
    m = Mission.model_validate(m.model_dump() | changes)
    world.seed(m)
    world.send("BP 120/80", id=3100)
    d = current_d(world)
    assert isinstance(d, MonitorDetails)
    assert len(d.readings) == (
        1 if state in {"open", "waiting_patient", "blocked", "unreachable", "overdue"} else 0
    )


def test_value_correction_extra_detach_restore_and_replacement(world: PatientWorld) -> None:
    seed_new(world)
    refs = []
    for i in range(3):
        world.send(f"BP {120 + i}/80", id=3100 + i)
        refs.append(current_d(world).readings[-1].source_ref)
    assert [r.slot for r in current_d(world).readings] == [0, 1, None]
    assert fact_change(world, refs[0].id, None).status == "accepted"
    assert set(filled(current_d(world))) == {1}
    assert fact_change(world, refs[2].id, "BP 129/80").status == "accepted"
    d = current_d(world)
    assert set(filled(d)) == {1}
    assert next(r for r in d.readings if r.value == "129/80").slot is None
    world.send("BP 130/80", id=3104)
    assert set(filled(current_d(world))) == {0, 1}
    from sanad.steward.corrections import current_facts

    detached = next(
        r
        for r in current_facts(world.store, world.patient_scope, include_detached=True)
        if r.body.get("root_fact_id") == refs[0].id
    )
    assert fact_change(world, detached.id, "BP 128/80").status == "accepted"
    d = current_d(world)
    assert filled(d)[0].value == "130/80" and filled(d)[1].value == "121/80"
    assert next(r for r in d.readings if r.value == "128/80").slot is None
    assert current_details(world.store, world.patient_scope, current(world)) == d


def test_22_hour_prompt_requires_existing_quiet_consent(world: PatientWorld) -> None:
    m = seed_new(world)
    world.clock.now = local("2026-09-13T22:00")
    from sanad.contact.scheduler import schedule

    outcome = schedule(world.runtime.steward, to_record(m, world.patient_scope))
    assert outcome.status == "accepted"
    assert not any(i.slot_id == "monitor:monitor:1" for i in world.patient_intents())
    reviews = world.rows("review")
    assert any(r.body.get("source_id") == "monitor:monitor:1" for r in reviews)


def token_for(p: Proposal, at: datetime) -> ScribeCallback:
    return ScribeCallback(
        id=p.confirmation_nonce_hash,
        scope=p.scope,
        proposal_id=p.id,
        proposal_version=p.version,
        actor_subject=APPLICANT,
        action="confirm",
        expires_at=p.expires_at,
        created_at=at,
        updated_at=at,
    )


@pytest.mark.parametrize("mixed", [False, True])
def test_6j_refusal_precedes_schedule_discard(
    world: PatientWorld, mixed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.clock.now = local("2026-09-13T09:50")
    p = world.dictate(
        "Synthetic Patient: " + TEXT,
        {
            "patient": {"name_as_spoken": "Synthetic Patient"},
            "missions": [{"kind": "MONITOR", "text": TEXT}],
        },
    )
    assert p.displayed_schedules[0].slots[0] == local("2026-09-13T10:00")
    assert "Sun 10:00" in "\n".join(render_card(p))
    before = world.scribe.repo.load(p.scope, "scribe_proposal", p.id, Proposal)
    changed = p
    if mixed:
        changed = p.model_copy(
            update={
                "candidate": p.candidate.model_copy(
                    update={"facts": (FactCandidate(category="condition", text="request CBC"),)}
                )
            }
        )
    world.clock.advance(timedelta(minutes=15))
    actor = world.owner
    writes = []
    original = world.store._atomic

    def capture(*args: Any, **kwargs: Any) -> bool:
        writes.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(world.store, "_atomic", capture)
    result = world.scribe.committer.confirm(
        changed, token_for(p, world.clock()), actor, "schedule-confirm"
    )
    after = world.scribe.repo.load(p.scope, "scribe_proposal", p.id, Proposal)
    if mixed:
        assert result.template == "scribe_stale" and writes == [] and after == before
        assert not any(i.template_id == "monitor_schedule_changed" for i in world.cards())
    else:
        assert result.template == "monitor_schedule_changed" and writes
        assert (
            after is not None
            and after.status == "rejected"
            and after.reason == "monitor_schedule_changed"
        )
    assert not world.rows("mission")


@pytest.mark.parametrize("language", ["en", "ar"])
def test_text_extra_acknowledgement_survives_validator(language: str) -> None:
    assert wants_treatment_change("Recorded as an additional reading.") is False
    d = plan()
    at = local("2026-09-13T10:00")
    for i in range(3):
        d = add(d, at, str(i))
    assert [r.slot for r in d.readings] == [0, 1, None]
    text = patient_reply(d, d.readings[-1].source_ref, language, ZONE)
    assert (
        "Recorded as an additional reading." if language == "en" else "سجلتها كقراءة إضافية."
    ) in text


@pytest.mark.parametrize(
    "key,fields,expected",
    [
        ("patient_evidence_kept", {}, "I saved the photo in your record for the doctor."),
        (
            "patient_evidence_accepted",
            {"title": "CBC"},
            "I received CBC and recorded it for the doctor; the doctor evaluates the result.",
        ),
    ],
)
def test_non_monitor_and_no_reading_photo_text_unchanged(
    key: str, fields: dict[str, str], expected: str
) -> None:
    from sanad.evidence.templates import render

    assert render(key, "en", **fields) == expected


@pytest.mark.parametrize("new_patient", [False, True])
def test_legacy_card_refuses_before_fingerprint_checks(
    world: PatientWorld, new_patient: bool
) -> None:
    world.clock.now = local("2026-09-13T09:50")
    name = "New patient Schedule Test" if new_patient else "Synthetic Patient"
    p = world.dictate(
        name + ": " + TEXT,
        {
            "patient": {"name_as_spoken": "Schedule Test" if new_patient else "Synthetic Patient"},
            "missions": [{"kind": "MONITOR", "text": TEXT}],
        },
    )
    legacy = p.model_copy(
        update={"displayed_schedules": (), "evidence_fingerprint": "pre-release-fingerprint"}
    )
    result = world.scribe.committer.confirm(
        legacy, token_for(p, world.clock()), world.owner, "legacy-confirm"
    )
    assert result.template == "monitor_schedule_changed"
    saved = world.scribe.repo.load(p.scope, "scribe_proposal", p.id, Proposal)
    assert saved is not None and saved.status == "rejected"
    assert not world.rows("mission")


def test_stored_preview_used_after_reselection(world: PatientWorld) -> None:
    from sanad.store.records import Patient, from_record

    world.clock.now = local("2026-09-13T09:50")
    second = world.named_stub("Synthetic Patient")
    original = from_record(world.rows("patient")[0], Patient)
    # Both actual records match, forcing the real selection/reseal path.
    assert original.id != second.id
    p = world.dictate(
        "Synthetic Patient: " + TEXT,
        {
            "patient": {"name_as_spoken": "Synthetic Patient"},
            "missions": [{"kind": "MONITOR", "text": TEXT}],
        },
    )
    assert p.choices
    legacy = p.model_copy(update={"displayed_schedules": ()})
    world.seed(legacy)
    raw = world.button(original.display_name, p)
    world.tap(raw=raw)
    selected = world.proposal
    assert (
        selected.displayed_schedules
        and selected.displayed_schedules[0].slot_rule == "window-next-v1"
    )
    world.tap("✅ Confirm", id=21)
    saved = world.scribe.repo.load(selected.scope, "scribe_proposal", selected.id, Proposal)
    assert saved is not None and saved.status == "confirmed"


def test_changed_blocked_monitor_does_not_refuse_independent_item(world: PatientWorld) -> None:
    from sanad.scribe.extract import ProposalIssue
    from sanad.scribe.grounding import seal
    from sanad.scribe.resolver import Context

    p = world.dictate(
        "Synthetic Patient: " + TEXT + ". Request CBC.",
        {
            "patient": {"name_as_spoken": "Synthetic Patient"},
            "missions": [{"kind": "MONITOR", "text": TEXT}, {"kind": "TEST", "text": "CBC"}],
        },
    )
    blocked = p.model_copy(
        update={
            "issues": (*p.issues, ProposalIssue(item="mission:0", code="timing_unclear")),
            "displayed_schedules": (),
        }
    )
    blocked = seal(blocked, Context())
    result = world.scribe.committer.confirm(
        blocked, token_for(p, world.clock()), world.owner, "partial-confirm"
    )
    assert result.template == "scribe_confirmed"
    assert [r.body["kind"] for r in world.rows("mission")] == ["TEST"]


def test_value_and_time_replacement_frees_and_reassigns(world: PatientWorld) -> None:
    seed_new(world)
    world.clock.now = local("2026-09-14T10:00")
    world.send("BP 120/80 2026-09-13T07:00Z", id=3100)
    ref = current_d(world).readings[0].source_ref
    assert current_d(world).readings[0].slot == 0
    assert fact_change(world, ref.id, "BP 125/80 2026-09-13T20:30Z").status == "accepted"
    d = current_d(world)
    assert [(r.value, r.slot, r.observed_at) for r in d.readings] == [
        ("125/80", 1, datetime(2026, 9, 13, 20, 30, tzinfo=UTC))
    ]
    assert current_details(world.store, world.patient_scope, current(world)) == d


def test_voice_delayed_reading_and_replay_use_original_slot(world: PatientWorld) -> None:
    from store.test_concierge_media import media_message, providers

    seed_new(world)
    world.clock.now = local("2026-09-14T09:45")
    speech, files, _ = providers(world, "BP 120/80\nNUMBERS: 120, 80")
    world.post(media_message(id=3100))
    d = current_d(world)
    assert len(d.readings) == 1 and d.readings[0].slot == 1
    world.post(media_message(id=3100))
    assert current_d(world) == d and len(speech.calls) == 1 and len(files.calls) == 1


@pytest.mark.parametrize("only_monitor", [False, True])
def test_photo_preview_and_legacy_refusal_preserve_confirmability(
    world: PatientWorld, only_monitor: bool
) -> None:
    from store import photo_fixtures as pf

    from sanad.scribe.extract import ProposalIssue
    from sanad.scribe.photos import PhotoTurn

    world.clock.now = local("2026-09-13T09:50")
    pf.providers(world, pf.prescription(), pf.prescription())
    world.post(pf.photo(caption="Synthetic Patient", id=3100))
    original = world.proposal
    assert original.photo is not None
    p = original.model_copy(
        update={
            "candidate": original.candidate.model_copy(
                update={"missions": (MissionCandidate(kind="MONITOR", text=TEXT),)}
            ),
            "issues": (),
        }
    ).with_displayed_schedules()
    assert "Sun 10:00" in "\n".join(render_card(p))
    if only_monitor:
        p = p.model_copy(
            update={"candidate": p.candidate.model_copy(update={"orders": (), "facts": ()})}
        )
        assert not PhotoTurn.confirmable(p)
        result = world.scribe.committer.confirm(
            p, token_for(p, world.clock()), world.owner, "photo-only-confirm"
        )
        assert result.template == "scribe_stale"
        blocked = original.model_copy(
            update={
                "issues": tuple(
                    ProposalIssue(item=f"order:{i}", code="dose_missing")
                    for i, _ in enumerate(original.candidate.orders)
                )
            }
        )
        assert not PhotoTurn.confirmable(blocked)
    else:
        assert PhotoTurn.confirmable(p)
        legacy = p.model_copy(update={"displayed_schedules": ()})
        result = world.scribe.committer.confirm(
            legacy, token_for(p, world.clock()), world.owner, "photo-legacy-confirm"
        )
        assert result.template == "monitor_schedule_changed"
        saved = world.scribe.repo.load(p.scope, "scribe_proposal", p.id, Proposal)
        assert saved is not None and saved.status == "rejected"
