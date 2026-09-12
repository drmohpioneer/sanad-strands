"""Code owns offers and disclosure; every composed response uses the existing gates."""

from sanad.agents.hygiene import patient_failure
from sanad.concierge.answer import Bundle, ConciergeAnswer, gate
from sanad.domain.entities import BarrierAttempt, BarrierPlace
from sanad.domain.language import effective
from sanad.presentation.context import PresentationContext
from sanad.resolver.policy import POLICY
from sanad.safety.models import OutputContext
from sanad.safety.policy import SafetyPolicy

OWNER_REVIEW_PENDING = True
TEMPLATES = {
    "area": ("تحب أدور في أنهي منطقة أو حي؟", "Which area or neighbourhood should I search near?"),
    "detail": (
        "إيه الصعوبة العملية اللي محتاج مساعدة فيها؟",
        "What practical difficulty do you need help with?",
    ),
    "disclosure": (
        "الأماكن دي قريبة من المنطقة اللي قلتها. مش شايف أسعارهم أو المتوفر عندهم، "
        "ومش متأكد إن عندهم المطلوب، ومحجزتش حاجة.",
        "These places are near the area you named. I cannot see their prices or "
        "stock, cannot confirm they have what you need, and have not booked anything.",
    ),
    "unavailable": (
        "ملقيتش خيارات أقدر أتحقق منها. ممكن تسأل العيادة عن مساعدة عملية لتنفيذ "
        "طلب الدكتور. العائق لسه مسجل.",
        "I could not verify any options. You can ask the clinic about practical "
        "help with your doctor's request. The barrier remains recorded.",
    ),
    "unresolved": (
        "العائق لسه مسجل مع اللي جربناه، ومطلوب الدكتور لسه ما اكتملش.",
        "The barrier and what we tried remain recorded; your doctor's request is still unfinished.",
    ),
    "handed": (
        "سجلت الصعوبة واللي جربناه لمراجعة الدكتور. المطلوب لسه ما اكتملش.",
        "The difficulty and what we tried are recorded for your doctor's review. "
        "The request is still unfinished.",
    ),
    "resolved": (
        "سجلت إنك بتقول إن العائق اتحل. ده مش بلاغ إتمام طلب الدكتور.",
        "Your report that the barrier is resolved is recorded. This does not report "
        "completing your doctor's request.",
    ),
    "place": ("{name}: {distance} م{details}", "{name}: {distance} m{details}"),
}


def render(key: str, language: str | PresentationContext, **fields: str) -> str:
    from sanad.presentation import resolver
    from sanad.presentation.catalog import opaque_fields
    from sanad.presentation.catalog import render as catalog_render
    from sanad.presentation.context import resolve

    context = resolve(language, "patient")
    catalog_key = "resolver." + key
    wanted = resolver.FIELDS[catalog_key]
    return catalog_render(
        resolver.CATALOG, catalog_key, context, **opaque_fields({k: fields[k] for k in wanted})
    )


def question_ok(text: str, fact: str, language: str, safety: SafetyPolicy) -> bool:
    language = effective(language, audience="patient")
    permitted = render(fact, language)
    context = OutputContext(language=language, mode="plan_explanation")
    bundle = Bundle("", context, (permitted,), (), (), (), True)
    return (
        gate(ConciergeAnswer(reply=text, kind="plan", needs_doctor=False), bundle, safety) is None
    )


def place_line(place: BarrierPlace, language: str) -> str:
    fields = [p for p in (place.address, place.opening_hours, place.phone) if p]
    return render(
        "place",
        language,
        name=place.name,
        distance=str(place.distance_m),
        details=("; " + "; ".join(fields)) if fields else "",
    )


def patient_reply(attempt: BarrierAttempt, language: str, safety: SafetyPolicy) -> str:
    language = effective(language, audience="patient")
    if attempt.outcome == "asked" and attempt.question:
        return attempt.question
    key = (
        "resolved"
        if attempt.state == "resolved"
        else "handed"
        if attempt.state == "handed_to_doctor"
        else "unavailable"
        if attempt.outcome in {"places_unavailable", "area_ambiguous"}
        else "unresolved"
    )
    text = render(key, language)
    if attempt.outcome == "places_offered" and attempt.places:
        text = (
            render("disclosure", language)
            + "\n"
            + "\n".join(place_line(p, language) for p in attempt.places[: POLICY.shown_results])
        )
    # Values are supplied by the adapter, never by the model or patient words.
    context = OutputContext(language=language, mode="plan_explanation", allowed_numbers=(text,))
    if patient_failure(text, context, safety):
        return render("unavailable", language)
    return text


def doctor_summary(attempt: BarrierAttempt, language: str) -> str:
    en = effective(language, audience="doctor") == "en"
    asked = any(s.outcome == "asked" for s in attempt.steps)
    parts = []
    if asked or attempt.answered:
        parts.append(
            ("asked and answered" if attempt.answered else "asked and unanswered")
            if en
            else ("سألنا واتجاوب" if attempt.answered else "سألنا ومفيش إجابة")
        )
    if attempt.places:
        parts.append("places offered" if en else "عرضنا أماكن")
    if any(s.outcome in {"places_unavailable", "area_ambiguous"} for s in attempt.steps):
        parts.append("places unavailable" if en else "الأماكن غير متاحة")
    states = {
        "resolved": ("اتحل حسب بلاغ المريض", "resolved by patient report"),
        "unresolved": ("لسه عائق", "unresolved"),
        "handed_to_doctor": ("لمراجعة الدكتور", "handed to doctor"),
    }
    parts.append(states[attempt.state][en])
    parts.append(
        ("budget spent" if en else "الميزانية المستخدمة")
        + f": {attempt.questions_spent}/1, {attempt.searches_spent}/2"
    )
    return "; ".join(parts)
