# Sanad · سند

Sanad carries a doctor's plan after the visit: natural Telegram conversations become confirmed patient records and timed missions; patients receive follow-up and practical help; one doctor liaison reports danger, completion and unresolved deadlines.

**Status: slices 00 to 04 accepted (2026-09-06); slice 05 implemented for review: verified Telegram ingress, doctor applications and approval, and a send adapter. Safety and account wording await owner review.** The package includes typed domain transitions, conditional memory/DynamoDB storage, fenced processing, recovery sweeps, a deterministic safety kernel and delivery through captured and Telegram transports, with synthetic local verification. The full care workflow and deployment remain planned. This is the single active project folder.

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

All reads require a trusted account/doctor/patient/intake scope. The patient profile contains operational authority facts; doctor approval snapshots now come from the account service. Patient enrollment and browser authentication remain in slice 06; order-head and patient fixtures remain synthetic. A profile that has been leased requires its current fence for subsequent ordinary commands; claimed work is committed with `command.work_claim`. Session writes require that same live patient fence and a version check. Receipt completion is committed with its processing claim. The store preserves delivery attempt-start and reports an expired started attempt as uncertain. The Steward checks current authority facts, and the dispatcher checks clinical eligibility immediately before sending.

`to_record` and `from_record` preserve the accepted domain models and operational storage values. Model bodies are JSON strings inside DynamoDB items so JSON numbers remain lossless; callers receive a dictionary in `StoredRecord.body`. Keys use only opaque identifiers, with delimiter escaping. Patient names appear only in the explicitly specified, doctor-scoped board index.

Due queries require an internal `WorkerCapability` and return hints. Re-read the base record using each hint's resolved scope before acting. The global index's continuation keys remain in a bounded cache in the store instance; restart or cursor eviction requires restarting that query. Other paginated lists bind their cursor to the exact query. Reconciliation repairs derived projections under a version check and reports missing canonical clocks without inventing a deadline.

Run both backends against exactly the same parity tests:

```sh
make install test test-ddb lint typecheck build
```

`make test` runs the memory suite without starting Java. `make test-ddb` explicitly runs memory and DynamoDB Local and fails if Local is unavailable. The fixture starts Java from `.tools/amazon-corretto-21.jdk/Contents/Home/bin/java` with `.tools/dynamodb-local/DynamoDBLocal.jar` and its adjacent `DynamoDBLocal_lib`, in-memory on a free port, with telemetry disabled and dummy local credentials. It waits for readiness, isolates each test table, then stops the process and removes temporary files. Only numeric loopback connections are permitted; no AWS account is used. For an optional Local run that clearly skips missing tools, use `make test TEST_ARGS='tests/store --ddb'`. The `DynamoDBLocal` context manager also accepts explicit Java and jar paths.

## Steward and recovery

`sanad.steward.service.Steward(store, clock, policy_provider)` handles the released commands: confirmation, extension/reopen, cancellation/unsuccessful closure, review acknowledgment/resolution, patient replies, evaluated objective reports, evidence corrections and patient contact preferences. It acquires a patient lease, strongly reads current truth, applies the domain transitions and commits aggregates, audit, due projections, reviews and outbound intents together. The policy provider returns `StewardPolicy(timing, operations)`; all work uses the injected clock. A passed escalation receives its own `DeadlineReached` commit before an ordinary mission event. Replays return the stored outcome; old fences, claim generations and source versions cannot write.

`InboundProcessor.accept` requires server-established scope, principal and source chat. A caller may acknowledge input only after a durable receipt returns. `process_inbound` claims that receipt and completes it in the same transaction as the accepted effects. A crash before completion leaves reclaimable work. Exceptions schedule bounded retries; exhaustion moves the receipt to `needs_attention` with an independently timed review. The receipt retains its own accountability clock; later wakes do not reprocess exhausted input. `UrgentService.raise_incident` accepts already-classified facts and a supplied safety-template ID. Its separate transaction deduplicates the source, increments the safety epoch and records the incident, response review and two intents without waiting for the ordinary lease. Safety screening and clinical template content belong to slice 04.

`Dispatcher` uses the channel-neutral `Transport` protocol. The included `CapturedTransport` records synthetic sends and supports scripted acceptance, timeout and failure. Every attempt persists before network work. Current recipient authority, relevant consent/epochs, source/order versions, fulfillment validity and deadline generation determine eligibility. DANGER bypasses routine consent, freshness and chase-budget rules while retaining recipient authorization. Provider acceptance records a message ID; it never claims human reading. An interrupted or timed-out attempt is uncertain. Routine uncertainty goes to timed delivery review; DANGER permits one resend of the same incident. Chase reservations survive uncertainty and are released only with proof that the attempt was unsent.

`Sweeper.sweep(lane, shard, now, SweepBudget(...))` pages due work with a scoped worker capability, re-reads each base record and obeys item/time budgets. Missed ticks handle each deadline generation once and suppress obsolete queued output. Bookkeeping transitions re-arm mission, follow-up and review clocks without inventing contact, completion or material changes. `reconcile(scope, cursor, limit)` repairs derived projections only. Pending work remains durable when a worker is busy or a budget ends. Tests use `tests/harness.py`'s `FakeClock` and `crash_after` to exercise these boundaries on memory and DynamoDB Local; `make test-ddb` includes the processing and delivery scenarios. Scheduled prompt planning, `PromptAccepted` and chase ladders remain in slice 11.

## Safety kernel

`sanad.safety` screens the full permitted text or caption before conversational limits, ordinary locks or generation. It applies copied Arabic/Franco/English phrase and concept rules, BP thresholds and lab rules without a model. Verdicts are frozen Pydantic values with explicit policy versions and source spans where available. Unknown analytes, units, values, cutoffs and missing protocols never establish a normal result. A normal table result means no applicable critical threshold was crossed; it is not clinical clearance or a diagnosis. Unprocessed media content belongs to the caller's `media_failure` route.

Use the public policy-explicit functions, including `screen_text`, `grade_bp`, `find_bp`, `grade_lab`, `validate_patient_output`, `wants_treatment_change` and `render_urgent`. `Quantity` preserves raw values and units; `LabCandidate` supplies the printed flag/cutoff and `grade_lab` accepts an explicit baseline or abdominal-pain second fact. `OutputContext` supplies active order references/drug names, explicitly allowed number/unit strings, mode and truthful notification status. Every generated sentence is checked for unsupported reassurance, medicines and clinical numbers, dose/frequency imperatives, treatment changes, emergency numbers and doctor-awareness claims.

Urgent templates cover emergency directions, doctor danger reports, unreadable-media resend and safety acknowledgment in Arabic/English and m/f/u grammatical forms. Their fields are checked at import and at render. Patient templates contain no drug or dose and make no doctor-delivery claim. `to_incident_facts(verdict, source=..., policy=...)` returns frozen `IncidentFacts` and severity; pass `facts.unique_source_key`, `facts.as_payload()` and severity to slice 03's `UrgentService.raise_incident`. This conversion and source deduplication are exercised against the real in-memory Steward.

`SAFETY_POLICY_V1_CARDIOLOGY_DRAFT` retains Mohamed's original approval date and adult cardiology cohort while explicitly marking **Sanad v2 clinical re-approval pending**. The [reuse inventory](src/sanad/safety/_provenance.py) records copied paths, commit, date, modifications, source SHA-256 and 110 exact constant fingerprints. Copied module functions retain historical compatibility behavior; new callers use `sanad.safety`, whose stronger boundary rules are documented in the [slice 04 report](docs/contracts/04-safety-kernel.md#report). Narrow legacy style/type exemptions preserve copied source/test bodies; the new APIs and tests retain strict checks.

## Telegram channel and accounts

`TelegramSettings.from_env()` explicitly loads these environment names; the app factory never reads `.env`:

| Name | Purpose |
|---|---|
| `SANAD_TELEGRAM_BOT_ID` | Decimal bot user ID |
| `TELEGRAM_BOT_TOKEN_SANAD_STRANDS` | Bot credential, held as `SecretStr` |
| `SANAD_TELEGRAM_WEBHOOK_SECRET` | Secret echoed by Telegram in `X-Telegram-Bot-Api-Secret-Token` |
| `SANAD_ADMIN_TELEGRAM_USER_ID` | The single configured decimal administrator ID |
| `SANAD_TELEGRAM_API_BASE` | Optional; defaults to `https://api.telegram.org`, overrides allowed only in explicit test mode |

Pass validated settings and a persistent store to `create_app(telegram_settings=..., store=...)`. The factory mounts `POST /tg` and keeps `GET /health` available without configuration. An unconfigured webhook returns 503; a wrong or missing secret returns an empty 401. Invalid JSON or oversized permitted text returns 400. Bot senders, non-private chats, missing senders, mismatched private recipients and updates naming another bot receive a counted 200 drop. No account or patient data is stored for those dropped updates.

The webhook screens the full permitted text/caption, resolves the numeric sender's current identity and persists a recoverable receipt before returning 200. Processing runs separately after acknowledgment and is also callable as `route_receipt(runtime, receipt_key)` for recovery. An unfinished duplicate joins that work; a completed duplicate does nothing, even if approval changed the sender's scope. Background processing is a latency aid; persisted ingress clocks and claims allow a worker to recover after restart. Deployment and tick composition remain in 07.

An unknown sender's first text or bare `/start` creates one application. Display names, usernames and forwarded claims confer no role. The admin receives opaque, hashed, expiring, single-use approve/reject actions. Repeated applications are capped at one acknowledgment per 24 hours, and pending repeats do not notify the admin again. Rejection followed by `/start` starts a new pending version. The typed account service also supports suspension and reinstatement: each changes the authorization epoch atomically with coverage ownership. Old routine guidance remains invalid; a verified suspended doctor still receives DANGER under the explicit safety exception. Account administration grants no chart access.

Bound patient danger goes through the existing urgent Steward before ordinary processing. Other senders receive only the general emergency template. Patient text is explicitly deferred without guessing a mission; patient media creates timed unresolved work. Doctor text/media gets a receipt acknowledgment describing the unavailable capability. No media is downloaded and no model is called. These bounded behaviors do not implement patient enrollment, dictation or clinical conversation.

`Dispatcher` validates account recipients, source versions, application state, current admin configuration and applicable authorization epochs immediately before sending. `TelegramTransport` sends plain text with optional inline buttons and records provider acceptance separately from uncertainty or failure. It honors retry guidance, redacts URL string/repr logging, and implements callback acknowledgment. Application startup performs no provider request. Local tests inject `CapturedTransport`, `httpx.MockTransport` or ASGI transport; only the existing DynamoDB Local fixture opens numeric loopback connections.

The ops-only `register_webhook(settings, public_url, http=...)` calls `setWebhook` with the configured secret, `allowed_updates=["message", "callback_query"]` and `drop_pending_updates=False`. Slice 07 will invoke it against the authorized new deployment. It is never invoked by tests, startup or Make targets. Account wording review and admin ID configuration remain owner gates; slice 06 must recheck `auth_epoch` on every browser request and exchange.

## Prior work

The original Sanad was developed in August 2026 for Google's All Things Agentic hackathon. Slice 04 copies five safety modules and four test files from the frozen Google source at `b65f569`, splitting its normalizer into a sixth module. The [typed reuse inventory](src/sanad/safety/_provenance.py) and [slice report](docs/contracts/04-safety-kernel.md#report) distinguish copied tables and tests from the new policy boundary. The source project remains frozen. Publication and clinical approval are separate gates; see [sources and contest requirements](docs/research/hackathon-and-sources.md).
