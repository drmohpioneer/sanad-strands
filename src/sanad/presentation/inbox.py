"""Doctor inbox deadlines are dates, not source identifiers."""

from sanad.presentation.catalog import Catalog, opaque_fields, render, validate_catalog
from sanad.presentation.context import PresentationContext

CATALOG: Catalog = {
    "inbox.due_row": {
        "en": "{name}: {kind}.\nDue {due}. Waiting: {hours} hours. State: {state}.",
        "ar": "{name}: {kind}.\nالموعد {due}. الانتظار: {hours} ساعة. الحالة: {state}.",
    }
}
validate_catalog("inbox", CATALOG)


def due_row(context: PresentationContext, **fields: str) -> str:
    return render(CATALOG, "inbox.due_row", context, **opaque_fields(fields))
