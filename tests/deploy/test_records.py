import copy
import json
from pathlib import Path

import pytest
from handover_fakes import FakeAWS, item, key

from deploy import records
from deploy.cleanup import row_body
from deploy.common import OperationError, command
from sanad.domain import PatientScope

DOCTOR = "doctor#one"
PATIENT = "patient#one"
PK = "D#doctor%23one#P#patient%23one"
PREFIX = "doctor%23one/patient%23one/"


def seeded() -> FakeAWS:
    aws = FakeAWS()
    for doctor, patient, pk in [
        (DOCTOR, PATIENT, PK),
        (DOCTOR, "two", "D#doctor%23one#P#two"),
        ("other", PATIENT, "D#other#P#patient%23one"),
    ]:
        scope = {"doctor_id": doctor, "patient_id": patient}
        tenant = pk.split("#P#")[0]
        aws.add(
            item(pk, "PROFILE", entity_type="patient", **scope, clinical_text="unredacted history")
        )
        aws.add(item(pk, "FACT#one", clinical_text="original text"))
        aws.add(item(tenant, "NAME#" + patient, patient_id=patient))
        aws.add(item("SUBJECT#bot#" + pk, "BINDING", **scope))
        for kind in ("TOKEN", "SESSION", "IN"):
            aws.add(item(kind + "#" + pk, "META", scope=scope))
        aws.add(item(tenant, "INTAKE_DRAFT#" + patient, selected_patient_id=patient))
        aws.add(item(tenant, "REVIEW#" + patient, patient_id=patient))
        aws.add(item(tenant, "REUSE_OFFER#" + patient, source_patient_id=patient))
    for prefix in (PREFIX, "doctor%23one/two/", "other/patient%23one/"):
        for version in ("v1", "v2", "marker"):
            aws.objects.append({"Key": prefix + "original", "VersionId": version})
        aws.media[prefix + "original"] = b"original bytes"
    return aws


def test_export_bytes_unredacted_json_and_private_files(tmp_path: Path) -> None:
    aws = seeded()
    destination = tmp_path / "record"
    counts = records.records(aws, "export", "dev", DOCTOR, PATIENT, out_dir=destination)
    assert counts["rows"] == 7 and counts["left_in_place"]["rows"] == 3
    assert not destination.exists()
    records.records(aws, "export", "dev", DOCTOR, PATIENT, out_dir=destination, yes=True)
    content = json.loads((destination / "records.json").read_text())
    assert len(content["rows"]) == 7
    assert "unredacted history" in json.dumps(content)
    assert (destination / "media-000000.bin").read_bytes() == b"original bytes"
    assert content["media"] == [{"key": PREFIX + "original", "file": "media-000000.bin"}]
    assert destination.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in destination.iterdir())
    with pytest.raises(OperationError, match="overwrite"):
        records.records(aws, "export", "dev", DOCTOR, PATIENT, out_dir=destination, yes=True)


def test_delete_exact_patient_audit_first_s3_first_and_idempotency() -> None:
    aws = seeded()
    before = copy.deepcopy(aws.tables["sanad-dev-data"])
    counts = records.records(aws, "delete", "dev", DOCTOR, PATIENT)
    assert counts["rows"] == 7 and counts["s3_objects"] == 3
    assert aws.tables["sanad-dev-data"] == before
    selected, _, _ = records.patient_selection(
        aws,
        "sanad-dev-data",
        PatientScope(
            doctor_id=DOCTOR,
            patient_id=PATIENT,
        ),
    )
    records.records(aws, "delete", "dev", DOCTOR, PATIENT, yes=True)
    after = aws.tables["sanad-dev-data"]
    assert {k: v for k, v in after.items() if k in before} == {
        k: v for k, v in before.items() if k not in {key(r) for r in selected}
    }
    audit = [row_body(r) for k, r in after.items() if k not in before]
    assert len(audit) == 1 and audit[0]["entity_type"] == "audit_event"
    assert audit[0]["source_refs"] == [
        "patient:" + PATIENT,
        "rows:7",
        "s3_versions:3",
        "left_in_place:3",
    ]
    writes = [name for name, _ in aws.calls if name in {"put", "s3-delete", "ddb-delete"}]
    assert writes == ["put", "s3-delete", "ddb-delete"]
    assert all(not o["Key"].startswith(PREFIX) for o in aws.objects)
    retry = records.records(aws, "delete", "dev", DOCTOR, PATIENT, yes=True)
    assert retry["rows"] == retry["s3_objects"] == 0
    assert sum(name == "put" for name, _ in aws.calls) == 1


def test_failure_retains_patient_rows_and_retry() -> None:
    aws = seeded()
    before = copy.deepcopy(aws.tables["sanad-dev-data"])
    aws.s3_failure = True
    with pytest.raises(OperationError, match="rows retained"):
        records.records(aws, "delete", "dev", DOCTOR, PATIENT, yes=True)
    assert all(aws.tables["sanad-dev-data"][k] == v for k, v in before.items())
    assert not any(name == "ddb-delete" for name, _ in aws.calls)
    aws.s3_failure = False
    records.records(aws, "delete", "dev", DOCTOR, PATIENT, yes=True)


def test_reappearing_rows_refuse_completion() -> None:
    aws = seeded()
    aws.reappear = item(PK, "OUT#late")
    with pytest.raises(OperationError, match="reappeared"):
        records.records(aws, "delete", "dev", DOCTOR, PATIENT, yes=True)


@pytest.mark.parametrize("action", ["export", "delete"])
def test_missing_patient_and_environment_refusal(action: str, tmp_path: Path) -> None:
    aws = seeded()
    for env, patient in [("judge", PATIENT), ("dev", "absent")]:
        with pytest.raises(OperationError):
            records.records(aws, action, env, DOCTOR, patient, yes=True, out_dir=tmp_path / "out")
    assert not any(name in {"put", "s3-delete", "ddb-delete"} for name, _ in aws.calls)


def test_judge_cli_refuses_before_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["records", "delete", "--env", "judge", "--doctor", DOCTOR, "--patient", PATIENT, "--yes"],
    )
    monkeypatch.setattr(records, "session", lambda: pytest.fail("No AWS"))
    with pytest.raises(SystemExit, match="--env dev"):
        command(records.main)


def test_malicious_s3_key_cannot_escape_private_export(tmp_path: Path) -> None:
    aws = seeded()
    aws.media[PREFIX + "../../../../escape"] = b"hostile filename"
    records.records(aws, "export", "dev", DOCTOR, PATIENT, yes=True, out_dir=tmp_path / "safe")
    assert sorted(p.name for p in (tmp_path / "safe").iterdir()) == [
        "media-000000.bin",
        "media-000001.bin",
        "records.json",
    ]
    assert not (tmp_path / "escape").exists()


def test_real_account_scoped_patient_credentials_are_deleted() -> None:
    aws = seeded()
    for prefix in ("TOKEN", "SESSION"):
        for doctor, patient in ((DOCTOR, PATIENT), (DOCTOR, "two"), ("other", PATIENT)):
            aws.add(
                item(
                    f"{prefix}#account-{doctor}-{patient}",
                    "META",
                    scope={"bot_id": "1234"},
                    doctor_id=doctor,
                    patient_id=patient,
                )
            )
    records.records(aws, "delete", "dev", DOCTOR, PATIENT, yes=True)
    for prefix in ("TOKEN", "SESSION"):
        assert (f"{prefix}#account-{DOCTOR}-{PATIENT}", "META") not in aws.tables["sanad-dev-data"]
        assert (f"{prefix}#account-{DOCTOR}-two", "META") in aws.tables["sanad-dev-data"]
        assert (f"{prefix}#account-other-{PATIENT}", "META") in aws.tables["sanad-dev-data"]


def test_export_includes_only_linked_intake_originals(tmp_path: Path) -> None:
    aws = seeded()
    intake = {"doctor_id": DOCTOR, "intake_id": "draft"}
    aws.add(
        item(
            PK,
            "PATIENT_MEDIA#original",
            entity_type="patient_media",
            media_scope=intake,
            media_work_id="work",
            source_receipt_id="receipt",
        )
    )
    linked_key = "doctor%23one/intake/draft/original"
    aws.add(
        item(
            "D#doctor%23one#INTAKE#draft",
            "MEDIA#work",
            scope=intake,
            receipt_id="receipt",
            source_blob_ref="s3://synthetic-bucket/" + linked_key,
        )
    )
    aws.media[linked_key] = b"intake original"
    aws.media["doctor%23one/intake/draft/unrelated"] = b"unrelated private"
    destination = tmp_path / "record"
    records.records(aws, "export", "dev", DOCTOR, PATIENT, yes=True, out_dir=destination)
    content = json.loads((destination / "records.json").read_text())
    assert len(content["linked_media_rows"]) == 1
    assert {m["key"] for m in content["media"]} == {PREFIX + "original", linked_key}
    assert {p.read_bytes() for p in destination.glob("*.bin")} == {
        b"original bytes",
        b"intake original",
    }


def test_export_refuses_cross_doctor_intake_reference(tmp_path: Path) -> None:
    aws = seeded()
    aws.add(
        item(
            PK,
            "PATIENT_MEDIA#original",
            entity_type="patient_media",
            media_scope={"doctor_id": "other", "intake_id": "draft"},
            media_work_id="work",
            source_receipt_id="receipt",
        )
    )
    with pytest.raises(OperationError, match="doctor mismatch"):
        records.records(
            aws, "export", "dev", DOCTOR, PATIENT, yes=True, out_dir=tmp_path / "record"
        )
    assert not (tmp_path / "record").exists()
