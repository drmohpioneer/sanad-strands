"""The released frequency/duration request shape is a TASK until slice 13."""

import re

from sanad.scribe.names import normalize

_COUNT = (
    r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"واحد|اثنين|اتنين|ثلاث|ثلاثة|ثلاثه|اربع|اربعة|اربعه|خمس|خمسة|خمسه|ست|سته|سبع|سبعه)"
)
_FREQUENCY = re.compile(
    rf"{_COUNT}\s+(?:times?\s+(?:(?:a|per|each)\s+day|daily)|مرات\s+(?:في\s+اليوم|يوميا))",
    re.I,
)
_DURATION = re.compile(
    rf"(?:for|لمده|مده)\s+({_COUNT})\s*(days?|hours?|weeks?|ايام|يوم|ساعات|ساعه|اسابيع|اسبوع)",
    re.I,
)

_ENGLISH_COUNTS = dict(
    zip(
        "one two three four five six seven eight nine ten".split(),
        (str(n) for n in range(1, 11)),
        strict=True,
    )
)


def spoken_counts(text: str) -> set[str]:
    """Word counts for presentation coverage only; never clinical numeric support."""
    return {_ENGLISH_COUNTS[t] for t in normalize(text).split() if t in _ENGLISH_COUNTS}


def request_counts(text: str) -> set[str]:
    normalized = normalize(text)
    counts = {match[0].split()[0] for match in _FREQUENCY.finditer(normalized)} | {
        match[1] for match in _DURATION.finditer(normalized)
    }
    return {_ENGLISH_COUNTS.get(value, value) for value in counts}


def task_request(text: str) -> bool:
    text = normalize(text)
    return bool(
        re.search(r"\b(?:measure|record|chart|monitor)\b|قياس|قيس|سجل|جدول", text)
        and _FREQUENCY.search(text)
    )


def duration_expression(text: str) -> str | None:
    match = _DURATION.search(normalize(text))
    return match[0] if match else None


def task_instruction(text: str) -> str:
    """Translate the request grammar; resolve the metric through the shared vocabulary."""
    from sanad.scribe.resolver import resolve_name

    normalized = normalize(text)
    frequency = _FREQUENCY.search(normalized)
    if not frequency:
        return text
    metric = normalized[: frequency.start()].strip(" ,،:")
    metric = re.sub(
        r"^(?:i\s+)?(?:asked\s+(?:him|her)\s+to\s+)?(?:please\s+)?(?:do\s+(?:a\s+)?)?", "", metric
    )
    metric = re.sub(r"^(?:measure|record|monitor|chart|قياس|قيس|سجل|جدول)\s+", "", metric)
    metric = re.sub(r"\s+(?:chart|record)$", "", metric).strip(" ,،:")
    resolved = resolve_name(metric, "finding", text)
    metric = resolved.latin or metric
    words = {
        "واحد": "one",
        "اثنين": "two",
        "اتنين": "two",
        "ثلاث": "three",
        "ثلاثه": "three",
        "اربع": "four",
        "اربعه": "four",
        "خمس": "five",
        "خمسه": "five",
        "ست": "six",
        "سته": "six",
        "سبع": "seven",
        "سبعه": "seven",
    }
    count = frequency[0].split()[0]
    result = metric[:1].upper() + metric[1:] + " chart, " + words.get(count, count) + " times a day"
    duration = _DURATION.search(normalized)
    if duration:
        unit = {
            "ايام": "days",
            "يوم": "days",
            "ساعات": "hours",
            "ساعه": "hours",
            "اسابيع": "weeks",
            "اسبوع": "weeks",
        }.get(duration[2], duration[2])
        result += " for " + words.get(duration[1], duration[1]) + " " + unit
    return result
