"""Pure, unit-aware add-only doctor alerts. Kernel policy tables are unchanged."""

import json
from collections.abc import Sequence
from hashlib import sha256
from typing import Literal, Protocol, runtime_checkable

from sanad.domain.boundaries import _BoundaryValue
from sanad.safety.aliases import analyte, normalize
from sanad.safety.kernel import _converted, _rule_for, _value
from sanad.safety.labs import unit_key
from sanad.safety.models import Quantity
from sanad.safety.policy import SafetyPolicy


class AlertLike(Protocol):
    @property
    def text(self) -> str: ...
    @property
    def metric(self) -> str: ...
    @property
    def comparator(self) -> Literal["gt", "ge", "lt", "le"]: ...
    @property
    def threshold(self) -> str: ...
    @property
    def unit(self) -> str | None: ...


class AlertInstruction(_BoundaryValue):
    text: str
    metric: str
    comparator: Literal["gt", "ge", "lt", "le"]
    threshold: str
    unit: str | None = None
    floor_mode: Literal["add_only"] = "add_only"


class LabInput(Protocol):
    @property
    def analyte(self) -> str: ...
    @property
    def value(self) -> str | None: ...
    @property
    def unit(self) -> str | None: ...


@runtime_checkable
class ReadingInput(Protocol):
    @property
    def analyte(self) -> str: ...
    @property
    def raw_value(self) -> str | None: ...
    @property
    def raw_unit(self) -> str | None: ...


class AlertOrder(_BoundaryValue):
    head_id: str
    active: bool
    instruction: AlertInstruction


class AlertHit(_BoundaryValue):
    head_id: str
    metric: str
    row_digest: str
    status: Literal["hit", "cannot_judge"]
    reason: Literal["patient_alert", "alert_unit_mismatch", "alert_value_unverified"]
    comparator: str
    threshold: str
    unit: str | None
    floor_mode: Literal["add_only"] = "add_only"


def row_digest(name: str, value: str | None, unit: str | None) -> str:
    return sha256(
        json.dumps({"name": name, "value": value, "unit": unit}, sort_keys=True).encode()
    ).hexdigest()


def metric(name: str) -> str:
    normalized = normalize(name)
    return {
        "systolic": "systolic",
        "diastolic": "diastolic",
        "انقباضي": "systolic",
        "انبساطي": "diastolic",
    }.get(normalized, analyte(name))


def quantity(value: str, unit: str | None, name: str, policy: SafetyPolicy) -> float | None:
    q = Quantity(raw_value=value, raw_unit=unit)
    if name in {"systolic", "diastolic"}:
        return _value(q) if unit and unit_key(unit) == unit_key("mmHg") else None
    rule = _rule_for(name, policy)
    return _converted(q, rule) if rule else None


def alert_allowed(alert: AlertLike, policy: SafetyPolicy) -> bool:
    name = metric(alert.metric)
    threshold = quantity(alert.threshold, alert.unit, name, policy)
    if threshold is None:
        return False
    if name in {"systolic", "diastolic"}:
        high = (
            policy.bp_thresholds.systolic_crisis
            if name == "systolic"
            else policy.bp_thresholds.diastolic_crisis
        )
        low = policy.bp_thresholds.systolic_low if name == "systolic" else None
        # BP upper limits are inclusive in the kernel.
        return (
            (threshold < high or (threshold == high and alert.comparator == "ge"))
            if alert.comparator in {"gt", "ge"}
            else low is None or threshold >= low
        )
    rule = _rule_for(name, policy)
    if rule is None:
        return False
    if alert.comparator in {"gt", "ge"}:
        return rule.high is None or threshold <= rule.high
    return rule.low is None or threshold >= rule.low


def refused_at_write(alert: AlertLike, policy: SafetyPolicy) -> bool:
    # Existing unitless dictation remains an unverified instruction, never a hit.
    # The canonical table is used here only to reject an evidently looser floor.
    rule = _rule_for(metric(alert.metric), policy)
    checked = (
        alert
        if alert.unit or rule is None
        else AlertInstruction(
            text=alert.text,
            metric=alert.metric,
            comparator=alert.comparator,
            threshold=alert.threshold,
            unit=rule.unit,
        )
    )
    return not alert_allowed(checked, policy) if checked.unit is not None else False


def apply_alerts(
    rows_or_readings: Sequence[LabInput | ReadingInput],
    alert_heads: Sequence[AlertOrder],
    policy: SafetyPolicy,
) -> tuple[AlertHit, ...]:
    policy.require_supported_tables()
    hits: list[AlertHit] = []
    for row in rows_or_readings:
        name = row.analyte
        value = row.raw_value if isinstance(row, ReadingInput) else row.value
        unit = row.raw_unit if isinstance(row, ReadingInput) else row.unit
        measurements = [(metric(name), value, unit)]
        if normalize(name) == "bp" and value and len(value.split("/")) == 2:
            a, b = value.split("/")
            measurements = [("systolic", a, unit), ("diastolic", b, unit)]
        for name, value, unit in measurements:
            digest = row_digest(name, value, unit)
            for head in alert_heads:
                alert = head.instruction
                if not head.active or metric(alert.metric) != name:
                    continue
                # A malformed/looser stored alert cannot change the kernel floor.
                if not alert_allowed(alert, policy):
                    if quantity(alert.threshold, alert.unit, name, policy) is None:
                        hits.append(
                            AlertHit(
                                head_id=head.head_id,
                                metric=name,
                                row_digest=digest,
                                status="cannot_judge",
                                reason="alert_unit_mismatch",
                                comparator=alert.comparator,
                                threshold=alert.threshold,
                                unit=alert.unit,
                            )
                        )
                    continue
                number = quantity(value, unit, name, policy) if value else None
                threshold = quantity(alert.threshold, alert.unit, name, policy)
                status: Literal["hit", "cannot_judge"] = "hit"
                reason: Literal[
                    "patient_alert", "alert_unit_mismatch", "alert_value_unverified"
                ] = "patient_alert"
                if number is None:
                    status, reason = (
                        "cannot_judge",
                        "alert_unit_mismatch"
                        if not unit or quantity("1", unit, name, policy) is None
                        else "alert_value_unverified",
                    )
                elif value and value.strip().startswith(("<", ">", "≤", "≥")):
                    status, reason = "cannot_judge", "alert_value_unverified"
                else:
                    assert threshold is not None
                    passed = {
                        "gt": number > threshold,
                        "ge": number >= threshold,
                        "lt": number < threshold,
                        "le": number <= threshold,
                    }[alert.comparator]
                    if not passed:
                        continue
                hits.append(
                    AlertHit(
                        head_id=head.head_id,
                        metric=name,
                        row_digest=digest,
                        status=status,
                        reason=reason,
                        comparator=alert.comparator,
                        threshold=alert.threshold,
                        unit=alert.unit,
                    )
                )
    return tuple(dict.fromkeys(hits))
