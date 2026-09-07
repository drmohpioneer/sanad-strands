"""Small explicit intent vocabulary; no model supplies a mutation or identity."""

import re
import unicodedata

from sanad.safety.normalize import normalize


def normalized(text: str) -> str:
    return " ".join(normalize(unicodedata.normalize("NFKC", text)).split())


def contains(text: str, *phrases: str) -> bool:
    padded = " " + normalized(text) + " "
    return any(" " + normalized(phrase) + " " in padded for phrase in phrases)


def is_question(text: str) -> bool:
    return (
        "?" in text
        or "؟" in text
        or contains(
            text,
            "ايه",
            "إيه",
            "ليه",
            "امتى",
            "ازاي",
            "هل",
            "ممكن",
            "ينفع",
            "يعني",
            "what",
            "why",
            "when",
            "how",
            "can",
            "should",
            "عايز اكلم الدكتور",
            "هو الدكتور",
        )
    )


def asks_doctor(text: str) -> bool:
    return contains(
        text,
        "عايز اكلم الدكتور",
        "عايزة اكلم الدكتور",
        "وصّل للدكتور",
        "اسأل الدكتور",
        "عايز اسأل الدكتور",
        "talk to the doctor",
    )


def plan_command(text: str) -> bool:
    return normalized(text) in {normalized(v) for v in ("خطتي", "أعمل إيه", "/plan", "my plan")}


def asks_history(text: str) -> bool:
    return contains(text, "كنت باخد ايه قبل كده", "ادويتي القديمة", "previous medicines")


def plan_question(text: str, drugs: tuple[str, ...]) -> bool:
    return asks_history(text) or contains(
        text,
        "الدكتور قال",
        "الدكتور قالك",
        "جرعتي",
        "خطتي",
        "ميعاد",
        "الجرعة",
        "دوايا",
        "my dose",
        "my plan",
        *drugs,
    )


def sentences(text: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in re.split(r"(?<!\d)[.!؟?](?!\d)|[\n؛]", text) if v.strip())
