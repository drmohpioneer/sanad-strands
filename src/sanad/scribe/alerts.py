"""Dictated alert instructions require a matching explicit source clause."""

import logging
import re

from sanad.scribe.extract import DictationCandidate, extracted_numbers
from sanad.scribe.names import normalize

logger = logging.getLogger(__name__)
_INSTRUCTION = re.compile(
    r"\b(?:(?:tell|notify|alert) me|let me know) if\b|"
    r"(?<!\w)و?(?:قوللي|تقوللي|قول لي|قولي|بلغني|ابلغني)\s+لو\s+",
    re.I,
)
_NEGATED = re.compile(r"(?:do not|don't|never|لا|ما)\s*$", re.I)
_NOTIFY_AFTER = re.compile(
    r"\b(?:(?:tell|notify|alert) me|let me know)\b|"
    r"(?<!\w)و?(?:قوللي|تقوللي|قول لي|قولي|بلغني|ابلغني|يتصل بيا)",
    re.I,
)


def _words(text: str) -> str:
    # Compare the instruction's words only. Numeric disagreements still reach
    # the unchanged numeric guard instead of being hidden by this filter.
    return " ".join(re.findall(r"[^\W\d_]+|[<>]=?|[≤≥]", normalize(text)))


def explicitly_requested(alert: str, source: str) -> bool:
    proposed = normalize(alert)
    cue = _INSTRUCTION.search(proposed)
    if cue:
        if _NEGATED.search(proposed[: cue.start()]):
            return False
        proposed = proposed[cue.end() :]
    words = _words(proposed)
    if not words:
        return False
    for clause in re.split(r"\.(?!\d)|[;؛\n]", normalize(source)):
        instruction = _INSTRUCTION.search(clause)
        if instruction:
            if _NEGATED.search(clause[: instruction.start()]):
                continue
            condition = clause[instruction.end() :]
        else:
            notification = _NOTIFY_AFTER.search(clause)
            if not notification or _NEGATED.search(clause[: notification.start()]):
                continue
            before = clause[: notification.start()]
            if not re.search(r"\b(?:if|لو)\b", before):
                continue
            condition = re.sub(r"\b(?:if|لو)\b", "", before)
        condition_words = _words(condition)
        negations = {"no", "not", "without", "never", "مش", "مفيش", "بدون", "لا"}
        if set(condition_words.split()) & negations != set(words.split()) & negations:
            continue
        if re.search(r"(?<!\w)و?" + re.escape(words) + r"(?!\w)", condition_words):
            return True
    return False


def prepare_alerts(candidate: DictationCandidate, source: str) -> DictationCandidate:
    kept = [i for i, alert in enumerate(candidate.alerts) if explicitly_requested(alert, source)]
    if len(kept) == len(candidate.alerts):
        return candidate
    result = candidate.model_copy(update={"alerts": tuple(candidate.alerts[i] for i in kept)})
    from sanad.scribe.merge import remap_metadata

    targets: dict[str, tuple[str, ...]] = {
        f"alert:{i}": () for i in range(len(candidate.alerts)) if i not in kept
    }
    targets.update({f"alert:{old}": (f"alert:{new}",) for new, old in enumerate(kept)})
    remap_metadata(result, targets)
    represented = set(extracted_numbers(result))
    result._dropped_numbers = tuple(
        dict.fromkeys(
            (
                *candidate._dropped_numbers,
                *(n for n in extracted_numbers(candidate) if n not in represented),
            )
        )
    )
    logger.info("scribe_unrequested_alert_dropped count=%d", len(candidate.alerts) - len(kept))
    return result
