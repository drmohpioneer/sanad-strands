# Sanad · سند

Sanad carries a doctor's plan after the visit: natural Telegram conversations become confirmed patient records and timed missions; patients receive follow-up and practical help; one doctor liaison reports danger, completion and unresolved deadlines.

**Status: slice 00 accepted (2026-09-06): offline package, typed foundation and health endpoint.** The full care workflow and deployment remain planned. This is the single active project folder.

Start with the [A–Z master plan](docs/master-plan.md), then the [full roadmap](docs/roadmap.md). The [architecture](docs/architecture.md), [domain model](docs/domain-model.md), [care policy](docs/care-policy.md), [agent design](docs/agent-design.md), [security](docs/safety-security.md), [verification matrix](docs/verification.md) and [operations plan](docs/operations.md) supply the implementation detail.

[Current state](docs/project_state.md) records the planning/release state. The [contract 00 report](docs/contracts/00-skeleton-and-domain.md#report) records the implementation evidence and verdict. [Contract workflow](docs/contracts/README.md) explains the working loop.

## Run and test

Install [uv](https://docs.astral.sh/uv/) and Make, then run from the repository root:

```sh
make install
make test lint typecheck
make build
make run
```

`make install` uses `uv sync --locked` to provision the Python version in `.python-version` (3.12.14) and install `uv.lock` into `.venv`. The system Python may be a different version. The Makefile keeps uv's downloaded interpreter and package cache in ignored `.uv-python/` and `.uv-cache/` directories. The first install needs internet access for Python and packages; after caching them, `UV_OFFLINE=1 make install` also works. Tests, lint, type checking and builds run offline with the installed dependencies. No cloud credentials, bot token or environment file is required.

`make run` serves on `http://127.0.0.1:8000`. GET `/health` returns:

```json
{"ok":true,"service":"sanad","revision":"dev"}
```

Applications can pass a deployment revision directly with `sanad.api.app.create_app(revision="my-revision")`. The factory reads no secrets or environment configuration. Health tests use HTTPX's in-process ASGI transport. The suite blocks internet sockets and DNS before application imports and during tests; local Unix socket pairs remain available for asyncio. `make build` creates a wheel and source distribution in `dist/` using the locked build backend.

Domain values live in `sanad.domain.boundaries` and match the [canonical foundation shapes](docs/domain-model.md#exact-foundation-value-shapes-contract-00). They are frozen and reject unknown fields. Invalid input raises `pydantic.ValidationError`; identifiers stay strings, integer versions/epochs are strict, and supplied datetimes must be aware and are normalized to UTC. Missing optional metadata remains `None` in Python and is omitted from serialized output. `model_dump_json()` and `model_validate_json()` preserve reference stages, provenance and timing anchors. Constructing a principal or accepted-fact reference validates its shape only; authentication, ownership and clinical acceptance require later services. Use validated constructors for revisions; Pydantic's unchecked `model_construct()` and `model_copy(update=...)` are not input-validation interfaces.

Resolved versions are pinned in `pyproject.toml` and `uv.lock`: FastAPI 0.141.1, Pydantic 2.13.5, Uvicorn 0.52.4, tzdata 2026.3; pytest 9.1.1, pytest-socket 0.8.1, HTTPX 0.28.1, Ruff 0.16.6, mypy 1.20.2 and Hatchling 1.32.0. The lock was generated with uv 0.12.7. Provider SDKs and live integrations belong to later contracts.

## Prior work

The original Sanad was developed in August 2026 for Google's All Things Agentic hackathon. This project is a fresh Strands/AWS build with planned selective reuse of SDK-independent modules and tests. No product code has been copied yet. Actual source commits, licenses, reused modules/assets and new work are inventoried before publication; see [sources and contest requirements](docs/research/hackathon-and-sources.md).
