"""Minimal bounded Telegram schema; display/forward metadata is discarded."""

from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, ConfigDict, Field, StrictBool, model_validator

from sanad.domain.boundaries import _BoundaryValue


def decimal_id(value: object) -> str:
    if type(value) is int and value >= 0:
        return str(value)
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return value
    raise ValueError("Telegram ID must be a lossless decimal integer")


type DecimalId = Annotated[str, BeforeValidator(decimal_id), Field(max_length=32)]
type Text = Annotated[str, Field(strict=True, max_length=4096)]
type Handle = Annotated[str, Field(strict=True, max_length=256, min_length=1)]


class TelegramValue(_BoundaryValue):
    model_config = ConfigDict(frozen=True, extra="ignore", hide_input_in_errors=True)


class TelegramUser(TelegramValue):
    id: DecimalId | None = None
    is_bot: StrictBool = False


class TelegramChat(TelegramValue):
    # Groups have negative IDs; their type is checked before any identity use.
    id: Annotated[int | str, Field(union_mode="left_to_right")]
    type: Annotated[str, Field(strict=True, max_length=32)]

    @model_validator(mode="before")
    @classmethod
    def lossless_chat(cls, value: object) -> object:
        if isinstance(value, dict):
            id = value.get("id")
            if type(id) is not int and not (
                isinstance(id, str)
                and id.removeprefix("-").isascii()
                and id.removeprefix("-").isdecimal()
            ):
                raise ValueError("chat ID must be a lossless integer")
        return value


class TelegramMedia(TelegramValue):
    file_id: Handle


class TelegramEntity(TelegramValue):
    type: Annotated[str, Field(strict=True, max_length=32)]
    offset: Annotated[int, Field(strict=True, ge=0)]
    length: Annotated[int, Field(strict=True, ge=0)]


class TelegramMessage(TelegramValue):
    message_id: DecimalId
    date: Annotated[int, Field(strict=True, ge=0)]
    chat: TelegramChat
    sender: TelegramUser | None = Field(default=None, alias="from")
    via_bot: TelegramUser | None = None
    text: Text | None = None
    caption: Text | None = None
    photo: Annotated[tuple[TelegramMedia, ...], Field(max_length=10)] = ()
    voice: TelegramMedia | None = None
    document: TelegramMedia | None = None
    entities: Annotated[tuple[TelegramEntity, ...], Field(max_length=100)] = ()

    @model_validator(mode="after")
    def bounded_text(self) -> Self:
        if len(self.readable_text) > 4096:
            raise ValueError("readable message exceeds Telegram limit")
        return self

    @property
    def readable_text(self) -> str:
        return "\n".join(x for x in (self.text, self.caption) if x is not None)

    @property
    def media(self) -> TelegramMedia | None:
        return self.photo[-1] if self.photo else self.voice or self.document

    @property
    def kind(self) -> Literal["photo", "voice", "document", "text", "unsupported"]:
        return (
            "photo"
            if self.photo
            else "voice"
            if self.voice
            else "document"
            if self.document
            else ("text" if self.text is not None else "unsupported")
        )


class CallbackMessage(TelegramValue):
    chat: TelegramChat


class TelegramCallback(TelegramValue):
    id: Handle
    sender: TelegramUser | None = Field(default=None, alias="from")
    message: CallbackMessage | None = None
    data: Annotated[str, Field(strict=True, max_length=64)] = ""


class TelegramUpdate(TelegramValue):
    update_id: DecimalId
    bot_id: DecimalId | None = None
    message: TelegramMessage | None = None
    callback_query: TelegramCallback | None = None

    @model_validator(mode="after")
    def single_input(self) -> Self:
        if self.message is not None and self.callback_query is not None:
            raise ValueError("one input per Telegram update")
        return self
