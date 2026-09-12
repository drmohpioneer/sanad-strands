# ruff: noqa: E501 -- handwritten byte-for-byte card specification
"""Synthetic dictations and independently written, exact expected card text."""

from dataclasses import dataclass

from sanad.scribe.extract import DictationCandidate


@dataclass(frozen=True)
class DictationExample:
    input: str
    candidate: DictationCandidate
    card: str
    extraction_calls: int = 2


SYNTHETIC_TABLE = (
    DictationExample(
        "المريض أحمد رضا عنده ضغط مزمن، ابدأ بيزوبرولول 5 مج مرة الصبح، "
        "واطلب تحليل كرياتينين، وبلغني لو البوتاسيوم فوق 5.5.",
        DictationCandidate.model_validate(
            {
                "patient": {"name_as_spoken": "أحمد رضا"},
                "facts": [
                    {
                        "category": "condition",
                        "clinical_kind": "Dx",
                        "text": "ضغط مزمن",
                        "clinical_en": "chronic hypertension",
                    }
                ],
                "orders": [
                    {
                        "action": "start",
                        "drug": "بيزوبرولول",
                        "dose": "5 مج",
                        "frequency": "مرة الصبح",
                    }
                ],
                "missions": [
                    {"kind": "TEST", "text": "تحليل كرياتينين", "clinical_en": "creatinine"}
                ],
                "alerts": ["البوتاسيوم فوق 5.5"],
            }
        ),
        """مريض جديد: أحمد رضا
الأدوية:
Bisoprolol 5 mg, مرة الصبح (بداية)
المطلوب:
TEST: creatinine: الموعد: الأحد 20 سبتمبر، 10 الصبح (افتراضي 14 يوم)؛ لو متعملش هبلّغك: الأحد 20 سبتمبر، 10 الصبح
تأكيد بداية Bisoprolol: الأربعاء 9 سبتمبر، 10 الصبح؛ لو متأكدش هبلّغك في نفس الموعد.
متابعة اليوم الثالث من تاريخ البداية اللي المريض يبلّغنا بيه؛ التأكيد هنا مش دليل إنه بدأ.
التاريخ المرضي:
Dx: hypertension, مزمن
بلّغني لو:
البوتاسيوم فوق 5.5
محتاج تأكيد:
الأسماء اللي بالعربي اتكتبت زي ما سمعتها؛ لو عايز تكتبها بالإنجليزي عدّلها
✅ تمام | ✏️ تعديل | ❌ إلغاء
صالح 30 دقيقة""",
    ),
    DictationExample(
        "مريض جديد منى حسن، عندها حساسية بنسلين، اعملي تحليل سكر بعد 4 ساعات.",
        DictationCandidate.model_validate(
            {
                "patient": {"name_as_spoken": "منى حسن"},
                "facts": [{"category": "allergy", "text": "بنسلين", "clinical_en": "penicillin"}],
                "missions": [
                    {"kind": "TEST", "text": "تحليل سكر", "timing_expression": "بعد 4 ساعات"}
                ],
            }
        ),
        """مريض جديد: منى حسن
المطلوب:
TEST: glucose: الموعد: الأحد 6 سبتمبر، 7 بالليل (صريح: بعد 4 ساعات)؛ لو متعملش هبلّغك: الأحد 6 سبتمبر، 7 بالليل
التاريخ المرضي:
History: بنسلين
محتاج تأكيد:
الأسماء اللي بالعربي اتكتبت زي ما سمعتها؛ لو عايز تكتبها بالإنجليزي عدّلها
✅ تمام | ✏️ تعديل | ❌ إلغاء
صالح 30 دقيقة""",
    ),
    DictationExample(
        "المريض علي سالم، وقف إيبوبروفين، واطلب التحاليل القديمة.",
        DictationCandidate.model_validate(
            {
                "patient": {"name_as_spoken": "علي سالم"},
                "orders": [{"action": "stop", "drug": "إيبوبروفين"}],
                "missions": [
                    {
                        "kind": "SEND_RECORDS",
                        "text": "التحاليل القديمة",
                        "clinical_en": "previous test results",
                    }
                ],
            }
        ),
        """مريض جديد: علي سالم
الأدوية:
Ibuprofen (إيقاف)
Ibuprofen: doctor instructed, not on file before
المطلوب:
SEND_RECORDS: التحاليل القديمة: الموعد: الأربعاء 9 سبتمبر، 10 الصبح (افتراضي 3 يوم)؛ لو متعملش هبلّغك: الأربعاء 9 سبتمبر، 10 الصبح
التاريخ المرضي:
أدوية قديمة: History: Ibuprofen: doctor instructed stop; no prior order on file; dose unknown
✅ تمام | ✏️ تعديل | ❌ إلغاء
صالح 30 دقيقة""",
    ),
    DictationExample(
        "المريض سامح، ابدأ أملوديبين من غير ما أقول الجرعة.",
        DictationCandidate.model_validate(
            {
                "patient": {"name_as_spoken": "سامح"},
                "orders": [{"action": "start", "drug": "أملوديبين"}],
            }
        ),
        """مريض جديد: سامح
الأدوية:
Amlodipine (بداية)
محتاج تأكيد:
جرعة "Amlodipine" إيه؟
✅ تمام | ✏️ تعديل | ❌ إلغاء
صالح 30 دقيقة""",
    ),
    DictationExample(
        "المريض عمر، ابدأ أملوديبين خمسة مليجرام.",
        DictationCandidate.model_validate(
            {
                "patient": {"name_as_spoken": "عمر"},
                "orders": [{"action": "start", "drug": "أملوديبين", "dose": "خمسة مليجرام"}],
            }
        ),
        """مريض جديد: عمر
الأدوية:
Amlodipine خمسة mg (بداية)
محتاج تأكيد:
جرعة "Amlodipine" إيه؟
✅ تمام | ✏️ تعديل | ❌ إلغاء
صالح 30 دقيقة""",
    ),
    DictationExample(
        "المريض وليد، ابدأ أب 5 مج.",
        DictationCandidate.model_validate(
            {
                "patient": {"name_as_spoken": "وليد"},
                "orders": [{"action": "start", "drug": "أب", "dose": "5 مج"}],
            }
        ),
        """مريض جديد: وليد
الأدوية:
أب 5 mg (بداية)
محتاج تأكيد:
الأسماء اللي بالعربي اتكتبت زي ما سمعتها؛ لو عايز تكتبها بالإنجليزي عدّلها
✅ تمام | ✏️ تعديل | ❌ إلغاء
صالح 30 دقيقة""",
    ),
    DictationExample(
        "المريض ياسر، عنده تاريخ عملية زائدة.",
        DictationCandidate.model_validate(
            {
                "patient": {"name_as_spoken": "ياسر"},
                "facts": [
                    {"category": "history", "text": "عملية زائدة", "clinical_en": "appendectomy"}
                ],
            }
        ),
        """مريض جديد: ياسر
التاريخ المرضي:
History: عملية زائدة
محتاج تأكيد:
الأسماء اللي بالعربي اتكتبت زي ما سمعتها؛ لو عايز تكتبها بالإنجليزي عدّلها
✅ تمام | ✏️ تعديل | ❌ إلغاء
صالح 30 دقيقة""",
    ),
    DictationExample(
        "المريض هاني كان بياخد أسبرين زمان، سجله في الأدوية القديمة.",
        DictationCandidate.model_validate(
            {
                "patient": {"name_as_spoken": "هاني"},
                "facts": [
                    {
                        "category": "medication_history",
                        "text": "أسبرين زمان",
                        "clinical_en": "past aspirin use",
                    }
                ],
            }
        ),
        """مريض جديد: هاني
التاريخ المرضي:
History: أسبرين زمان
محتاج تأكيد:
الأسماء اللي بالعربي اتكتبت زي ما سمعتها؛ لو عايز تكتبها بالإنجليزي عدّلها
✅ تمام | ✏️ تعديل | ❌ إلغاء
صالح 30 دقيقة""",
    ),
    DictationExample(
        "المريض حسن، عايزه يحضر زيارة متابعة.",
        DictationCandidate.model_validate(
            {
                "patient": {"name_as_spoken": "حسن"},
                "missions": [
                    {"kind": "VISIT", "text": "حضور زيارة متابعة", "clinical_en": "follow up visit"}
                ],
            }
        ),
        """مريض جديد: حسن
المطلوب:
VISIT: حضور زيارة متابعة: الموعد: الثلاثاء 6 أكتوبر، 10 الصبح (افتراضي 30 يوم)؛ لو متعملش هبلّغك: الثلاثاء 6 أكتوبر، 10 الصبح
✅ تمام | ✏️ تعديل | ❌ إلغاء
صالح 30 دقيقة""",
    ),
    DictationExample(
        "المريض باسم، عايزه يبلغنا إنه اتصل بالعيادة.",
        DictationCandidate.model_validate(
            {
                "patient": {"name_as_spoken": "باسم"},
                "missions": [
                    {
                        "kind": "TASK",
                        "text": "الإبلاغ عن الاتصال بالعيادة",
                        "clinical_en": "report contacting the clinic",
                    }
                ],
            }
        ),
        """مريض جديد: باسم
المطلوب:
TASK: الإبلاغ عن الاتصال بالعيادة: الموعد: الأحد 13 سبتمبر، 10 الصبح (افتراضي 7 يوم)؛ لو متعملش هبلّغك: الأحد 13 سبتمبر، 10 الصبح
✅ تمام | ✏️ تعديل | ❌ إلغاء
صالح 30 دقيقة""",
    ),
    DictationExample(
        "المريضة سارة، بلغني لو مش عارفة تعمل التحليل.",
        DictationCandidate.model_validate(
            {
                "patient": {"name_as_spoken": "سارة"},
                "alerts": ["مش عارفة تعمل التحليل"],
            }
        ),
        """مريض جديد: سارة
بلّغني لو:
مش عارفة تعمل التحليل
محتاج تأكيد:
سمعت إنك طلبت تحليل/فحص بس مش لاقيه في الكارت؛ قول لي إيه هو
✅ تمام | ✏️ تعديل | ❌ إلغاء
صالح 30 دقيقة""",
        extraction_calls=3,
    ),
    DictationExample(
        "أحمد ومنى، غير العلاج بتاعه واعمل التحليل ليها.",
        DictationCandidate.model_validate({"ambiguities": ["مريضين"]}),
        """المريض: مين المريض؟
محتاج تأكيد:
سمعت إنك طلبت تحليل/فحص بس مش لاقيه في الكارت؛ قول لي إيه هو
سمعت "مريضين"، توضح المقصود؟
✅ تمام | ✏️ تعديل | ❌ إلغاء
صالح 30 دقيقة""",
        extraction_calls=3,
    ),
)

# Exact inputs recovered from the architect's 08 measurement, 2026-09-06
# 21:39:49Z and repeated 21:42:15Z. They deliberately do not name a patient.
# Expected candidates/cards below are handwritten, not captured provider output.
MEASURED = (
    DictationExample(
        "ابدأ بيزوبرولول 5 مليجرام مرة واحدة الصبح، وأتورفاستاتين 40 مليجرام بالليل. "
        "لو البوتاسيوم عدى 5.5 قولي فوراً.",
        DictationCandidate.model_validate(
            {
                "orders": [
                    {
                        "action": "start",
                        "drug": "بيزوبرولول",
                        "dose": "5 مليجرام",
                        "frequency": "مرة واحدة الصبح",
                    },
                    {
                        "action": "start",
                        "drug": "أتورفاستاتين",
                        "dose": "40 مليجرام",
                        "timing": "بالليل",
                    },
                ],
                "alerts": ["البوتاسيوم عدى 5.5"],
            }
        ),
        """المريض: مين المريض؟
الأدوية:
Bisoprolol 5 mg, مرة واحدة الصبح (بداية)
Atorvastatin 40 mg, بالليل (بداية)
المطلوب:
تأكيد بداية Bisoprolol: الأربعاء 9 سبتمبر، 10 الصبح؛ لو متأكدش هبلّغك في نفس الموعد.
تأكيد بداية Atorvastatin: الأربعاء 9 سبتمبر، 10 الصبح؛ لو متأكدش هبلّغك في نفس الموعد.
متابعة اليوم الثالث من تاريخ البداية اللي المريض يبلّغنا بيه؛ التأكيد هنا مش دليل إنه بدأ.
بلّغني لو:
البوتاسيوم عدى 5.5
✅ تمام | ✏️ تعديل | ❌ إلغاء
صالح 30 دقيقة""",
    ),
    DictationExample(
        "زود السبيرونولاكتون لـ 25 مليجرام، وخلي الأبيكسابان زي ما هو 5 مليجرام مرتين، "
        "وتحليل كرياتينين بعد أسبوعين.",
        DictationCandidate.model_validate(
            {
                "orders": [
                    {"action": "change", "drug": "السبيرونولاكتون", "dose": "25 مليجرام"},
                    {
                        "action": "continue",
                        "drug": "الأبيكسابان",
                        "dose": "5 مليجرام",
                        "frequency": "مرتين",
                    },
                ],
                "missions": [
                    {
                        "kind": "TEST",
                        "text": "تحليل كرياتينين",
                        "clinical_en": "creatinine",
                        "timing_expression": "بعد أسبوعين",
                    }
                ],
            }
        ),
        """المريض: مين المريض؟
الأدوية:
Spironolactone 25 mg (تغيير)
Apixaban 5 mg, مرتين
المطلوب:
TEST: creatinine: الموعد: الأحد 20 سبتمبر، 3 العصر (صريح: بعد أسبوعين)؛ لو متعملش هبلّغك: الأحد 20 سبتمبر، 3 العصر
✅ تمام | ✏️ تعديل | ❌ إلغاء
صالح 30 دقيقة""",
    ),
    DictationExample(
        "وقف الميتفورمين، وابدأ إمباجليفلوزين 10 مليجرام الصبح، والضغط لو نزل تحت 90 يتصل بيا.",
        DictationCandidate.model_validate(
            {
                "orders": [
                    {"action": "stop", "drug": "الميتفورمين"},
                    {
                        "action": "start",
                        "drug": "إمباجليفلوزين",
                        "dose": "10 مليجرام",
                        "timing": "الصبح",
                    },
                ],
                "alerts": ["الضغط نزل تحت 90"],
            }
        ),
        """المريض: مين المريض؟
الأدوية:
Metformin (إيقاف)
Empagliflozin 10 mg, الصبح (بداية)
المطلوب:
تأكيد بداية Empagliflozin: الأربعاء 9 سبتمبر، 10 الصبح؛ لو متأكدش هبلّغك في نفس الموعد.
متابعة اليوم الثالث من تاريخ البداية اللي المريض يبلّغنا بيه؛ التأكيد هنا مش دليل إنه بدأ.
بلّغني لو:
الضغط نزل تحت 90
✅ تمام | ✏️ تعديل | ❌ إلغاء
صالح 30 دقيقة""",
    ),
)

TABLE: tuple[DictationExample, ...] = MEASURED + SYNTHETIC_TABLE

# Privacy-preserving synthetic restaging of 11b's owner phone test. The complete
# private transcript and media remain under lane/spikes/; this uses a new name
# and only the contract's clinical fields, with independently written card text.
OWNER_SYNTHETIC = DictationExample(
    "المريض سامي تجربة 53 سنة ذكر عنده ضغط وسكر. ECG: T wave inversion lateral. "
    "Echo: EF 45%, segmental hypokinesia inferoposterolateral. "
    "ماشي على إكس فورش إتش سي تي 560 12.5 وكونكور 5 مج مرة يوميا. "
    "زودته فورسيجا وطلبت BUN, creatinine, Na, K.",
    DictationCandidate.model_validate(
        {
            "patient": {"name_as_spoken": "سامي تجربة", "age": "53", "sex": "male"},
            "orders": [
                {"action": "continue", "drug": "إكس فورش إتش سي تي", "dose": "560 12.5"},
                {"action": "continue", "drug": "كونكور", "dose": "5 مج", "frequency": "مرة يوميا"},
                {"action": "start", "drug": "فورسيجا"},
            ],
            "missions": [{"kind": "TEST", "text": "BUN, creatinine, Na, K"}],
            "facts": [
                {
                    "category": "condition",
                    "clinical_kind": "Dx",
                    "text": "ضغط وسكر",
                    "clinical_en": "hypertension, diabetes",
                    "terms": [{"spoken": "ضغط وسكر", "english": "hypertension, diabetes"}],
                },
                {
                    "category": "finding",
                    "clinical_kind": "ECG",
                    "text": "T wave inversion lateral",
                    "terms": [
                        {
                            "spoken": "T wave inversion lateral",
                            "english": "T wave inversion lateral",
                        }
                    ],
                    "clinical_en": "T wave inversion lateral",
                },
                {
                    "category": "finding",
                    "clinical_kind": "Echo",
                    "text": "EF 45%, segmental hypokinesia inferoposterolateral",
                    "terms": [
                        {
                            "spoken": "EF 45%, segmental hypokinesia inferoposterolateral",
                            "english": "EF 45%, segmental hypokinesia inferoposterolateral",
                        }
                    ],
                    "clinical_en": "EF 45%, segmental hypokinesia inferoposterolateral",
                },
            ],
        }
    ),
    """مريض جديد: سامي تجربة، 53 سنة، ذكر
الأدوية:
Exforge HCT 560 12.5
Concor 5 mg, مرة يوميا
Forxiga (بداية)
المطلوب:
TEST: BUN, creatinine, Na, K: الموعد: الأحد 20 سبتمبر، 10 الصبح (افتراضي 14 يوم)؛ لو متعملش هبلّغك: الأحد 20 سبتمبر، 10 الصبح
التاريخ المرضي:
Dx: hypertension, diabetes
ECG: T wave inversion, lateral
Echo: EF 45%, segmental hypokinesia inferoposterolateral
محتاج تأكيد:
سمعت 45، ده الـ EF؟
سمعت "560 12.5" لـ Exforge HCT، قصدك 5/160/12.5؟
جرعة "Forxiga" إيه؟
✅ تمام | ✏️ تعديل | ❌ إلغاء
صالح 30 دقيقة""",
)
TABLE = (*TABLE, OWNER_SYNTHETIC)

# The owner identified this note as synthetic and explicitly released this
# exact seventeenth card. The text below is handwritten from contract 11e.
OWNER_TRANSCRIPT = DictationExample(
    "مريض جديد أحمد سعد عنده 53 سنة ضغطه سكر ECG في تي أوف انفرجين في اللاترال "
    "الأكو فانكشن 45% سيجمنتال انفروبوسترو لاترال جايب أنجينا ماشي على "
    "إكس فورش إتش سي تي 560 12.5 وكونكور 5 زودته فورسيجا هنتابع موضوع الفورسيجا "
    "وطلبت منه بانو كريات وسوديوم وبوتاسيوم يعملوه",
    DictationCandidate.model_validate(
        {
            "patient": {"name_as_spoken": "أحمد سعد", "age": "53"},
            "orders": [
                {"action": "continue", "drug": "إكس فورش إتش سي تي", "dose": "560 12.5"},
                {"action": "continue", "drug": "كونكور", "dose": "5"},
                {"action": "start", "drug": "فورسيجا"},
            ],
            "missions": [{"kind": "TEST", "text": "بانو كريات وسوديوم وبوتاسيوم"}],
            "facts": [
                {"category": "condition", "text": "ضغطه سكر"},
                {"category": "finding", "text": "ECG في تي أوف انفرجين في اللاترال"},
                {"category": "finding", "text": "الأكو فانكشن 45% سيجمنتال انفروبوسترو لاترال"},
                {"category": "complaint", "text": "أنجينا"},
            ],
        }
    ),
    """مريض جديد: أحمد سعد، 53 سنة
الأدوية:
Exforge HCT 560 12.5
Concor 5
Forxiga (بداية)
المطلوب:
TEST: BUN, creatinine, Na, K: الموعد: الأحد 20 سبتمبر، 10 الصبح (افتراضي 14 يوم)؛ لو متعملش هبلّغك: الأحد 20 سبتمبر، 10 الصبح
التاريخ المرضي:
Dx: hypertension, diabetes
ECG: T wave inversion, lateral
Echo: EF 45%
Complaint: angina
محتاج تأكيد:
سمعت 45، ده الـ EF؟
سمعت "560 12.5" لـ Exforge HCT، قصدك 5/160/12.5؟
جرعة "Forxiga" إيه؟
سمعت "سيجمنتال انفروبوسترو لاترال"؛ وضّح العبارة الطبية.
✅ تمام | ✏️ تعديل | ❌ إلغاء
صالح 30 دقيقة""",
)
TABLE = (*TABLE, OWNER_TRANSCRIPT)
