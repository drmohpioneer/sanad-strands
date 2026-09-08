"""The sole spoken-name resolver; no IO or clinical authority in matching."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import TYPE_CHECKING, Literal

from sanad.media.numbers import numbers_in
from sanad.scribe.names import NameEntry, Resolution, dictionary, normalize

if TYPE_CHECKING:
    from sanad.scribe.extract import DrugMention
    from sanad.scribe.lookup import DrugLookup, DrugLookupService
    from sanad.scribe.memory import NameVocabulary
    from sanad.scribe.proposal import Proposal
    from sanad.store.protocol import Store
    from sanad.store.records import Doctor, NameMemory

_ARABIC = re.compile(r"[\u0621-\u064a]")
type NameKind = Literal["drug", "test", "finding"]
type Tier = Literal["memory", "clinic", "seed", "lookup", "proposal", "unresolved"]


@dataclass(frozen=True)
class Context:
    vocabulary: NameVocabulary | None = None
    lookups: Mapping[str, DrugLookup] = field(default_factory=dict)
    generic: str | None = None
    forbidden: Callable[[str], bool] = lambda value: False


@dataclass(frozen=True)
class Resolved:
    latin: str | None
    generic: str | None
    tier: Tier
    spoken: str
    entry: NameEntry | None = None
    conflict: bool = False

    def legacy(self) -> Resolution:
        source: Literal["memory", "rxnorm", "seed"] = (
            "memory"
            if self.tier in {"memory", "clinic"}
            else "rxnorm"
            if self.tier == "lookup"
            else "seed"
        )
        return Resolution(self.latin, self.entry, self.conflict, source)


def context(service: DrugLookupService, generic: str | None = None) -> Context:
    return Context(service.vocabulary, service.results, generic, service.contains_identity)


def generic_key(value: str) -> tuple[str, ...]:
    return tuple(
        sorted(normalize(s) for s in re.split(r"/|\s+and\s+|\s*\+\s*", value) if s.strip())
    )


def hint_names(
    doctor: NameVocabulary | None = None, *, learned: tuple[str, ...] = (), limit: int = 400
) -> str:
    """A request's cached doctor/clinic vocabulary followed by the shared seed."""
    rows = tuple(r.latin for tier in doctor.rows for r in tier) if doctor else ()
    names = dict.fromkeys((*rows, *learned, *(e.latin for e in dictionary())))
    return ", ".join(tuple(names)[: min(400, max(0, limit))])


def find_memory(vocabulary: NameVocabulary, name: str, kind: str = "drug") -> NameMemory | None:
    for tier in vocabulary.rows:
        matches = [
            r
            for r in tier
            if r.kind == kind
            and normalize(name) in {normalize(s) for s in (r.latin, *r.spoken_forms)}
        ]
        if matches:
            identities = {
                generic_key(r.generic) if kind == "drug" else (normalize(r.latin),) for r in matches
            }
            return matches[0] if len(identities) == 1 else None
    return None


def resolve_name(
    spoken: str,
    kind: NameKind,
    source: str,
    proposal: str | None = None,
    ctx: Context | None = None,
) -> Resolved:
    """Resolve one identity. An unanchored proposal cannot borrow a known name."""
    ctx = ctx or Context()
    unresolved = Resolved(None, None, "unresolved", spoken)
    if ctx.forbidden(spoken) or (proposal and ctx.forbidden(proposal)):
        return Resolved(None, None, "unresolved", spoken, conflict=True)
    if not spoken.strip():
        return unresolved
    if kind != "drug" and (valued := re.fullmatch(r"(.+?)\s+(\d+(?:\.\d+)?\s*[%٪]?)", spoken)):
        # A quantity is display data, never part of the reusable name identity.
        proposed_name = re.sub(r"\s+\d+(?:\.\d+)?\s*[%٪]*$", "", proposal or "") or None
        base = resolve_name(valued[1], kind, source, proposed_name, ctx)
        if base.latin and base.tier in {"memory", "clinic", "seed"}:
            latin = re.sub(r"^(?:ECG|Echo|Complaint|History|Dx):\s*", "", base.latin)
            return Resolved(latin + " " + valued[2], base.generic, base.tier, spoken, base.entry)
    seed_kind: Literal["drug", "term"] = "drug" if kind == "drug" else "term"
    anchor = entry_for(spoken, seed_kind)
    suggested = entry_for(proposal, seed_kind) if proposal else None
    if (
        kind == "drug"
        and ctx.generic
        and suggested
        and generic_key(ctx.generic) != generic_key(suggested.generic)
    ):
        return Resolved(None, suggested.generic, "unresolved", spoken, suggested, True)
    selected: Resolved | None = None
    if ctx.vocabulary:
        for tier_name, rows in zip(("memory", "clinic"), ctx.vocabulary.rows, strict=True):
            matches = [
                r
                for r in rows
                if r.kind == kind
                and normalize(spoken) in {normalize(s) for s in (r.latin, *r.spoken_forms)}
            ]
            if matches:
                identities = {
                    generic_key(r.generic) if kind == "drug" else (normalize(r.latin),)
                    for r in matches
                }
                if len(identities) != 1:
                    return unresolved
                row = matches[0]
                entry = NameEntry(
                    seed_kind,
                    row.latin,
                    row.generic,
                    row.spoken_forms,
                    tuple(s for s in row.strengths_seen if "/" in s),
                )
                selected = Resolved(
                    row.latin,
                    row.generic,
                    "memory" if tier_name == "memory" else "clinic",
                    spoken,
                    entry,
                )
                break
    if selected is None and anchor:
        selected = Resolved(anchor.latin, anchor.generic, "seed", spoken, anchor)
    if kind == "drug" and selected:
        expected = [
            ctx.generic,
            suggested.generic if suggested else None,
            *(
                found.generic
                for query in (spoken, proposal, selected.latin)
                if query
                if (found := ctx.lookups.get(normalize(query))) and found.found
            ),
        ]
        if any(g and generic_key(g) != generic_key(selected.generic or "") for g in expected):
            return Resolved(None, selected.generic, "unresolved", spoken, selected.entry, True)
    if selected:
        return selected
    # A fetched result verifies an anchored query, never an arbitrary proposed drug.
    for query in (spoken, proposal):
        if kind != "drug" or not query:
            continue
        found = ctx.lookups.get(normalize(query))
        if found and found.found and (query == spoken or proposal_anchored(spoken, query, source)):
            if ctx.generic and generic_key(ctx.generic) != generic_key(found.generic):
                return Resolved(None, found.generic, "unresolved", spoken, conflict=True)
            if normalize(found.canonical) != normalize(query) and not ctx.generic:
                continue
            entry = NameEntry("drug", found.canonical, found.generic, (spoken,), found.strengths)
            return Resolved(found.canonical, found.generic, "lookup", spoken, entry)
    proposed = proposal or (spoken if spoken.isascii() else None)
    if proposed and proposal_anchored(spoken, proposed, source):
        return Resolved(proposed, ctx.generic, "proposal", spoken)
    return unresolved


def proposal_anchored(spoken: str, proposal: str, source: str) -> bool:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9 %/,()+.\-]{0,119}", proposal):
        return False
    if not set(numbers_in(proposal)) <= set(numbers_in(spoken)):
        return False
    if "%" in proposal and not re.search(r"[%٪]", spoken):
        return False
    if latin_in_source(proposal, source):
        return True
    if normalize(spoken) not in normalize(source):
        return False
    left = normalize(spoken)
    right = normalize(_phonetic(proposal) if _ARABIC.search(spoken) else proposal)
    return abs(len(left) - len(right)) <= 2 and edit_distance(left, right) <= 2


_CONNECTORS = {"و", "في", "فى", "in", "and", "of", "the"}
_REQUEST_WORDS = {
    "طلبت",
    "وطلبت",
    "منه",
    "منها",
    "اعمل",
    "يعمل",
    "يعملوه",
    "تحليل",
    "تحاليل",
    "فحص",
    "عايز",
    "محتاج",
    "test",
    "tests",
    "lab",
    "labs",
}


def _variants(spoken: str) -> tuple[str, ...]:
    variants = [spoken]
    for prefix in ("وال", "ال", "و"):
        if spoken.startswith(prefix) and len(spoken) > len(prefix) + 2:
            variants.append(spoken[len(prefix) :])
    return tuple(variants)


def resolve_fragments(
    spoken: str, kind: NameKind, source: str, ctx: Context | None = None
) -> tuple[Resolved, ...]:
    """Split display fragments without ever replacing an unmatched analyte by its sentence."""
    ctx = ctx or Context()
    words = list(re.finditer(r"[^\s,،;؛:]+", spoken))
    result: list[Resolved] = []
    i = 0
    while i < len(words):
        word = words[i][0]
        key = normalize(word)
        if key in _CONNECTORS:
            i += 1
            continue
        # Quantities attach to the immediately preceding term without changing digits.
        if re.fullmatch(r"\d+(?:[.٫]\d+)?[%٪]?|[%٪]", word) and result:
            prior = result[-1]
            result[-1] = Resolved(
                (prior.latin + ("" if word in {"%", "٪"} else " ") + word) if prior.latin else None,
                prior.generic,
                prior.tier,
                prior.spoken + " " + word,
                prior.entry,
                prior.conflict,
            )
            i += 1
            continue
        match: tuple[int, Resolved] | None = None
        for end in range(min(len(words), i + 8), i, -1):
            raw = spoken[words[i].start() : words[end - 1].end()]
            if any(c in raw for c in (",", "،", ";", "؛", ":")) or numbers_in(raw):
                continue
            for variant in _variants(raw):
                # Find the longest exact identity before considering spelling variants.
                if not _exact_known(variant, kind, ctx):
                    continue
                resolved = resolve_name(variant, kind, source, None, ctx)
                if resolved.latin:
                    match = (
                        end,
                        Resolved(
                            resolved.latin, resolved.generic, resolved.tier, raw, resolved.entry
                        ),
                    )
                    break
            if match:
                break
        if match is None and kind == "test" and key in _REQUEST_WORDS:
            i += 1
            continue
        if match is None or match[0] == i + 1:
            # A short spelling difference can span a multiword identity (for
            # example an article). A complete phrase may outrank its first word
            # (Echo versus Echo Function). All matching stays in this resolver.
            for end in range(min(len(words), i + 8), i + 1, -1):
                raw = spoken[words[i].start() : words[end - 1].end()]
                if any(c in raw for c in (",", "،", ";", "؛", ":")) or numbers_in(raw):
                    continue
                resolved = resolve_name(raw, kind, source, None, ctx)
                if resolved.latin and resolved.tier in {"seed", "memory", "clinic"}:
                    match = (end, resolved)
                    break
        if match is None:
            resolved = (
                resolve_name(word, kind, source, None, ctx)
                if len(key) >= 4 or word.isascii()
                else Resolved(None, None, "unresolved", word)
            )
            if resolved.latin:
                match = (i + 1, resolved)
        if match:
            i, resolved = match
            result.append(resolved)
        else:
            if result and result[-1].tier == "unresolved":
                prior = result.pop()
                word = prior.spoken + " " + word
            result.append(Resolved(None, None, "unresolved", word))
            i += 1
    return tuple(result)


def _exact_known(spoken: str, kind: NameKind, ctx: Context) -> bool:
    if ctx.vocabulary and find_memory(ctx.vocabulary, spoken, kind):
        return True
    key = normalize(spoken)
    return any(
        key in spellings
        for _, spellings in _seed_rows(dictionary(), "drug" if kind == "drug" else "term")
    )


@lru_cache(maxsize=4)
def _seed_rows(
    entries: tuple[NameEntry, ...], kind: str
) -> tuple[tuple[NameEntry, frozenset[str]], ...]:
    return tuple(
        (e, frozenset(normalize(s) for s in (e.latin, *e.arabic_spellings, *e.latin_spellings)))
        for e in entries
        if e.kind == kind
    )


def edit_distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, left in enumerate(a, 1):
        row = [i]
        for j, right in enumerate(b, 1):
            row.append(min(row[-1] + 1, previous[j] + 1, previous[j - 1] + (left != right)))
        previous = row
    return previous[-1]


def entry_for(text: str, kind: Literal["drug", "term"] = "drug") -> NameEntry | None:
    key = normalize(text)
    rows = _seed_rows(dictionary(), kind)
    exact = [entry for entry, spellings in rows if key in spellings]
    if len(exact) == 1:
        return exact[0]
    if exact or not key:
        return None
    arabic = bool(_ARABIC.search(key))
    if not arabic and len(key) < 4:
        return None
    if not arabic and not re.search(r"[a-z]", key):
        return None
    quantities = numbers_in(key)
    distances = []
    for entry, spellings in rows:
        distance = min(
            (
                edit_distance(key, spelling)
                for s in spellings
                if abs(len(key) - len(spelling := s)) <= 2
                and bool(_ARABIC.search(s)) == arabic
                and numbers_in(spelling) == quantities
            ),
            default=3,
        )
        distances.append((distance, entry))
    minimum = min(d for d, _ in distances)
    closest = [e for d, e in distances if d == minimum]
    return closest[0] if minimum <= 2 and len(closest) == 1 else None


def latin_in_source(name: str, source: str) -> bool:
    return bool(
        re.fullmatch(r"[A-Za-z][A-Za-z0-9 /+-]*", name)
        and re.search(r"(?<![\w])" + re.escape(name) + r"(?![\w])", source, re.I)
    )


def source_spans(
    name: str, source: str, kind: NameKind = "test", ctx: Context | None = None
) -> tuple[tuple[int, int], ...]:
    """Literal token/alias anchors, with at most one edit and unchanged quantities."""
    ctx = ctx or Context()
    aliases = {name}
    for entry in dictionary():
        if entry.kind == ("drug" if kind == "drug" else "term") and normalize(
            entry.latin
        ) == normalize(name):
            aliases.update((*entry.arabic_spellings, *entry.latin_spellings))
    if ctx.vocabulary:
        for rows in ctx.vocabulary.rows:
            for row in rows:
                if row.kind == kind and normalize(row.latin) == normalize(name):
                    aliases.update(row.spoken_forms)
    tokens = list(re.finditer(r"[\w%/+-]+", source))
    spans: set[tuple[int, int]] = set()
    for alias in aliases:
        key = normalize(alias)
        size = len(re.findall(r"[\w%/+-]+", alias))
        if not size:
            continue
        for start in range(len(tokens) - size + 1):
            a, b = tokens[start].start(), tokens[start + size - 1].end()
            for variant in _variants(source[a:b]):
                value = normalize(variant)
                if value == key or (
                    min(len(value), len(key)) >= 4
                    and abs(len(value) - len(key)) <= 1
                    and numbers_in(value) == numbers_in(key)
                    and edit_distance(value, key) <= 1
                ):
                    spans.add((a, b))
    return tuple(sorted(spans))


def source_anchored(
    name: str, source: str, kind: NameKind = "test", ctx: Context | None = None
) -> bool:
    return bool(source_spans(name, source, kind, ctx))


_TEST_REQUEST = re.compile(
    r"(?:\b(?:i\s+)?(?:ordered|request(?:ed)?)(?:\s+(?:tests?|labs?))?\s*:?\s+|"
    r"(?:و?طلبت|تحاليل|تحليل)\s+)([^.;؛\n]+)",
    re.I,
)
_NEXT_REQUEST = re.compile(
    r"\s+and\s+(?:i\b|(?:can\s+)?(?:add|start|increase|decrease|change|visit|review)\b)"
    r"|\s+و(?:ب?يراجع|يحضر|يزور|يقيس|يسجل|بلغني)\b",
    re.I,
)
_TEST_TIMING = re.compile(
    r"\s+(?:بعد|خلال|بكره|بكرا|غدا)\b|"
    r"\s+(?:in|within|after|by|on|for)\s+"
    r"(?:\d|one\b|two\b|three\b|four\b|five\b|six\b|seven\b|a\b|an\b|next\b|"
    r"today\b|tomorrow\b|mon\w*\b|tue\w*\b|wed\w*\b|thu\w*\b|fri\w*\b|sat\w*\b|sun\w*\b)",
    re.I,
)


def _test_gap(text: str) -> str:
    """Remove request grammar at gap boundaries, preserving the heard test wording."""
    words = list(re.finditer(r"[^\s,،;؛:]+", text))
    grammar = _REQUEST_WORDS | {"and", "و", "please"}
    while words and normalize(words[0][0]) in grammar:
        words.pop(0)
    while words and normalize(words[-1][0]) in grammar:
        words.pop()
    return text[words[0].start() : words[-1].end()].strip(" ,،:") if words else ""


def unresolved_test_fragments(
    names: tuple[str, ...], source: str, ctx: Context | None = None
) -> tuple[str, ...]:
    """Quote gaps in a spoken test request, never a model's guessed replacement."""
    fragments: list[str] = []
    for request in _TEST_REQUEST.finditer(source):
        clause = _TEST_TIMING.split(_NEXT_REQUEST.split(request[1], maxsplit=1)[0], maxsplit=1)[
            0
        ].strip()
        spans = sorted({span for name in names for span in source_spans(name, clause, ctx=ctx)})
        # A clause belonging to another TEST must not contaminate this one.
        if names and not spans:
            continue
        end = 0
        gaps: list[str] = []
        for a, b in spans:
            if a > end:
                gaps.append(clause[end:a])
            end = max(end, b)
        gaps.append(clause[end:])
        for gap in gaps:
            gap = _test_gap(gap)
            if gap and normalize(gap) not in _CONNECTORS:
                fragments.append(gap)
    return tuple(dict.fromkeys(fragments))


def resolve_tests(
    spoken: str, source: str, ctx: Context | None = None, *, clarified: bool = False
) -> tuple[tuple[Resolved, ...], tuple[str, ...]]:
    """Only anchored analytes reach display; keep heard gaps for one clarification."""
    accepted: list[Resolved] = []
    rejected: list[Resolved] = []
    for resolved in resolve_fragments(spoken, "test", source, ctx):
        if (
            not resolved.conflict
            and all(
                source_anchored(part.strip(), source, ctx=ctx)
                for part in (resolved.latin or resolved.spoken).split(",")
            )
            and normalize(resolved.latin or resolved.spoken)
            not in {"one", "two", "three", "four", "five", "i", "ordered", "request", "requested"}
        ):
            accepted.append(resolved)
        else:
            rejected.append(resolved)
    fragments = (
        ()
        if clarified
        else unresolved_test_fragments(
            tuple(part.strip() for r in accepted for part in (r.latin or r.spoken).split(",")),
            source,
            ctx,
        )
    )
    if rejected and not fragments:
        fragments = tuple(
            dict.fromkeys(
                r.spoken
                for r in rejected
                if re.search(r"(?<!\w)" + re.escape(r.spoken) + r"(?!\w)", source, re.I)
            )
        ) or ("",)
    return tuple(accepted), fragments


def test_reply_resolves(
    spoken: str, previous: tuple[str, ...], reply: str, source: str, ctx: Context
) -> bool:
    """A reply names the replacement analyte, or explicitly restates the retained set."""
    resolved, _ = resolve_tests(spoken, source, ctx)
    names = tuple(part.strip() for r in resolved for part in (r.latin or r.spoken).split(","))
    prior = {normalize(name) for name in previous}
    introduced = tuple(name for name in names if normalize(name) not in prior)
    answered = introduced or names
    return bool(answered) and all(source_anchored(name, reply, ctx=ctx) for name in answered)


def _phonetic(english: str) -> str:
    """Bounded mechanical English-to-Arabic spelling, with no medical semantics."""
    value = normalize(english)
    for left, right in (
        ("sh", "ش"),
        ("ch", "تش"),
        ("ph", "ف"),
        ("th", "ث"),
        ("kh", "خ"),
        ("gh", "غ"),
        ("oo", "و"),
        ("ee", "ي"),
    ):
        value = value.replace(left, right)
    letters: dict[str, str | int | None] = {
        "a": "ا",
        "b": "ب",
        "c": "ك",
        "d": "د",
        "e": "ي",
        "f": "ف",
        "g": "ج",
        "h": "ه",
        "i": "ي",
        "j": "ج",
        "k": "ك",
        "l": "ل",
        "m": "م",
        "n": "ن",
        "o": "و",
        "p": "ب",
        "q": "ك",
        "r": "ر",
        "s": "س",
        "t": "ت",
        "u": "و",
        "v": "ف",
        "w": "و",
        "x": "كس",
        "y": "ي",
        "z": "ز",
    }
    return value.translate(str.maketrans(letters))


def latin_terms(text: str) -> str:
    """Legacy entry point using the same fragment resolver as the card."""
    return ", ".join(r.latin or r.spoken for r in resolve_fragments(text, "finding", text))


def drug_mentions(text: str, service: DrugLookupService) -> tuple[DrugMention, ...]:
    from sanad.scribe.extract import DrugMention

    entries = (*service.vocabulary.entries(), *(e for e in dictionary() if e.kind == "drug"))
    result: dict[str, DrugMention] = {}
    for entry in entries:
        for spelling in (entry.latin, *entry.arabic_spellings, *entry.latin_spellings):
            if re.search(
                r"(?<!\w)و?" + re.escape(normalize(spelling)) + r"(?!\w)", normalize(text)
            ):
                resolved = resolve_name(spelling, "drug", text, ctx=context(service))
                if resolved.latin:
                    result.setdefault(
                        resolved.generic or resolved.latin,
                        DrugMention(
                            spoken=spelling,
                            name_latin=resolved.latin,
                            generic=resolved.generic,
                        ),
                    )
    return tuple(result.values())


def learn(
    store: Store,
    doctor: Doctor,
    proposal: Proposal,
    clock: Callable[[], datetime],
) -> tuple[NameMemory, ...]:
    """Compile both tiers together; the caller puts every row in ScribeConfirm."""
    from sanad.auth.service import revise
    from sanad.scribe.policy import DRAFT_SCRIBE_POLICY
    from sanad.store import keys
    from sanad.store.keys import AccountScope
    from sanad.store.records import NameMemory, from_record

    now = clock()
    result: dict[tuple[str, str], NameMemory] = {}
    for name in proposal.names:
        if name.kind == "finding":
            from sanad.scribe.card import plain
            from sanad.scribe.terms import vocabulary_term

            name = name.model_copy(
                update={
                    "spoken": vocabulary_term(plain(name.spoken)),
                    "latin": vocabulary_term(plain(name.latin)),
                }
            )
        if proposal.blocked(name.item) or not name.latin or not name.learnable:
            continue
        for scope in (doctor.scope, AccountScope(bot_id=doctor.telegram_bot_id)):
            id = keys.digest(keys.partition(scope) + ":" + name.kind + ":" + normalize(name.latin))
            key = (keys.partition(scope), id)
            current = result.get(key)
            if current is None:
                row = store.get(scope, "name_memory", id)
                current = from_record(row, NameMemory) if row else None
            spoken = tuple(dict.fromkeys((*(current.spoken_forms if current else ()), name.spoken)))
            strengths = tuple(
                dict.fromkeys((*(current.strengths_seen if current else ()), *name.strengths))
            )
            values = {
                "latin": name.latin,
                "generic": name.generic,
                "spoken_forms": spoken[-DRAFT_SCRIBE_POLICY.spoken_forms_max :],
                "strengths_seen": strengths,
                "last_confirmed_at": now,
                "source": "doctor_confirmation",
            }
            # A name appearing twice on one card is one confirmation, not two votes.
            if key in result:
                result[key] = NameMemory.model_validate(current.model_dump() | values)  # type: ignore[union-attr]
            elif current:
                result[key] = revise(
                    current, now, confirmations=current.confirmations + 1, **values
                )
            else:
                result[key] = NameMemory.model_validate(
                    {
                        "id": id,
                        "scope": scope,
                        "kind": name.kind,
                        "created_at": now,
                        "updated_at": now,
                        **values,
                    }
                )
    return tuple(result.values())
