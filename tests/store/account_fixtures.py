"""Synthetic accounts are always approved by the account service."""

import asyncio
from dataclasses import dataclass
from typing import cast
from uuid import uuid4

import httpx
from fastapi import FastAPI
from harness import FakeClock
from pydantic import BaseModel, JsonValue, SecretStr

from sanad.accounts.commands import ApplyAsDoctor, ApproveDoctor
from sanad.accounts.records import Application, Doctor, SubjectBinding
from sanad.api.app import create_app
from sanad.api.internal import process_event
from sanad.channels.telegram.router import TelegramRuntime
from sanad.channels.telegram.settings import TelegramSettings
from sanad.channels.transport import CapturedTransport
from sanad.domain import PatientScope, Principal
from sanad.store import keys
from sanad.store._base import StoreBase, Write
from sanad.store.keys import ScopedKey
from sanad.store.records import (
    CommandEnvelope,
    CommitRequest,
    InboundReceipt,
    OutboundIntent,
    PatientProfile,
    from_record,
    model_scope,
    record_item,
    to_record,
)

BOT, ADMIN, APPLICANT, PATIENT = "4242", "10001", "20002", "30003"


def settings(admin: str = ADMIN) -> TelegramSettings:
    return TelegramSettings(
        bot_id=BOT,
        admin_user_id=admin,
        bot_token=SecretStr("4242:synthetic-token-value"),
        webhook_secret=SecretStr("synthetic-webhook-secret"),
    )


def update(subject: str = APPLICANT, text: str = "/start", id: int = 1) -> dict[str, JsonValue]:
    return {
        "update_id": id,
        "message": {
            "message_id": id,
            "date": 1788678000,
            "chat": {"id": subject, "type": "private"},
            "from": {"id": subject, "is_bot": False},
            "text": text,
        },
    }


def callback(raw: str, subject: str = ADMIN, id: int = 2) -> dict[str, JsonValue]:
    return {
        "update_id": id,
        "callback_query": {
            "id": f"callback-{id}",
            "from": {"id": subject, "is_bot": False},
            "message": {"chat": {"id": subject, "type": "private"}},
            "data": raw,
        },
    }


@dataclass
class AccountWorld:
    store: StoreBase
    clock: FakeClock
    runtime: TelegramRuntime
    app: FastAPI
    transport: CapturedTransport

    @classmethod
    def create(cls, store: StoreBase, clock: FakeClock, *, process: bool = True) -> "AccountWorld":
        transport = CapturedTransport()

        def submit(key: ScopedKey) -> None:
            process_event(
                app.state.telegram,
                {"type": "process_receipt", "receipt": key.model_dump(mode="json")},
            )

        app = create_app(
            telegram_settings=settings(),
            store=store,
            clock=clock,
            transport=transport,
            process_receipts=process,
            receipt_submit=submit,
        )
        return cls(store, clock, cast(TelegramRuntime, app.state.telegram), app, transport)

    def post(
        self, body: dict[str, JsonValue] | bytes, *, secret: str | None = "synthetic-webhook-secret"
    ) -> httpx.Response:
        async def run() -> httpx.Response:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://sanad.test",
                trust_env=False,
            ) as client:
                headers = {} if secret is None else {"X-Telegram-Bot-Api-Secret-Token": secret}
                if isinstance(body, bytes):
                    return await client.post("/tg", content=body, headers=headers)
                return await client.post("/tg", json=body, headers=headers)

        return asyncio.run(run())

    def actor(self, subject: str = ADMIN) -> Principal:
        return self.store.authorize(BOT, subject).principal

    def apply(self, subject: str = APPLICANT) -> Application:
        result = self.runtime.accounts.apply(
            ApplyAsDoctor(
                command_id="apply:" + uuid4().hex,
                actor=self.actor(subject),
                private_chat_id=subject,
                claimed_name="Synthetic Doctor",
                claimed_specialty="Synthetic Specialty",
                claimed_city="Cairo",
            )
        )
        assert result.status in {"accepted", "already_in_state"}, result
        app = self.runtime.accounts.application(keys.digest(f"{BOT}:{subject}"))
        assert app is not None
        return app

    def approve(self, subject: str = APPLICANT) -> Doctor:
        app = self.apply(subject)
        result = self.runtime.accounts.approve(
            ApproveDoctor(
                command_id="approve:" + uuid4().hex,
                actor=self.actor(),
                application_id=app.id,
                expected_application_version=app.version,
            )
        )
        assert result.status in {"accepted", "already_in_state"}, result
        decided = self.runtime.accounts.application(app.id)
        assert decided and decided.doctor_id
        doctor = self.runtime.accounts.doctor(decided.doctor_id)
        assert doctor is not None
        return doctor

    def put_patient(self, doctor: Doctor, *models: BaseModel) -> None:
        rows = tuple(to_record(m, model_scope(m)) for m in models)
        scope = model_scope(models[0])
        result = self.store.commit(
            CommitRequest(
                command=CommandEnvelope(
                    command_id="fixture:" + uuid4().hex,
                    scope=scope,
                    principal=self.actor(doctor.telegram_user_id),
                    payload={"fixture": True},
                    requested_at=self.clock(),
                ),
                puts=rows,
                expected=tuple(r.ref for r in rows),
            )
        )
        assert result.status == "accepted", result

    def patient(self, doctor: Doctor, *, active: bool = True, profile: bool = True) -> PatientScope:
        scope = PatientScope(doctor_id=doctor.id, patient_id="synthetic-patient")
        binding = SubjectBinding(
            id=keys.subject(BOT, PATIENT).pk,
            scope=self.runtime.accounts.scope,
            telegram_user_id=PATIENT,
            role_set=frozenset({"patient"}),
            doctor_id=doctor.id,
            patient_id=scope.patient_id,
            status="active" if active else "frozen",
            private_chat_id=PATIENT,
            created_at=self.clock(),
            updated_at=self.clock(),
        )
        # Slice 06 owns the write workflow; only its synthetic binding fixture uses CAS here.
        assert self.store._atomic([Write(record_item(to_record(binding, binding.scope)), None)], [])
        if profile:
            self.put_patient(
                doctor,
                PatientProfile(
                    id=scope.patient_id,
                    doctor_id=doctor.id,
                    patient_id=scope.patient_id,
                    recipient_subject=PATIENT,
                    recipient_ref=PATIENT,
                    binding_epoch=1,
                    binding_active=active,
                    consent_active=True,
                    consent_version=1,
                    created_at=self.clock(),
                    updated_at=self.clock(),
                ),
            )
        return scope

    def receipt(self, id: int) -> InboundReceipt:
        key = keys.inbound("telegram", keys.digest(f"{BOT}:{id}"))
        raw = self.store._read(key)
        assert raw is not None
        from sanad.store.records import item_record

        return from_record(item_record(raw), InboundReceipt)

    def intents(self) -> list[OutboundIntent]:
        rows, cursor = self.store.list_records(self.runtime.accounts.scope, "outbound_intent")
        assert cursor is None
        return [from_record(r, OutboundIntent) for r in rows]

    def token(self, action: int = 0) -> str:
        intent = next(i for i in self.intents() if i.template_id == "admin_new_application")
        assert intent.payload is not None
        markup = intent.payload["reply_markup"]
        assert isinstance(markup, dict)
        keyboard = markup["inline_keyboard"]
        assert isinstance(keyboard, list) and isinstance(keyboard[0], list)
        button = keyboard[0][action]
        assert isinstance(button, dict) and isinstance(button["callback_data"], str)
        return button["callback_data"]

    def dispatch(self, intent: OutboundIntent) -> OutboundIntent:
        result = self.runtime.dispatcher.dispatch_one(
            to_record(intent, intent.scope).scoped_key(intent.scope),
            "synthetic-delivery",
            self.clock(),
        )
        assert result is not None
        return result
