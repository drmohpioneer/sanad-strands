import ast
import asyncio
import logging
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from store.account_fixtures import settings

from sanad.api.app import create_app
from sanad.channels.telegram import wording
from sanad.channels.telegram.settings import ENV_NAMES, TelegramSettings
from sanad.channels.telegram.transport import TelegramTransport
from sanad.channels.transport import CallbackOutcome, ProvablyUnsent, SendOutcome


@pytest.mark.parametrize("name", ENV_NAMES)
@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_environment_reports_only_variable_names(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str | None
) -> None:
    synthetic = ("4242", "4242:synthetic-token-value", "synthetic-webhook-secret", "10001")
    for variable, data in zip(ENV_NAMES, synthetic, strict=True):
        monkeypatch.setenv(variable, data)
    if value is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError) as caught:
        TelegramSettings.from_env()
    assert name in str(caught.value)
    assert "synthetic-token-value" not in str(
        caught.value
    ) and "synthetic-webhook-secret" not in str(caught.value)


def test_settings_secrets_are_redacted_in_reprs_dumps_and_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = settings()
    token, secret = config.bot_token.get_secret_value(), config.webhook_secret.get_secret_value()
    for text in (repr(config), str(config), str(config.model_dump()), config.model_dump_json()):
        assert token not in text and secret not in text
    with pytest.raises(ValidationError) as caught:
        TelegramSettings.model_validate(
            {
                "SANAD_TELEGRAM_BOT_ID": "bad",
                "TELEGRAM_BOT_TOKEN_SANAD_STRANDS": token,
                "SANAD_TELEGRAM_WEBHOOK_SECRET": secret,
                "SANAD_ADMIN_TELEGRAM_USER_ID": "10001",
                "spoof": token,
            }
        )
    assert token not in str(caught.value) and secret not in repr(caught.value)
    with pytest.raises(ValidationError):
        config.bot_token = SecretStr("replacement")  # type: ignore[misc]
    for name, value in zip(ENV_NAMES, ("4242", token, secret, "10001"), strict=True):
        monkeypatch.setenv(name, value)
    assert TelegramSettings.from_env() == config
    monkeypatch.setenv("SANAD_TELEGRAM_API_BASE", "https://telegram.test")
    with pytest.raises(ValidationError, match="SANAD_TELEGRAM_API_BASE"):
        TelegramSettings.from_env()
    assert TelegramSettings.from_env(for_tests=True).api_base == "https://telegram.test"


def test_health_and_unconfigured_webhook_read_no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*ENV_NAMES, "SANAD_TELEGRAM_API_BASE"):
        monkeypatch.delenv(name, raising=False)
    app = create_app()

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://sanad.test", trust_env=False
        ) as client:
            assert (await client.get("/health")).status_code == 200
            response = await client.post("/tg", json={"update_id": 1})
            assert response.status_code == 503 and response.content == b""

    asyncio.run(run())
    assert app.state.telegram is None


@pytest.mark.parametrize(
    "http_status,body,status,code,retryable,retry_after",
    [
        (200, {"ok": True, "result": {"message_id": 77}}, "accepted", None, False, None),
        (
            400,
            {"ok": False, "description": "Bad Request: chat not found"},
            "failed",
            "telegram_chat_not_found",
            False,
            None,
        ),
        (
            403,
            {"ok": False, "description": "Forbidden: bot was blocked by the user"},
            "failed",
            "telegram_blocked",
            False,
            None,
        ),
        (
            403,
            {"ok": False, "description": "Forbidden: user is deactivated"},
            "failed",
            "telegram_deactivated",
            False,
            None,
        ),
        (
            429,
            {"ok": False, "parameters": {"retry_after": 7}},
            "failed",
            "telegram_rate_limited",
            True,
            7,
        ),
        (500, {"ok": False}, "failed", "telegram_server_error", True, None),
        (503, None, "failed", "telegram_server_error", True, None),
        (200, None, "uncertain", "telegram_unparseable_success", False, None),
        (200, {"ok": True, "result": {}}, "uncertain", "telegram_unparseable_success", False, None),
    ],
)
def test_provider_outcome_mapping(
    http_status: int,
    body: object,
    status: str,
    code: str | None,
    retryable: bool,
    retry_after: int | None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/sendMessage")
        import json

        payload = json.loads(request.content)
        assert payload == {"chat_id": "20002", "text": "Synthetic plain text <>&"}
        assert "parse_mode" not in payload
        return (
            httpx.Response(http_status, json=body)
            if body is not None
            else httpx.Response(http_status, content=b"not json")
        )

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as http:
        result = TelegramTransport(settings(), http).send(
            "20002", {"text": "Synthetic plain text <>&"}
        )
    assert result.status == status and result.code == code and result.retryable == retryable
    assert result.retry_after_seconds == retry_after
    assert result.provider_message_id == ("77" if status == "accepted" else None)
    assert SendOutcome.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.ReadError,
        httpx.WriteError,
        httpx.RemoteProtocolError,
    ],
)
def test_connection_phase_and_timeout_are_honest_and_redacted(
    failure: type[httpx.RequestError], caplog: pytest.LogCaptureFixture
) -> None:
    config = settings()
    caplog.set_level(logging.DEBUG)

    def handler(request: httpx.Request) -> httpx.Response:
        logging.getLogger("httpx").info("Synthetic request %s", request.url)
        logging.getLogger("sanad.synthetic_url_probe").info("Synthetic URL %s", request.url)
        assert config.bot_token.get_secret_value().encode() in request.url.raw_path
        assert config.bot_token.get_secret_value() not in str(request.url)
        raise failure(str(request.url), request=request)

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as http:
        transport = TelegramTransport(config, http)
        assert config.bot_token.get_secret_value() not in repr(transport)
        if failure is httpx.ConnectError:
            with pytest.raises(ProvablyUnsent) as caught:
                transport.send("20002", {"text": "Synthetic request"})
            assert config.bot_token.get_secret_value() not in str(caught.value)
        else:
            assert transport.send("20002", {"text": "Synthetic request"}).status == "uncertain"
    assert config.bot_token.get_secret_value() not in caplog.text
    assert config.webhook_secret.get_secret_value() not in caplog.text
    assert "/bot<redacted>/" in caplog.text
    assert all(
        config.bot_token.get_secret_value() not in str(record.__dict__) for record in caplog.records
    )


def test_keyboard_and_callback_api_share_the_client_and_plaintext(
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": True if "answerCallbackQuery" in request.url.path else {"message_id": 88},
            },
        )

    caplog.set_level(logging.INFO)
    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as http:
        transport = TelegramTransport(settings(), http)
        assert (
            transport.send(
                "10001",
                {
                    "text": "Synthetic application",
                    "reply_markup": {
                        "inline_keyboard": [
                            [{"text": "Approve", "callback_data": "synthetic-opaque-token"}]
                        ],
                    },
                },
            ).status
            == "accepted"
        )
        answered = transport.answer_callback("query-1", wording.render("callback_refused", "ar"))
        assert answered.status == "accepted"
        assert CallbackOutcome.model_validate_json(answered.model_dump_json()) == answered
    import json

    assert (
        json.loads(requests[0].content)["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        == "synthetic-opaque-token"
    )
    assert json.loads(requests[1].content) == {
        "callback_query_id": "query-1",
        "text": wording.render("callback_refused", "ar"),
    }
    assert settings().bot_token.get_secret_value() not in caplog.text
    assert requests[0].extensions["redacted_url"].endswith("/bot<redacted>/sendMessage")


def test_provider_echo_cannot_leak_token_or_payload() -> None:
    config = settings()
    private = "synthetic private message"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"ok": False, "description": f"{config.bot_token.get_secret_value()} {private}"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as http:
        result = TelegramTransport(config, http).send("20002", {"text": private})
    assert result.code == "telegram_http_400"
    assert (
        private not in str(result)
        and config.bot_token.get_secret_value() not in result.model_dump_json()
    )


def test_wording_checks_exact_fields_and_contains_untrusted_claims() -> None:
    assert wording.OWNER_REVIEW_PENDING is True
    wording.check_templates()
    for key in wording.TEMPLATES:
        if key == "admin_new_application":
            continue
        assert wording.render(key, "ar") == wording.TEMPLATES[key][0]
        with pytest.raises(ValueError):
            wording.render(key, "ar", role="admin")
    with pytest.raises(ValueError):
        wording.render("admin_new_application", "ar", name="Synthetic")
    rendered = wording.render(
        "admin_new_application",
        "ar",
        name="<b>ADMIN</b>\n\u202e{role}",
        specialty="x" * 500,
        city="Synthetic Cairo",
    )
    assert "<b>" not in rendered and "{role}" not in rendered and "\u202e" not in rendered
    assert "&lt;b&gt;ADMIN&lt;/b&gt;" in rendered and "x" * 161 not in rendered
    assert "unverified" in rendered and "مش متحققة" in rendered


def test_dependency_direction_and_registration_is_not_invoked() -> None:
    root = Path(__file__).resolve().parents[2]
    for package in ("domain", "safety", "store"):
        for path in (root / "src" / "sanad" / package).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                names = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else []
                )
                assert all(
                    not name.startswith(("sanad.channels", "sanad.accounts")) for name in names
                ), path
    # Inspect the ops declaration; never exercise the registration function in tests.
    path = root / "src/sanad/channels/telegram/transport.py"
    tree = ast.parse(path.read_text())
    registration = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "register_webhook"
    )
    source = ast.get_source_segment(path.read_text(), registration)
    assert source and '"drop_pending_updates": False' in source
    assert '"allowed_updates": ["message", "callback_query"]' in source
    for file in (root / "src/sanad/api/app.py", root / "Makefile"):
        assert "register_webhook(" not in file.read_text()
