"""Bounded public drug lookup. Requests contain one drug name, never a dictation."""

import logging
import re
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic
from typing import Any, Literal
from uuid import uuid4

import httpx

from sanad.domain import Principal
from sanad.domain.boundaries import _BoundaryValue
from sanad.models.timeouts import EXTRACTION_TIMEOUT
from sanad.scribe.memory import NameVocabulary
from sanad.scribe.names import NameEntry, Resolution, entry_for, normalize, split_drug_dose
from sanad.scribe.policy import DRAFT_SCRIBE_POLICY
from sanad.scribe.repository import ScribeRepository
from sanad.store import keys
from sanad.store.records import NAME_CACHE_SCOPE, NameCache

logger = logging.getLogger(__name__)
RXNORM_URL = "https://rxnav.nlm.nih.gov/REST/"
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9 /+().,-]{0,119}")


def generic_key(value: str) -> tuple[str, ...]:
    return tuple(
        sorted(normalize(s) for s in re.split(r"/|\s+and\s+|\s*\+\s*", value) if s.strip())
    )


class DrugLookup(_BoundaryValue):
    found: bool = False
    canonical: str = ""
    generic: str = ""
    strengths: tuple[str, ...] = ()
    source: Literal["rxnorm", "memory", "seed"] = "rxnorm"


@dataclass
class LookupBudget:
    calls: int = 0
    lock: Lock = field(default_factory=Lock)

    def take(self) -> bool:
        with self.lock:
            if self.calls >= DRAFT_SCRIBE_POLICY.lookups_per_card:
                return False
            self.calls += 1
            return True


class DrugLookupService:
    def __init__(
        self,
        repo: ScribeRepository,
        vocabulary: NameVocabulary,
        actor: Principal,
        *,
        client: httpx.Client | None = None,
        budget: LookupBudget | None = None,
        forbidden_names: tuple[str, ...] = (),
    ):
        self.repo, self.vocabulary, self.actor = repo, vocabulary, actor
        self.client, self.budget = client, budget or LookupBudget()
        self.forbidden_names = tuple(normalize(n) for n in forbidden_names if n)
        self.results: dict[str, DrugLookup] = {}
        self.table: list[dict[str, object]] = []
        self.deadline = monotonic() + EXTRACTION_TIMEOUT
        self.patient_identity_pending = False

    def contains_identity(self, value: str) -> bool:
        key = normalize(value)
        return any(
            n == key or (len(n) > 2 and re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", key))
            for n in self.forbidden_names
        )

    def _get(self, path: str, params: dict[str, str], deadline: float) -> dict[str, Any]:
        remaining = deadline - monotonic()
        if self.client is None or remaining <= 0 or not self.budget.take():
            raise ValueError("lookup_unavailable")
        # Stream with a byte cap; redirects, HTML and error bodies are never interpreted.
        with self.client.stream(
            "GET",
            RXNORM_URL + path,
            params=params,
            timeout=min(remaining, DRAFT_SCRIBE_POLICY.rxnorm_timeout),
            follow_redirects=False,
        ) as response:
            response.raise_for_status()
            if "application/json" not in response.headers.get("content-type", "").lower():
                raise ValueError("invalid_content_type")
            chunks = bytearray()
            for chunk in response.iter_bytes():
                chunks.extend(chunk)
                if len(chunks) > 128_000 or monotonic() > deadline:
                    raise ValueError("lookup_response_bound")
            import json

            value = json.loads(chunks)
            if not isinstance(value, dict):
                raise ValueError("invalid_response")
            return value

    def _remote(self, name: str) -> DrugLookup:
        deadline = min(self.deadline, monotonic() + DRAFT_SCRIBE_POLICY.rxnorm_timeout)
        approximate = self._get(
            "approximateTerm.json", {"term": name, "maxEntries": "3", "option": "1"}, deadline
        )
        candidates = approximate.get("approximateGroup", {}).get("candidate", [])
        if not isinstance(candidates, list) or not candidates:
            return DrugLookup()
        candidates = sorted(candidates, key=lambda c: float(c["score"]), reverse=True)
        best = candidates[0]
        if len({c["rxcui"] for c in candidates if float(c["score"]) == float(best["score"])}) != 1:
            return DrugLookup()
        id = str(best["rxcui"])
        if not re.fullmatch(r"[0-9]{1,12}", id):
            return DrugLookup()
        properties = self._get(f"rxcui/{id}/properties.json", {}, deadline).get("properties", {})
        if not isinstance(properties, dict) or str(properties.get("rxcui")) != id:
            return DrugLookup()
        related = self._get(f"rxcui/{id}/related.json", {"tty": "IN+BN+SCDC"}, deadline)
        groups = related.get("relatedGroup", {}).get("conceptGroup", [])
        ingredients: list[str] = []
        brands: list[str] = []
        strengths: list[str] = []
        for group in groups:
            for concept in group.get("conceptProperties", []):
                text = str(concept.get("name", ""))
                if not _NAME.fullmatch(text):
                    raise ValueError("invalid_vocabulary")
                tty = group.get("tty")
                if tty == "IN":
                    ingredients.append(text)
                elif tty == "BN":
                    brands.append(text)
                elif tty == "SCDC":
                    strengths.extend(
                        re.findall(
                            r"\d+(?:\.\d+)?\s*(?:MG|MCG|ML|UNT)(?:/\d+(?:\.\d+)?\s*(?:MG|ML))?",
                            text,
                            re.I,
                        )
                    )
        canonical = str(properties.get("name", ""))
        if properties.get("tty") == "IN":
            ingredients = [canonical]
        exact_brands = [b for b in brands if normalize(b) == normalize(name)]
        if exact_brands:
            canonical = exact_brands[0]
        elif len(brands) == 1:
            canonical = brands[0]
        if not _NAME.fullmatch(canonical) or not ingredients:
            return DrugLookup()
        generic = "/".join(sorted(set(ingredients), key=normalize))
        if len(generic) > 240:
            return DrugLookup()
        return DrugLookup(
            found=True,
            canonical=canonical,
            generic=generic,
            strengths=tuple(dict.fromkeys(strengths)),
        )

    def lookup_drug(self, name: str) -> DrugLookup:
        started = monotonic()
        key = normalize(name)
        if key in self.results:
            return self.results[key]
        # Reject identities, sentences, URLs and dose-bearing source snippets.
        clean, dose = split_drug_dose(name)
        if (
            not _NAME.fullmatch(name)
            or dose
            or len(name.split()) > 6
            or self.contains_identity(name)
        ):
            return DrugLookup()
        if self.patient_identity_pending and not (self.vocabulary.find(name) or entry_for(name)):
            # Defer a novel name's HTTP verification until extraction supplies the
            # patient identity to exclude. This never prevents a novel name's card.
            return DrugLookup()
        result = DrugLookup()
        outcome = "not_found"
        failure_code: str | None = None
        http_status: int | None = None
        remembered = self.vocabulary.find(name)
        if remembered:
            result = DrugLookup(
                found=True,
                canonical=remembered.latin,
                generic=remembered.generic,
                strengths=remembered.strengths_seen,
                source="memory",
            )
            outcome = "memory"
        else:
            cached = self.repo.load(NAME_CACHE_SCOPE, "name_cache", keys.digest(key), NameCache)
            if cached and cached.expires_at > self.repo.clock():
                result = DrugLookup(
                    found=True,
                    canonical=cached.canonical,
                    generic=cached.generic,
                    strengths=cached.strengths,
                )
                outcome = "cache"
            else:
                try:
                    result = self._remote(clean)
                    outcome = "found" if result.found else "not_found"
                except Exception as error:
                    outcome = "unavailable"
                    if isinstance(error, httpx.HTTPStatusError):
                        http_status = error.response.status_code
                        failure_code = "http_status"
                    elif isinstance(error, httpx.TimeoutException):
                        failure_code = "timeout"
                    elif isinstance(error, ValueError) and str(error) in {
                        "lookup_unavailable",
                        "invalid_content_type",
                        "lookup_response_bound",
                        "invalid_response",
                        "invalid_vocabulary",
                    }:
                        failure_code = str(error)
                    else:
                        failure_code = (
                            "invalid_response"
                            if isinstance(error, (ValueError, TypeError, KeyError))
                            else "unavailable"
                        )
                    logger.info(
                        "rxnorm_lookup_failed outcome=unavailable code=%s status=%s calls=%d",
                        failure_code,
                        http_status,
                        self.budget.calls,
                    )
                if result.found:
                    now = self.repo.clock()
                    cache = NameCache(
                        id=keys.digest(key),
                        query=name,
                        canonical=result.canonical,
                        generic=result.generic,
                        strengths=result.strengths,
                        version=cached.version + 1 if cached else 1,
                        created_at=cached.created_at if cached else now,
                        updated_at=now,
                        expires_at=now + DRAFT_SCRIBE_POLICY.name_cache_ttl,
                    )
                    try:
                        self.repo.commit(self.actor, "ScribeNameCache", uuid4().hex, (cache,))
                    except Exception:
                        logger.info("rxnorm_cache_write outcome=unavailable")
        if not result.found and (seed := entry_for(name)) is not None:
            result = DrugLookup(
                found=True,
                canonical=seed.latin,
                generic=seed.generic,
                strengths=seed.fixed_combination_strengths,
                source="seed",
            )
        self.results[key] = result
        self.table.append(
            {
                "name": name,
                **result.model_dump(mode="json"),
                "ms": round((monotonic() - started) * 1000, 2),
                "outcome": outcome,
                "failure_code": failure_code,
                "http_status": http_status,
            }
        )
        logger.info(
            "rxnorm_lookup outcome=%s found=%s calls=%d ms=%d",
            outcome,
            result.found,
            self.budget.calls,
            round((monotonic() - started) * 1000),
        )
        return result

    def resolve(
        self, spoken: str, source: str, proposed: str | None, generic: str | None
    ) -> Resolution:
        if self.contains_identity(spoken) or (proposed and self.contains_identity(proposed)):
            return Resolution(None, conflict=True)
        remembered = self.vocabulary.find(spoken) or (
            self.vocabulary.find(proposed) if proposed else None
        )
        anchor = entry_for(spoken)
        suggestion = entry_for(proposed) if proposed else None
        expected = remembered.generic if remembered else anchor.generic if anchor else generic
        if (expected and generic and generic_key(expected) != generic_key(generic)) or (
            anchor and suggestion and generic_key(anchor.generic) != generic_key(suggestion.generic)
        ):
            return Resolution(None, anchor, True)
        name = remembered.latin if remembered else proposed or (anchor.latin if anchor else spoken)
        result = self.lookup_drug(name)
        if not result.found:
            return Resolution(None, anchor, bool(anchor and proposed and not suggestion))
        if expected and generic_key(result.generic) != generic_key(expected):
            return Resolution(None, anchor, True, result.source)
        if not expected and normalize(result.canonical) != normalize(name):
            return Resolution(None)
        # An RxNorm brand variant verifies identity, not a request to substitute brands.
        latin = remembered.latin if remembered else anchor.latin if anchor else name
        entry = NameEntry(
            "drug",
            latin,
            result.generic,
            (spoken,),
            result.strengths if not anchor else anchor.fixed_combination_strengths,
        )
        return Resolution(latin, entry, False, result.source)
