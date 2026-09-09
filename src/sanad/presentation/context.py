"""Resolve once at a render boundary; nested renderers receive the same value."""

from dataclasses import dataclass

from sanad.domain.language import Audience, Language, effective


@dataclass(frozen=True, slots=True)
class PresentationContext:
    locale: Language
    audience: Audience

    def __post_init__(self) -> None:
        if self.locale not in {"ar", "en"} or self.audience not in {"doctor", "patient"}:
            raise ValueError("presentation_context")


def resolve(language: str | PresentationContext, audience: Audience) -> PresentationContext:
    if isinstance(language, PresentationContext):
        if language.audience != audience:
            raise ValueError("presentation_audience")
        return language
    return PresentationContext(effective(language, audience=audience), audience)
