# How Sanad works

Sanad follows a doctor's plan between visits. The doctor speaks or types the plan, confirms it, and Sanad turns it into requests with deadlines. It then helps the patient carry them out and brings results, danger and unfinished work back to the doctor.

One design rule shapes everything below: **agents propose, code decides.** Strands agents do the language work. A deterministic component, the Steward, is the only thing that can change a record.

## The life of one message

```
 Telegram chat            Doctor or patient web page
      |                              |
      v                              v
 Webhook: verify, save receipt   Browser API: session, scope, CSRF
      \______________________________/
                     |
                     v
         Danger screen (deterministic, runs first)
            |                          |
   ordinary message             danger found
            |                          |
            v                          v
   Worker picks up the       Fixed safety guidance to the patient
   saved receipt             and an urgent report to the doctor
            |                (never waits for a model)
            v
   Strands agent on Amazon Bedrock (Nova)
   Gemini for voice, photos and cross-checks
            |
            |  typed proposal (never a write)
            v
   Steward: identity, permission, version, doctor confirmation
            |
            |  one DynamoDB transaction:
            |  record change + audit event + outgoing message
            v
   Delivery: recheck access, consent and current orders, then send
```

Every minute, EventBridge triggers a signed tick through a private relay. The tick returns to due deadlines, reminders, follow-ups, reviews and any work that needs recovery. Follow-up never depends on a chat staying open.

## The six agents

| Agent | Folder | What it proposes |
|---|---|---|
| Scribe | `src/sanad/scribe/` | A plan from the doctor's text or voice note. Two extractions run separately and are merged. Every field on the confirmation card must point back to the doctor's words, or it becomes a question. |
| Concierge | `src/sanad/concierge/` | Replies to the patient from the accepted plan and a set of doctor-reviewed explanations. |
| Coordinator | `src/sanad/coordinator/` | The wording of each follow-up request. |
| Resolver | `src/sanad/resolver/` | Practical help when something gets in the way, such as nearby places from a map search. It never books, quotes prices or swaps a medicine. |
| Evidence Reader | `src/sanad/evidence/` | What a photo or PDF says. Two independent readers must agree on at least half the rows before a card is built; otherwise nothing is recorded. |
| Liaison | `src/sanad/liaison/` | How danger, completed requests and unresolved work are reported to the doctor. |

Models: Amazon Nova Lite for reasoning and Nova Micro for classification, through Amazon Bedrock. Gemini for speech, vision and independent cross-checks. Exact model IDs are pinned in `src/sanad/models/registry.py`.

## The deterministic core

| Part | Folder | Responsibility |
|---|---|---|
| Steward | `src/sanad/steward/` | The only place that commits changes. Validates every proposal, commits atomically, fences stale workers and dispatches outgoing messages. |
| Safety | `src/sanad/safety/` | Danger screening of all readable text and captions, clinical thresholds, fixed safety wording, and validation of every sentence written for a patient. |
| Contact | `src/sanad/contact/` | When and how often to contact each patient: reminders, quiet hours, the follow-up ladder and the weekly doctor summary. |
| Monitor | `src/sanad/monitor/` | Reading schedules and coverage. A missing reading stays missing. |
| Domain | `src/sanad/domain/` | Values, clocks and allowed state changes, with no storage or network access. |
| Store | `src/sanad/store/` | Doctor-scoped persistence: DynamoDB in the cloud, an in-memory store for tests and local runs. |

## States that stay separate

- **Received, complete, reviewed.** A file arriving is not a result, and a result is not a doctor's review.
- **Start report and day-three check.** Reporting that a medicine was started never closes the separate day-three follow-up.
- **Delivered and read.** A message reaching the doctor's phone is not proof the doctor has read it. Reviews stay open until the doctor resolves them.
- **Old and new orders.** When the doctor changes an order, the earlier version stays in history and an old action cannot overwrite the new plan.

## Who can see what

- Every record, request, reading and file belongs to one doctor. A doctor sees only their own patients.
- A patient joins through an invitation, reads and accepts the consent terms, and the doctor confirms the intended person before the patient can see the plan.
- The administrator approves doctor accounts but has no access to clinical records.

## Code map

```
src/sanad/
  api/           HTTP entry points (Lambda, internal tick)
  channels/      Telegram adapter and the channel-neutral transport
  web/           Doctor dashboard, patient page, admin page, demo pages
  accounts/      Doctor applications and approval
  auth/          Invitations, consent, sign-in links, sessions
  agents/        Strands agent factory and scoped tools
  scribe/        Doctor dictation to a confirmed plan
  concierge/     Patient conversation and approved explanations
  coordinator/   Request wording
  resolver/      Practical help with barriers
  evidence/      Photos and PDFs to confirmed observations
  liaison/       Reports to the doctor
  steward/       Validation, atomic commits, recovery, delivery
  safety/        Danger screening and sentence validation
  contact/       Reminders, quiet hours, follow-up ladder, weekly summary
  monitor/       Reading schedules and coverage
  media/         Private media storage, voice and image preparation
  models/        Pinned model providers
  domain/        Values, clocks and transitions
  store/         Memory and DynamoDB persistence
  presentation/  Wording shown to doctors and patients
  ops/           Workers, sweeps, recovery
deploy/          CloudFormation stack, build, deploy, smoke checks, restore, rollback
tests/           Hermetic tests, DynamoDB Local parity, browser checks, live checks
docs/            Architecture, domain model, safety and security, operations
```

## How it is tested

- **Hermetic tests** run with a fake clock, scripted model providers, a captured transport and the in-memory store: `make test`.
- **DynamoDB parity** runs the store-facing tests again against DynamoDB Local: `make test-ddb`.
- **Browser checks** drive the real pages in Chromium: `make test-browser`.
- **Deployment smoke checks** run against the deployed stack after every deploy: model access, health, tick, webhook, tenant isolation, enrollment, sessions and cost.

For the full reference, see [architecture.md](architecture.md), [domain-model.md](domain-model.md) and [safety-security.md](safety-security.md).
