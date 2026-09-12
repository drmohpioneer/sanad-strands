"""The exact contract 18d credential classifier, without stores or transports."""

import pytest

from sanad.steward.credential_message import is_credential_message


@pytest.mark.parametrize(
    "template_id",
    ["doctor_login_link", "patient_login_link", "admin_login_link", "scribe_invitation"],
)
@pytest.mark.parametrize("text", [None, "", "No URL needed for a template marker."])
def test_credential_templates(template_id: str, text: str | None) -> None:
    assert is_credential_message(template_id, text)


@pytest.mark.parametrize("path", ["/pl/", "/d/", "/ad/", "/p/", "t.me/bot?start="])
@pytest.mark.parametrize("token", ["a" * 43, "Z" * 41 + "_-", "0123456789" * 4 + "abc"])
def test_exact_link_patterns(path: str, token: str) -> None:
    assert is_credential_message("patient_reply", "Sent https://example.test" + path + token)


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "Your ordinary reply.",
        "https://sourceforge.net/p/name/",
        "https://example.test/pl/" + "a" * 42,
        "https://t.me/bot?start=" + "a" * 42,
        "https://example.test/unrelated/" + "a" * 43,
        "token " + "a" * 43,
        "https://example.test/pl/" + "a" * 42 + "+",
        "https://example.test/pl/" + "é" * 43,
    ],
)
def test_no_other_heuristics(text: str | None) -> None:
    assert not is_credential_message("patient_reply", text)


def test_telegram_deep_link() -> None:
    assert is_credential_message("patient_reply", "https://t.me/synthetic_bot?start=" + "x" * 43)
