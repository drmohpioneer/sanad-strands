"""Build a public, allowlisted operating record; default previews without writing."""

import argparse
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from deploy.common import (
    PARAMETERS,
    ROOT,
    OperationError,
    client,
    command,
    configuration_revision,
    dev_only,
    dev_outputs,
    history,
    operator_arguments,
    parameter_values,
    parse_instant,
    previous_release,
    safe_public_text,
    session,
)
from deploy.deploy import check_schema
from deploy.ops import MISSING_ALARMS
from deploy.restore import CLASSES, restore_name
from sanad.models.registry import ModelRegistry


def public(value: Any) -> str:
    return safe_public_text(str(value))


def hash_field(value: Any, *, image: bool = False) -> str:
    pattern = r"sha256:[a-f0-9]{64}" if image else r"[a-f0-9]{40,64}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise OperationError("Invalid revision/digest; public record refused")
    return value


def number(value: Any) -> str:
    # Numeric AWS values may be strings. Never interpolate an unvalidated one.
    text = str(value)
    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        raise OperationError("Invalid numerical field; public record refused")
    return text


def restore_evidence(path: Path | None) -> list[str]:
    if path is None:
        return ["Not run: architect must save restore output under docs/evidence/."]
    if path.is_symlink():
        raise OperationError("Restore evidence symlink refused")
    candidates = []
    for line in path.read_text().splitlines():
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict) and "reconciliation" in candidate:
            candidates.append(candidate)
    if not candidates:
        raise OperationError("Restore evidence has no completed reconciliation")
    evidence = candidates[-1]
    target = evidence.get("target", "")
    suffix = target.removeprefix("sanad-dev-data-restore-")
    if target != restore_name(suffix) or evidence.get("source") != "sanad-dev-data":
        raise OperationError("Restore evidence target mismatch")
    rec = evidence["reconciliation"]
    if evidence.get("mode") != "execute" or rec.get("sends_enabled") is not False:
        raise OperationError("Restore evidence must prove execution with sends disabled")
    lines = [
        "Evidence: [restore output](../evidence/" + public(path.name) + ")",
        "Target: sanad-dev-data-restore-" + public(suffix),
        "Reconciled at: " + parse_instant(rec["reconciled_at"]).isoformat(),
        "Sends enabled: false",
    ]
    for kind in CLASSES:
        for side in ("live", "restored"):
            data = rec[side][kind]
            lines.append(f"{side} {kind}: rows={number(data['rows'])}")
            for field in ("oldest_unfinished_inbound", "oldest_due_work"):
                measure = data[field]
                at = measure["oldest_at"]
                lines.append(
                    f"{field}: count={number(measure['count'])}, oldest="
                    + (parse_instant(at).isoformat() if at else "none")
                )
            for purpose, count in data["would_send_by_kind"].items():
                lines.append(f"Would send {public(purpose)}: {number(count)}")
        lines.append(f"{kind} differing stored versions: {number(rec['differing_versions'][kind])}")
    return lines


def render_record(
    aws: Any,
    env: str,
    *,
    root: Path = ROOT,
    evidence: Path | None = None,
    now: datetime | None = None,
) -> tuple[str, str]:
    out = dev_outputs(aws, env)
    records = history(root / "deploy/releases/dev.json")
    deployed = next(
        (
            r
            for r in reversed(records)
            if r.get("status") == "passed" and r.get("image_digest") == out.get("ImageDigest")
        ),
        None,
    )
    if deployed is None:
        raise OperationError("Live image has no matching passing release history")
    sha = hash_field(deployed["git_sha"])
    image = hash_field(out["ImageDigest"], image=True)
    schema = check_schema(client(aws, "dynamodb"), out["TableName"], initialize=False)
    ssm = client(aws, "ssm")
    values, versions = parameter_values(ssm, env, include_operator=True)
    config = hash_field(configuration_revision(ssm, env))
    operator = public(values.get("operator-name", "not set"))
    before = [
        line
        for line in (root / "docs/backlog.md").read_text().splitlines()
        if line.startswith("|") and "before row 21" in line
    ]
    sections: dict[str, list[str]] = {
        "Revision and image": [
            f"Revision: {sha}",
            f"Image: {image}",
            "Source: deploy/releases/dev.json matched to live stack ImageDigest.",
        ],
        "Schema": [
            f"Stored schema version: {schema}; checked without initialization.",
            "Migration: no migration available; mismatched schema refuses rollback.",
        ],
        "Configuration and parameter versions": [
            "Configuration revision: " + config,
            *[
                public(name) + ": version " + number(version)
                for name, version in sorted(versions.items())
                if name in {f"/sanad/dev/{key}" for key in PARAMETERS}
            ],
        ],
        "Policy and model versions": [
            "Policy/source revision: " + sha,
            "Models are imported from this checkout; verify it against the deployed revision.",
            *[
                public(role) + ": " + public(model)
                for role, model in ModelRegistry().model_dump().items()
                if role in {"worker", "cross_check", "classifier", "vision", "speech"}
            ],
            "Clinical policy decisions: [clinical readiness](../clinical-readiness.md).",
        ],
        "Alarms": [],
        "Budget and current spend": [],
        "Log retention": [],
        "Point-in-time recovery": [],
        "Last restore rehearsal": [],
        "Rollback rehearsal": [],
        "Limits and open items": [
            "Activation CLOSED: part F and owner decisions are separate gates.",
            "English synthetic development only; no clinical validation claimed.",
            "Budget is a notification, not a spending cap.",
            "Table scans are not atomic snapshots. Restored authority can predate revocations.",
            *[public(row) for row in before],
            *MISSING_ALARMS,
        ],
        "Accountable operator": [operator],
    }
    template = json.loads((root / "deploy/stack.yaml").read_text())["Resources"]
    alarm_names = [
        resource["Properties"]["AlarmName"]["Fn::Sub"].replace("${Environment}", env)
        for resource in template.values()
        if resource["Type"] == "AWS::CloudWatch::Alarm"
    ]
    alarm_states = {}
    for page in (
        client(aws, "cloudwatch").get_paginator("describe_alarms").paginate(AlarmNames=alarm_names)
    ):
        for alarm in page.get("MetricAlarms", []):
            if alarm.get("AlarmName") in alarm_names:
                alarm_states[alarm["AlarmName"]] = public(alarm["StateValue"])
    sections["Alarms"] = [
        public(name) + ": " + alarm_states.get(name, "not found") for name in alarm_names
    ]
    budget_name = f"sanad-{env}-monthly"
    budget = client(aws, "budgets").describe_budget(
        AccountId=os.environ["AWS_ACCOUNT_ID"],
        BudgetName=budget_name,
    )["Budget"]
    sections["Budget and current spend"] = [
        public(budget_name)
        + ": limit "
        + number(budget["BudgetLimit"]["Amount"])
        + " "
        + public(budget["BudgetLimit"]["Unit"]),
        "Current spend: "
        + number(budget["CalculatedSpend"]["ActualSpend"]["Amount"])
        + " "
        + public(budget["CalculatedSpend"]["ActualSpend"]["Unit"]),
        "AWS account budget scope; excludes Gemini spend.",
    ]
    for group in (
        "/aws/lambda/sanad-dev-app",
        "/aws/lambda/sanad-dev-relay",
        "/aws/codebuild/sanad-dev-build",
    ):
        found = None
        for page in (
            client(aws, "logs")
            .get_paginator("describe_log_groups")
            .paginate(logGroupNamePrefix=group)
        ):
            for data in page.get("logGroups", []):
                if data.get("logGroupName") == group:
                    found = data
        retention = (
            number(found["retentionInDays"]) + " days"
            if found and "retentionInDays" in found
            else "never expire"
            if found
            else "not found"
        )
        sections["Log retention"].append(group + ": " + retention)
    pitr = client(aws, "dynamodb").describe_continuous_backups(TableName=out["TableName"])
    pitr = pitr["ContinuousBackupsDescription"]["PointInTimeRecoveryDescription"]
    sections["Point-in-time recovery"] = [public(pitr["PointInTimeRecoveryStatus"])]
    for field in ("EarliestRestorableDateTime", "LatestRestorableDateTime"):
        if field in pitr:
            sections["Point-in-time recovery"].append(
                field + ": " + parse_instant(str(pitr[field])).isoformat()
            )
    if evidence is None:
        files = sorted((root / "docs/evidence").glob("restore-dev-*.json*"))
        evidence = files[-1] if files else None
    if evidence is not None and evidence.resolve().parent != (root / "docs/evidence").resolve():
        raise OperationError("Restore evidence must be under docs/evidence")
    sections["Last restore rehearsal"] = restore_evidence(evidence)
    try:
        prior = previous_release(records)
        prior_image = hash_field(prior["image_digest"], image=True)
        sections["Rollback rehearsal"].append("Previous passing image: " + prior_image)
        rehearsals = [r for r in records if r.get("action") == "rollback"][-2:]
        complete = (
            len(rehearsals) == 2
            and rehearsals[0].get("image_digest") == prior_image
            and rehearsals[1].get("image_digest") == image
            and all(
                r.get("status") == "passed"
                and r.get("smoke")
                and all(
                    isinstance(check, dict) and check.get("status") == "PASSED"
                    for check in r["smoke"].values()
                )
                for r in rehearsals
            )
        )
        sections["Rollback rehearsal"].append(
            "Both directions recorded with passing smokes."
            if complete
            else "Not run for this image pair: architect must rehearse both directions with smokes."
        )
        if complete:
            for rehearsal in rehearsals:
                sections["Rollback rehearsal"].append(
                    parse_instant(rehearsal["timestamp"]).isoformat()
                    + ": "
                    + hash_field(rehearsal["image_digest"], image=True)
                )
                for group, check in rehearsal["smoke"].items():
                    sections["Rollback rehearsal"].append(
                        public(group) + ": " + public(check["status"])
                    )
    except OperationError as error:
        if str(error).startswith("No previous distinct"):
            sections["Rollback rehearsal"].append("No previous passing distinct image available.")
        else:
            raise
    sections["Rollback rehearsal"].append(
        "Command (authorized architect rehearsal): python -m deploy.rollback --env dev; "
        "repeat to return; each invocation runs deployment smokes."
    )
    text = "# Sanad development operating release\n\n"
    text += "Collected: " + (now or datetime.now(UTC)).isoformat() + "\n"
    for heading, lines in sections.items():
        text += "\n## " + heading + "\n\n" + "\n".join("- " + line for line in lines) + "\n"
    # Every untrusted string above passed public(), typed hash, number or date validation.
    # Final defense after removing only explicitly validated typed digest/revision values.
    checked = text
    for value in (sha, image, config):
        checked = checked.replace(value, "<validated digest>")
    for match in re.findall(r"sha256:[a-f0-9]{64}", checked):
        checked = checked.replace(match, "<validated image>")
    checked = re.sub(
        r"sanad-dev-data-restore-[a-z0-9-]{1,63}", "<validated restore table>", checked
    )
    safe_public_text(checked)
    return sha, text


def release_record(
    aws: Any,
    env: str,
    *,
    yes: bool = False,
    root: Path = ROOT,
    evidence: Path | None = None,
) -> Path:
    sha, text = render_record(aws, env, root=root, evidence=evidence)
    destination = root / "docs/releases" / f"dev-{sha}.md"
    print(
        json.dumps(
            {
                "mode": "execute" if yes else "dry-run",
                "files": 1,
                "sections": text.count("\n## "),
                "bytes": len(text.encode()),
                "missing_alarms": len(MISSING_ALARMS),
                **({"preview": text} if not yes else {}),
            },
            sort_keys=True,
        )
    )
    if yes:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            raise OperationError("Release record symlink refused")
        temporary = destination.with_suffix(".tmp")
        with temporary.open("x") as file:
            file.write(text)
        temporary.replace(destination)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    operator_arguments(parser)
    parser.add_argument("--restore-evidence", type=Path)
    args = parser.parse_args()
    dev_only(args.env)
    release_record(session(), args.env, yes=args.yes, evidence=args.restore_evidence)


if __name__ == "__main__":
    command(main)
