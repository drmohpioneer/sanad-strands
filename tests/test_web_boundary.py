import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from sanad.channels.telegram import wording
from sanad.web import pages
from sanad.web.settings import WebSettings


@pytest.mark.parametrize(
    "origin",
    [
        "http://sanad.example",
        "https://sanad.example/",
        "https://a.example/path",
        "https://user:password@a.example",
        "https://a.example?x=1",
        "https://a.example#x",
        "https://a.example\\evil",
        "https://a.example:0",
        "https://a.example\n",
        "https://a.example:",
    ],
)
def test_web_origin_requires_https_origin(origin: str) -> None:
    with pytest.raises(ValidationError):
        WebSettings(public_base_url=origin, bot_username="synthetic_sanad_bot")


def test_render_escapes_all_untrusted_page_fields() -> None:
    attack = '<img src=x onerror="bad()">&\'"'
    values = [
        pages.continue_page(attack, attack),
        pages.invitation_page(attack, attack),
        pages.doctor_home(attack, attack),
        pages.patient_home(attack, 1),
    ]
    for text in values:
        assert attack not in text and "<img" not in text and "<script" not in text
        assert "&lt;img" in text and 'dir="rtl"' in text


def test_all_enrollment_wording_is_single_effective_language() -> None:
    assert len(wording.ENROLLMENT_TEMPLATES) == 13
    assert wording.OWNER_REVIEW_PENDING and pages.OWNER_REVIEW_PENDING
    for key in wording.ENROLLMENT_TEMPLATES:
        fields = {k: "Synthetic" for k in wording.FIELDS[key]}
        if "link" in fields:
            fields["link"] = "https://sanad.example/d/" + "A" * 43
        text = wording.render(key, "en", **fields)
        assert len(text) <= 4096
        assert not any("\u0600" <= c <= "\u06ff" for c in text)
        assert any(c.isascii() and c.isalpha() for c in text)
        assert "<script" not in text


def test_dependency_direction_has_no_lower_imports_of_auth_web_patients() -> None:
    root = Path(__file__).parents[1] / "src" / "sanad"
    for package in ("accounts", "channels", "steward", "store", "domain", "safety"):
        for file in (root / package).rglob("*.py"):
            tree = ast.parse(file.read_text())
            for node in ast.walk(tree):
                modules = (
                    [node.module]
                    if isinstance(node, ast.ImportFrom)
                    else [n.name for n in node.names]
                    if isinstance(node, ast.Import)
                    else []
                )
                assert not any(
                    m
                    and m.split(".")[:2]
                    in [["sanad", "auth"], ["sanad", "web"], ["sanad", "patients"]]
                    for m in modules
                )


def test_admin_api_imports_only_account_service_and_web_plumbing() -> None:
    file = Path(__file__).parents[1] / "src/sanad/web/api_admin.py"
    for node in ast.walk(ast.parse(file.read_text())):
        modules = (
            [node.module]
            if isinstance(node, ast.ImportFrom)
            else [n.name for n in node.names]
            if isinstance(node, ast.Import)
            else []
        )
        for module in modules:
            if module and module.startswith("sanad."):
                assert module == "sanad.accounts.service" or module.startswith("sanad.web")
