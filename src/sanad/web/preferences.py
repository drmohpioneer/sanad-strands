"""Doctor language adapter to the existing durable Scribe /lang command."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from sanad.auth.login import LoginService
from sanad.channels.telegram.router import route_receipt
from sanad.domain.language import contest_english, effective
from sanad.safety import screen_text
from sanad.store import keys
from sanad.store.records import InboundReceipt, OperationalClock, WebSession, to_record
from sanad.web.routes import SESSION_COOKIE, require_session


class LanguageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: Literal["en", "ar"]
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
            "effective_language": effective(doctor.language),
            "version": doctor.version,
            "timezone": doctor.timezone,
            "contest_english": contest_english(),
        }

    @router.post("/api/preferences")
    def language(
        body: LanguageBody, request: Request, session: Annotated[WebSession, Depends(guard)]
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
        text = "/lang " + body.language
        verdict = screen_text(text, policy=runtime.safety_policy)
        now = login.clock()
        transport_key = keys.digest(f"{session.id}:{body.command_id}:{body.language}")
        receipt = InboundReceipt(
            id=keys.inbound("web-language", keys.digest(transport_key)).pk,
            scope=doctor.scope,
            transport="web-language",
            transport_key=transport_key,
            source_subject=auth.principal.subject,
            # The accepted Scribe command validates the account's private reply destination.
            # This comes from the account, not from browser input or claimed Telegram data.
            source_chat=doctor.private_chat_id,
            channel="web-language",
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
        accepted = login.store.accept_inbound(transport_key, to_record(receipt, receipt.scope))
        if accepted.record is None or accepted.status not in {"created", "existing"}:
            raise HTTPException(503)
        # Reuses parsing, work claim, authority checks, ScribeLanguage transaction,
        # audit and queued reply. No provider or transport dispatch is called here.
        result = route_receipt(
            runtime, accepted.record.scoped_key(receipt.scope), owner="web-language"
        )
        if result.status not in {"accepted", "duplicate", "completed"}:
            raise HTTPException(409)
        return {"status": "accepted"}

    return router
