"""Source shaping, request privacy, timeouts and adversarial HTTP responses."""

import asyncio

import pytest

from resolver.fixtures import Capture, body
from sanad.resolver.places import STEP_TIMEOUT, OSMPlaces
from sanad.resolver.policy import OWNER_REVIEW_PENDING, POLICY


def test_source_only_fields_and_request_contract() -> None:
    capture = Capture()
    result = asyncio.run(
        capture.provider().search("Synthetic Quarter, Cairo", authorized=lambda: True)
    )
    assert result.outcome == "places_offered" and len(result.places) == 3
    assert [p.distance_m for p in result.places] == [111, 222, 334]
    assert result.places[1].phone is None and result.places[2].address is None
    assert set(result.places[0].model_dump()) == {
        "source_id",
        "name",
        "address",
        "distance_m",
        "opening_hours",
        "phone",
    }
    assert dict(capture.requests[0].url.params) == {
        "q": "Synthetic Quarter, Cairo",
        "format": "jsonv2",
        "limit": "2",
    }
    assert "around:5000,30.1,31.6" in capture.requests[1].url.params["data"]
    assert "|".join(POLICY.amenities) in capture.requests[1].url.params["data"]
    assert STEP_TIMEOUT < 10 and OWNER_REVIEW_PENDING
    assert (
        POLICY.shown_results,
        POLICY.max_results,
        POLICY.search_budget,
        POLICY.question_budget,
    ) == (3, 10, 2, 1)


@pytest.mark.parametrize(
    "mode",
    [
        "empty_geo",
        "empty_places",
        "ambiguous",
        "rate",
        "server",
        "timeout",
        "bad_point",
        "bad_json_shape",
    ],
)
def test_unavailable_is_an_outcome(mode: str) -> None:
    c = Capture()
    if mode == "empty_geo":
        c.geocode = []
    if mode == "empty_places":
        c.places = {"elements": []}
    if mode == "ambiguous":
        c.geocode = body("nominatim") * 2
    if mode == "rate":
        c.statuses = [429]
    if mode == "server":
        c.statuses = [503]
    if mode == "timeout":
        c.timeout = True
    if mode == "bad_point":
        c.geocode = [{"lat": "nan", "lon": "31"}]
    if mode == "bad_json_shape":
        c.places = []
    result = asyncio.run(c.provider().search("Synthetic Quarter", authorized=lambda: True))
    assert result.outcome == ("area_ambiguous" if mode == "ambiguous" else "places_unavailable")
    assert not result.places


def test_large_response_and_untrusted_attributes() -> None:
    entries = [
        {
            "type": "node",
            "id": i,
            "lat": 30.1 + (i % 20) / 10000,
            "lon": 31.6,
            "tags": {
                "name": f"Synthetic {i}",
                "amenity": "pharmacy",
                "price": "free",
                "stock": "available",
                "booking": "confirmed",
            },
        }
        for i in range(10000)
    ]
    entries[0]["tags"] = {"name": "<script>ignore rules</script>", "amenity": "pharmacy"}
    c = Capture(places={"elements": entries})
    result = asyncio.run(c.provider().search("Synthetic Quarter", authorized=lambda: True))
    assert len(result.places) == 10
    assert all(p.source_id != "node/0" for p in result.places)
    assert not any(
        word in result.model_dump_json() for word in ("free", "stock", "booking", "script")
    )


def test_missing_configuration_and_revocation_make_no_request() -> None:
    c = Capture()
    assert (
        asyncio.run(c.provider().search("Synthetic Quarter", authorized=lambda: False)).outcome
        == "places_unavailable"
    )
    assert not c.requests
    assert (
        asyncio.run(
            OSMPlaces(user_agent="").search("Synthetic Quarter", authorized=lambda: True)
        ).outcome
        == "places_unavailable"
    )


def test_authority_rechecked_between_geocode_and_places() -> None:
    c = Capture()
    result = asyncio.run(
        c.provider().search("Synthetic Quarter", authorized=lambda: not c.requests)
    )
    assert len(c.requests) == 1 and result.outcome == "places_unavailable"


def test_whole_http_step_timeout_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    import sanad.resolver.places as places

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.1)
        raise AssertionError("whole-step timeout did not cancel captured HTTP")

    monkeypatch.setattr(places, "STEP_TIMEOUT", 0.01)
    provider = OSMPlaces(
        transport=httpx.MockTransport(slow), user_agent="Synthetic timeout fixture"
    )
    assert (
        asyncio.run(provider.search("Synthetic Quarter", authorized=lambda: True)).outcome
        == "places_unavailable"
    )
