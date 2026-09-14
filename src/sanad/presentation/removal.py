"""Approved removal confirmation words, with no patient notification."""

from sanad.presentation.catalog import Catalog, validate_catalog

CATALOG: Catalog = {
    "removal.open": {"en": "Remove patient", "ar": "إزالة المريض"},
    "removal.remove": {"en": "Remove", "ar": "إزالة"},
    "removal.keep": {"en": "Keep", "ar": "إبقاء"},
    "removal.name": {"en": "Patient's name", "ar": "اسم المريض"},
    "removal.banner": {"en": "Removed on {date}", "ar": "تمت الإزالة في {date}"},
    "removal.result": {"en": "{name} was removed.", "ar": "تمت إزالة {name}."},
    "removal.not_sent": {
        "en": "Not sent: this patient was removed.",
        "ar": "لم تُرسل: تمت إزالة هذا المريض.",
    },
    "removal.confirm": {
        "en": (
            "Remove {name}? Sanad stops messages to this patient now (a message already "
            "being sent may still arrive, and emergency safety replies still work), cancels "
            "their open requests and reminders (items with a danger history stay open for "
            "you), and deletes their messages, voice notes and photos after 30 days. Their "
            "open reviews and questions stay with you. The medical record you accepted is "
            "kept. Type the patient's name to confirm."
        ),
        "ar": (
            "إزالة {name}؟ سند هيوقف الرسائل للمريض دلوقتي (رسالة بدأ إرسالها ممكن توصل، "
            "وردود الطوارئ هتفضل شغالة)، ويلغي طلباته وتذكيراته المفتوحة (البنود اللي فيها "
            "تاريخ خطر هتفضل مفتوحة ليك)، ويحذف رسايله وتسجيلاته الصوتية وصوره بعد 30 يوم. "
            "المراجعات والأسئلة المفتوحة هتفضل عندك. السجل الطبي اللي اعتمدته محفوظ. اكتب "
            "اسم المريض للتأكيد."
        ),
    },
}
validate_catalog("removal", CATALOG)


def words(name: str, language: str = "en") -> dict[str, str]:
    return {
        key.removeprefix("removal."): values["ar" if language == "ar" else "en"].replace(
            "{name}", name
        )
        for key, values in CATALOG.items()
    }
