"""Plain browser controls; paired entries keep the later Arabic adapter possible."""

from sanad.presentation.catalog import Catalog, validate_catalog

CATALOG: Catalog = {
    "conversation": {"en": "Your conversation", "ar": "محادثتك"},
    "message": {"en": "Your message", "ar": "رسالتك"},
    "send": {"en": "Send", "ar": "إرسال"},
    "older": {"en": "Older messages", "ar": "رسائل أقدم"},
    "no_messages": {"en": "No messages yet.", "ar": "لا توجد رسائل بعد."},
    "legacy": {"en": "Message sent on", "ar": "أُرسلت رسالة في"},
    "credential": {"en": "A sign-in link was sent on", "ar": "أُرسل رابط تسجيل الدخول في"},
    "upload": {"en": "Upload a document or photo", "ar": "إرسال مستند أو صورة"},
    "file": {"en": "Choose an image", "ar": "اختر صورة"},
    "caption": {"en": "Add a message (optional)", "ar": "أضف رسالة (اختياري)"},
    "limits": {
        "en": "Images up to 8 MiB, 8,000 pixels per side and 20 million pixels.",
        "ar": "صور حتى ٨ ميبي بايت، و٨٠٠٠ بكسل لكل جانب، و٢٠ مليون بكسل.",
    },
    "uploading": {"en": "Sending your image…", "ar": "جار إرسال صورتك…"},
    "received": {"en": "Received", "ar": "وصلت"},
    "processing": {"en": "Checking your document", "ar": "جار مراجعة مستندك"},
    "accepted": {"en": "Accepted", "ar": "تم القبول"},
    "needs_doctor_review": {"en": "Needs your doctor's review", "ar": "تحتاج مراجعة دكتورك"},
    "not_used": {"en": "Not used", "ar": "لم تُستخدم"},
    "rejected": {"en": "Could not use this image", "ar": "تعذر استخدام الصورة"},
    "unreadable": {"en": "Please send a clearer image.", "ar": "أرسل صورة أوضح من فضلك."},
    "too_large": {"en": "Please choose a smaller image.", "ar": "اختر صورة أصغر من فضلك."},
    "not_a_document": {
        "en": "Please choose a document or medical photo.",
        "ar": "اختر مستندًا أو صورة طبية من فضلك.",
    },
    "unsupported": {
        "en": "Please choose a supported image file.",
        "ar": "اختر ملف صورة مدعومًا من فضلك.",
    },
    "preferences": {"en": "Reminder preferences", "ar": "تفضيلات التذكيرات"},
    "enabled": {"en": "Reminders enabled", "ar": "التذكيرات مفعلة"},
    "paused": {"en": "Reminders paused", "ar": "التذكيرات متوقفة"},
    "stop": {"en": "Stop reminders", "ar": "إيقاف التذكيرات"},
    "resume": {"en": "Resume reminders", "ar": "استئناف التذكيرات"},
    "confirm": {"en": "Yes, resume reminders", "ar": "نعم، استأنف التذكيرات"},
    "quiet_start": {"en": "Quiet hours start", "ar": "بداية ساعات الهدوء"},
    "quiet_end": {"en": "Quiet hours end", "ar": "نهاية ساعات الهدوء"},
    "save": {"en": "Save quiet hours", "ar": "حفظ ساعات الهدوء"},
    "saved": {"en": "Saved.", "ar": "تم الحفظ."},
    "sent": {"en": "Message received.", "ar": "وصلت رسالتك."},
    "still_working": {
        "en": "Still working on your message. Your reply will appear here.",
        "ar": "لسه بنراجع رسالتك. الرد هيظهر هنا.",
    },
    "pending": {"en": "Received. Preparing your reply.", "ar": "وصل الطلب. بنجهز الرد."},
    "failed": {
        "en": "Could not complete this request. Try again.",
        "ar": "تعذر إكمال الطلب. حاول مجددًا.",
    },
    "expired": {"en": "Please sign in again from Telegram.", "ar": "سجل الدخول مجددًا من تيليجرام."},
    "conflict": {
        "en": "This request has changed or expired. Refresh and try again.",
        "ar": "تغير الطلب أو انتهت صلاحيته. حدّث الصفحة وحاول مجددًا.",
    },
    "quiet_invalid": {"en": "Choose two different times.", "ar": "اختر وقتين مختلفين."},
}
CATALOG = {"patient_browser." + key: value for key, value in CATALOG.items()}
validate_catalog("patient_browser", CATALOG)
