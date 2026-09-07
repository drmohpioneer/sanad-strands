"""One-document classification from the accepted coarse read; no model call."""

import re
import unicodedata
from collections.abc import Sequence
from typing import Literal

from sanad.domain import Mission
from sanad.domain.entities import SendRecordsDetails, TestDetails, VisitDetails
from sanad.media.vision import DocumentRead
from sanad.safety.aliases import (
    ANALYTE_ALIASES as ANALYTE_ALIASES,
)
from sanad.safety.aliases import (
    analyte as analyte,
)
from sanad.safety.aliases import (
    normalize as normalize,
)
from sanad.safety.labs import rule_for

type Category = Literal[
    "lab_result",
    "imaging_report",
    "discharge_summary",
    "prescription",
    "medication_list",
    "monitor_screen",
    "other",
]

CATEGORY_ALIASES: dict[str, Category] = {
    "lab": "lab_result",
    "labs": "lab_result",
    "lab_result": "lab_result",
    "old labs": "lab_result",
    "تحليل": "lab_result",
    "تحاليل": "lab_result",
    "prescription": "prescription",
    "old prescription": "prescription",
    "روشتة": "prescription",
    "روشته": "prescription",
    "الروشتة القديمة": "prescription",
    "medication_list": "medication_list",
    "medication list": "medication_list",
    "قائمة الادوية": "medication_list",
    "قائمة ادوية": "medication_list",
    "imaging_report": "imaging_report",
    "imaging": "imaging_report",
    "اشعة": "imaging_report",
    "discharge_summary": "discharge_summary",
    "discharge": "discharge_summary",
    "خروج": "discharge_summary",
    "تقرير خروج": "discharge_summary",
    "monitor_screen": "monitor_screen",
    "other": "other",
    "report": "other",
    "تقرير": "other",
}


def category_alias(value: str) -> Category | None:
    return next((v for k, v in CATEGORY_ALIASES.items() if normalize(k) == normalize(value)), None)


def expected(mission: Mission) -> tuple[Category, ...]:
    details = mission.details
    if isinstance(details, TestDetails):
        return ("lab_result",)
    if isinstance(details, SendRecordsDetails):
        return tuple(dict.fromkeys(c for v in details.categories if (c := category_alias(v))))
    if isinstance(details, VisitDetails) and details.objective == "report_received":
        return ("other", "discharge_summary", "imaging_report")
    if mission.details.kind == "MONITOR":
        return ("monitor_screen",)
    if (
        mission.objective_predicate.kind == "evidence"
        and mission.objective_predicate.evaluator == "task_evidence"
    ):
        return tuple(dict.fromkeys(CATEGORY_ALIASES.values()))
    return ()


def caption_categories(caption: str) -> set[Category]:
    body = normalize(caption)
    cues: tuple[tuple[Category, str], ...] = (
        ("lab_result", r"تحليل|تحاليل|\blabs?\b|نتيجة"),
        ("prescription", r"روشت[ةه]|prescription|علاج"),
        ("medication_list", r"قا[ئي]?م[ةه].*ادوية|medication list|drug list"),
        ("imaging_report", r"اشعة|imaging|radiology|\bx ray\b|\bct\b|\bmri\b"),
        ("discharge_summary", r"خروج|discharge"),
        (
            "monitor_screen",
            r"جهاز.*(?:ضغط|سكر)|شاشة.*(?:ضغط|سكر)|glucometer|monitor app|bp monitor",
        ),
    )
    return {kind for kind, pattern in cues if re.search(pattern, body, re.I)}


def kind_hint(caption: str, missions: Sequence[Mission]) -> str:
    if re.search(r"تحليل|تحاليل|\blab\b", caption, re.I):
        return "lab"
    if re.search(r"روشت[ةه]|prescription|علاج", caption, re.I):
        return "prescription"
    if re.search(r"تقرير|report", caption, re.I):
        return "other"
    if not caption.strip() and len(missions) == 1 and missions[0].kind in {"TEST", "SEND_RECORDS"}:
        kinds = expected(missions[0])
        if len(kinds) == 1:
            return {
                "lab_result": "lab",
                "prescription": "prescription",
                "medication_list": "prescription",
            }.get(kinds[0], "other")
    return "unknown"


def classify(read: DocumentRead, caption: str, missions: Sequence[Mission]) -> Category:
    readers = (read.first, read.second)
    if read.first.document_type != read.second.document_type:
        return "other"
    items = [row.item for reader in readers for row in reader.items]
    names = " ".join(normalize(i.name) for i in items)
    cues = caption_categories(names)
    caption_cues = caption_categories(caption)
    lab = any(rule_for(analyte(i.name)) is not None for i in items)
    drugs = any(i.dose or i.frequency or i.route or i.timing for i in items)
    if "monitor_screen" in cues | caption_cues and (
        lab or "bp" in names.split() or "pressure" in names
    ):
        return "monitor_screen"
    if lab and not drugs and read.first.document_type == "lab":
        return "lab_result"
    if drugs and not lab and read.first.document_type == "prescription":
        return "medication_list" if "medication_list" in caption_cues else "prescription"
    if lab or drugs:
        return "other"
    reports = cues & {"imaging_report", "discharge_summary"}
    if len(reports) == 1 and read.first.document_type == "other":
        return next(iter(reports))
    # Caption and expected categories may disambiguate a coarse readable report;
    # contradictory cues never acquire a more specific category.
    plausible = {c for m in missions for c in expected(m)}
    hinted = caption_cues & plausible & {"imaging_report", "discharge_summary", "medication_list"}
    if len(hinted) == 1 and not cues and items and read.first.document_type == "other":
        return next(iter(hinted))
    return "other"


def multiple_documents(read: DocumentRead, caption: str) -> bool:
    strings = [caption, *(n for r in (read.first, read.second) for n in r.notes)]
    if any(
        re.search(
            r"two (?:papers|documents)|multiple documents|ورقتين|ورقين|مستندين|"
            r"اكتر من (?:ورقة|مستند)",
            normalize(s),
        )
        for s in strings
    ):
        return True
    for reader in (read.first, read.second):
        if (
            reader.printed_date
            and len(set(re.findall(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", reader.printed_date))) > 1
        ):
            return True
        if reader.printed_identity_hint.text and re.search(
            r"[\n;؛]|\s/\s|\s&\s", reader.printed_identity_hint.text
        ):
            return True
    return False


def identity_outcome(
    first_hint: str | None, second_hint: str | None, display_name: str
) -> Literal["match", "mismatch", "unverifiable"]:
    """Only mutually corroborated Latin names can establish a mismatch.

    Non-Latin text, missing names and institution headings are unknown, including
    when unreadable script happens to equal the patient's stored display name.
    """
    from sanad.evidence.policy import POLICY

    honorifics = {
        "د",
        "دكتور",
        "الدكتور",
        "استاذ",
        "الاستاذ",
        "حاج",
        "الحاج",
        "السيد",
        "السيدة",
        "mr",
        "mrs",
        "dr",
    }

    def tokens(value: str) -> set[str]:
        return set(normalize(value).split()) - honorifics

    def readable(hint: str | None) -> set[str]:
        if not hint or any(c.isalpha() and "LATIN" not in unicodedata.name(c, "") for c in hint):
            return set()
        words = tokens(hint)
        if words & {
            "clinic",
            "lab",
            "labs",
            "laboratory",
            "laboratories",
            "hospital",
            "medical",
            "centre",
            "center",
            "unreadable",
            "unknown",
        }:
            return set()
        return {w for w in words if any(c.isalpha() for c in w)}

    first, second, patient = readable(first_hint), readable(second_hint), tokens(display_name)
    if any(len(hint & patient) >= POLICY.identity_match_min_tokens for hint in (first, second)):
        return "match"
    if first and second and first & second:
        return "mismatch"
    return "unverifiable"


def document_identity(read: DocumentRead, display_name: str) -> str:
    """Exclude a name matching an explicitly observed clinic/lab header.

    The accepted reader has no header field; use only labelled notes/name-only
    rows already returned by it, without interpreting clinical rows as headers.
    """
    headers = []
    for reader in (read.first, read.second):
        lines = [
            *reader.notes,
            *(r.item.name for r in reader.items if not r.item.value and not r.item.dose),
        ]
        for line in lines:
            match = re.match(
                r"\s*(?:clinic|lab(?:oratory)?|hospital|header)(?:\s+name)?\s*:\s*(.+)", line, re.I
            )
            if match:
                headers.append(normalize(match[1]))
    first, second = (
        None if hint and normalize(hint) in headers else hint
        for hint in (read.first.printed_identity_hint.text, read.second.printed_identity_hint.text)
    )
    return identity_outcome(first, second, display_name)
