"""Draft bilingual full sentences rendered through the presentation resolver."""

from sanad.presentation.catalog import Catalog, opaque_fields, validate_catalog
from sanad.presentation.catalog import render as interpolate
from sanad.presentation.context import PresentationContext, resolve

CATALOG: Catalog = {
    "liaison.page": {
        "en": "Review inbox — page {page} of {pages}.",
        "ar": "قائمة المراجعات — صفحة {page} من {pages}.",
    },
    "liaison.empty": {
        "en": "There are no open or acknowledged reviews.",
        "ar": "مفيش مراجعات مفتوحة أو تم الإقرار باستلامها.",
    },
    "liaison.row": {
        "en": "{name}: {kind}.\nSource: {source}. Waiting: {hours} hours. State: {state}.",
        "ar": "{name}: {kind}.\nالمصدر: {source}. الانتظار: {hours} ساعة. الحالة: {state}.",
    },
    "liaison.open": {"en": "Open", "ar": "مفتوحة"},
    "liaison.acknowledged": {
        "en": "Acknowledged; still unresolved",
        "ar": "تم الإقرار باستلامها؛ لسه محتاجة حسم",
    },
    "liaison.unnamed": {"en": "Patient name not recorded", "ar": "اسم المريض مش مسجل"},
    "liaison.unassigned": {"en": "No patient linked", "ar": "غير مرتبطة بمريض"},
    "liaison.changed": {
        "en": "The source changed materially after the notice.",
        "ar": "المصدر اتغيّر بشكل جوهري بعد التنبيه.",
    },
    "liaison.usage": {
        "en": "Use /inbox followed by a page number to see another page.",
        "ar": "استخدم /inbox وبعده رقم الصفحة لعرض صفحة تانية.",
    },
    "liaison.ack": {"en": "Acknowledge {n}", "ar": "إقرار باستلام {n}"},
    "liaison.dispose": {"en": "Record disposition {n}: {action}", "ar": "تسجيل حسم {n}: {action}"},
    "liaison.reason": {
        "en": (
            "Record only the review disposition ({action}). This does not "
            "perform the underlying operation. Supply your reason:\n/resolve "
            "{offer} your reason"
        ),
        "ar": (
            "تسجيل حسم المراجعة فقط ({action}). ده مش بينفّذ الإجراء نفسه. اكتب "
            "السبب:\n/resolve {offer} السبب"
        ),
    },
    "liaison.reason_required": {
        "en": "An explicit reason is required. Use the disposition button and supply your reason.",
        "ar": "لازم سبب صريح. استخدم زر حسم المراجعة واكتب السبب.",
    },
    "liaison.ack_done": {
        "en": "Acknowledgment recorded. The review remains unresolved.",
        "ar": "اتسجّل الإقرار بالاستلام. المراجعة لسه محتاجة حسم.",
    },
    "liaison.resolve_done": {
        "en": (
            "Review disposition recorded: {action}. Reason: {reason}. No "
            "underlying operation was performed by this action."
        ),
        "ar": "اتسجّل حسم المراجعة: {action}. السبب: {reason}. الاختيار ده ما نفّذش الإجراء نفسه.",
    },
    "liaison.stale": {
        "en": (
            "This offer is stale: {reason}. Nothing was changed. Open /inbox "
            "for the current review."
        ),
        "ar": "الاختيار ده بقى قديم: {reason}. مفيش حاجة اتغيّرت. افتح /inbox للمراجعة الحالية.",
    },
    "liaison.fact_open": {
        "en": "This notice includes unresolved review work.",
        "ar": "التنبيه ده فيه مراجعات لسه محتاجة حسم.",
    },
    "liaison.fact_ack": {
        "en": "Acknowledged reviews still require a disposition.",
        "ar": "المراجعات اللي اتسجّل استلامها لسه محتاجة حسم.",
    },
    "liaison.fact_delivery": {
        "en": "Message delivery does not establish acknowledgment or resolution.",
        "ar": "توصيل الرسالة مش معناه إقرار بالاستلام أو حسم المراجعة.",
    },
}
validate_catalog("liaison", CATALOG)


def render(key: str, language: str | PresentationContext, **fields: str) -> str:
    return interpolate(
        CATALOG, "liaison." + key, resolve(language, "doctor"), **opaque_fields(fields)
    )


STALE_REASONS = {
    "offer_missing": ("The saved offer is no longer available", "الاختيار المحفوظ مش متاح"),
    "offer_expired_or_used": (
        "The offer expired or was already used",
        "الاختيار انتهت صلاحيته أو اتستخدم",
    ),
    "review_or_source_changed": (
        "The review or its source changed after this notice",
        "المراجعة أو مصدرها اتغيّر بعد التنبيه",
    ),
    "source_version_changed": (
        "The source version does not match this notice",
        "نسخة المصدر مش مطابقة للتنبيه",
    ),
    "doctor_authority_changed": (
        "Doctor access changed after this notice",
        "صلاحية الدكتور اتغيّرت بعد التنبيه",
    ),
    "reason_required": ("An explicit reason is required", "لازم سبب صريح"),
    "default": (
        "Doctor access, the review, or this offer changed",
        "صلاحية الدكتور أو المراجعة أو الاختيار اتغيّر",
    ),
}
for _key, (_en, _ar) in STALE_REASONS.items():
    CATALOG["liaison.reason_" + _key] = {"en": _en, "ar": _ar}
validate_catalog("liaison", CATALOG)


def stale_reason(code: str, language: str | PresentationContext) -> str:
    return render("reason_" + (code if code in STALE_REASONS else "default"), language)
