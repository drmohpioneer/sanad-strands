# Sanad · سند

Sanad carries a doctor's plan after the visit: natural Telegram conversations become confirmed patient records and timed missions; patients receive follow-up and practical help; one doctor liaison reports danger, completion and unresolved deadlines.

**Status: development with synthetic patients.** The package includes typed domain transitions, conditional memory/DynamoDB storage, fenced processing, recovery sweeps, a deterministic safety kernel, Telegram account/enrollment flows and an AWS development deployment. Doctor text, voice and photos produce editable confirmation cards in the configured worker. Visit reports, task completion and doctor question commands are implemented locally for review. The full care workflow remains in development. This is the single active project folder.

Start with the [A–Z master plan](docs/master-plan.md), then the [full roadmap](docs/roadmap.md). The [architecture](docs/architecture.md), [domain model](docs/domain-model.md), [care policy](docs/care-policy.md), [agent design](docs/agent-design.md), [security](docs/safety-security.md), [verification matrix](docs/verification.md) and [operations plan](docs/operations.md) supply the implementation detail.

Practical barriers can attach to all six care-mission kinds. The bounded Resolver
asks one clarification and can search OpenStreetMap near an area the patient
names. Options disclose that prices, stock and suitability are unknown and
nothing was booked. Unresolved attempts remain recorded with the mission and
appear in its existing deadline report. This capability has offline fixture
verification only; provider coverage and real-care readiness remain unproven.
The optional adapter reads its descriptive User-Agent from `SANAD_OSM_USER_AGENT`;
without configuration it returns the ordinary unavailable outcome. No account
key is required and no personal contact address is hard-coded.

[Current state](docs/project_state.md) records the planning/release state. The [contract 00 report](docs/contracts/00-skeleton-and-domain.md#report) records the implementation evidence and verdict. [Contract workflow](docs/contracts/README.md) explains the working loop.

## Run and test

Install [uv](https://docs.astral.sh/uv/), Make and Node.js (24 LTS), then run from the repository root:

```sh
make install
make test lint typecheck
make build
make run
```

`make install` uses `uv sync --locked` to provision the Python version in `.python-version` (3.12.14) and install `uv.lock` into `.venv`. The system Python may be a different version. The Makefile keeps uv's downloaded interpreter and package cache in ignored `.uv-python/` and `.uv-cache/` directories. The first install needs internet access for Python and packages; after caching them, `UV_OFFLINE=1 make install` also works. Tests, lint, type checking and builds run offline with the installed dependencies. No cloud credentials, bot token or environment file is required.

`make build` requires `make check-js`, which parses every `src/sanad/web/static/*.js`
asset with `node --check`. Missing Node or a syntax error fails before packaging;
the default tests also exercise both refusal cases.

For real browser regression checks, install the pinned Playwright Chromium once
with `uv run playwright install chromium`, then run `make test-browser`. This
separate browser target requires Chromium, OpenSSL and permission to launch a
browser and bind loopback HTTPS. It serves the accepted synthetic login fixture,
uses device scale factor 2, and asserts current corrected readings and the
correction controls through actual authenticated routes. Missing tools or launch
failures are errors, never skips. The normal Python golden path is
`make install test test-ddb lint typecheck build`; it includes JavaScript parsing
but does not substitute for the required browser target.

`make run` serves on `http://127.0.0.1:8000`. GET `/health` returns:

```json
{"ok":true,"service":"sanad","revision":"dev"}
```

Applications can pass a deployment revision directly with `sanad.api.app.create_app(revision="my-revision")`. The factory reads no secrets or environment configuration. Health tests use HTTPX's in-process ASGI transport. The suite blocks internet sockets and DNS before application imports and during tests; local Unix socket pairs remain available for asyncio. `make build` creates a wheel and source distribution in `dist/` using the locked build backend.

Domain values live in `sanad.domain.boundaries` and match the [canonical foundation shapes](docs/domain-model.md#exact-foundation-value-shapes-contract-00). They are frozen and reject unknown fields. Invalid input raises `pydantic.ValidationError`; identifiers stay strings, integer versions/epochs are strict, and supplied datetimes must be aware and are normalized to UTC. Missing optional metadata remains `None` in Python and is omitted from serialized output. `model_dump_json()` and `model_validate_json()` preserve reference stages, provenance and timing anchors. Constructing a principal or accepted-fact reference validates its shape only; authentication, ownership and clinical acceptance require later services. Use validated constructors for revisions; Pydantic's unchecked `model_construct()` and `model_copy(update=...)` are not input-validation interfaces.

`sanad.domain` also exposes typed missions, independent follow-up tasks and review obligations, creation factories, objective specifications, timing resolution and pure transition functions. Transitions return a new aggregate and inert effects or a typed rejection; documented replays return the original object without effects. Explicit doctor instants survive exactly, inferred timing retains its reason, and a supplied timing policy controls defaults and accountability intervals. Medication START and its independent day-three response produce separate completion intents; corrections preserve history and create review work. The shipped `draft-2026-09` policy is an operational draft. The layer performs no storage or delivery, and callers remain responsible for authenticated scope, current authority and deterministic evidence evaluation. See [contract 01](docs/contracts/01-domain-transitions-and-deadlines.md#report) for tests and conservative edge-case choices.

Resolved versions are pinned in `pyproject.toml` and `uv.lock`: FastAPI 0.141.1, Pydantic 2.13.5, Uvicorn 0.52.4, tzdata 2026.3, boto3 1.43.89, strands-agents 1.54.0; pytest 9.1.1, pytest-socket 0.8.1, HTTPX 0.28.1, Ruff 0.16.6, mypy 1.20.2 and Hatchling 1.32.0. The lock was generated with uv 0.12.7. Adding Strands preserved every previous dependency version.

## Models and media

`ModelRegistry` fixes the measured Bedrock IDs in `us-east-1`, at temperature 0:

| Role | Model ID |
| --- | --- |
| Worker; Scribe, Concierge, Coordinator, Resolver, Evidence Reader, Liaison | `us.amazon.nova-lite-v1:0` |
| Independent cross-check | `us.amazon.nova-pro-v1:0` |
| Classification only | `us.amazon.nova-micro-v1:0` |
| Vision, first and second reader | Nova Lite and Nova Pro above |
| Speech | `mistral.voxtral-small-24b-2507` |

The registry records Nova 2 Lite's dropped instruction and Voxtral Mini's misheard number as reasons for rejection. `make_agent` constructs a fresh Strands agent with server-bound scope, explicit tool allow-lists, a six-call/20-second guard and a 25-second model ceiling. Tools recheck scope in their bodies. `propose` requests plain JSON using a field description, validates it with Pydantic and returns a complete candidate or a typed failure without a validation retry loop. Valid span claims become source provenance; malformed entries are dropped, and absent, repeated or out-of-bounds claims stay unsupported. Callers can disable span requests with `want_spans=False`; Scribe does so and retains receipt, transcript, model and prompt provenance without field spans. Patient fields must be declared by the caller; their sentences pass the existing safety validator and selected-language gate after reasoning text is removed. Conversation memory uses bounded `SessionSnapshot` data and the caller's patient lease; an outdated fence discards the output.

Speech uses one user message containing mp3 audio and the versioned Egyptian-verbatim instruction, followed by a requested `NUMBERS:` line. The speech vocabulary includes the dictionary's English drug, test and finding names. The adapter splits at the last marker even when inline, and retains `numbers`, ordered numeric `heard_numbers` tokens, and `disputed_numbers` found in only one reading. Words, punctuation and newlines in the metadata are permitted; a marker with no following digit is malformed. Missing or malformed metadata disputes every transcript number; empty transcription is a typed failure. The adapter never substitutes a disputed value into the transcript. The container warms ffmpeg/ffprobe during initialization and converts ogg/opus, wav and m4a into 48 kbps mono mp3, with separate 20-second probe and 60-second conversion caps, a five-minute duration limit and a 20 MiB input cap. Number helpers retain Arabic-Indic digits and ranges. Disputed numbers remain questions on the doctor's editable card; agreement within one model reply cannot establish audio accuracy.

Vision accepts PNG/JPEG up to 8 MiB, 8,000 pixels per edge and 20 million pixels. Bounded header checks precede model calls; no Pillow dependency or local pixel decode is used. A field description requests printed values, units, reference ranges and medication fields; echoed descriptions or the obsolete example return `template_echo`. Two independent reads produce field disagreements. **Printed identity is a low-reliability hint; only the doctor's selection determines the patient.** Missing units remain `cannot_judge`, and the deterministic kernel grades lab values independently of the model's flag.

Media retrieval receives an explicit Telegram client and private S3 store. It sniffs bytes, uses scoped content hashes and server-side encryption, and persists claimed `MediaWork` through fetch and normalization. Interrupted work is recoverable; expired handles produce a timed review and resend intent. Doctor voice extraction now records a private transcript and explicitly associates the media work with the completed doctor receipt. Other extraction remains pending for its later workflow. The configured AWS worker supplies the registry, S3, Telegram-file and speech dependencies.

Default tests use `ScriptedModel` through the real Strands/Bedrock adapter, scripted speech/vision/conversion, `FakeTelegramFiles` and `FakeS3`, with internet sockets disabled. `make test-ddb` separately checks the same durable behavior against DynamoDB Local. The isolated `SANAD_LIVE=1 make live-check` entry point selects only contract 08, reserves spend before each request and refuses to overwrite existing evidence. The [accepted 08 run](docs/evidence/live-08-2026-09-07b.json) passed 15/15 checks, including conservative speech fallback with every recognized number disputed; it does not establish transcription accuracy. The separate 09a result is documented below.

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

The webhook screens the full permitted text/caption, resolves the numeric sender's current identity and persists a recoverable receipt before returning 200. An explicitly injected invoker submits the receipt reference for separate processing; the AWS runtime invokes the same Lambda asynchronously. An unfinished duplicate joins that work; a completed duplicate does nothing, even if approval changed the sender's scope. Failed hand-offs retain pending work for the authenticated sweep. Without an invoker, the receipt stays pending. The webhook uses no in-process background task.

An unknown sender's first text or bare `/start` creates one application. Display names, usernames and forwarded claims confer no role. The admin receives opaque, hashed, expiring, single-use approve/reject actions. Repeated applications are capped at one acknowledgment per 24 hours, and pending repeats do not notify the admin again. Rejection followed by `/start` starts a new pending version. The typed account service also supports suspension and reinstatement: each changes the authorization epoch atomically with coverage ownership. Old routine guidance remains invalid; a verified suspended doctor still receives DANGER under the explicit safety exception. Account administration grants no chart access.

Bound patient danger goes through the existing urgent Steward before ordinary processing. Other senders receive only the general emergency template. Patient text is explicitly deferred without guessing a mission; patient media creates timed unresolved work. Doctor text, voice, photos and image documents use the Scribe workflows described below. Patient conversation and care executors remain in their later contracts.

`Dispatcher` validates account recipients, source versions, application state, current admin configuration and applicable authorization epochs immediately before sending. `TelegramTransport` sends plain text with optional inline buttons and records provider acceptance separately from uncertainty or failure. It honors retry guidance, redacts URL string/repr logging, and implements callback acknowledgment. Application startup performs no provider request. Local tests inject `CapturedTransport`, `httpx.MockTransport` or ASGI transport; only the existing DynamoDB Local fixture opens numeric loopback connections.

The ops-only `register_webhook(settings, public_url, http=...)` calls `setWebhook` with the configured secret, `allowed_updates=["message", "callback_query"]` and `drop_pending_updates=False`. The deployment operator command invokes it against the configured stack's `/tg` endpoint. It is never invoked by startup or Make targets. Account wording review remains an owner gate; browser requests and exchanges recheck `auth_epoch`.

## Login and patient claim

`WebSettings.from_env()` reads `SANAD_PUBLIC_BASE_URL` (the HTTPS origin every link is built from) and `SANAD_TELEGRAM_BOT_USERNAME` (for the `t.me` deep link). Pass it to `create_app(web_settings=...)` alongside the Telegram settings.

An approved doctor sends `/login` in Telegram and receives a ten-minute single-use link. `GET /d/<token>` shows a neutral Continue page and never consumes the token, so link previews cannot burn it; only the same-origin `POST` consumes it, rotates the session cookie and redirects to `/a`. Every authenticated request re-reads the doctor's authorization epoch, so suspension ends the session immediately. Writes need the CSRF token from the `sanad_csrf` cookie in a form field or `X-CSRF-Token` header and an `Origin` header equal to the configured site.

A doctor creates a patient record stub and issues an invitation: an opaque token behind `<site>/p/<token>`, stored hashed, 24-hour draft expiry. The landing page shows only the doctor's name and a Telegram deep link. In Telegram the patient's `/start <token>` records a pending claim (a second scanner is refused and the first claim is kept), the consent text is presented with accept/decline buttons, and the doctor then confirms the person's identity from the encounter. Confirmation is one transaction across invitation, claim, patient, binding, the global subject index and the delivery authority row; a subject already bound anywhere fails the whole transaction without disclosing the other record. Revocation raises the binding and delivery epochs so queued routine messages are suppressed while danger alerts are unaffected. A bound patient's `/login` opens `/pl/<token>` → `/pp` with the same discipline.

Locally everything runs against the in-memory store, the FastAPI test client and captured transports; the same tests run on DynamoDB Local. Deployment smokes verify real HTTPS, the neutral Continue page, security headers and CSRF rejection. Full doctor/patient journeys and enrollment wording, including consent, remain subject to their later verification and owner-review gates.

## Doctor dictation

Voice processing uses separate caps: 10 seconds to probe audio, 40 to convert it, 30 to transcribe and 15 to extract the plan. With the 15-second inline delivery pass, these named budgets total 110 seconds within the 120-second worker limit. Speech uses a 28-second provider read timeout and extraction uses 13 seconds, each with a 2-second connection timeout and no automatic provider retries. RxNorm lookups share the extraction deadline, with a three-second cap and at most six HTTP requests per card.

The contest build targets English across doctor dictation, cards, notices, patient
conversation and reminders (Decision 023). New doctor and patient records default
to English; `/lang en` or `/lang ar` selects the doctor's language, which a patient
inherits at binding. Arabic speech, phonetics and labels remain available behind
that setting as a declared upgrade. The contest photo target is printed or typed
Latin-script documents; handwriting and Arabic images are declared upgrades, with
photo acceptance governed by the separate photo contracts.

English covers account notices, help, confirmation and edit replies, photo cards,
reading-choice buttons, intake choices, failure explanations and instruction-column
captions. These render in the doctor's current language, including after a language
switch; messages already sent keep their wording. Names and text read from a paper
retain their original script. Enrollment and consent messages retain their accepted
bilingual wording.

An approved doctor sends English text or voice. The worker searches only that
doctor's patients and returns an English confirmation card showing identity,
medications, requested work, deadlines and escalation times, history and questions.
Dates omit seconds; inferred default dates use local 10:00 (draft policy).
Explicit instants remain exact in storage. Unclear patient identity offers up to
five scoped matches; a new record is created only on confirmation.

Names have one resolver: the doctor's vocabulary, clinic vocabulary, seed names, already-fetched drug lookups, then an English proposal supported by the spoken spelling or literal source. A name never substitutes a different generic. The speech hint uses the same tables, capped at 400 names; the Scribe hint is capped at 200. Public lookup results expire after 30 days. These limits remain draft policy. Confirmation teaches only the vocabulary displayed on the card, atomically with the accepted care plan.

Each dictation has two independent extractions in parallel within the same fifteen-second budget. History and missions come from the primary reading. The second checks medication, patient and TEST fields; it never adds history, missions or free questions. Code asks about conflicting fields and retains the numeric guards. A failed extraction can retry once inside the deadline; a surviving reading can still produce the card. Single-source status adds “Heard once” only to an existing question. Two failures use the unavailable response.

The card keeps the patient, medications, required work, history, alerts and short questions in that order. Medication lines use the resolved name and heard dose, with frequency only when spoken; starting, stopping or changing adds its action. Compressed compound doses stay as heard behind their dose question. TEST lines list individual analytes, such as `BUN, creatinine, Na, K`. Each fact gets a line in spoken order, with a code-selected `ECG:`, `Echo:`, `Complaint:`, `Dx:`, `History:` or `Finding:` prefix. Findings and complaints survive extraction; unknown categories remain history. Separator-only dose differences and age units do not create questions, and generic ambiguity placeholders are omitted. Unresolved fragments appear plainly. At most one shared line explains that Arabic names were kept as heard. Unsupported numeric instructions remain blocked and omitted from the instruction line. Due messages use the existing bounded inline delivery pass, with the minute tick for recovery.

| Command | Behavior |
| --- | --- |
| `/start`, `/help` | Welcome and command help |
| `/new <name>` | Propose a new patient with that name |
| `/find <name>` | Look up a patient without writing a care plan |
| `/qr <name>` | Issue an invitation for an unambiguous patient, or ask which patient |
| `/cancel` | Discard the pending card |
| `/intake` | Reopen a private photo awaiting patient selection |
| `/lang en`, `/lang ar` | Set the doctor's language for subsequent turns |

If your dictation mentions a requested test or examination and the primary reading
omits it, Scribe retries once within the existing extraction deadline. If it is
still absent, the card asks which items were requested
and cannot be confirmed until you supply the missing request. Unrelated corrections
keep that question open. A bare label already included in another fact's terms,
such as an extra `History: ECG`, is removed from the card.

Every displayed TEST analyte must match a transcript token or an accepted alias
with at most one spelling edit. An unanchored model guess is omitted and the card
quotes the heard fragment once for clarification. That test stays blocked until
the doctor supplies its name; unrelated replies preserve the question. A corrected
test stays corrected on later replies. Echo findings keep the transcript wording:
`function 45%` is not changed to `EF 45%`.

Tap **✅ Confirm** to save the valid items together, or **❌ Cancel** to discard. While a card is open, answer its questions or send a correction by text or voice; **✏️ Edit** is an optional hint. The same card keeps its patient, unanswered fields and original 30-minute expiry, with new buttons and “Card updated from your reply”. Arabic mode retains the Arabic labels. An undisputed verified drug name keeps its verification; answering a compound-dose question also clears its associated compressed-number question. Use `/new`, name a different existing patient, or begin with “New patient” to start another card. A new-patient phrase mid-message asks whether this is a correction or a new patient before proceeding. Old, expired and used buttons cannot confirm a revision. Long cards place buttons on the last message.

Spoken changes retain both values on one line, for example
`Exforge 5/160 → Exforge HCT 10/160/25 (change)`. Code requires the stated previous
values and a matching ingredient family; it does not choose a medication substitute.
When extraction shortens the new brand, a longer brand explicitly named at the
change target survives only when that ingredient family is verified. Previous
values already verified on the change line do not produce duplicate questions.
An extra bare continue for a drug already on a start, change or stop line is
removed along with its current-medication question. A standalone continue stays.
`Forxiga (start)` without a spoken dose asks for the dose.

"Blood pressure chart, 3 times a day for 5 days" becomes one MONITOR with 15
readings, starting the next local day at 08:00, 14:00 and 20:00. The confirmation
card shows the first reading and the actual deadline. Patients receive scheduled
prompts and can send a value or a readable monitor photo. Each accepted reading
fills its nearest slot within three hours; another reading for that slot replaces
its displayed value. Readings outside those windows stay in the chart as extras.
The doctor receives a table on completion or at the deadline, including missing
slots, extras and a descriptive trend after three filled slots. Danger remains immediate.

Supported schedules cover blood pressure (mmHg), glucose (mg/dL), weight (kg) and
pulse (bpm), up to four readings per day for 30 days. Weight and pulse have no
default safety-table judgment. Unsupported requests keep the TASK form; existing
TASKs are not migrated. Schedule hours, tolerance and coverage remain draft policy
pending owner review. Duplicate history folds before size checks. An oversized card retains
every order and mission, shows six history lines and offers the rest through Edit.
Frequency and duration counts already shown in the request do not raise unassigned
number questions; actual dose disagreements remain blocked.

Patient creation, accepted facts, orders, care plan, missions and learned names commit atomically. A current medication without a recorded order can create its head on confirmation, without a day-three task. A new patient's explicitly stated previous brand/dose can support a change; an unknown existing order still requires clarification. Starting a medication creates the separate day-three follow-up; confirmation does not mean the patient has taken it.

A missing numeric dose, unclear short drug name, unsupported or disputed number, unresolved timing, or conflicting instructions remains blocked and is explained on the card. Say the dose as a number. Age and every spoken order dose count as placed numbers. A heard number missing from the extracted fields gets one short question asking which item it belongs to; valid independent items remain confirmable. This check includes the original dictation and every correction, so a replaced number can still need clarification. Spoken word numbers are not converted into digits. Malformed items are dropped with a number clarification or an ambiguity line; unknown model keys are ignored. Existing orders can be changed, stopped or continued as described below.

New-patient confirmation automatically queues a QR invitation. A dictation containing «عايز أبعت له اللينك» can request one too. The QR opens the existing single-use exchange and consent/identity-confirmation flow; it is valid for 24 hours. QR pixels are generated when sending and are not stored. Issuance and delivery are recoverable, and suspension invalidates unsent access.

Doctor browser sessions can review their learned vocabulary through `GET /api/names` (read only) and read `GET /api/patients` and `GET /api/patients/{id}` for their own records. The existing session and authorization-epoch checks apply; another doctor's identifier returns 404.

Slice 09a was accepted at `0f32caa`; it has not been deployed. The authorized [attempt-2 live check](docs/evidence/live-09a-2026-09-07b.json) passed the three original dictations with `scribe-v2`: all candidates validated, every source number was covered, no unsupported number appeared, and all order counts matched. It made exactly three provider requests at an estimated $0.00025770. The [earlier failed evidence](docs/evidence/live-09a-2026-09-07.json) is retained. See the [attempt-2 report](docs/contracts/09a-scribe-dictation-and-card.md#report-attempt-2) for tests and limits; this small extraction check does not establish speech accuracy or clinical readiness. The isolated `SANAD_LIVE=1 make live-check-09a` target reserves spend against a $0.20 cap and refuses another attempt-2 run.

## Doctor photos and amendments

**Supported photos: printed or typed documents in Latin script**, including English lab reports, typed prescriptions and discharge papers. Handwriting in any script and Arabic script inside images are not yet supported.

Send the document as an ordinary photo or image attachment, including HEIC/HEIF. Both use the same orientation, contrast and JPEG conversion path, and retain the delivered image and the version shown to the readers privately. An editable card requires two successful readers agreeing on names in at least half the rows, rounded up using the larger reading's row count. Disagreements, number checks and shifted-row blocks remain in force. If the readings fall below that threshold, both stay private and the honest reply adds «قريت الورقة قراءتين مختلفتين، مش هسجّل منها حاجة», with no proposed orders, facts or buttons.

The honest reply says «مش قادر أقرا الورقة دي بثقة. صوّرها من فوق في نور كويس، أو قول لي اللي فيها وهسجّلها» and discloses the supported scope: «المدعوم حاليًا: مستندات مطبوعة أو مكتوبة بالكمبيوتر بحروف لاتينية؛ خط اليد والكتابة العربية في الصور لسه مش مدعومين.» When either reader fails twice, its single surviving read stays private and the reply adds «قريت الورقة قراءة واحدة بس، مش هسجّل منها حاجة». Ordinary compressed photos use the same pipeline without requiring a file resend. Oversized, damaged and unsupported inputs retain their specific acquisition reason.

The current readers cannot reliably transcribe Arabic from images. Arabic fields are left blank. When an editable prescription row contains dropped Arabic, the card is followed by a private picture of the instruction column captioned «التعليمات بالعربي زي ما هي في الورقة؛ الصفوف جنبها في الكارت». The doctor reads those pixels and edits or confirms the rows; the picture never becomes an automatically accepted instruction. The 8 MiB and pixel limits still apply. HEIC conversion is covered locally; the [11d report](docs/contracts/11d-photo-reading-as-sent.md#report-attempt-4) records offline verification and the preserved earlier live failures.

A caption names the patient through the same private lookup as dictation; «روشتة» or «تحليل» supplies the document hint. A retained printed patient name appears as «الاسم المطبوع (غير مؤكد)» and never selects a patient. With no caption, an existing pending card supplies the selected patient; otherwise a photo passing the agreement gate becomes private intake with the last five patients, «مريض جديد» and «مش دلوقتي». A new patient needs a name and confirmation. Unassociated intake receives a clarification obligation after 24 hours and can be reopened with `/intake`; insufficient reader agreement still receives the fallback without buttons.

Both readers' results and provenance are retained. On a document passing the agreement gate, the card shows the remaining field disagreements; the affected item stays blocked until the doctor chooses a reading or edits the field. Tap **✏️ تعديل** and send a correction, or use `صف 1: الاسم=Potassium؛ القيمة=4.1؛ الوحدة=mmol/L` (`الجرعة` for medication). Missing lab units remain «بدون وحدة» and `cannot_judge`; printed flags and injected instructions remain observations. Unreadable, oversized or unsupported files receive a reason and a retake request.

On documents passing the agreement gate, danger detected in either reading or a corrected lab value creates an urgent incident before the card. On the honest fallback, a critical row must be readable by both successful readers; one survivor cannot establish document-derived danger. An unassigned photo creates a private intake concern and its own review obligation when that rule is met; later association preserves the earlier alert references. This safety work does not accept the proposed clinical facts or orders. Those require **✅ تمام**.

Changes and stops show the old and new instructions on the card. Confirmation retains every order version, updates the current head and invalidates queued routine guidance through the delivery epoch. An identical «continue» line says «زي ما هو» and makes no clinical change. CHANGE and STOP create medication missions without another automatic day-three follow-up; an explicitly requested check-in creates its own follow-up. A concurrently changed order makes the card stale.

The session-protected record API includes all facts with provenance and visibility, order heads and complete history, missions with review status, follow-ups, open reviews, pending cards and private media references. `GET /api/patients/{id}/media/{media_id}` returns bytes with `no-store` after checking scope and the current session; `GET /api/intake` lists that doctor's pending drafts. Foreign identifiers return 404. Slice 09b is awaiting independent review. Its single [live check](docs/evidence/live-09b-2026-09-07.json) passed three of four synthetic images; the glare image produced an extra candidate row, blocked by the disagreement and shifted-row checks. The isolated `SANAD_LIVE=1 make live-check-09b` target has consumed its one-run allowance: eight requests, estimated $0.01042076 under the $0.20 cap. See the [09b Report](docs/contracts/09b-scribe-photos-amendments-and-record-api.md#report) for the failed measurement and verification limits.

## Patient conversation

After a doctor changes or stops a medicine, patients can reply "I took the new dose" or "I stopped Atorvastatin", naming the medicine when there is more than one. "I started Atorvastatin" also acknowledges a matching change when no START instruction remains for that medicine. Sanad records what the patient reports and relays unmatched instructions to the doctor. A combined "I stopped the old medicine and started the new medicine" records both when each medicine is clear.

After a start-date question, reply with "yesterday", a weekday, `YYYY-MM-DD` or "3 days ago" within 24 hours. Reports older than seven days keep the start date uncertain. Patients can also report "I can't afford it", "not available", "I forgot" or another supported difficulty. Sanad records the barrier, preserves the deadline and pauses start-chase contact for one day; it does not promise a solution. A day-three answer keeps its own outcome. Medication reports are labeled as self-reported. Arabic phrases remain supported alongside English, and these clarification and barrier policy values await owner review.
Patients can say `booked Cardiology tomorrow`, `attended Cardiology`, or `couldn't go Cardiology` after an appointment. Booking fulfils a booking request; an attendance request waits for an attendance report. A request for the visit report still needs the document. An explicit booking day records a local visit window; a day after the doctor's deadline leaves that deadline unchanged for the doctor's decision. When several requests match, the patient chooses a visit or task with a button. `finished diary` records a task as self-reported, pending the doctor's acceptance. Arabic report phrases remain available.

Before an attendance/report visit, an eligible routine brief lists its local date and the patient's open tests and records due by the visit. It follows the existing consent, quiet-hour and contact-budget rules. An unsupported task is marked as a recorded request on the doctor's card, receives no patient chase, and retains its deadline.

Bound, consented patients can send text or voice to ask about their active plan and supported health terms. `خطتي`, `أعمل إيه` and `/plan` show the current doctor instructions and next task without a model. For other answers, the Concierge selects sentences, it does not write them. One bounded proposal selects from permitted sentences: every sentence must match a current plan line or a labeled education excerpt and pass the safety, language, source, number and length checks. Treatment changes and unanswered questions enter a timed doctor QUESTION queue; the reply makes no promise about when the doctor will answer.

Patients can report a medication start, answer an existing day-three check-in, or send readings. These remain self-reports with their original observation and voice provenance. A matching START report anchors its independent check-in; a matching CHANGE acknowledgment creates no automatic day-three task. A MONITOR completes only when every confirmed slot has an accepted reading; completion creates an independent doctor review. If two schedules request the same metric, the patient chooses which one the reading belongs to. Photos and documents have durable pending media work; evidence interpretation is described in the patient evidence section below. Danger keeps the existing deterministic path before ordinary conversation.

`وقف الرسايل` stops routine reminders while preserving the doctor's orders and access to patient-initiated answers. Snoozes last at most seven days. Resuming requires a separate consent button. Quiet-hour changes retain clinical times and identify any slot requiring its own consent. The patient page `/pp` and APIs `/api/patient/me` and `/api/patient/plan` share the active plan, next tasks, last reading, preferences and open questions.

The versioned [education set](src/sanad/concierge/education/sources.yaml) contains 17 cardiology and practical-help entries. Its [fetch ledger](src/sanad/concierge/education/sources-fetch-2026-09-07.json) records actual public URLs, titles and HTTP statuses. **Every entry is pending owner review and is enabled only in synthetic environments until signed.** Public medical sources support clinical summaries; Sanad's local product rules support invitation/photo instructions, and the existing safety policy supplies emergency wording. Retrieval is local and deterministic, with at most two entries and no runtime network lookup.

`SANAD_LIVE=1 make live-check-10` is the isolated five-request Nova Lite check. Its authorized attempt-2 allowance is recorded in `docs/evidence/live-10-2026-09-07b.json` before network access and cannot be repeated by overwriting evidence. See the [contract 10 report](docs/contracts/10-patient-onboarding-concierge-and-education.md#report-attempt-2) for measured results and remaining review gates.

## Doctor questions and task reports

`/questions` lists six open questions with patient name, one active-plan line and age. `/questions 2` shows the next page. `/answer <n> <text>` and `/close <n>` use the most recently shown page, whose numbers remain fixed for one hour. Answers must pass the patient-output validator and the 700-character reply cap. Closing records an unsuccessful closure and tells the patient to ask at the visit. A full contact stop or unreachable patient leaves delivery visibly pending to the doctor; stopping routine reminders still permits a solicited answer.

A treatment-changing answer stays private on the question. The doctor must dictate and confirm the plan amendment. A subsequent tick resolves the question and sends only a fixed notice to read the updated plan; the held clinical sentence is never sent. `plan`, `/plan` and `الخطة` open the plan without a model. A task completion notice offers **Accept ✅** or **Not enough ↩** for 30 minutes. Acceptance records the doctor's action without resolving other reviews; reopening sets a new three-day deadline and tells the patient what is still required. These timing values and wording remain pending owner review.

## Reminders and accountability

**The contact policy and all reminder wording are pending owner review.** A linked, consented patient receives up to three reminders for an unfinished test, visit, task, requested record, or medication START report. Computed reminders use 10:00 in the patient's timezone: the first eligible morning, the midpoint of a sufficiently long deadline, and the day before it. Quiet hours defer these reminders until the quiet window ends. Across all missions, at most one chase is accepted per local day, at least 24 hours after the previous one. Three unanswered chases make the mission visibly unreachable; a reply resets the unanswered sequence, while the three-chase limit remains.

Confirmed monitoring slots have their own prompts, within two hours of the slot. A reported medication start anchors the independent day-three check-in, with its existing two-day response window. A scheduled slot inside quiet hours requires separate consent; without it, a doctor review records the contact problem. Missed windows are recorded and rescheduled where appropriate, so recovery does not send a backlog of expired prompts. A stop or snooze invalidates queued routine messages. Contact never changes the doctor's deadline, escalation time, check-in time or monitoring slots.

Reminder wording can name the specific readings or document types still outstanding,
with local times and the recorded deadline. Sanad lists up to three items and the
number of additional items. One bounded Coordinator turn selects among facts
computed by the existing executors; it cannot invent a clinical sentence or move
a contact. A refused, unavailable or timed-out model uses the original reminder.
Single-choice prompts do not call a model. Wording and draft limits await owner
review; contract 16b has scripted-provider evidence only, with no live allowance.

The doctor still receives the individual deadline notice even when the patient is unlinked, unreachable or opted out. Seven days after the first accepted notice, unresolved items enter one weekly bundle for the owning doctor, up to 20 lines with an overflow count. Acknowledgment does not resolve an item. Current resolved items are omitted when sending; an empty bundle stops until new work re-arms it. Accepted deliveries are counted once and recover without resending after a restart; uncertain deliveries retain a timed review.

## Deploy

Development uses synthetic data only. `deploy/stack.yaml` declares one CloudFormation stack: the ARM CodeBuild project and ECR repository, private versioned S3 bucket, DynamoDB table with its three indexes/PITR/TTL, app Lambda and public HTTPS function URL, private tick relay and minute schedule, scoped IAM roles, 30-day logs, error/throttle alarms and a $20 monthly account budget. The template is JSON, a YAML-compatible format, so offline schema tests need no additional parser.

AWS credentials, `AWS_DEFAULT_REGION=us-east-1` and `AWS_ACCOUNT_ID` must already be configured in the process. Commands verify the account and region without printing credentials. Only the secret operator and budget-email lookup read the project `.env`; the app loads SSM through its execution role. The seven SSM parameters are managed by `ops.py`, separately from the stack.

The first deployment requires two passes. With no image, the first pass creates the build infrastructure and explicitly records application smokes as skipped. Then build, configure secrets, deploy the digest with full smokes, and register the new bot:

```sh
uv run --no-sync python -m deploy.deploy --env dev
uv run --no-sync python -m deploy.build --env dev
uv run --no-sync python -m deploy.ops secrets set --env dev
uv run --no-sync python -m deploy.deploy --env dev --image sha256:<digest-from-build>
uv run --no-sync python -m deploy.ops webhook register --env dev
```

`secrets set` reads `TELEGRAM_BOT_TOKEN_SANAD_STRANDS`, `SANAD_ADMIN_TELEGRAM_USER_ID`, `SANAD_TELEGRAM_BOT_USERNAME` and `SANAD_BUDGET_EMAIL`. It generates the webhook and tick secrets once and preserves them on reruns. Before the app exists, the URL parameter holds an explicit bootstrap sentinel; deploy replaces it with the stack output before full smoke. Changes to the other parameter versions refresh both Lambda configurations. No-op deployments keep unchanged configuration versions.

Subsequent releases use build, deploy and rollback:

```sh
uv run --no-sync python -m deploy.build --env dev
uv run --no-sync python -m deploy.deploy --env dev --image sha256:<digest-from-build>
uv run --no-sync python -m deploy.rollback --env dev
```

The build uploads an archive excluding secrets, local environments, caches and `lane/`, pins its S3 version for CodeBuild, and records the Git revision, archive hash and immutable image digest. Builder dependencies come from the lock; the final image contains only the wheel and locked runtime dependencies. Release history in `deploy/releases/dev.json` records passing and failed smokes, image/source hashes, stack revision, SSM parameter versions and both schema versions. Schema mismatches stop before updating the app. A smoke failure leaves the deployed stack in place and names the failed check; rollback selects the previous distinct digest with passing smokes. Rollback changes the image and preserves current data and SSM configuration.

The app has 3,008 MB and a 120-second timeout; the relay has a 25-second timeout. There are no reserved concurrency settings: the measured account limit is 10. The relay skips a minute on app throttling or timeout; the ingress invoker retries three times with backoff, then leaves the durable receipt for recovery. Tick HMAC verification and nonce consumption use one shared signer and DynamoDB CAS with a 600-second TTL. Tick work uses the existing scoped sweep and its 100-item/10-second budget. The budget and CloudWatch alarms provide visibility; this slice makes no model calls.

```sh
uv run --no-sync python -m deploy.ops secrets check --env dev
uv run --no-sync python -m deploy.ops webhook info --env dev
uv run --no-sync python -m deploy.ops tick fire --env dev
uv run --no-sync python -m deploy.ops logs tail --env dev
```

Stack deletion retains the versioned bucket and the seven SSM parameters. After intentionally deleting the stack, `python -m deploy.cleanup retained-bucket --env dev` removes all retained object versions, delete markers and incomplete uploads, then deletes the bucket. `python -m deploy.ops secrets delete --env dev` removes the parameters. The cleanup command refuses to empty a bucket while its stack still exists. See [operations](docs/operations.md) for live outputs and [contract 07](docs/contracts/07-aws-development-deployment.md#report) for measured evidence and review status.

## Prior work

The original Sanad was developed in August 2026 for Google's All Things Agentic hackathon. Slice 04 copies five safety modules and four test files from the frozen Google source at `b65f569`, splitting its normalizer into a sixth module. The [typed reuse inventory](src/sanad/safety/_provenance.py) and [slice report](docs/contracts/04-safety-kernel.md#report) distinguish copied tables and tests from the new policy boundary. The source project remains frozen. Publication and clinical approval are separate gates; see [sources and contest requirements](docs/research/hackathon-and-sources.md).

## Patient evidence

Contract 12 is implemented for architect review; wording and operational limits are
`OWNER_REVIEW_PENDING`. The contest scope is English across doctor dictation,
cards, notices, patient conversation and reminders (Decision 023). Supported photo
input is printed or typed documents in Latin script. Handwriting in any script,
Arabic in images, Arabic dictation and Arabic patient conversation are declared
upgrades; existing Arabic templates remain behind the recipient's language setting.
Patients can send one lab slip, report, old prescription,
medication list or device screen per photo (PNG/JPEG image attachments). The image
is acknowledged immediately and read twice. Readable values are screened by the
safety kernel and active doctor-specific alerts before any association decision.

A matching document is checked against the doctor's requested analytes, units,
dates, categories and document count. Partial reports can accumulate on the same
mission. Patients are asked which request a document belongs to when the match is
ambiguous. An unreadable, Arabic or conflicting printed name is unverifiable;
it does not prevent reading or evaluation. The doctor receives the paper's review
card with **This patient's paper ✅** and **Not this patient ❌**. Fulfilment waits
for confirmation of every required paper's identity. Rejection retains the paper,
detaches it from the open mission and asks the patient whose paper it is. A name is
a mismatch only when both readers corroborate a different Latin name. Unclear images,
multiple papers in one photo, duplicates and incomplete evidence receive explicit
responses. A device reading is retained for later slot assignment.

The doctor can use `/evidence` cards or the authenticated evidence API to associate,
accept task evidence, confirm identity or reject a document. The record API exposes old medication
lists as `medication_list_seen`, labelled "History, not a current order" for an English doctor;
current orders stay
under the doctor's separate confirmation flow. Fulfilment records the original
receipt time and creates its independent result review. A deadline notice identifies
an on-time file whose reading or identity confirmation is still pending. Confirming
later preserves the original receipt time and deadline. Critical rows raise danger
immediately, before the identity decision; confirming whose paper it is never
approves a disputed clinical value. Post-fulfilment corrections use the patient-scoped contract-19 workflow described below.

## Grounded doctor cards

Fixed doctor and patient messages use English/Arabic catalogs with matching named
placeholders. A message's locale and audience are fixed when rendering begins;
doctor and patient preferences are independent, with the contest override still
selecting English for both. Clinical values retain their exact text after the
existing security sanitisation. Catalog migration preserves each locale's current
wording, including punctuation and legacy labels. Dictation and photo layouts
keep their existing behavior. See [presentation boundaries](docs/architecture.md#presentation).

Dictated numeric separators are canonicalized before extraction in both interface languages;
the raw speech response remains private beside the canonical transcript. Medication actions,
doses, findings and requested tests carry field-level evidence. The card asks about unsupported
or ambiguous claims, preserves an unknown multiword test as one question, distinguishes
unverified medication names and excludes unsupported items from confirmation. Stored prior
orders and code-computed deadlines and monitoring schedules remain supported. The synthetic
corpus checks both rendered languages and confirmed records with scripted providers; it does
not establish speech accuracy or clinical readiness. See the [11g implementation report](docs/contracts/11g-grounded-card.md#report-attempt-3), pending architect review.

## Browser surfaces

The doctor's `/a` dashboard provides patient search, filters, stable sorting and 50-row pages. `/a/patients/{patient_id}` shows orders, care requests, monitoring slots and evidence provenance. `/a/inbox` lists patient and unassigned reviews, `/a/history` shows resolved patient and unassigned reviews, and `/a/preferences` saves language through the same durable `/lang` command. Acknowledge/resolve remain in Telegram until slice-17 integration is reviewed. The contest override still displays English.

The patient's `/pp` browser is read-only in this slice: medication plan, requests, medication reports, last reading, reminders and open questions. Messages continue through Telegram. An upload API exists (see below); the dashboard control for it is not built. No admin browser or new identity is added. Existing session, consent, CSRF and epoch guards remain in force.

`/demo` uses a labelled, separate static synthetic dataset without authenticated navigation or a clinical data source. The UI uses self-hosted fonts, light/dark themes and structural RTL; no npm, bundler or external assets. [Design tokens and computed contrast](docs/design-system.md) are documented. Actual browser inspection remains an open contract-18 acceptance gate; offline test success alone does not establish visual, accessibility or clinical readiness.

## Patient browser uploads

A signed-in patient can submit a single image through `POST /api/patient/uploads`.
The API returns HTTP 202 with `status: received`, an opaque staged `handle`, and
`receipt_id` only after durable receipt acceptance. Image receipt time is when
the complete bounded body arrives. Receipt is not evidence
acceptance or medical review. Existing workers read the image twice, apply the
agreement and safety rules, and record the outcome on the same patient evidence
and doctor record surfaces used for Telegram images. Dashboard upload controls
belong to slice 18.

Send original image bytes as the request body with their actual image MIME type.
Include the existing session/CSRF cookies, `Origin`, and `X-CSRF-Token`. An optional
`X-Upload-Caption` header holds standard base64 of UTF-8 caption text (at most
4,096 characters and 8,192 encoded header characters). Captions never belong in
the URL. Multipart bodies and query parameters are refused. The server derives
all ownership and identity fields from the authenticated patient session.
The actual streamed body is limited to 8 MiB, 8,000 pixels per dimension and
20 million pixels, with a 30-second transfer timeout. The declared MIME type
must match a safely decodable single-frame image. Printed/typed Latin-script
photo support remains unchanged; no new OCR language or clinical capability is
implied. Rejected images retain permitted dangerous captions for safety handling.

Uploads are privately staged with a durable owner/receipt ledger. An unfinished
stage becomes recoverable after ten minutes. Recovery attaches complete bytes
only while its saved authorization remains valid; otherwise it disposes the
unlinked object. Disposal retains a daily cleanup clock to catch late writes.
A staged handle attaches once and can be fetched repeatedly by that receipt's
workers. It is neither a download URL nor a credential. No browser storage or
provider calls bypass the normal evidence processing path.

Checkpoint 1 of 18b is implemented locally for review, with synthetic verification.
Administrator browser sessions remain unreleased checkpoint 2. Telegram is still
required by runtime setup, patient consent/binding and delivery; this change does
not establish that Telegram can yet be removed from the application.


### Doctor review inbox

Use `/inbox` (or `/inbox 2`) to list open and acknowledged reviews, five at a time,
with overdue work first. Acknowledge records receipt and leaves work unresolved.
A disposition button requests an explicit reason through `/resolve`; it records
only the review disposition. Answering a question, extending a mission, associating
evidence or restoring coverage still uses its separate guarded operation.

Inbox and normal doctor-notice buttons bind the displayed record. Changed source,
review or doctor access refuses the old offer and asks for a fresh inbox. Liaison
may order code-approved facts within a six-second, one-turn budget; a refusal uses
the existing notice. DANGER retains its independent path. Both languages use the
presentation resolver; the contest override continues to display English.

Contract 17 is implemented locally pending architect review. Rich digest timing,
packing, proposed replies, reusable answers and deferred question rings remain
follow-on work. Intake review clocks are not rearmed by this slice.


### Correcting an accepted patient record

The owning doctor's record view offers **Correct or detach** for accepted evidence
and facts, **Amend instruction** for active medication orders, and **Preview reopening**
for completed/closed work. Every correction requires a reason and preserves the
original. Detached facts remain accessible in retained history. A monitor correction
updates the current value and coverage while retaining the actual observation time.

Doctor notices expose **Correct record**, which opens current version-specific
`/correct` guidance. `/corrections PATIENT_ID` lists examples. The browser correction
section shows the before/after values, actor, reason, predicate and possible patient
exposure. **Review and validate current evidence** restores invalidated reliance only
after the server checks it. **Record doctor follow-up decision** records a separate
response; it does not message the patient. Provider-accepted instructions cannot be
unsent. Reopening always requires its separate preview and confirmation.

This local contract-19 implementation is pending architect review and rendered
browser verification; it is not accepted or clinically validated. Unassigned intake
correction and re-filing onto another patient are outside this slice.
