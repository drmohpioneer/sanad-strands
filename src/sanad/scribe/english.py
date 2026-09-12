"""Pure English card assembly over the typed Scribe view."""

from sanad.domain.language import effective
from sanad.scribe.proposal import Proposal
from sanad.scribe.view import (
    REASONS as REASONS,
)
from sanad.scribe.view import (
    CardView,
    build_view,
)
from sanad.scribe.view import (
    date as date,
)
from sanad.scribe.view import (
    questions as questions,
)


def render_wording(template: str, language: str, **values: str) -> str:
    from sanad.channels.telegram import wording

    # This legacy reply id already uses the help surface; keep its English wording.
    if template == "scribe_help":
        if effective(language, audience="doctor") == "en":
            return "Use /new, /find, /qr, /cancel, /intake, /lang en or /lang ar."
        template = "doctor_help"
    return wording.render(template, language, **values)


def _lines(view: CardView, *, history_heading: bool = True) -> list[str]:
    lines = [view.heading.prefix + ", ".join(view.heading.parts)]
    lines.extend("• " + ", ".join(choice.parts) for choice in view.choices)
    for title, values in (("Medications:", view.medications), ("Requested:", view.requested)):
        if values:
            lines.extend((title, *values))
    if view.history:
        if history_heading:
            lines.append("History:")
        lines.extend(view.history)
        if view.history_hidden:
            lines.append(f"… ({view.history_hidden} more, edit to see)")
    if view.alerts is not None:
        lines.extend(("Notify me if:", *view.alerts))
    if view.questions:
        lines.extend(("Needs confirmation:", *view.questions))
    lines.extend((*view.notices, view.buttons, view.validity))
    return lines


def render_view(view: CardView) -> tuple[str, ...]:
    from sanad.scribe.card import split_card

    return split_card("\n".join(_lines(view)))


def render(proposal: Proposal) -> tuple[str, ...]:
    return render_view(build_view(proposal))
