# Sanad

Sanad carries a doctor's plan after the visit. A doctor sends text, voice notes or prescription photos through Telegram, checks the proposed record and confirms the plan. Sanad turns those instructions into requests with deadlines, helps the patient follow them, collects results and reports, and brings danger, completed requests and unresolved work back to the doctor. The dashboard and patient page show the same saved plan, with evidence and changes kept in context.

## Why Sanad

**The pain.** A clinic visit ends with a plan: start a medicine, send a test, check blood pressure twice a day. After the patient leaves, that plan lives in scattered messages. A result that never arrives goes unnoticed, a practical obstacle quietly stops treatment, and a dangerous symptom can sit in the same queue as a routine question. Doctors cannot see what is still open, and patients have no one to ask between visits.

**How Sanad resolves it.**

- **The doctor speaks, Sanad organises.** A voice note, text, prescription photo or PDF becomes a plan on one confirmation card. Every field points back to the doctor's own words or becomes a question, and doses are never invented.
- **Every instruction is owed work.** Each request carries a deadline and a review time on a durable clock. A minute tick returns to unfinished work, so nothing depends on a chat staying open, and nothing counts as done until it is.
- **Danger comes first.** Every message is screened before the normal conversation. An emergency gets fixed safety guidance and an urgent doctor report at once, without waiting for any model.
- **Results stay honest.** Two independent readers must agree before a value is recorded; a missing reading stays missing; received, complete and reviewed stay separate states.
- **Patients get real help.** Plain-English plans, reminders outside quiet hours, answers from doctor-reviewed explanations, a day-three check after starting a medicine, and practical help when a lab is too far or a medicine is out of stock.
- **One view for the doctor.** A dashboard of who is urgent, waiting or late, with the full record, documents beside what was read, reading charts and actions from the page.

**Safe by design.**

- **Deterministic safety, not model judgement.** A fixed safety kernel screens every readable message and caption before any agent runs. Clinical thresholds and emergency wording are code, and a failed or slow model can never suppress a danger report.
- **Every patient sentence is checked.** Before anything reaches a patient, a validator confirms it is backed by the doctor's accepted plan or an approved explanation. Anything it cannot support becomes a question for the doctor.
- **The doctor stays in charge.** Sanad does not diagnose, prescribe or substitute medicines, never invents a dose, and never treats silence as adherence.
- **Privacy and consent built in.** Each doctor sees only their own patients. A patient reads and accepts the consent terms, and the doctor confirms the intended person before anything is shared. A QR code alone reveals nothing. Sign-in links are single use and short lived, browser actions are protected against forgery, and the administrator approves accounts without any access to clinical records.

**Reliable under failure.**

- **Nothing half saved.** Each change, its audit event and any outgoing message commit in one DynamoDB transaction.
- **No stale or duplicate messages.** Workers are fenced, and delivery rechecks the current consent, access and orders immediately before sending. Replayed Telegram updates are processed once.
- **History is never lost.** Corrections and changed orders keep the earlier version, and an old action cannot overwrite a newer plan.
- **Work survives restarts.** Incoming messages are saved before they are acknowledged, and every deadline, follow-up and review lives on its own durable clock.

**Proven, not promised.**

- More than 7,500 hermetic tests, about 4,700 of them repeated against DynamoDB Local, and about 400 browser checks on the real pages.
- Every deployment runs live smoke checks for model access, health, the signed minute tick, the webhook, tenant isolation, enrollment, sessions and cost.
- Models were chosen by measurement: the registry pins the selected models and records the rejected ones with their reasons.

**Agents that stay in their lane.** Six Strands agents on Amazon Nova, each with one responsibility, only its own scoped tools, a tool budget and a typed proposal as output. Their conversations sit in fenced sessions that cannot overwrite newer state. None of them can change a record: a deterministic Steward validates every proposal and commits the change, its audit event and any outgoing message in one DynamoDB transaction.

## Who it is for

Sanad is for doctors managing follow-up between visits and patients trying to carry out their instructions. Scattered messages make it easy to lose a missing result, an unanswered question or a practical obstacle. Sanad keeps each request visible until its outcome is recorded, while separating receipt of evidence, completion and medical review.

## How it works

Agents propose; code decides. Six Strands agents on Amazon Bedrock do the language work: the Scribe turns dictation into a plan, the Concierge talks with the patient, the Coordinator words each request, the Resolver helps with practical barriers, the Evidence Reader reads photos and PDFs, and the Liaison reports to the doctor. None of them can change a record. Danger screening runs before any of this, and a minute tick returns to every unfinished deadline. [How Sanad works](docs/how-it-works.md) follows one message through the system and maps the code.

## Architecture

![Sanad architecture and trust boundaries](docs/diagrams/architecture.svg)

Telegram messages enter an authenticated webhook. The Lambda application saves incoming work before acknowledging it, and workers handle the saved requests. Browser sessions restrict each doctor to their patients; patients see their own plan, and administrators see account applications. An EventBridge minute tick wakes recovery, reminders and unresolved work.

Strands agents on Amazon Bedrock use Nova for bounded reasoning. Gemini reads voice notes and photos. Their output is a proposal: deterministic checks and doctor confirmation control accepted records and instructions. DynamoDB stores records, deadlines and outgoing messages; private S3 stores media. Danger screening has a separate immediate path, and sending rechecks current access, consent and instructions. See the [architecture reference](docs/architecture.md) for details.

## Run locally

Install uv and Python 3.12. From the repository root, install the locked dependencies once, then start the demo server:

```sh
uv sync --locked
UV_OFFLINE=1 uv run --offline --no-sync python - <<'PYTHON'
import uvicorn
from pydantic import SecretStr
from sanad.api.app import create_app
from sanad.channels.telegram.settings import TelegramSettings
from sanad.channels.transport import CapturedTransport
from sanad.store.memory import MemoryStore
from sanad.web.settings import WebSettings

app = create_app(
    store=MemoryStore(),
    transport=CapturedTransport(),
    synthetic=True,
    process_receipts=False,
    consent_policy=lambda doctor_id: None,
    telegram_settings=TelegramSettings(
        bot_id="1", bot_token=SecretStr("offline"),
        webhook_secret=SecretStr("offline"), admin_user_id="2",
    ),
    web_settings=WebSettings(
        public_base_url="https://localhost:8000", bot_username="offline_bot",
    ),
)
uvicorn.run(app, host="127.0.0.1", port=8000)
PYTHON
```

Open **http://127.0.0.1:8000/demo**. Its navigation also opens the synthetic patient and administrator views. These pages use separate static fictional records, with no sign-in or cloud connection. The app uses an in-memory store and captures messages locally; the sample settings above connect to no account. Stopping the server discards in-memory state. Interactive Telegram, voice, photo and authenticated account testing uses the separately configured hosted environment.

## Try Sanad

There are four ways to see Sanad, from a quick look to full hands-on use.

1. **Watch the film.** A short technical film shows the product working end to end: https://youtu.be/57616QzepME
2. **Look at the demo pages, no sign-in.** Fictional records in the real doctor, patient and administrator pages: [doctor](https://btx35drwcqejwxymdcwfzwku5m0cynoz.lambda-url.us-east-1.on.aws/demo), [patient](https://btx35drwcqejwxymdcwfzwku5m0cynoz.lambda-url.us-east-1.on.aws/demo/patient), [administrator](https://btx35drwcqejwxymdcwfzwku5m0cynoz.lambda-url.us-east-1.on.aws/demo/admin).
3. **Use it for real on the hosted synthetic environment.** Open the doctor access link supplied privately with the contest submission. Your own Telegram account becomes an approved doctor at once. Invite a second Telegram account as your patient, then send text, a voice note and a photo from both sides, and open the doctor dashboard and patient page from the sign-in links. The [testing instructions](docs/judge-instructions.md) walk through every step. The environment holds synthetic data only; never send real patient information.
4. **Run it on your own computer.** The local setup below needs no accounts.

For the design behind these steps, read [How Sanad works](docs/how-it-works.md): the life of one message, the six agents, the deterministic core and a map of the code.

## Supported scope

This build supports English doctor text and voice, confirmation cards, printed or typed Latin-script photos, PDF documents of up to 10 pages with each page read separately, patient enrollment with consent and doctor identity confirmation, and six kinds of care request: medication reports, tests, monitoring, visits, records and tasks. Patient questions have a separate timed reply workflow. Patients can send messages, images and PDF documents, pause or resume reminders, set quiet hours and change their own reading times going forward. Doctors can review evidence, answer questions, save reusable answers, choose a question digest time, correct records and amend instructions while retaining history. On the web, doctors see their patients grouped by what needs attention, open a patient card with the latest readings and a chart, reply to questions, resolve reviews and act on missed requests from the record, and remove a patient, which stops all contact after an authenticated confirmation.

Medication follow-up records a reported start and an independent day-three check-in. It does not prove ingestion. Partial results stay incomplete, disputed readings need clarification, and completing a request does not clear its medical review. Practical-help searches can suggest places when configured, with no claim about availability, price or booking. General explanations depend on the approved source set; unavailable explanations are referred to the doctor.

Handwriting, Arabic speech and conversation, Arabic text in photos, and generated speech are not supported in this build. Sanad does not diagnose, prescribe independently, substitute medication, infer adherence from silence, share patients across doctors, or provide booking, payments, EHR integration, WhatsApp or telephone care. Recurring dose and refill reminders are outside this build.

Development and demonstrations use synthetic patients only. Real-patient activation is closed pending clinical policies, education and consent review, privacy and care-coverage arrangements. Operational tooling includes isolated restore, rollback, health reporting and patient export/deletion, but the live restore and rollback rehearsals and release record are still pending. Nine health measures lack complete instrumentation and four thresholds lack alarms. Synthetic checks do not establish clinical readiness.

## Reuse inventory

Versions below come from [pyproject.toml](pyproject.toml) and [uv.lock](uv.lock). The table includes every direct runtime, development and build library. Dependencies retain their own licenses and notices.

| Library | Version | Use | License |
|---|---|---|---|
| fastapi | 0.141.1 | HTTP application | MIT |
| pydantic | 2.13.5 | Validated data | MIT |
| uvicorn | 0.52.4 | HTTP server | BSD-3-Clause |
| tzdata | 2026.3 | Timezones | Apache-2.0; timezone data public domain |
| boto3 | 1.43.89 | AWS services | Apache-2.0 |
| httpx | 0.28.1 | HTTP clients | BSD-3-Clause |
| strands-agents | 1.54.0 | Bounded reasoning | Apache-2.0 |
| segno | 1.6.6 | Invitation QR images | BSD-3-Clause |
| pillow | 12.3.0 | Image normalization | MIT-CMU |
| pillow-heif | 1.7.0 | HEIF decoding | BSD-3-Clause source; GPLv2 binary wheels |
| pypdfium2 | 5.13.0 | PDF page rendering | BSD-3-Clause, Apache-2.0 (bundled PDFium BSD-3-Clause and permissive third-party notices) |
| arabic-reshaper | 3.0.1 | Text-layout checks | MIT |
| hatchling | 1.32.0 | Package build | MIT |
| mypy | 1.20.2 | Type checks | MIT |
| playwright | 1.60.0 | Browser checks | Apache-2.0 |
| pytest | 9.1.1 | Tests | MIT |
| pytest-socket | 0.8.1 | Offline test isolation | MIT |
| python-bidi | 0.6.11 | Text-layout checks | LGPL-3.0-or-later |
| ruff | 0.16.6 | Lint and formatting | MIT |

pypdfium2 bundles PDFium and its third-party notices for PDF page rendering.

The `pillow-heif` source is BSD-3-Clause. Its binary wheels are declared GPLv2 because of x265 and bundle LGPLv3 libheif and libde265, plus the GPL-3.0-with-GCC-exception MinGW runtime on Windows. Anyone redistributing those wheels must meet their GPLv2 and LGPLv3 source and notice terms. Sanad's MIT code places no extra restriction on that. `python-bidi` is LGPL-3.0-or-later and development-only. The indirect dependencies `certifi` (runtime) and `pathspec` (development and build, through mypy and hatchling) are MPL-2.0.

The [model registry](src/sanad/models/registry.py) contains the following selected and disabled models. Hosted models are accessed through provider APIs; no model weights are bundled or licensed by this repository.

| Model ID | Configuration | License or access terms |
|---|---|---|
| `us.amazon.nova-lite-v1:0` | Selected for reasoning; disabled for photo reading | Proprietary Amazon Nova / Bedrock service terms |
| `us.amazon.nova-micro-v1:0` | Selected for classification | Proprietary Amazon Nova / Bedrock service terms |
| `gemini-3.5-flash-lite` | Selected for independent cross-checks | Proprietary Google Gemini API terms |
| `gemini-3.8-flash` | Selected for voice and photo reading | Proprietary Google Gemini API terms |
| `gemini-2.5-flash` | Disabled | Proprietary Google Gemini API terms |
| `gemini-3-flash-preview` | Disabled | Proprietary Google Gemini API terms |
| `mistral.voxtral-small-24b-2507` | Disabled | Apache-2.0 weights; Bedrock service terms for API access |
| `whisper-large-v3-turbo` | Disabled | MIT weights; hosting service terms for API access |
| `us.amazon.nova-2-lite-v1:0` | Disabled | Proprietary Amazon Nova / Bedrock service terms |
| `mistral.voxtral-mini-3b-2507` | Disabled | Apache-2.0 weights; hosting service terms for API access |

Fonts are self-hosted and retain their SIL Open Font License 1.1 notices.

| Font | License |
|---|---|
| Inter | [SIL OFL-1.1](src/sanad/web/static/inter-OFL.txt) |
| Playfair Display | [SIL OFL-1.1](src/sanad/web/static/playfairdisplay-OFL.txt) |
| Noto Sans Arabic | [SIL OFL-1.1](src/sanad/web/static/notosansarabic-OFL.txt) |
| Noto Naskh Arabic | [SIL OFL-1.1](src/sanad/web/static/notonaskharabic-OFL.txt) |

The diagram renderer uses Mermaid CLI 11.12.0 (MIT). `docs/diagrams/render.sh` needs macOS and an existing Playwright Chromium. Run it to regenerate the SVG.

## License

Sanad's original source is licensed under the [MIT License](LICENSE). Copyright 2026 Mohamed Mostafa. Third-party software, fonts and hosted model services retain the separate terms listed above.
