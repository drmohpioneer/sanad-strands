import ast
from datetime import timedelta, timezone
from pathlib import Path

import pytest
from domain_fixtures import NOW, followup, mission, review
from pydantic import BaseModel, TypeAdapter, ValidationError

from sanad.domain import PatientScope, TenantScope
from sanad.store import keys
from sanad.store.keys import IntakeScope, Key
from sanad.store.records import (
    Accepted,
    CommitResult,
    Duplicate,
    Forbidden,
    SessionSnapshot,
    StaleVersion,
    StoredRecord,
    TooLarge,
    from_record,
    item_record,
    record_item,
    to_record,
)
from store.fixtures import OTHER, SCOPE, event, inbound, intent, profile


@pytest.mark.parametrize(
    "model",
    [mission(), followup(), review(), profile(), event("synthetic-command"), intent(), inbound()],
)
def test_lossless_record_roundtrip(model: BaseModel) -> None:
    record = to_record(model, SCOPE)
    assert record.body == model.model_dump(mode="json")
    wire = record_item(record)
    restored = item_record(wire)
    assert restored == record
    assert from_record(restored, type(model)) == model
    assert restored.created_at.tzinfo is NOW.tzinfo
    with pytest.raises(ValueError, match="scope"):
        to_record(model, OTHER)


def test_projections_use_fixed_utc_time_and_terminal_records_drop_work() -> None:
    local = NOW.astimezone(timezone(timedelta(hours=3)))
    from sanad.domain import MissionState, ReviewState, WorkClock

    clock = WorkClock(next_action_at=local, work_lane="mission", work_shard="3")
    record = to_record(mission(work_clock=clock), SCOPE)
    assert record.due_lane_shard == "mission#3"
    assert record.due_sort == "2026-09-06T12:00:00.000000Z#mission#synthetic-mission"
    assert keys.instant(NOW.replace(year=1)).startswith("0001-")
    assert to_record(mission(MissionState.fulfilled), SCOPE).due_sort is None
    resolved = to_record(review(ReviewState.resolved), SCOPE)
    assert resolved.review_pk is resolved.review_sort is resolved.due_sort is None
    pending = to_record(review(), SCOPE)
    assert pending.review_pk == "D#synthetic-doctor"
    assert pending.review_sort == f"2026-09-09T12:00:00.000000Z#{pending.id}"
    assert to_record(profile(normalized_name="synthetic name"), SCOPE).patients_sort == (
        "synthetic name#synthetic-patient"
    )
    assert record.patients_pk is None


def test_all_schema_key_groups_and_lossless_telegram_strings() -> None:
    tenant = TenantScope(doctor_id="d")
    patient = PatientScope(doctor_id="d", patient_id="p")
    intake = IntakeScope(doctor_id="d", intake_id="i")
    hashed = "a" * 64
    cases = [
        (keys.doctor(tenant), Key("D#d", "PROFILE")),
        (keys.doctor(tenant, "POLICY", "2"), Key("D#d", "POLICY#2")),
        (keys.doctor(tenant, "APPLICATION", "a"), Key("D#d", "APPLICATION#a")),
        (keys.doctor(tenant, "BUNDLE"), Key("D#d", "BUNDLE")),
        (keys.patient(patient), Key("D#d#P#p", "PROFILE")),
        (keys.patient(patient, "ORDER", "o"), Key("D#d#P#p", "ORDER#o#HEAD")),
        (keys.patient(patient, "ORDER", "o", version=2), Key("D#d#P#p", "ORDER#o#V#2")),
        (keys.patient(patient, "PLAN", "a", version=3), Key("D#d#P#p", "PLAN#a#V#3")),
        (keys.patient(patient, "EVIDENCE", "e"), Key("D#d#P#p", "EVIDENCE#e#HEAD")),
        (
            keys.patient(patient, "SESSION", "m", role="coordinator"),
            Key("D#d#P#p", "SESSION#coordinator#m"),
        ),
        (keys.intake(intake), Key("D#d#INTAKE#i", "PROFILE")),
        (
            keys.intake(intake, "PHOTO", hashed, processing_version=2),
            Key("D#d#INTAKE#i", f"PHOTO#{hashed}#2"),
        ),
        (keys.event(patient, NOW, "e"), Key("D#d#P#p", "EVENT#2026-09-06T12:00:00.000000Z#e")),
        (keys.token("login", hashed), Key(f"TOKEN#login#{hashed}", "META")),
        (
            keys.subject("bot", "90071992547409931234567890"),
            Key("SUBJECT#bot#90071992547409931234567890", "BINDING"),
        ),
        (keys.web_session(hashed), Key(f"SESSION#{hashed}", "META")),
        (keys.inbound("telegram", hashed), Key(f"IN#telegram#{hashed}", "META")),
        (keys.media(patient, "m"), Key("D#d#P#p", "MEDIA#m")),
        (keys.photo(patient, hashed, 1), Key("D#d#P#p", f"PHOTO#{hashed}#1")),
        (keys.outbox(patient, "i"), Key("D#d#P#p", "OUT#i")),
        (keys.outbox(intake, "i", "a"), Key("D#d#INTAKE#i", "ATTEMPT#i#a")),
        (keys.uniqueness(patient, "CMD", "c"), Key("D#d#P#p", "CMD#c")),
        (keys.uniqueness(patient, "OUTKEY", hashed), Key("D#d#P#p", f"OUTKEY#{hashed}")),
        (keys.uniqueness(patient, "REVIEWKEY", hashed), Key("D#d#P#p", f"REVIEWKEY#{hashed}")),
        (
            keys.contact(patient, "chase", local_day="2026-09-06"),
            Key("D#d#P#p", "CONTACT#2026-09-06#chase"),
        ),
        (keys.contact(patient, "scheduled"), Key("D#d#P#p", "CONTACT#scheduled")),
        (keys.operational("tick", "NONCE", hashed), Key("OPS#tick", f"NONCE#{hashed}")),
        (keys.operational("worker", "ISSUE", "i"), Key("OPS#worker", "ISSUE#i")),
    ]
    for actual, expected in cases:
        assert actual == expected


def test_delimiter_injection_cannot_change_scope_and_content_never_enters_primary_keys() -> None:
    first = PatientScope(doctor_id="d#P#p", patient_id="x")
    second = PatientScope(doctor_id="d", patient_id="p#P#x")
    assert keys.patient(first) != keys.patient(second)
    assert keys.component("a#b") != keys.component("a%23b")
    record = to_record(mission(title="synthetic clinical sentence +201234567890"), SCOPE)
    assert "clinical" not in record.pk + record.sk and "+201234567890" not in record.pk + record.sk
    with pytest.raises(ValueError):
        keys.subject("bot", 123)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        keys.token("login", "raw-secret")


def test_envelope_mismatch_and_unvalidated_models_are_rejected() -> None:
    record = to_record(mission(), SCOPE)
    mismatched = StoredRecord.model_validate(record.model_dump() | {"doctor_id": OTHER.doctor_id})
    with pytest.raises(ValueError, match="envelope"):
        from_record(mismatched, type(mission()))
    with pytest.raises(ValueError):
        from_record(record, type(followup()))
    with pytest.raises(ValidationError):
        to_record(mission().model_copy(update={"work_clock": None}), SCOPE)


def test_typed_results_roundtrip_and_sessions_are_bounded() -> None:
    adapter: TypeAdapter[CommitResult] = TypeAdapter(CommitResult)
    accepted = Accepted(event_ids=("e",), resulting_versions=())
    results: list[CommitResult] = [
        accepted,
        Duplicate(original=accepted),
        Forbidden(),
        StaleVersion(),
        TooLarge(reason="item_count"),
    ]
    for result in results:
        assert adapter.validate_json(adapter.dump_json(result)) == result
    data = {
        "id": "synthetic-memory",
        "scope": SCOPE,
        "role": "concierge",
        "blob": {"summary": "synthetic"},
        "safety_epoch": 0,
        "fence_generation": 1,
        "session_version": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }
    snapshot = SessionSnapshot.model_validate(data)
    assert from_record(to_record(snapshot, SCOPE), SessionSnapshot) == snapshot
    with pytest.raises(ValidationError, match="32 KiB"):
        SessionSnapshot.model_validate(data | {"blob": {"summary": "x" * 32768}})


def test_store_import_boundaries() -> None:
    import sanad.store

    assert sanad.store.__file__ is not None
    for path in Path(sanad.store.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            modules = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            assert not any(
                m.split(".")[0] in {"fastapi", "strands", "openai", "anthropic"} for m in modules
            )
            if any(m.split(".")[0] == "boto3" for m in modules):
                assert path.name in {"dynamodb.py", "dynamodb_local.py"}
