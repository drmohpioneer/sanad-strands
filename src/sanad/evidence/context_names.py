"""Names-only hints from current, patient-scoped medication orders."""

from sanad.domain import PatientScope
from sanad.scribe.extract import OrderCandidate
from sanad.scribe.records import CareOrderHead, CareOrderVersion
from sanad.steward.types import records
from sanad.store.protocol import Store
from sanad.store.records import from_record


def active_drug_names(store: Store, scope: PatientScope, limit: int) -> tuple[str, ...]:
    if not isinstance(scope, PatientScope):
        raise ValueError("patient scope required")
    if limit <= 0:
        return ()
    names: dict[str, None] = {}
    for row in records(store, scope, "care_order_head"):
        head = from_record(row, CareOrderHead)
        if head.status != "active" or head.type != "medication":
            continue
        version = store.get(scope, "care_order_version", head.current_version_id)
        if version is None:
            continue
        order = from_record(version, CareOrderVersion)
        instruction = order.structured_instruction
        if (
            order.order_id == head.id
            and order.order_version == head.current_order_version
            and isinstance(instruction, OrderCandidate)
            and instruction.drug.strip()
        ):
            names[instruction.drug.strip()] = None
            if len(names) >= limit:
                break
    return tuple(names)
