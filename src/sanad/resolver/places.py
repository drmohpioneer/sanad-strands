"""Fixed-host OSM adapter. Source data is untrusted, bounded and never clinical."""

import asyncio
import math
import os
import re
from collections.abc import Callable
from typing import Literal, Protocol, Self

import httpx
from pydantic import Field, model_validator

from sanad.domain.boundaries import _BoundaryValue
from sanad.domain.entities import BarrierPlace
from sanad.resolver.policy import POLICY

CONNECT_TIMEOUT = 4.0
READ_TIMEOUT = 4.0
STEP_TIMEOUT = 9.0
MAX_RESPONSE_BYTES = 2_000_000
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"


class PlacesResult(_BoundaryValue):
    outcome: Literal["places_offered", "places_unavailable", "area_ambiguous"]
    places: tuple[BarrierPlace, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if (self.outcome == "places_offered") != bool(self.places):
            raise ValueError("places_outcome_mismatch")
        return self


class PlacesProvider(Protocol):
    async def search(self, area: str, *, authorized: Callable[[], bool]) -> PlacesResult: ...


def attribute(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return None
    if any(ord(c) < 32 for c in value) or re.search(r"[<>\[\]{}]|https?://", value):
        return None
    return value.strip()


def distance(lat: float, lon: float, other_lat: float, other_lon: float) -> int:
    a, b = math.radians(lat), math.radians(other_lat)
    dlat, dlon = b - a, math.radians(other_lon - lon)
    h = math.sin(dlat / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(dlon / 2) ** 2
    return round(6_371_000 * 2 * math.asin(math.sqrt(min(1, max(0, h)))))


def point(value: object) -> tuple[float, float]:
    if not isinstance(value, dict):
        raise ValueError("point_missing")
    lat, lon = float(value["lat"]), float(value["lon"])
    if (
        not math.isfinite(lat)
        or not math.isfinite(lon)
        or not (-90 <= lat <= 90 and -180 <= lon <= 180)
    ):
        raise ValueError("point_invalid")
    return lat, lon


class OSMPlaces:
    def __init__(
        self, *, transport: httpx.AsyncBaseTransport | None = None, user_agent: str | None = None
    ):
        self.transport = transport
        self.user_agent = (
            user_agent if user_agent is not None else os.environ.get("SANAD_OSM_USER_AGENT", "")
        )

    async def search(self, area: str, *, authorized: Callable[[], bool]) -> PlacesResult:
        unavailable = PlacesResult(outcome="places_unavailable")
        if not self.user_agent.strip() or not attribute(area, 120) or not authorized():
            return unavailable
        try:
            async with (
                asyncio.timeout(STEP_TIMEOUT),
                httpx.AsyncClient(
                    transport=self.transport,
                    timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
                    headers={"User-Agent": self.user_agent, "Accept": "application/json"},
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
            ):
                # Retry policy belongs to the durable search counter, never httpx.
                async def fetch(url: str, params: dict[str, str]) -> object:
                    if not authorized():
                        raise ValueError("authority_changed")
                    async with client.stream("GET", url, params=params) as response:
                        response.raise_for_status()
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > MAX_RESPONSE_BYTES:
                                raise ValueError("response_bound")
                        import json

                        return json.loads(data)

                geo = await fetch(NOMINATIM_URL, {"q": area, "format": "jsonv2", "limit": "2"})
                if not isinstance(geo, list) or not geo:
                    return unavailable
                if len(geo) != 1:
                    return PlacesResult(outcome="area_ambiguous")
                lat, lon = point(geo[0])
                amenities = "|".join(POLICY.amenities)
                query = (
                    f'[out:json][timeout:4];nwr["amenity"~"^({amenities})$"]'
                    f"(around:{POLICY.search_radius_m},{lat},{lon});out center tags;"
                )
                data = await fetch(OVERPASS_URL, {"data": query})
                if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
                    return unavailable
                places: dict[str, BarrierPlace] = {}
                for item in data["elements"]:
                    if not isinstance(item, dict) or not isinstance(item.get("tags"), dict):
                        continue
                    tags = item["tags"]
                    if tags.get("amenity") not in POLICY.amenities:
                        continue
                    name = attribute(tags.get("name"), 60)
                    if (
                        not name
                        or item.get("type") not in {"node", "way", "relation"}
                        or type(item.get("id")) is not int
                    ):
                        continue
                    try:
                        other = point(item if "lat" in item else item.get("center"))
                        metres = distance(lat, lon, *other)
                        if metres > POLICY.search_radius_m:
                            continue
                        address = attribute(tags.get("addr:full"), 100) or attribute(
                            ", ".join(
                                str(tags[k])
                                for k in ("addr:housenumber", "addr:street", "addr:city")
                                if k in tags
                            ),
                            100,
                        )
                        source_id = f"{item['type']}/{item['id']}"
                        places[source_id] = BarrierPlace(
                            source_id=source_id,
                            name=name,
                            address=address,
                            distance_m=metres,
                            opening_hours=attribute(tags.get("opening_hours"), 60),
                            phone=attribute(tags.get("phone", tags.get("contact:phone")), 40),
                        )
                    except (ValueError, TypeError, KeyError):
                        continue
                    # Keep bounded memory even for a hostile large response.
                    if len(places) > POLICY.max_results:
                        farthest = max(places, key=lambda k: (places[k].distance_m, k))
                        del places[farthest]
                result = tuple(sorted(places.values(), key=lambda p: (p.distance_m, p.source_id)))
                return (
                    PlacesResult(outcome="places_offered", places=result) if result else unavailable
                )
        except (TimeoutError, httpx.HTTPError, ValueError, KeyError, TypeError):
            return unavailable
