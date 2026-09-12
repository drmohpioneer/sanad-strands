from deploy.common import ROOT


def test_every_incident_has_its_own_numbered_runbook() -> None:
    operations = (ROOT / "docs/operations.md").read_text()
    recovery = operations.split("## Recovery playbooks\n", 1)[1].split("\n## ", 1)[0]
    incidents = [
        line.split("|")[1].strip()
        for line in recovery.splitlines()
        if line.startswith("| ") and not line.startswith("| Incident")
    ]
    runbooks = (ROOT / "docs/runbooks.md").read_text()
    assert len(incidents) == 10
    for incident in incidents:
        page = runbooks.split("## " + incident + "\n", 1)[1].split("\n## ", 1)[0]
        assert "\n1. " in page and "\n2. " in page
        assert "Resolved means" in page or "resolved" in page.lower()


def test_all_routine_procedures_and_honest_unavailable_paths() -> None:
    text = (ROOT / "docs/runbooks.md").read_text()
    for title in (
        "Doctor approval and suspension",
        "Patient invitation and re-invitation",
        "Account recovery",
        "Policy updates",
        "Opt-out",
        "Safety review",
        "Stop and resume of contact",
        "Retention",
        "Export",
        "Deletion",
    ):
        assert "\n## " + title + "\n" in text
    assert "not available in this build" in text
    assert "--yes" in text
    assert "activation\nremains closed" in text
