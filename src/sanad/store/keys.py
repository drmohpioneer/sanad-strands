"""Single-table keys. Inputs are authenticated scopes and opaque identifiers.

Percent-escape components so an opaque ID containing '#' cannot change scope.
The patient-name board projection is the schema's sole name-based index; names
are never inputs to primary-key constructors.
"""

import re
from datetime import datetime
from hashlib import sha256
from typing import Literal, NamedTuple
from urllib.parse import quote

from sanad.domain import NonblankStr, PatientScope, TenantScope, utc_instant
from sanad.domain.boundaries import _BoundaryValue

SCHEMA_VERSION = 1
SCHEMA_KEY = ("META", "schema_version")


class IntakeScope(TenantScope):
    intake_id: NonblankStr


class AccountScope(_BoundaryValue):
    bot_id: NonblankStr


type Scope = PatientScope | IntakeScope | TenantScope | AccountScope


class Key(NamedTuple):
    pk: str
    sk: str


class ScopedKey(_BoundaryValue):
    scope: Scope
    pk: NonblankStr
    sk: NonblankStr

    @property
    def key(self) -> Key:
        return Key(self.pk, self.sk)


def component(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("key components require nonblank opaque strings")
    return quote(value, safe="-_.:")


def digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def instant(value: datetime) -> str:
    return utc_instant(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def tenant_pk(scope: TenantScope) -> str:
    return f"D#{component(scope.doctor_id)}"


def partition(scope: Scope) -> str:
    if isinstance(scope, AccountScope):
        return f"ACCT#{component(scope.bot_id)}"
    pk = tenant_pk(scope)
    if isinstance(scope, PatientScope):
        return f"{pk}#P#{component(scope.patient_id)}"
    if isinstance(scope, IntakeScope):
        return f"{pk}#INTAKE#{component(scope.intake_id)}"
    return pk


def doctor(
    scope: TenantScope,
    kind: Literal["PROFILE", "POLICY", "APPLICATION", "BUNDLE", "NAME"] = "PROFILE",
    id: str | None = None,
) -> Key:
    return Key(tenant_pk(scope), kind if kind in {"PROFILE", "BUNDLE"} else _suffix(kind, id))


def _suffix(kind: str, id: str | None) -> str:
    if id is None:
        raise ValueError("this record key requires an ID")
    return f"{kind}#{component(id)}"


def patient(
    scope: PatientScope,
    kind: Literal[
        "PROFILE",
        "FACT",
        "ORDER",
        "PLAN",
        "MISSION",
        "FOLLOWUP",
        "REVIEW",
        "INCIDENT",
        "CARD",
        "EVIDENCE",
        "SESSION",
    ] = "PROFILE",
    id: str | None = None,
    *,
    version: int | None = None,
    role: str | None = None,
) -> Key:
    if not isinstance(scope, PatientScope):
        raise ValueError("patient keys require PatientScope")
    sk = kind if kind == "PROFILE" else _suffix(kind, id)
    if kind in {"ORDER", "EVIDENCE", "PLAN"}:
        if kind == "PLAN" and version is None:
            raise ValueError("plan keys require a version")
        if version is not None and (type(version) is not int or version < 1):
            raise ValueError("key versions must be positive integers")
        sk += "#HEAD" if version is None else f"#V#{version}"
    if kind == "SESSION":
        if role is None:
            raise ValueError("session keys require a role")
        sk = f"SESSION#{component(role)}#{component(id or '')}"
    return Key(partition(scope), sk)


def intake(
    scope: IntakeScope,
    kind: Literal["PROFILE", "PROPOSAL", "MEDIA", "PHOTO", "CONCERN", "REVIEW"] = "PROFILE",
    id: str | None = None,
    *,
    processing_version: int | None = None,
) -> Key:
    sk = kind if kind == "PROFILE" else _suffix(kind, id)
    if kind == "PHOTO":
        if type(processing_version) is not int or processing_version < 1:
            raise ValueError("photo keys require a positive processing version")
        sk += f"#{processing_version}"
    return Key(partition(scope), sk)


def event(scope: Scope, accepted_at: datetime, event_id: str) -> Key:
    return Key(partition(scope), f"EVENT#{instant(accepted_at)}#{component(event_id)}")


def token(purpose: str, token_hash: str) -> Key:
    return Key(f"TOKEN#{component(purpose)}#{_hash(token_hash)}", "META")


def _hash(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError("expected a SHA-256 hex digest, never a credential")
    return value


def subject(bot_id: str, telegram_user_id: str) -> Key:
    if not isinstance(telegram_user_id, str) or not re.fullmatch(r"[0-9]+", telegram_user_id):
        raise ValueError("Telegram user ID must be a lossless decimal string")
    return Key(f"SUBJECT#{component(bot_id)}#{telegram_user_id}", "BINDING")


def web_session(session_hash: str) -> Key:
    return Key(f"SESSION#{_hash(session_hash)}", "META")


def inbound(transport: str, transport_key_digest: str) -> Key:
    return Key(f"IN#{component(transport)}#{_hash(transport_key_digest)}", "META")


def media(scope: PatientScope, id: str) -> Key:
    return Key(partition(scope), _suffix("MEDIA", id))


def photo(scope: PatientScope, content_hash: str, processing_version: int) -> Key:
    if type(processing_version) is not int or processing_version < 1:
        raise ValueError("photo keys require a positive processing version")
    return Key(partition(scope), f"PHOTO#{_hash(content_hash)}#{processing_version}")


def outbox(scope: Scope, intent_id: str, attempt_id: str | None = None) -> Key:
    if attempt_id is None:
        return Key(partition(scope), _suffix("OUT", intent_id))
    return Key(partition(scope), f"ATTEMPT#{component(intent_id)}#{component(attempt_id)}")


def uniqueness(scope: Scope, kind: Literal["CMD", "OUTKEY", "REVIEWKEY"], value: str) -> Key:
    return Key(partition(scope), f"{kind}#{component(value) if kind == 'CMD' else _hash(value)}")


def contact(scope: PatientScope, slot_id: str, *, local_day: str | None = None) -> Key:
    suffix = component(slot_id)
    if local_day is not None:
        suffix = f"{component(local_day)}#{suffix}"
    return Key(partition(scope), f"CONTACT#{suffix}")


def operational(service: str, kind: Literal["NONCE", "ISSUE"], id: str) -> Key:
    return Key(f"OPS#{component(service)}", _suffix(kind, id))
