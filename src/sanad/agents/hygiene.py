"""Conservative parsing and patient-language gates before schema acceptance."""

import json
import re
from typing import Any

from sanad.safety.kernel import validate_patient_output
from sanad.safety.models import OutputContext
from sanad.safety.policy import SafetyPolicy

# Measured in contract 11d; adding a reader requires a separately accepted probe.
ARABIC_CAPABLE_READERS: frozenset[str] = frozenset()
ARABIC_SCRIPT = re.compile(
    r"[\u0600-\u06ff\u0750-\u077f\u0870-\u089f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff\U00010e60-\U00010e7f\U0001ee00-\U0001eeff]"
)
DOCUMENT_ITEM_FIELDS = (
    "name",
    "value",
    "unit",
    "flag",
    "ref",
    "dose",
    "frequency",
    "route",
    "timing",
)


def document_rail(raw: dict[str, Any], model_id: str) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Null whole Arabic-bearing fields, including mixed Latin/Arabic values.

    Drop paths are code-owned metadata, never provider-supplied notes. Blank
    cells retain row positions, so the doctor can edit the same row on the card.
    """
    dropped: list[str] = []

    def field(value: Any, path: str) -> Any:
        if not isinstance(value, str):
            return value
        if model_id not in ARABIC_CAPABLE_READERS and ARABIC_SCRIPT.search(value):
            dropped.append(path)
            return None
        if "[unreadable]" in value.casefold() or "[غير مقروء]" in value:
            return None
        return value

    result = dict(raw)
    for name in ("printed_name", "printed_date"):
        result[name] = field(raw.get(name), name)
    if isinstance(raw.get("items"), list):
        result["items"] = [
            {key: field(row.get(key), f"items.{i}.{key}") for key in DOCUMENT_ITEM_FIELDS}
            if isinstance(row, dict)
            else row
            for i, row in enumerate(raw["items"])
        ]
    if isinstance(raw.get("notes"), list):
        result["notes"] = [field(note, f"notes.{i}") for i, note in enumerate(raw["notes"])]
    return result, tuple(dropped)


def clean_text(text: str) -> str:
    text = re.sub(
        r"<(thinking|think|analysis|reasoning)\b[^>]*>.*?</\1\s*>", "", text, flags=re.I | re.S
    )
    # An unclosed block has no safely identifiable patient answer.
    text = re.sub(r"<(?:thinking|think|analysis|reasoning)\b.*", "", text, flags=re.I | re.S)
    text = text.strip()
    if re.match(r"^(?:reasoning|analysis|thoughts?|تفكير|تحليل)\s*:", text, re.I):
        final = re.search(r"(?:^|\n)(?:final(?: answer)?|answer|الإجابة|الرد)\s*:\s*", text, re.I)
        text = text[final.end() :] if final else ""
    return re.sub(
        r"^(?:final(?: answer)?|answer|الإجابة|الرد)\s*:\s*", "", text, flags=re.I
    ).strip()


def clean_values(value: Any) -> Any:
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, dict):
        return {key: clean_values(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_values(item) for item in value]
    return value


def json_object(text: str) -> dict[str, Any]:
    """Permit one object inside prose/fences; reject ambiguity and duplicate keys."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    cleaned = clean_text(text)
    start = cleaned.find("{")
    if start < 0:
        raise ValueError("invalid_json")
    value, end = json.JSONDecoder(object_pairs_hook=pairs).raw_decode(cleaned[start:])
    if (
        not isinstance(value, dict)
        or "{" in cleaned[start + end :]
        or "}" in cleaned[start + end :]
    ):
        raise ValueError("ambiguous_json")
    return value


def template_echo(text: str, *templates: str) -> bool:
    """Recognize literal descriptions and equivalent JSON examples before validation."""

    def normalized(value: str) -> str:
        cleaned = clean_text(value)
        cleaned = re.sub(r"^```[^\n]*\n|\n```$", "", cleaned)
        return " ".join(cleaned.split())

    for template in templates:
        if normalized(text) == normalized(template):
            return True
        try:
            if clean_values(json_object(text)) == clean_values(json_object(template)):
                return True
        except (ValueError, TypeError):
            pass
    return False


def arabic_ratio(text: str) -> float:
    letters = [char for char in text if char.isalpha()]
    return sum(
        "\u0600" <= char <= "\u06ff" or "\ufb50" <= char <= "\ufeff" for char in letters
    ) / max(1, len(letters))


def patient_failure(text: str, context: OutputContext, policy: SafetyPolicy) -> str | None:
    if not text.strip():
        return "empty_output"
    if (context.language == "ar" and arabic_ratio(text) < 0.5) or (
        context.language == "en" and arabic_ratio(text) > 0.5
    ):
        return "language_drift"
    # Validate the whole reply as well as every sentence; decimal points stay intact.
    sentences = re.split(r"(?<!\d)[.!؟?](?!\d)|[\n؛]", text)
    if any(
        not validate_patient_output(s, context=context, policy=policy).ok
        for s in [text, *sentences]
        if s.strip()
    ):
        return "unsafe_output"
    return None
