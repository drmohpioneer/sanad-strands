"""Explicit web configuration; never implicitly reads an environment file."""

import os
from typing import Annotated, Self
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, field_validator

from sanad.domain.boundaries import _BoundaryValue


class WebSettings(_BoundaryValue):
    model_config = ConfigDict(
        frozen=True, extra="forbid", hide_input_in_errors=True, populate_by_name=True
    )
    clinic_contact: str = Field(default="", alias="SANAD_CLINIC_CONTACT", max_length=160)
    consent_retention: str = Field(
        default="Development environment: synthetic data only, reset at any time.",
        alias="SANAD_CONSENT_RETENTION",
        min_length=1,
        max_length=160,
    )
    public_base_url: str = Field(alias="SANAD_PUBLIC_BASE_URL")
    bot_username: Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{4,31}$")] = Field(
        alias="SANAD_TELEGRAM_BOT_USERNAME"
    )

    @field_validator("public_base_url")
    @classmethod
    def origin(cls, value: str) -> str:
        try:
            url = urlsplit(value)
            valid = (
                url.scheme == "https"
                and url.hostname
                and not url.username
                and not url.password
                and not url.path
                and not url.query
                and not url.fragment
                and url.port != 0
                and not url.netloc.endswith(":")
                and value.isascii()
                and not any(ord(c) < 33 or ord(c) > 126 for c in value)
                and "\\" not in value
                and "?" not in value
                and "#" not in value
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("SANAD_PUBLIC_BASE_URL requires a clean HTTPS origin without slash")
        return value

    @classmethod
    def from_env(cls) -> Self:
        data = {}
        for name in ("SANAD_PUBLIC_BASE_URL", "SANAD_TELEGRAM_BOT_USERNAME"):
            value = os.environ.get(name)
            if not value:
                raise ValueError(f"Missing environment variable: {name}")
            data[name] = value
        if value := os.environ.get("SANAD_CONSENT_RETENTION"):
            data["SANAD_CONSENT_RETENTION"] = value
        if value := os.environ.get("SANAD_CLINIC_CONTACT"):
            data["SANAD_CLINIC_CONTACT"] = value
        return cls.model_validate(data)
