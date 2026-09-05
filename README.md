# Sanad · سند

Sanad carries a doctor's plan after the visit: natural Telegram conversations become confirmed patient records and timed missions; patients receive follow-up and practical help; one doctor liaison reports danger, completion and unresolved deadlines.

**Status: slices 00 and 01 accepted (2026-09-06); slice 02 implemented and awaiting review.** The package includes typed domain transitions and conditional memory/DynamoDB storage. The full care workflow and deployment remain planned. This is the single active project folder.

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

`sanad.domain` also exposes typed missions, independent follow-up tasks and review obligations, creation factories, objective specifications, timing resolution and pure transition functions. Transitions return a new aggregate and inert effects or a typed rejection; documented replays return the original object without effects. Explicit doctor instants survive exactly, inferred timing retains its reason, and a supplied timing policy controls defaults and accountability intervals. Medication START and its independent day-three response produce separate completion intents; corrections preserve history and create review work. The shipped `draft-2026-09` policy is an operational draft. The layer performs no storage or delivery, and callers remain responsible for authenticated scope, current authority and deterministic evidence evaluation. See [contract 01](docs/contracts/01-domain-transitions-and-deadlines.md#report) for tests and conservative edge-case choices.

Resolved versions are pinned in `pyproject.toml` and `uv.lock`: FastAPI 0.141.1, Pydantic 2.13.5, Uvicorn 0.52.4, tzdata 2026.3, boto3 1.43.89; pytest 9.1.1, pytest-socket 0.8.1, HTTPX 0.28.1, Ruff 0.16.6, mypy 1.20.2 and Hatchling 1.32.0. The lock was generated with uv 0.12.7. Model providers and live cloud integrations belong to later contracts.

## Store

`sanad.store.protocol.Store` has a dictionary implementation, `sanad.store.memory.MemoryStore()`, and `sanad.store.dynamodb.DynamoStore(client, table_name)`. The DynamoDB adapter receives its client explicitly. `ensure_table(client, table_name)` creates the table and its due-work, unresolved-review and patient-board indexes. Constructing an adapter performs no provisioning or credential lookup.

Commands atomically persist versioned records, immutable audit events, outbound intents and uniqueness markers. `CommitRequest.expected` contains the **target write versions**: version 1 requires absence; version 2 requires stored version 1. `command.expected_versions` instead names current versions used as read guards. A duplicate command with the same canonical payload returns its original accepted result; a different payload returns a typed conflict. Oversized batches are rejected before submission.

All reads require a trusted doctor/patient/intake scope. The patient profile is only a storage stub. A profile that has been leased requires its current fence for subsequent commands; claimed work is committed with `command.work_claim`. Session writes require that same live patient fence and a version check. Receipt completion is committed with its processing claim. The store preserves delivery attempt-start and reports an expired started attempt as uncertain. These are storage operations; current authentication, consent and send-time clinical eligibility belong to the later services.

`to_record` and `from_record` preserve the accepted domain models and operational storage values. Model bodies are JSON strings inside DynamoDB items so JSON numbers remain lossless; callers receive a dictionary in `StoredRecord.body`. Keys use only opaque identifiers, with delimiter escaping. Patient names appear only in the explicitly specified, doctor-scoped board index.

Due queries require an internal `WorkerCapability` and return hints. Re-read the base record using each hint's resolved scope before acting. The global index's continuation keys remain in a bounded cache in the store instance; restart or cursor eviction requires restarting that query. Other paginated lists bind their cursor to the exact query. Reconciliation repairs derived projections under a version check and reports missing canonical clocks without inventing a deadline.

Run both backends against exactly the same parity tests:

```sh
make install test test-ddb lint typecheck build
```

`make test` runs the memory suite without starting Java. `make test-ddb` explicitly runs memory and DynamoDB Local and fails if Local is unavailable. The fixture starts Java from `.tools/amazon-corretto-21.jdk/Contents/Home/bin/java` with `.tools/dynamodb-local/DynamoDBLocal.jar` and its adjacent `DynamoDBLocal_lib`, in-memory on a free port, with telemetry disabled and dummy local credentials. It waits for readiness, isolates each test table, then stops the process and removes temporary files. Only numeric loopback connections are permitted; no AWS account is used. For an optional Local run that clearly skips missing tools, use `make test TEST_ARGS='tests/store --ddb'`. The `DynamoDBLocal` context manager also accepts explicit Java and jar paths.

## Prior work

The original Sanad was developed in August 2026 for Google's All Things Agentic hackathon. This project is a fresh Strands/AWS build with planned selective reuse of SDK-independent modules and tests. No product code has been copied yet. Actual source commits, licenses, reused modules/assets and new work are inventoried before publication; see [sources and contest requirements](docs/research/hackathon-and-sources.md).
