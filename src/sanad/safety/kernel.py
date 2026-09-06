"""Deterministic safety for the FULL permitted input, before any cap or lock.

Unknown is never normal; a missing protocol is not a safe result. Screening
covers readable text and captions only, never hidden content of unprocessed
media (the caller's media_failure route). A verdict is not a diagnosis. The
copied adult cardiology policy awaits Sanad v2 re-approval.
"""

import math
import re
import unicodedata
from typing import Literal

from sanad.domain import ObservationRef, TextSpan
from sanad.safety import labs, sentinel, templates, validator, vitals
from sanad.safety.models import (
    IncidentFacts,
    LabCandidate,
    LabVerdict,
    OutputContext,
    Quantity,
    Reason,
    ScreenVerdict,
    Severity,
    ValidationVerdict,
    Violation,
    ViolationReason,
    VitalVerdict,
)
from sanad.safety.normalize import (
    _DIACRITICS,
    _LETTER_VARIANTS,
    _NON_TEXT,
    FRANCO_ALIASES,
)
from sanad.safety.normalize import (
    normalize as _normalize,
)
from sanad.safety.policy import LabRule, SafetyPolicy


def normalize(text: str, *, policy: SafetyPolicy) -> str:
    policy.require_supported_tables()
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    return _normalize(text)


def _spans(text: str, form: str) -> tuple[TextSpan, ...]:
    """Map normalized matches to original Unicode offsets, including aliases.

    NFKC may compose across code points. If per-character mapping differs from
    the actual normalizer, omit the optional span instead of inventing offsets.
    """
    chars: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(text):
        folded = unicodedata.normalize("NFKC", char)
        folded = _DIACRITICS.sub("", folded).translate(_LETTER_VARIANTS).lower()
        for letter in _NON_TEXT.sub(" ", folded):
            chars.append(letter)
            offsets.append(index)
    normalized = " "
    spans: list[tuple[int, int]] = [(0, 0)]
    for token in re.finditer(r"\S+", "".join(chars)):
        word = FRANCO_ALIASES.get(token.group(), token.group())
        original = (offsets[token.start()], offsets[token.end() - 1] + 1)
        normalized += word + " "
        spans.extend([original] * len(word) + [(original[1], original[1])])
    if normalized != _normalize(text):
        return ()
    found = []
    start = normalized.find(form)
    while start >= 0:
        covered = [(a, b) for a, b in spans[start : start + len(form)] if a < b]
        if covered:
            found.append(TextSpan(start=min(a for a, _ in covered), end=max(b for _, b in covered)))
        start = normalized.find(form, start + 1)
    return tuple(found)


def grade_bp(systolic: int, diastolic: int, *, policy: SafetyPolicy) -> VitalVerdict:
    policy.require_supported_tables()
    if type(systolic) is not int or type(diastolic) is not int:
        raise ValueError("blood pressure requires two integers")
    thresholds = policy.bp_thresholds
    level: Literal["crisis", "low", "normal", "implausible"] = "normal"
    concept = None
    plausible = systolic > diastolic
    for value, (low, high) in (
        (systolic, thresholds.plausible_systolic),
        (diastolic, thresholds.plausible_diastolic),
    ):
        plausible &= low <= value and (
            value < high if thresholds.upper_bounds_exclusive else value <= high
        )
    if not plausible:
        level, concept = "implausible", "blood pressure requires verification"
    elif systolic >= thresholds.systolic_crisis or diastolic >= thresholds.diastolic_crisis:
        level, concept = "crisis", vitals.CRISIS_CONCEPT
    elif systolic < thresholds.systolic_low:
        level, concept = "low", vitals.LOW_CONCEPT
    return VitalVerdict(
        level=level,
        systolic=systolic,
        diastolic=diastolic,
        concept=concept,
        decided_by="code (sanad.safety.kernel; copied core/vitals.py table)",
        thresholds_version=thresholds.thresholds_version,
        policy_version=policy.policy_version,
        rule_id=f"bp:{level}",
    )


def find_bp(text: str, *, policy: SafetyPolicy) -> tuple[tuple[int, int, TextSpan], ...]:
    """Retain implausible pairs for clarification; exclude fractions/dates/serials."""
    policy.require_supported_tables()
    found = []
    for match in vitals.BP_IN_TEXT.finditer(text):
        # Reject every component of a date or a longer slash-separated identifier.
        if re.search(r"[\d/\\]\s*$", text[: match.start()]) or re.match(
            r"\s*[/\\\d]", text[match.end() :]
        ):
            continue
        systolic, diastolic = int(match.group(1)), int(match.group(2))
        # Two short date/time components do not assert a BP measurement.
        labelled_bp = bool(
            re.search(
                r"(?:\bbp|blood pressure|ضغط[\w]*)\s*[:=]?\s*$",
                text[: match.start()],
                re.IGNORECASE,
            )
        )
        # Calendar/time bounds are syntax, not new clinical thresholds.
        date_or_time = (1 <= systolic <= 31 and 1 <= diastolic <= 12) or (
            0 <= systolic <= 23 and 0 <= diastolic <= 59
        )
        if date_or_time and not labelled_bp:
            continue
        found.append((systolic, diastolic, TextSpan(start=match.start(), end=match.end())))
    return tuple(found)


def _rule_for(analyte: str, policy: SafetyPolicy) -> LabRule | None:
    key = labs._key(analyte)
    key = labs._key(labs.ALIASES.get(key, analyte))
    return next(
        (
            rule
            for rule in policy.lab_rules
            if labs._key(rule.analyte).replace(" ", "") == key.replace(" ", "")
        ),
        None,
    )


def _value(quantity: Quantity) -> float | None:
    text = quantity.raw_value.strip().translate(labs._ARABIC_INDIC)
    text = text.replace("٫", ".").replace("−", "-")
    text = re.sub(r"(?<=\d),(?=\d{3}(\D|$))", "", text).replace(",", ".")
    text = re.sub(r"^[<>≤≥]\s*", "", text)
    if not labs._VALUE.fullmatch(text):
        return None
    value = float(text)
    return value if math.isfinite(value) else None


def _converted(quantity: Quantity, rule: LabRule) -> float | None:
    value = _value(quantity)
    if quantity.raw_unit is None or value is None:
        return None
    unit = labs.unit_key(quantity.raw_unit)
    if unit == labs.unit_key(rule.unit):
        return value
    factor = labs.UNIT_CONVERSIONS.get((rule.analyte, unit))
    return value * factor if factor is not None else None


def _known_cutoff_unit(unit: str | None, rule: LabRule) -> bool:
    if unit is None:
        return False
    key = labs.unit_key(unit)
    if not key:
        return rule.analyte in {"Pregnancy test", "Culture (blood/CSF)"}
    # Unit recognition only; no new conversion factor or clinical cutoff.
    return bool(re.fullmatch(r"(?:[munpµ]?g|mmol|mol|[mu]?iu)/(?:[dmu]?l)", key))


def grade_lab(
    candidate: LabCandidate,
    *,
    policy: SafetyPolicy,
    baseline: Quantity | None = None,
    second_fact_present: bool = False,
) -> LabVerdict:
    policy.require_supported_tables()
    if type(second_fact_present) is not bool:
        raise ValueError("second_fact_present must be a boolean")
    rule = _rule_for(candidate.analyte_raw, policy)
    canonical = rule.analyte if rule else candidate.analyte_raw.strip()
    rule_key = "lab:" + labs._key(canonical).replace(" ", "-")

    def result(
        level: Literal["critical", "flagged", "normal", "cannot_judge", "not_in_table"],
        reason: Reason,
        note: str,
        *,
        needs_second: bool = False,
    ) -> LabVerdict:
        return LabVerdict(
            level=level,
            analyte_canonical=canonical,
            rule_id=f"{rule_key}:{reason}" if rule else None,
            note=note,
            needs_second_fact=needs_second,
            reason=reason,
            policy_version=policy.policy_version,
        )

    if rule is None:
        return result("not_in_table", "not_in_table", labs.NOT_IN_TABLE_NOTE)
    flag = candidate.slip_flag
    if rule.needs_slip_cutoff:
        if _value(candidate.value) is not None and not _known_cutoff_unit(
            candidate.value.raw_unit, rule
        ):
            return result(
                "cannot_judge",
                "unknown_unit",
                "A numeric slip result requires a recognizable explicit unit, even when flagged.",
            )
        # A typed qualitative pregnancy flag is the explicitly permitted first fact.
        positive = labs.flag_is_high(flag)
        cutoff = candidate.slip_cutoff
        if rule.analyte == "Troponin" and cutoff is None:
            return result(
                "cannot_judge",
                "missing_cutoff",
                "Troponin requires a readable slip cutoff; a flag alone remains concern.",
            )
        if cutoff is not None:
            value, high = _value(candidate.value), _value(cutoff)
            unit, cutoff_unit = candidate.value.raw_unit, cutoff.raw_unit
            if (
                not _known_cutoff_unit(unit, rule)
                or not _known_cutoff_unit(cutoff_unit, rule)
                or labs.unit_key(unit) != labs.unit_key(cutoff_unit)
            ):
                return result(
                    "cannot_judge",
                    "unknown_unit",
                    "Slip cutoff and value require the same explicit unit; "
                    "no conversion is guessed.",
                )
            elif value is None or high is None:
                if not positive or rule.analyte == "Troponin":
                    return result(
                        "cannot_judge", "unknown_value", "The slip value or cutoff cannot be read."
                    )
            else:
                positive |= value > high
        if not positive:
            return result("cannot_judge", "missing_cutoff", labs.CANNOT_JUDGE_NOTE)
        if rule.two_factor and not second_fact_present:
            return result(
                "flagged",
                "missing_second_fact",
                "Positive test; abdominal-pain fact not supplied. "
                "The two-factor rule requires both; doctor review.",
                needs_second=True,
            )
        return result(
            "critical",
            "slip_flag" if labs.flag_is_high(flag) else "threshold",
            "Slip-based critical rule matched"
            + (" with abdominal-pain fact." if rule.two_factor else "."),
        )

    if candidate.value.raw_unit is None or (
        labs.unit_key(candidate.value.raw_unit) != labs.unit_key(rule.unit)
        and (rule.analyte, labs.unit_key(candidate.value.raw_unit)) not in labs.UNIT_CONVERSIONS
    ):
        return result(
            "cannot_judge",
            "unknown_unit",
            "Missing or unsupported unit; cannot judge, pending doctor review.",
        )
    value = _converted(candidate.value, rule)
    if value is None:
        return result("cannot_judge", "unknown_value", labs.CANNOT_JUDGE_NOTE)
    bounded = candidate.value.raw_value.strip().startswith(("<", ">", "≤", "≥"))
    below = rule.low is not None and value < rule.low
    above = rule.high is not None and value > rule.high
    if bounded:
        direction = candidate.value.raw_value.strip()[0]
        if not ((below and direction in "<≤") or (above and direction in ">≥")):
            return result(
                "cannot_judge",
                "bounded_value",
                "A bounded value does not establish a safe comparison.",
            )
    if below or above:
        return result("critical", "threshold", "Copied absolute critical threshold matched.")
    if rule.baseline_multiple is not None:
        base = _converted(baseline, rule) if baseline is not None else None
        if (
            base is None
            or base <= 0
            or (
                baseline is not None and baseline.raw_value.strip().startswith(("<", ">", "≤", "≥"))
            )
        ):
            return result(
                "cannot_judge",
                "missing_baseline",
                "Absolute threshold not crossed; the baseline comparison remains unknown.",
            )
        if value >= rule.baseline_multiple * base:
            return result("critical", "threshold", "Copied patient-baseline multiple matched.")
    if rule.low is None and rule.high is None and rule.baseline_multiple is None:
        return result(
            "cannot_judge",
            "missing_protocol",
            "This row supplies no numerical safety threshold or patient target.",
        )
    if labs.flag_is_high(flag) or labs.flag_is_low(flag):
        return result(
            "flagged",
            "slip_flag",
            "The slip flags this value; no copied critical threshold was crossed.",
        )
    return result(
        "normal",
        "within_table",
        "No applicable critical threshold crossed; this is not clinical clearance.",
    )


_NUMBER_TEXT = r"[<>≤≥]?\s*[-+]?\d+(?:[.,٫]\d+)?(?:[eE][-+]?\d+)?"
_FLAG_TEXT = "|".join(
    re.escape(flag)
    for flag in sorted(set(labs.HIGH_FLAGS + labs.LOW_FLAGS + ("negative",)), key=len, reverse=True)
)
_RESULT = re.compile(
    rf"\s*(?:\([^\n)]*\)\s*)?(?:[:=]\s*)?"
    rf"(?P<value>{_NUMBER_TEXT}|(?:{_FLAG_TEXT}|pending|unreadable|unknown|see comment)(?!\w))",
    re.IGNORECASE,
)
_UNIT = re.compile(r"\s*([a-zA-Zµμ×/][a-zA-Z0-9µμ×/^*.-]*)")
_CURRENT = tuple(
    _normalize(word)
    for word in ("now", "still", "again", "today", "دلوقتي", "لسه", "تاني", "النهارده")
)


def _lab_mentions(text: str, policy: SafetyPolicy) -> tuple[tuple[LabCandidate, TextSpan], ...]:
    names = set(labs.ALIASES) | {rule.analyte for rule in policy.lab_rules}
    names |= {rule.analyte for rule in labs.CRITICAL_LABS}
    names |= set(labs.EXTRA_PANEL_WORDS)
    # Also retain a plainly labelled unknown result; it must not become normal.
    names.update(
        match.group(1)
        for match in re.finditer(r"(?:^|[\n;])\s*([A-Za-z][A-Za-z-]+)\s+(?=\d)", text)
    )
    # A unit identifies a readable measurement even with an unlisted analyte.
    for match in re.finditer(rf"(?<!\w)([A-Za-z][A-Za-z-]+)\s+({_NUMBER_TEXT})", text):
        probe_unit = _UNIT.match(text, match.end())
        if probe_unit is not None and "/" in probe_unit.group(1):
            names.add(match.group(1))
    pattern = re.compile(
        r"(?<!\w)(?:"
        + "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
        + r")(?!\w)",
        re.IGNORECASE,
    )
    found = []
    for match in pattern.finditer(text):
        reading = _RESULT.match(text, match.end())
        if reading is None:
            continue
        raw = reading.group("value").strip()
        flag = raw if labs.flag_is_high(raw) or labs.flag_is_low(raw) else None
        unit_match = _UNIT.match(text, reading.end()) if flag is None else None
        unit = unit_match.group(1).rstrip(".") if unit_match else None
        end = unit_match.end() if unit_match else reading.end()
        found.append(
            (
                LabCandidate(
                    analyte_raw=match.group(),
                    value=Quantity(raw_value=raw, raw_unit=unit),
                    slip_flag=flag,
                ),
                TextSpan(start=match.start(), end=end),
            )
        )
    return tuple(found)


def screen_text(text: str, *, policy: SafetyPolicy) -> ScreenVerdict:
    """No length argument exists. Callers must supply the full permitted payload."""
    haystack = normalize(text, policy=policy)
    best = ScreenVerdict(
        level="none",
        rule_family="phrase",
        rule_id="no_match",
        reason="no_rule_matched",
        policy_version=policy.policy_version,
    )

    def retain(verdict: ScreenVerdict) -> None:
        nonlocal best
        rank = {"none": 0, "concern": 1, "danger": 2}
        if rank[verdict.level] > rank[best.level]:
            best = verdict

    never = haystack in {_normalize(phrase) for phrase in sentinel.NEVER_WAKE}
    if not never:
        for row, (concept, phrases) in enumerate(sentinel._NORMALIZED):
            for index, phrase in enumerate(phrases):
                if (
                    not phrase.strip()
                    or phrase not in haystack
                    or not sentinel.supported(phrase, haystack)
                ):
                    continue
                matches: tuple[TextSpan | None, ...] = _spans(text, phrase) or (None,)
                for span in matches:
                    sentence = validator._sentence_at(text, span.start) if span else text
                    local = _normalize(sentence)
                    if sentinel.laughing_context(local) and phrase in sentinel._LAUGHING_AMBIGUOUS:
                        continue
                    resolved = sentinel.resolved_tense(local) and not any(
                        marker in local for marker in _CURRENT
                    )
                    retain(
                        ScreenVerdict(
                            level="concern" if resolved else "danger",
                            concept=concept,
                            rule_family="phrase",
                            rule_id=f"must_wake:{row}:{index}",
                            matched_span=span,
                            reason="resolved_tense" if resolved else "threshold",
                            policy_version=policy.policy_version,
                        )
                    )
        # Preserve cross-token matching while letting a resolved earlier sentence
        # stand separately from a new, current complaint.
        for offset, sentence in [(0, text), *validator._sentences(text)]:
            local = _normalize(sentence)
            if sentinel.laughing_context(local) or (
                sentinel.resolved_tense(local) and not any(marker in local for marker in _CURRENT)
            ):
                continue
            for row, (concept, groups) in enumerate(sentinel._NORMALIZED_RULES):
                if all(any(form and form in local for form, _ in group) for group in groups):
                    retain(
                        ScreenVerdict(
                            level="danger",
                            concept=concept,
                            rule_family="concept",
                            rule_id=f"concept:{row}",
                            matched_span=TextSpan(start=offset, end=offset + len(sentence))
                            if sentence
                            else None,
                            reason="threshold",
                            policy_version=policy.policy_version,
                        )
                    )
    for systolic, diastolic, span in find_bp(text, policy=policy):
        bp = grade_bp(systolic, diastolic, policy=policy)
        if bp.level != "normal":
            retain(
                ScreenVerdict(
                    level="concern" if bp.level == "implausible" else "danger",
                    concept=bp.concept,
                    rule_family="vital",
                    rule_id=bp.rule_id,
                    matched_span=span,
                    reason="implausible" if bp.level == "implausible" else "threshold",
                    policy_version=policy.policy_version,
                )
            )
    for candidate, span in _lab_mentions(text, policy):
        lab = grade_lab(candidate, policy=policy, second_fact_present=labs.abdominal_pain(text))
        if lab.level != "normal":
            retain(
                ScreenVerdict(
                    level="danger" if lab.level == "critical" else "concern",
                    concept=lab.analyte_canonical,
                    rule_family="lab",
                    rule_id=lab.rule_id or "lab:not-in-table",
                    matched_span=span,
                    reason=lab.reason,
                    policy_version=policy.policy_version,
                )
            )
    return best


def wants_treatment_change(text: str, *, policy: SafetyPolicy) -> bool:
    policy.require_supported_tables()
    return validator.wants_treatment_change(text)


def validate_patient_output(
    text: str, *, context: OutputContext, policy: SafetyPolicy
) -> ValidationVerdict:
    policy.require_supported_tables()
    violations: list[Violation] = []

    def add(reason: ViolationReason, detail: str) -> None:
        violation = Violation(reason=reason, detail=detail)
        if violation not in violations:
            violations.append(violation)

    low = _normalize(text)
    drugs = " ".join(name for order in context.active_orders for name in order.drug_names)
    plan_low = _normalize(drugs)
    for phrase in validator.REASSURANCE:
        if _normalize(phrase) in low:
            add("unsupported_reassurance", phrase)
    for drug in [*validator._drugs(text), *validator.unknown_entities(text, drugs)]:
        if _normalize(drug) not in plan_low:
            add("drug_not_in_active_orders", drug)
    # Preserve the copied gate's contextual/bare-number guards as well as the
    # contract's stronger exact-unit check. No mode can clear a prior violation.
    legacy = validator.validate(
        text,
        "general" if context.mode == "general_education" else "plan",
        "\n".join((drugs, *context.allowed_numbers, policy.emergency_number)),
    )
    for reason in legacy.reasons:
        if reason.startswith("number "):
            add("unsupported_clinical_number", reason)
    allowed_units: set[tuple[str, str]] = set()

    def quantities(body: str) -> list[tuple[str, str]]:
        digits = validator.digit_form(body)
        found = []
        for match in validator._NUMBER.finditer(digits):
            tail = validator._UNIT_AFTER.match(digits, match.end())
            unit = tail.group(1).lower().rstrip(".,;:") if tail else ""
            if validator._CLASS_OF_UNIT.get(unit) is not None:
                found.append((validator._canonical(match.group()), labs.unit_key(unit)))
        return found

    for number in context.allowed_numbers:
        allowed_units.update(quantities(number))
    for value, unit in quantities(text):
        if (value, unit) not in allowed_units:
            add("unsupported_clinical_number", f"{value} {unit}")
    for _, sentence in validator._sentences(text):
        sentence_low = _normalize(sentence)
        imperative = any(_normalize(word) in sentence_low for word in validator.IMPERATIVES)
        dose = any(
            kind in {"dose", "count", "frequency"}
            for _, kind, _ in validator.classified_numbers(sentence)
        )
        frequency = bool(
            re.search(
                r"\b(daily|nightly|twice|once|every\s+\w+)\b|يوميا|مرتين|كل يوم", sentence_low
            )
        )
        if imperative and (dose or frequency or validator._drugs(sentence)):
            add("imperative_dose_or_frequency", sentence.strip())
    if wants_treatment_change(text, policy=policy):
        add("treatment_change", "Treatment-change phrase or concept matched.")
    # Named-drug substitutions also count when the patient's generic nouns are absent.
    if validator._drugs(text) and any(
        _normalize(word) in low
        for word in (
            "stop",
            "start",
            "add",
            "switch",
            "replace",
            "increase",
            "decrease",
            "زود",
            "وقف",
            "بطل",
            "بدل",
        )
    ):
        add("treatment_change", "Treatment-change verb accompanies a drug name.")
    body = validator.digit_form(text)
    for _, sentence in validator._sentences(body):
        if re.search(
            r"\b(call|dial|ambulance|emergency)\b|اتصل|اتصلي|اسعاف|الإسعاف", sentence, re.IGNORECASE
        ):
            for match in validator._NUMBER.finditer(sentence):
                if validator._canonical(match.group()) != policy.emergency_number:
                    add("wrong_emergency_number", match.group())
    awareness = (
        "your doctor knows",
        "your doctor has been notified",
        "your doctor was notified",
        "your doctor has been alerted",
        "your doctor has just been alerted",
        "your doctor is aware",
        "your doctor has read",
        "i told your doctor",
        "دكتورك اتبلغ",
        "الدكتور عارف",
        "دكتورك عارف",
    )
    if not context.doctor_notified and any(_normalize(phrase) in low for phrase in awareness):
        add("unsupported_doctor_awareness", "Doctor awareness is not established.")
    if any(
        _normalize(phrase) in low
        for phrase in ("your doctor has read", "your doctor read", "الدكتور قرا", "دكتورك قرا")
    ):
        add("unsupported_doctor_awareness", "Notification does not establish human reading.")
    return ValidationVerdict(
        ok=not violations, violations=tuple(violations), policy_version=policy.policy_version
    )


def render_urgent(
    template_id: Literal[
        "patient_emergency", "doctor_danger", "patient_unreadable_resend", "patient_safety_ack"
    ],
    *,
    language: Literal["ar", "en"],
    gender: Literal["m", "f", "u"],
    policy: SafetyPolicy,
    **fields: str,
) -> str:
    policy.require_supported_tables()
    if language not in {"ar", "en"} or gender not in {"m", "f", "u"}:
        raise ValueError("unsupported template language or gender")
    text = templates.URGENT_TEMPLATES[template_id][language][gender]
    wanted = templates.fields_of(text)
    supplied = dict(fields)
    if "emergency_number" in wanted:
        if "emergency_number" in supplied:
            raise ValueError("the emergency number comes only from the explicit policy")
        supplied["emergency_number"] = policy.emergency_number
    if set(supplied) != wanted:
        raise ValueError(f"{template_id} requires exactly {sorted(wanted)}")
    if any(not value.strip() or "{" in value or "}" in value for value in supplied.values()):
        raise ValueError("template fields must be nonblank and cannot carry placeholders")
    rendered = text.format(**supplied)
    if "{" in rendered or "}" in rendered:
        raise ValueError("unfilled template placeholder")
    return rendered


def to_incident_facts(
    verdict: ScreenVerdict | VitalVerdict | LabVerdict,
    *,
    source: ObservationRef,
    policy: SafetyPolicy,
) -> tuple[IncidentFacts, Severity]:
    policy.require_supported_tables()
    if verdict.policy_version != policy.policy_version:
        raise ValueError("verdict and supplied policy versions differ")
    if verdict.level in {"none", "normal"}:
        raise ValueError("a nonincident verdict cannot create incident facts")
    severity: Severity = (
        "danger" if verdict.level in {"danger", "crisis", "low", "critical"} else "concern"
    )
    rule_id = verdict.rule_id or "lab:not-in-table"
    facts = IncidentFacts(
        source=source,
        unique_source_key=f"{source.observation_id}#{verdict.rule_family}#{rule_id}",
        rule_family=verdict.rule_family,
        rule_id=rule_id,
        policy_version=verdict.policy_version,
        verdict=verdict,
    )
    return facts, severity
