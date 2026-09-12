"""Accepted resolver wording; legacy fragments and punctuation are intentional."""

from sanad.presentation.catalog import Catalog, validate_catalog

CATALOG: Catalog = {
    "resolver.area": {
        "ar": "تحب أدور في أنهي منطقة أو حي؟",
        "en": "Which area or neighbourhood should I search near?",
    },
    "resolver.detail": {
        "ar": "إيه الصعوبة العملية اللي محتاج مساعدة فيها؟",
        "en": "What practical difficulty do you need help with?",
    },
    "resolver.disclosure": {
        "ar": (
            "الأماكن دي قريبة من المنطقة اللي قلتها. مش شايف أسعارهم أو المتوفر "
            "عندهم، ومش متأكد إن عندهم المطلوب، ومحجزتش حاجة."
        ),
        "en": (
            "These places are near the area you named. I cannot see their prices "
            "or stock, cannot confirm they have what you need, and have not booked "
            "anything."
        ),
    },
    "resolver.unavailable": {
        "ar": (
            "ملقيتش خيارات أقدر أتحقق منها. ممكن تسأل العيادة عن مساعدة عملية "
            "لتنفيذ طلب الدكتور. العائق لسه مسجل."
        ),
        "en": (
            "I could not verify any options. You can ask the clinic about "
            "practical help with your doctor's request. The barrier remains "
            "recorded."
        ),
    },
    "resolver.unresolved": {
        "ar": "العائق لسه مسجل مع اللي جربناه، ومطلوب الدكتور لسه ما اكتملش.",
        "en": (
            "The barrier and what we tried remain recorded; your doctor's request "
            "is still unfinished."
        ),
    },
    "resolver.handed": {
        "ar": "سجلت الصعوبة واللي جربناه لمراجعة الدكتور. المطلوب لسه ما اكتملش.",
        "en": (
            "The difficulty and what we tried are recorded for your doctor's "
            "review. The request is still unfinished."
        ),
    },
    "resolver.resolved": {
        "ar": "سجلت إنك بتقول إن العائق اتحل. ده مش بلاغ إتمام طلب الدكتور.",
        "en": (
            "Your report that the barrier is resolved is recorded. This does not "
            "report completing your doctor's request."
        ),
    },
    "resolver.place": {
        "ar": "{name}: {distance} م{details}",
        "en": "{name}: {distance} m{details}",
    },
}

FIELDS = validate_catalog("resolver", CATALOG)
