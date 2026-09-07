"""Synthetic phone-photo pairs and transport/storage fixtures for 09b."""

from dataclasses import dataclass

from providers.fixtures import (
    FakeS3,
    FakeTelegramFiles,
    ScriptedConverter,
    ScriptedVision,
    document,
    png,
)
from pydantic import JsonValue

from sanad.domain import DRAFT_POLICY_2026_09
from sanad.media.retrieve import MediaRetriever
from sanad.media.vision import VisionAdapter
from sanad.steward.types import StewardPolicy
from sanad.store import keys
from sanad.store.keys import IntakeScope
from store.account_fixtures import APPLICANT, update
from store.scribe_fixtures import ScribeWorld


@dataclass(frozen=True)
class PhotoExample:
    name: str
    first: str
    second: str
    expected: str
    blocked: bool = False
    danger: bool = False
    unreadable: bool = False


def prescription(dose: str = "5 مج", drug: str = "بيزوبرولول") -> str:
    return document(
        document_type="prescription", items=[{"name": drug, "dose": dose, "timing": "بالليل"}]
    )


SHIFT = document(
    items=[
        {"name": "Bilirubin Total", "value": None, "unit": "mg/dL"},
        {"name": "Direct", "value": "3.1", "unit": "mg/dL"},
        {"name": "Indirect", "value": "0.8", "unit": "mg/dL"},
    ]
)
MISSING = document(items=[{"name": "Potassium", "value": "4.1"}])
TWO = document(notes=["two documents in one photo"])
TABLE = (
    PhotoExample("agree", prescription(), prescription(), "• بيزوبرولول 5 مج، بالليل (بداية)"),
    PhotoExample(
        "dose", prescription(), prescription("50 مج"), "⚠️ قراءتين مختلفتين: 5 مج / 50 مج", True
    ),
    PhotoExample(
        "drug",
        prescription(),
        prescription(drug="أتورفاستاتين"),
        "⚠️ قراءتين مختلفتين: بيزوبرولول / أتورفاستاتين",
        True,
    ),
    PhotoExample(
        "shift", SHIFT, SHIFT, "⚠️ الأرقام ممكن تكون متزحزحة عن الأسماء، راجع الصورة", True
    ),
    PhotoExample("missing_unit", MISSING, MISSING, "Potassium 4.1 بدون وحدة"),
    PhotoExample("danger", document(), document(), "⚠️ تم تنبيهك", danger=True),
    PhotoExample(
        "unreadable",
        document(unreadable=True),
        document(unreadable=True),
        "صوّر من فوق في نور كويس",
        unreadable=True,
    ),
    PhotoExample("two_documents", TWO, TWO, "الصورة محتاجة توضيح", True, True),
)


def photo(
    caption: str = "أحمد رضا", *, id: int = 10, as_document: bool = False
) -> dict[str, JsonValue]:
    body = update(APPLICANT, "", id)
    message = body["message"]
    assert isinstance(message, dict)
    message.pop("text")
    message["caption"] = caption
    if as_document:
        message["document"] = {
            "file_id": "synthetic-photo",
            "file_unique_id": "synthetic",
            "mime_type": "image/png",
        }
    else:
        message["photo"] = [
            {"file_id": "synthetic-photo", "file_unique_id": "synthetic", "width": 2, "height": 2}
        ]
    return body


def providers(
    world: ScribeWorld, *replies: str, data: bytes | None = None
) -> tuple[ScriptedVision, FakeTelegramFiles, FakeS3]:
    vision, files, s3 = (
        ScriptedVision(*replies),
        FakeTelegramFiles(data if data is not None else png()),
        FakeS3(),
    )
    world.scribe.media_factory = lambda receipt, actor: MediaRetriever(
        world.runtime.steward,
        s3,
        files,
        ScriptedConverter(),
        IntakeScope(doctor_id=world.doctor.id, intake_id=keys.digest(receipt.id)),
        actor,
        lambda: world.claims.doctor(actor) is not None,
        StewardPolicy(DRAFT_POLICY_2026_09),
    )
    world.scribe.vision_factory = lambda source: VisionAdapter(
        vision, source, world.runtime.safety_policy
    )
    world.app.state.media_store = s3
    return vision, files, s3
