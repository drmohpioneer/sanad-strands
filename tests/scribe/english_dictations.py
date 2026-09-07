"""Handwritten synthetic English example from the released oracle, not an ASR transcript."""

from typing import Any

SOURCE = (
    "New patient Ahmed Saad, 53. Diabetic and hypertensive. Taking Exforge 5/160. "
    "Increase to Exforge HCT 10/160/25. ECG: T-wave inversion in lateral leads. "
    "Echo: ejection fraction 45%. Blood pressure chart, 3 times a day for 5 days. "
    "Request tests CBC, sodium, potassium and lipid profile. I can add Forxiga."
)

VALUE: dict[str, Any] = {
    "patient": {"name_as_spoken": "Ahmed Saad", "age": "53"},
    "orders": [
        {
            "action": "change",
            "drug": "Exforge HCT",
            "dose": "10/160/25",
            "previous_drug": "Exforge",
            "previous_dose": "5/160",
        },
        {"action": "start", "drug": "Forxiga"},
    ],
    "facts": [
        {
            "category": "condition",
            "text": "Diabetic and hypertensive",
            "clinical_en": "diabetes, hypertension",
        },
        {
            "category": "finding",
            "text": "ECG: T-wave inversion in lateral leads",
            "clinical_en": "T wave inversion in lateral leads",
        },
        {"category": "finding", "text": "Echo: ejection fraction 45%", "clinical_en": "EF 45%"},
    ],
    "missions": [
        {
            "kind": "TASK",
            "text": "Blood pressure chart, 3 times a day for 5 days",
            "clinical_en": "Blood pressure chart, 3 times a day for 5 days",
            "timing_expression": "for 5 days",
        },
        {"kind": "TEST", "text": "CBC, sodium, potassium and lipid profile"},
    ],
}

MEDICATIONS = ("Exforge 5/160 → Exforge HCT 10/160/25 (change)", "Forxiga (start)")
HISTORY = ("Dx: diabetes, hypertension", "ECG: T wave inversion, lateral leads", "Echo: EF 45%")
