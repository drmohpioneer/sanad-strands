"""One injected HTTP capture; there is no external-network fallback."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from sanad.resolver.places import OSMPlaces

ROOT = Path(__file__).with_suffix("")


def body(name: str) -> Any:
    return json.loads((ROOT / (name + ".json")).read_text())


@dataclass
class Capture:
    geocode: object = field(default_factory=lambda: body("nominatim"))
    places: object = field(default_factory=lambda: body("overpass"))
    statuses: list[int] = field(default_factory=list)
    timeout: bool = False
    requests: list[httpx.Request] = field(default_factory=list)

    def request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host in {"nominatim.openstreetmap.org", "overpass-api.de"}
        assert request.headers["User-Agent"] == "Sanad offline contract16a fixture"
        assert request.extensions["timeout"] == {
            "connect": 4.0,
            "read": 4.0,
            "write": 4.0,
            "pool": 4.0,
        }
        if self.timeout:
            raise httpx.ReadTimeout("captured timeout", request=request)
        status = self.statuses.pop(0) if self.statuses else 200
        return httpx.Response(
            status,
            json=self.geocode if request.url.host == "nominatim.openstreetmap.org" else self.places,
        )

    def provider(self) -> OSMPlaces:
        return OSMPlaces(
            transport=httpx.MockTransport(self.request),
            user_agent="Sanad offline contract16a fixture",
        )
