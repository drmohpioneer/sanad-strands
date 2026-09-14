"""Released 11f wording versus the literal pre-implementation Arabic catalog."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from sanad.channels.telegram import wording
from sanad.scribe import card, crosscheck, photos
from sanad.scribe.extract import LabRowCandidate

BEFORE: dict[str, Any] = json.loads(Path(__file__).with_name("wording_11f_ar.json").read_text())
DOCTOR_IDS = tuple(wording.TEMPLATES | wording.SCRIBE_TEMPLATES)
PHOTO_FUNCTIONS = (
    crosscheck.shift_warning,
    crosscheck.photo_supported_scope,
    crosscheck.handwriting_reply,
    crosscheck.single_reader_warning,
    crosscheck.agreement_warning,
    crosscheck.column_caption,
)


def no_arabic(text: str) -> None:
    assert re.search(r"[\u0600-\u06ff]", text) is None, text


@pytest.mark.parametrize("key", DOCTOR_IDS)
def test_every_doctor_template_has_real_english_and_literal_arabic(key: str) -> None:
    values = {field: "Synthetic" for field in wording.FIELDS[key]}
    pair = wording.ALL_TEMPLATES[key]
    assert isinstance(pair, tuple) and len(pair) == 2 and all(pair)
    original = (
        BEFORE["TEMPLATES"]
        | BEFORE["SCRIBE_TEMPLATES"]
        | {"scribe_brand_change_line": "{old_drug} {old} ← {new_drug} {new}"}
        | {
            "dashboard_signed_out": "تم تسجيل الخروج من لوحة المتابعة.",
            "account_suspended": "حسابك موقوف. تواصل مع الإدارة.",
            "patient_not_linked": "لسه مش مرتبط بدكتور. افتح رابط الدعوة اللي بعته الدكتور.",
            "login_refused": "تعذر الدخول. تواصل مع الإدارة.",
            "monitor_schedule_changed": "مواعيد القياس اتغيرت. ابعت التعليمات من جديد.",
            "doctor_document_too_many_pages": (
                "ابعت لحد ١٠ صفحات لو سمحت، الصفحات اللي فيها النتائج بس."
            ),
            "doctor_document_too_large": (
                "حجم الملف كبير. ابعت ملف أصغر أو صور لصفحات النتائج لو سمحت."
            ),
            "doctor_document_encrypted": (
                "الملف محمي بكلمة سر. ابعته من غير كلمة سر أو على شكل صور لو سمحت."
            ),
            "doctor_document_invalid": ("مش قادرين نفتح الملف. ابعته تاني أو على شكل صور لو سمحت."),
            "doctor_document_unreadable": (
                "مش قادرين نقرا الملف. ابعت صور لصفحات النتائج لو سمحت."
            ),
            "doctor_document_blank": ("الملف باين فاضي. ابعت الصفحات اللي فيها النتائج لو سمحت."),
            "doctor_document_too_detailed": (
                "المستند فيه نتائج كتير مش هنعرف نعرضها كلها. راجع الصفحات لو سمحت."
            ),
            "doctor_document_word_unsupported": (
                "لسه مش بنقرا ملفات Word. ابعته PDF أو صور لصفحات النتائج لو سمحت."
            ),
        }
    )[key]
    # Addendum 6a supersedes bilingual account entries and long-dash prose.
    original = original.replace(" — ", ": ").replace("–", ", ")
    if key in wording.TEMPLATES:
        original = re.split(r"\n(?=[A-Z])", original)[0]
    if key == "doctor_welcome_back":
        original = (
            "أهلًا برجوعك. اكتب أو سجّل اللي عايز تعمله للمريض بكلامك، "
            "أو ابعت صورة الروشتة. اكتب /help لعرض الأوامر."
        )
    if key == "doctor_approved":
        original += " {name}"
    if key == "scribe_invitation":
        original += "\nأي /qr جديد بيلغي اللينك ده."
    assert pair[0].encode() == original.encode()
    assert wording.render(key, "ar", **values) == original.format(**values)
    no_arabic(wording.render(key, "en", **values))


def test_every_photo_function_in_both_languages_and_fixed_card_labels() -> None:
    for function in PHOTO_FUNCTIONS:
        no_arabic(function("en"))
        assert (
            function("ar").encode() == BEFORE["PHOTO_WORDING"][function.__name__.upper()].encode()
        )
    for catalog in (
        crosscheck.PHOTO_WORDING,
        card.PHOTO_LABELS,
        card.PHOTO_FACTS,
        card.PHOTO_ACTIONS,
        card.PHOTO_REASONS,
        wording.BUTTONS,
        wording.LABELS,
    ):
        for key, pair in catalog.items():
            assert isinstance(pair, tuple) and len(pair) == 2 and all(pair), key
            no_arabic(pair[1])
    assert "Arabic instruction column" in crosscheck.column_caption("en")
    assert "photographed as it is" in crosscheck.column_caption("en")


@pytest.mark.parametrize("reason", tuple(BEFORE["PHOTO_REASONS"]))
def test_photo_failure_reason_pairs_preserve_arabic(reason: str) -> None:
    pair = photos._REASONS[reason]
    assert pair[0].encode() == BEFORE["PHOTO_REASONS"][reason].encode()
    no_arabic(pair[1])
    no_arabic(photos.unreadable(reason, "en"))
    expected = (
        BEFORE["PHOTO_WORDING"]["HANDWRITING_REPLY"]
        if reason == "unreadable"
        else BEFORE["SCRIBE_TEMPLATES"]["doctor_photo_unreadable"].format(reason=pair[0])
    )
    assert photos.unreadable(reason, "ar") == expected


@pytest.mark.parametrize(
    "reason",
    [
        "invalid_document_json",
        "template_echo",
        "readers_failed",
        "timeout",
        "unavailable",
        "budget_exhausted",
        "unknown_failure",
    ],
)
def test_failed_and_unknown_photo_reading_never_exposes_a_code(reason: str) -> None:
    no_arabic(photos.unreadable(reason, "en"))
    assert reason not in photos.unreadable(reason, "en")
    original = BEFORE["PHOTO_WORDING"]["HANDWRITING_REPLY"]
    if reason == "unknown_failure":
        original = BEFORE["SCRIBE_TEMPLATES"]["doctor_photo_unreadable"].format(
            reason="الصورة ماوصلتش أو القراءة ماكملتش"
        )
    assert photos.unreadable(reason, "ar") == original


def test_lab_unit_and_printed_flag_are_wording_but_arabic_data_is_preserved() -> None:
    row = LabRowCandidate(analyte="Potassium", value="4.1", flag="H")
    assert crosscheck.lab_text(row, "ar") == "Potassium 4.1 بدون وحدة؛ علامة مطبوعة: H"
    assert crosscheck.lab_text(row, "en") == "Potassium 4.1 no unit; printed flag: H"
    arabic_row = row.model_copy(update={"analyte": "اسم من الورقة", "flag": "علامة"})
    assert crosscheck.lab_text(arabic_row, "en") == "اسم من الورقة 4.1 no unit; printed flag: علامة"


def test_enrollment_sentences_are_split_without_losing_either_language() -> None:
    for key, original in BEFORE["ENROLLMENT_TEMPLATES"].items():
        fields = {field: "Synthetic" for field in wording.FIELDS[key]}
        ar, en = re.split(r"\n(?=[A-Z])", original, maxsplit=1)
        if "{link}" in en:
            ar += "\n{link}"
        if key in {"doctor_login_link", "patient_login_link"}:
            ar = (
                "لينك دخولك لسند صالح لعشر دقايق ولمرة واحدة. انسخه والصقه في المتصفح، "
                "ومتدوسش عليه جوه تيليجرام.\n{link}"
            )
            en = (
                "Your one-time sign-in link, valid for ten minutes. Copy it and paste it "
                "into your browser. Do not tap it inside Telegram.\n{link}"
            )
        for language, expected in (("ar", ar), ("en", en)):
            expected = expected.replace("–", ", ").replace(" — ", ": ")
            assert wording.render(key, language, **fields) == expected.format(**fields)


@pytest.mark.parametrize(
    "bad",
    [
        "one language",
        ("Arabic",),
        ("Arabic", ""),
        ("Arabic", None),
        ("Arabic", "English", "extra"),
        ("{body}", "no body"),
        ("{body}", "{unknown}"),
        ("{body}", "{body!r}"),
        ("{body}", "{body:>20}"),
    ],
)
def test_import_checker_rejects_missing_halves_and_unsafe_or_mismatched_fields(
    monkeypatch: pytest.MonkeyPatch, bad: Any
) -> None:
    monkeypatch.setitem(wording.ALL_TEMPLATES, "scribe_card", bad)
    with pytest.raises(ValueError):
        wording.check_templates()


def test_checker_preserves_literal_braces(monkeypatch: pytest.MonkeyPatch) -> None:
    from sanad.presentation.doctor import CATALOG

    monkeypatch.setitem(
        wording.ALL_TEMPLATES, "scribe_stale", ("نص {{حرفي}}", "Literal {{braces}}")
    )
    wording.check_templates()
    monkeypatch.setitem(
        CATALOG, "doctor.scribe_stale", {"ar": "نص {{حرفي}}", "en": "Literal {{braces}}"}
    )
    assert wording.render("scribe_stale", "ar") == "نص {حرفي}"
    assert wording.render("scribe_stale", "en") == "Literal {braces}"


def test_no_arabic_oracle_catches_an_accidentally_copied_translation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sanad.presentation.doctor import CATALOG

    arabic = wording.SCRIBE_TEMPLATES["scribe_expired"][0]
    monkeypatch.setitem(wording.ALL_TEMPLATES, "scribe_expired", (arabic, arabic))
    monkeypatch.setitem(CATALOG, "doctor.scribe_expired", {"ar": arabic, "en": arabic})
    with pytest.raises(AssertionError):
        no_arabic(wording.render("scribe_expired", "en"))


@pytest.mark.parametrize("language", ["ar", "en"])
def test_escape_rules_and_arabic_patient_name_are_not_translation(language: str) -> None:
    text = wording.render(
        "admin_new_application",
        language,
        name="أحمد <b>\n\u202e{role}",
        specialty="x" * 200,
        city="Synthetic",
    )
    assert "أحمد" in text and "&lt;b&gt;" in text and "&#123;role&#125;" in text
    assert "\u202e" not in text and "<b>" not in text and "x" * 161 not in text
    assert wording.render("scribe_card", language, body="first\nsecond") == "first\nsecond"
    with pytest.raises(ValueError):
        wording.render("scribe_card", language)
    with pytest.raises(ValueError):
        wording.render("scribe_card", language, body="text", extra="field")
