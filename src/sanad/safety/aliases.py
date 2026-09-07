"""Explicit receipt-value aliases shared by evidence and the pure alert boundary."""

import re
import unicodedata

from sanad.safety.labs import rule_for

ANALYTE_ALIASES = {
    "بوتاسيوم": "Potassium",
    "البوتاسيوم": "Potassium",
    "كي": "Potassium",
    "كرياتينين": "Creatinine",
    "الكرياتينين": "Creatinine",
    "كرياتنين": "Creatinine",
    "صوديوم": "Sodium",
    "الصوديوم": "Sodium",
    "سكر": "Glucose",
    "السكر": "Glucose",
    "جلوكوز": "Glucose",
    "بيكربونات": "Bicarbonate",
    "هيموجلوبين": "Hemoglobin",
    "صفايح": "Platelets",
    "صفائح": "Platelets",
}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"[\u064b-\u065f\u0670ـ]", "", text)
    text = text.translate(str.maketrans("أإآى", "اااي"))
    return " ".join(re.sub(r"[^\w%]+", " ", text).split())


def analyte(name: str) -> str:
    name = ANALYTE_ALIASES.get(normalize(name), name)
    options = (name, re.sub(r"\s*\([^()]*\)\s*$", "", name), *re.findall(r"\(([^()]*)\)", name))
    rule = next((r for option in options if (r := rule_for(option))), None)
    return rule.analyte if rule else normalize(name)
