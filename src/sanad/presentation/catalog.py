"""Plain catalogs and opaque interpolation, with no locale discovery or translation."""

from dataclasses import dataclass
from string import Formatter

from sanad.domain.language import Language
from sanad.presentation.context import PresentationContext

type Catalog = dict[str, dict[Language, str]]


@dataclass(frozen=True, slots=True)
class OpaqueValue:
    """Text after the caller's accepted sanitisation, never a format program."""

    text: str

    def __post_init__(self) -> None:
        if type(self.text) is not str:
            raise ValueError("opaque_value_requires_text")


def placeholders(template: str) -> frozenset[str]:
    names = set()
    for _, name, spec, conversion in Formatter().parse(template):
        if name is not None:
            if not name.isidentifier() or spec or conversion:
                raise ValueError("catalog_unsafe_placeholder")
            names.add(name)
    return frozenset(names)


def validate_catalog(namespace: str, catalog: Catalog) -> dict[str, frozenset[str]]:
    """Called by every catalog module at import, before it can render anything."""
    fields = {}
    for key, locales in catalog.items():
        if not key.startswith(namespace + ".") or not key.removeprefix(namespace + "."):
            raise ValueError("catalog_namespace")
        if set(locales) != {"ar", "en"}:
            raise ValueError("catalog_locales")
        if any(not isinstance(text, str) or not text for text in locales.values()):
            raise ValueError("catalog_text")
        if placeholders(locales["ar"]) != placeholders(locales["en"]):
            raise ValueError("catalog_placeholder_mismatch")
        fields[key] = placeholders(locales["ar"])
    return fields


def render(catalog: Catalog, key: str, context: PresentationContext, **fields: OpaqueValue) -> str:
    template = catalog[key][context.locale]
    # Recheck format instructions even if a caller mutates the ordinary dict.
    if placeholders(template) != fields.keys():
        raise ValueError("catalog_fields")
    text = template.format(**{name: value.text for name, value in fields.items()})
    if any(value.text not in text for value in fields.values()):
        raise ValueError("clinical_placeholder_altered")
    return text


def opaque_fields(fields: dict[str, str]) -> dict[str, OpaqueValue]:
    return {name: OpaqueValue(value) for name, value in fields.items()}
