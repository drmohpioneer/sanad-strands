"""Stored RxNorm-shaped replies; no default test can reach the public provider.

The fixed IDs are fixture identifiers. These records exercise the documented
response fields without claiming that an upstream RxNorm snapshot was fetched.
"""

import httpx

DRUGS = {
    "exforge hct": (
        "1001",
        "Exforge HCT",
        ("amlodipine", "valsartan", "hydrochlorothiazide"),
        ("5 MG", "160 MG", "12.5 MG"),
    ),
    "forxiga": ("1002", "Farxiga", ("dapagliflozin",), ("5 MG", "10 MG")),
    "farxiga": ("1002", "Farxiga", ("dapagliflozin",), ("5 MG", "10 MG")),
    "bisoprolol": ("1003", "bisoprolol", ("bisoprolol",), ("5 MG", "10 MG")),
    "amlodipine": ("1004", "amlodipine", ("amlodipine",), ("2.5 MG", "5 MG", "10 MG")),
}


class RxNormFixture:
    def __init__(self, *, failure: str | None = None, generic: str | None = None):
        self.calls: list[httpx.Request] = []
        self.failure, self.generic = failure, generic
        self.client = httpx.Client(transport=httpx.MockTransport(self.respond))

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.url.host == "rxnav.nlm.nih.gov"
        assert request.method == "GET"
        if self.failure == "timeout":
            raise httpx.ReadTimeout("synthetic private provider body", request=request)
        if self.failure == "html":
            return httpx.Response(200, text="<html>not RxNorm JSON</html>")
        if request.url.path.endswith("approximateTerm.json"):
            row = DRUGS.get(request.url.params["term"].lower())
            candidates = (
                [{"rxcui": row[0], "score": "100", "rank": "1", "source": "RXNORM"}] if row else []
            )
            return httpx.Response(200, json={"approximateGroup": {"candidate": candidates}})
        id = request.url.path.split("/")[-2]
        row = next(row for row in DRUGS.values() if row[0] == id)
        if request.url.path.endswith("properties.json"):
            return httpx.Response(
                200,
                json={
                    "properties": {
                        "rxcui": id,
                        "name": row[1],
                        "tty": "BN" if id in {"1001", "1002"} else "IN",
                    }
                },
            )
        assert request.url.path.endswith("related.json")
        ingredients = (self.generic,) if self.generic else row[2]
        groups = [
            {"tty": "IN", "conceptProperties": [{"name": n} for n in ingredients]},
            {"tty": "BN", "conceptProperties": [{"name": row[1]}]},
            {
                "tty": "SCDC",
                "conceptProperties": [{"name": f"{ingredients[0]} {s}"} for s in row[3]],
            },
        ]
        return httpx.Response(200, json={"relatedGroup": {"conceptGroup": groups}})
