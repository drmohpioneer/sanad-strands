"""Doctor language adapter to the existing durable Scribe /lang command."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from sanad.auth.login import LoginService
from sanad.channels.telegram.router import route_receipt
from sanad.domain.language import contest_english, effective
from sanad.safety import screen_text
from sanad.store import keys
from sanad.store.records import InboundReceipt, OperationalClock, WebSession
from sanad.web.receipts import persist
from sanad.web.routes import SESSION_COOKIE, require_session


class LanguageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: Literal["en", "ar"]
    expected_version: int = Field(strict=True, ge=1)
    command_id: str = Field(pattern=r"^[A-Za-z0-9-]{1,64}$")


class DigestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    digest_time: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    digest_packing: Literal["one", "each"]
    expected_version: int = Field(strict=True, ge=1)
    command_id: str = Field(pattern=r"^[A-Za-z0-9-]{1,64}$")


def preference_router(login: LoginService) -> APIRouter:
    router = APIRouter()
    guard = require_session("doctor")

    @router.get("/api/preferences")
    def preferences(session: Annotated[WebSession, Depends(guard)]) -> dict[str, object]:
        doctor = login.accounts.doctor(session.doctor_id)
        if doctor is None:
            raise HTTPException(401)
        return {
            "language": doctor.language,
            "digest_time": doctor.digest_time,
            "digest_packing": doctor.digest_packing,
            "effective_language": effective(doctor.language),
            "version": doctor.version,
            "timezone": doctor.timezone,
            "contest_english": contest_english(),
        }

    @router.post("/api/preferences")
    def language(
        body: LanguageBody | DigestBody,
        request: Request,
        session: Annotated[WebSession, Depends(guard)],
    ) -> dict[str, str]:
        # This is a session-authenticated adapter, never a Telegram webhook impersonation.
        # Its transport and durable provenance are explicitly `web-language`.
        current = login.require(request.cookies.get(SESSION_COOKIE, ""), "doctor")
        doctor = login.accounts.doctor(session.doctor_id)
        if current is None or doctor is None:
            raise HTTPException(401)
        if doctor.version != body.expected_version:
            raise HTTPException(409)
        runtime = request.app.state.telegram
        auth = login.store.authorize(login.scope.bot_id, session.subject)
        if (
            auth.principal.doctor_id != session.doctor_id
            or auth.principal.auth_epoch != session.auth_epoch
        ):
            raise HTTPException(401)
        channel = "web-language" if isinstance(body, LanguageBody) else "web-digest"
        text = (
            "/lang " + body.language
            if isinstance(body, LanguageBody)
            else f"/digest {body.digest_time} {body.digest_packing}"
        )
        verdict = screen_text(text, policy=runtime.safety_policy)
        now = login.clock()
        transport_key = keys.digest(f"{session.id}:{body.command_id}:{text}")
        receipt = InboundReceipt(
            id=keys.inbound(channel, keys.digest(transport_key)).pk,
            scope=doctor.scope,
            transport=channel,
            transport_key=transport_key,
            source_subject=auth.principal.subject,
            # The accepted Scribe command validates the account's private reply destination.
            # This comes from the account, not from browser input or claimed Telegram data.
            source_chat=doctor.private_chat_id,
            channel=channel,
            kind="text",
            payload={"kind": "text", "text": text},
            principal=auth.principal,
            received_at=now,
            created_at=now,
            updated_at=now,
            safety_screen_state="screened",
            safety_policy_version=verdict.policy_version,
            safety_result=verdict.model_dump(mode="json"),
            work_clock=OperationalClock(next_action_at=now, work_lane="ingress"),
        )
        accepted = persist(login.store, receipt)
        assert accepted.record is not None
        # Reuses parsing, work claim, authority checks, ScribeLanguage transaction,
        # audit and queued reply. No provider or transport dispatch is called here.
        result = route_receipt(runtime, accepted.record.scoped_key(receipt.scope), owner=channel)
        if result.status not in {"accepted", "duplicate", "completed"}:
            raise HTTPException(409)
        return {"status": "accepted"}

    return router
