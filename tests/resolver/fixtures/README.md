# Offline HTTP fixture provenance

These response bodies are **authored synthetic fixtures**, captured by the tests'
`httpx.MockTransport`; they were not downloaded from OSM. No real venue, source
capture date, price, stock, phone or availability is claimed. Fictional identifiers,
coordinates and names are deliberate. The released no-network rule prevents a
fresh live capture in this slice.

The adapter's exact source endpoints are:

- `https://nominatim.openstreetmap.org/search?q=Synthetic+Quarter%2C+Cairo&format=jsonv2&limit=2`
- `https://overpass-api.de/api/interpreter`, GET parameter `data`:
  `[out:json][timeout:4];nwr["amenity"~"^(pharmacy|doctors|clinic|hospital|laboratory)$"](around:5000,30.1,31.6);out center tags;`

`nominatim.json` is the single-area response. `overpass.json` has three in-radius
options and a fourth outside the radius. Tests derive empty, ambiguous, malformed,
429/503, timeout, hostile-attribute and 10,000-element variants from these exact
bodies. No provider fallback may escape the captured transport.
