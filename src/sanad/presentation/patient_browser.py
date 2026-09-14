"""Plain browser controls in English and Egyptian Arabic."""

from sanad.presentation.catalog import Catalog, validate_catalog

CATALOG: Catalog = {
    "settings": {"en": "Settings", "ar": "الإعدادات"},
    "conversation": {"en": "Your conversation", "ar": "كلامك مع سند"},
    "message": {"en": "Your message", "ar": "رسالتك"},
    "send": {"en": "Send", "ar": "ابعت"},
    "older": {"en": "Older messages", "ar": "رسايل أقدم"},
    "no_messages": {"en": "No messages yet.", "ar": "لسه مفيش رسايل."},
    "legacy": {"en": "Message sent on", "ar": "رسالة اتبعتت يوم"},
    "credential": {"en": "A sign-in link was sent on", "ar": "لينك الدخول اتبعت يوم"},
    "upload": {"en": "Upload a document or photo", "ar": "ابعت مستند أو صورة"},
    "file": {"en": "Choose a photo or PDF", "ar": "اختار صورة أو PDF"},
    "caption": {"en": "Add a message (optional)", "ar": "ضيف رسالة (لو حابب)"},
    "limits": {
        "en": "Photos up to 8 MB. PDFs up to 20 MB and 10 pages.",
        "ar": "صور لحد ٨ ميجابايت. ملفات PDF لحد ٢٠ ميجابايت و١٠ صفحات.",
    },
    "uploading": {"en": "Sending your image…", "ar": "بنبعت صورتك…"},
    "received": {"en": "Received", "ar": "وصلت"},
    "processing": {"en": "Checking your document", "ar": "بنراجع مستندك"},
    "accepted": {"en": "Accepted", "ar": "اتقبلت"},
    "needs_doctor_review": {"en": "Needs your doctor's review", "ar": "محتاجة دكتورك يراجعها"},
    "not_used": {"en": "Not used", "ar": "متستخدمتش"},
    "rejected": {"en": "Could not use this image", "ar": "معرفناش نستخدم الصورة دي"},
    "unreadable": {"en": "Please send a clearer image.", "ar": "ابعت صورة أوضح لو سمحت."},
    "too_large": {"en": "Please choose a smaller image.", "ar": "اختار صورة أصغر لو سمحت."},
    "document_too_many_pages": {
        "ar": "ابعت لحد ١٠ صفحات لو سمحت، الصفحات اللي فيها النتائج بس.",
        "en": "Please send up to 10 pages: just the pages with the results.",
    },
    "document_too_large": {
        "ar": "حجم الملف كبير. ابعت ملف أصغر أو صور لصفحات النتائج لو سمحت.",
        "en": "This file is too large. Please send a smaller file or photos of the result pages.",
    },
    "document_encrypted": {
        "ar": "الملف محمي بكلمة سر. ابعته من غير كلمة سر أو على شكل صور لو سمحت.",
        "en": "This file is password protected. Please send it without a password or as photos.",
    },
    "document_invalid": {
        "ar": "مش قادرين نفتح الملف. ابعته تاني أو على شكل صور لو سمحت.",
        "en": "This file could not be opened. Please send it again or as photos.",
    },
    "document_unreadable": {
        "ar": "مش قادرين نقرا الملف. ابعت صور لصفحات النتائج لو سمحت.",
        "en": "This file could not be read. Please send photos of the result pages.",
    },
    "document_blank": {
        "ar": "الملف باين فاضي. ابعت الصفحات اللي فيها النتائج لو سمحت.",
        "en": "This file looks empty. Please send the pages with the results.",
    },
    "document_too_detailed": {
        "ar": "شكرًا. دكتورك هيراجع المستند ده.",
        "en": "Thank you. Your doctor will review this document.",
    },
    "document_word_unsupported": {
        "ar": "لسه مش بنقرا ملفات Word. ابعته PDF أو صور لصفحات النتائج لو سمحت.",
        "en": (
            "Word files can't be read yet. Please send it as a PDF "
            "or as photos of the result pages."
        ),
    },
    "not_a_document": {
        "en": "Please choose a document or medical photo.",
        "ar": "اختار مستند أو صورة طبية لو سمحت.",
    },
    "unsupported": {
        "en": "Please choose a supported image file.",
        "ar": "اختار ملف صورة نقدر نفتحه لو سمحت.",
    },
    "preferences": {"en": "Reminder preferences", "ar": "ظبط التذكيرات"},
    "enabled": {"en": "Reminders enabled", "ar": "التذكيرات شغالة"},
    "paused": {"en": "Reminders paused", "ar": "التذكيرات واقفة"},
    "stop": {"en": "Stop reminders", "ar": "وقف التذكيرات"},
    "resume": {"en": "Resume reminders", "ar": "شغل التذكيرات تاني"},
    "confirm": {"en": "Yes, resume reminders", "ar": "أيوه، شغل التذكيرات تاني"},
    "quiet_start": {"en": "Quiet hours start", "ar": "ساعات الهدوء تبدأ إمتى"},
    "quiet_end": {"en": "Quiet hours end", "ar": "ساعات الهدوء تخلص إمتى"},
    "save": {"en": "Save quiet hours", "ar": "احفظ ساعات الهدوء"},
    "saved": {"en": "Saved.", "ar": "اتحفظ."},
    "sent": {"en": "Message received.", "ar": "رسالتك وصلت."},
    "still_working": {
        "en": "Still working on your message. Your reply will appear here.",
        "ar": "لسه بنراجع رسالتك. الرد هيظهر هنا.",
    },
    "pending": {"en": "Received. Preparing your reply.", "ar": "وصل طلبك. بنجهز الرد."},
    "failed": {
        "en": "Could not complete this request. Try again.",
        "ar": "معرفناش نكمل طلبك. جرب تاني.",
    },
    "expired": {
        "en": "Please sign in again from Telegram.",
        "ar": "ادخل تاني من تيليجرام لو سمحت.",
    },
    "changed_elsewhere": {
        "en": "Your access changed elsewhere.",
        "ar": "اتغيرت صلاحية دخولك من مكان تاني.",
    },
    "conflict": {
        "en": "This request has changed or expired. Refresh and try again.",
        "ar": "الطلب اتغير أو وقته خلص. حدّث الصفحة وجرب تاني.",
    },
    "quiet_invalid": {"en": "Choose two different times.", "ar": "اختار ميعادين مختلفين."},
    "read": {"en": "Read by Sanad", "ar": "سند قراها"},
    "documents_sent": {"en": "Documents you sent", "ar": "المستندات اللي بعتها"},
    "reminder_explanation": {
        "en": (
            "Sanad writes to you on Telegram when your doctor is waiting for "
            "something from you: a reading, a photo, an answer. During quiet hours "
            "routine reminders wait, except at the times you agreed to; replies to "
            "your own messages and anything urgent are not held. If you pause "
            "reminders, routine messages stop, your doctor can see they are paused, "
            "and urgent messages are never held back by the pause."
        ),
        "ar": (
            "سند بيبعتلك على تيليجرام لما دكتورك يكون مستني منك حاجة: قراءة، صورة، أو"
            " إجابة. في ساعات الهدوء التذكيرات العادية بتستنى، إلا في المواعيد اللي "
            "إنت وافقت عليها؛ الردود على رسايلك وأي حاجة مستعجلة مبتستناش. لو وقفت "
            "التذكيرات، الرسايل العادية بتقف، ودكتورك بيشوف إنها واقفة، وأي رسالة "
            "مستعجلة مبتتعطلش بسبب الوقفة دي."
        ),
    },
}
CATALOG = {"patient_browser." + key: value for key, value in CATALOG.items()}
validate_catalog("patient_browser", CATALOG)
