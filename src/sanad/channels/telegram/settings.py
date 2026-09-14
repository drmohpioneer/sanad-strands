"""Explicit environment loading; health never reads credentials."""

import logging
import os
import re
from typing import Annotated, Self

from pydantic import (
    ConfigDict,
    Field,
    SecretStr,
    field_serializer,
    field_validator,
    model_validator,
)

from sanad.domain.boundaries import _BoundaryValue
from sanad.store.records import IdentityConfig

DEFAULT_API_BASE = "https://api.telegram.org"
ENV_NAMES = (
    "SANAD_TELEGRAM_BOT_ID",
    "TELEGRAM_BOT_TOKEN_SANAD_STRANDS",
    "SANAD_TELEGRAM_WEBHOOK_SECRET",
    "SANAD_ADMIN_TELEGRAM_USER_ID",
)
logger = logging.getLogger(__name__)


class TelegramSettings(_BoundaryValue):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_default=True,
        hide_input_in_errors=True,
        populate_by_name=True,
    )
    bot_id: Annotated[str, Field(strict=True, pattern=r"^[0-9]+$", max_length=32)] = Field(
        alias="SANAD_TELEGRAM_BOT_ID"
    )
    bot_token: SecretStr = Field(alias="TELEGRAM_BOT_TOKEN_SANAD_STRANDS")
    webhook_secret: SecretStr = Field(alias="SANAD_TELEGRAM_WEBHOOK_SECRET")
    admin_user_id: Annotated[str, Field(strict=True, pattern=r"^[0-9]+$", max_length=32)] = Field(
        alias="SANAD_ADMIN_TELEGRAM_USER_ID"
    )
    doctor_access_code: SecretStr | None = Field(default=None, alias="SANAD_DOCTOR_ACCESS_CODE")
    api_base: str = Field(default=DEFAULT_API_BASE, alias="SANAD_TELEGRAM_API_BASE")
    test_mode: bool = Field(default=False, exclude=True, repr=False)

    @field_validator("bot_token", "webhook_secret")
    @classmethod
    def nonblank_secret(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("required environment variable is blank")
        return value

    @field_validator("doctor_access_code")
    @classmethod
    def access_code_format(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        code = value.get_secret_value()
        if not code.strip():
            return None
        if re.fullmatch(r"[A-Za-z0-9_-]{16,64}", code) is None:
            logger.warning("doctor access code invalid")
            return None
        return value

    @field_serializer("bot_token", "webhook_secret", "doctor_access_code")
    def redact(self, value: SecretStr) -> str:
        return "**********"

    @model_validator(mode="after")
    def base(self) -> Self:
        if self.api_base != DEFAULT_API_BASE and not self.test_mode:
            raise ValueError("SANAD_TELEGRAM_API_BASE is overridable only for tests")
        if not self.api_base.startswith(("https://", "http://")) or any(
            value in self.api_base for value in ("?", "#", "@")
        ):
            raise ValueError("SANAD_TELEGRAM_API_BASE is invalid")
        return self

    @classmethod
    def from_env(cls, *, for_tests: bool = False) -> Self:
        values: dict[str, object] = {}
        for name in ENV_NAMES:
            value = os.environ.get(name)
            if value is None or not value.strip():
                raise ValueError(f"Missing environment variable: {name}")
            values[name] = value
        values["SANAD_TELEGRAM_API_BASE"] = os.environ.get(
            "SANAD_TELEGRAM_API_BASE", DEFAULT_API_BASE
        )
        values["SANAD_DOCTOR_ACCESS_CODE"] = os.environ.get("SANAD_DOCTOR_ACCESS_CODE", "")
        values["test_mode"] = for_tests
        return cls.model_validate(values)

    @property
    def identity(self) -> IdentityConfig:
        return IdentityConfig(bot_id=self.bot_id, admin_user_id=self.admin_user_id)
