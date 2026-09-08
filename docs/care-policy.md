# Care missions, deadlines and communication

Version: 1.0 · Reconciled full-build specification · Awaiting the final plan audit, then release of individual coding contracts. Nothing described here is implemented.

This document specifies the full agreed product, including all seven mission types, patient education, practical barrier solving and durable follow-up. The hackathon is a checkpoint. Required product capabilities cannot be dropped to fit its date. [Decisions](decisions.md) records owner choices; [roadmap](roadmap.md) assigns implementation slices.

## The mission contract

Every confirmed doctor instruction becomes a supported typed mission or an explicit unsupported item on the same confirmation card. Unsupported work is never silently discarded and never presented as something the bot can execute. One dictation can create several missions, with explicit dependencies, while unrelated valid instructions proceed.

Each mission carries its doctor and patient owner, type, objective, exact source instruction, active order/version where applicable, requested items, completion predicate, evidence policy, confirmed time, `due_at`, `review_at`, `next_action_at` (WorkClock), timing source/reason, policy version, grace, contact permissions, execution state, fulfilment validity and review requirements. Times are timezone-aware. The confirmation card shows both the target date and the time an unfinished objective will reach the doctor.

Execution states are `proposed`, `awaiting_link`, `open`, `waiting_patient`, `blocked`, `unreachable`, `overdue`, `fulfilled`, `cancelled`, `closed_unfulfilled` and `superseded`. `pending_review` is a review status, not an execution state. The UI may label a valid fulfilled objective "done", but it must say whether clinical review is still outstanding. A failed attempt closed with a reason never becomes fulfilled.

`ReviewObligation` is a separate, independently timed object for result review, correction disposition, unresolved danger or unsuccessful-work disposition. It has an owner, source/version, reason, `review_at`, next action, acknowledgment and resolution separately. It survives any terminal mission state. Follow-up work that must survive fulfilment, including the medication day-3 check-in, is a separate durable `FollowUpTask`.

## Required mission catalogue

| Type | What the doctor supplies or confirms | What fulfils the objective | Boundaries and remaining work |
|---|---|---|---|
| TEST | Named lab panel/analytes or imaging/report request; target date; any preparation instruction actually ordered | All requested evidence meets the confirmed identity, date and completeness predicate; partial reports may jointly fulfil it when the predicate permits | Evidence collection is not clinical clearance. DONE fires at valid fulfilment and a result-review obligation remains. A requested test must not be replaced by a different test. |
| MONITOR | Metric, expected unit, slots/window, reporting instructions, coverage predicate and applicable safety policy | Accepted readings satisfy the confirmed coverage rule; report lists readings, missing slots, trend when meaningful and safety events | Duplicates do not fill extra slots. Silence does not count as a normal reading. Unsupported metrics can be collected and sent for review but cannot be declared safe by an unrelated threshold table. |
| MEDICATION | Exact doctor-issued start/stop/change instruction, including the relevant drug and complete dosing details when applicable | Patient explicitly reports carrying out the requested instruction; the report says it is self-reported | The agreed START scope is acknowledgment plus an independent day-3 check-in created at confirmation. Stop/change use their exact acknowledgment wording and only explicitly ordered additional check-ins. A reporting deadline never becomes a medication stop date. Recurring dose reminders remain a separately recorded later owner choice. |
| SEND_RECORDS | Categories such as old labs, old prescriptions, medication packaging/list, discharge papers or imaging reports; requested period and count/completeness rule | Readable, correctly associated documents or inventory meet every requested category, period and count rule | Historical dates may precede the new request. A prescription/photo received as history cannot activate or replace a current medication order. Collection creates review work where specified. |
| VISIT | Requested action and date/window: arrange, report booking, report attendance or supply a visit report | The specific requested predicate is met; a booking is not attendance and attendance is not a received report | No invented booking or external appointment promise. A pre-visit brief is a timed reporting action, not evidence that the visit occurred. |
| QUESTION | The patient question, source message and relevant active plan; this support ticket may be created by the patient lane without a doctor confirmation | Doctor supplies an answer; the answer is delivered or remains visibly pending delivery | A treatment-changing answer must also create an explicit confirmed active-order amendment before it becomes executable guidance. Patient is told the queue and escalation behavior, never promised a doctor's response. |
| TASK | A bounded doctor request with an allowed action, verifiable completion predicate and deadline | The required patient report, evidence or doctor acceptance exists | Covers additional follow-up instructions without inventing tools or clinical instructions. An unsupported action is explicitly shown as unsupported; an unclear completion rule is clarified on the existing card. |

### TASK monitoring requests before slice 13

Contract 11e addendum 3: a dictated request to measure, record or chart a metric
with a frequency and duration is one TASK, never TEST or MONITOR. Its instruction
retains the spoken request in clinical English and uses the existing `doctor_task`
patient-report predicate. An explicit “for five days” means due at the receipt
anchor plus five days; absent duration uses the TASK default. No measurement slots,
units or coverage are inferred. Slice 13 upgrades this shape to MONITOR.

For medication lists and old documents, collection does not mean medication reconciliation or clinical interpretation. For a multi-part request, receiving one readable file is insufficient unless that file meets all confirmed requirements. TEST date requirements do not apply wholesale to SEND_RECORDS.

## Deadline inference and the three clocks

Contract 11b draft (`OWNER_REVIEW_PENDING`): a type-default deadline uses the
local date of anchor plus offset, at `default_deadline_local_time="10:00"` in
the doctor's policy timezone. Computed DST gaps/folds choose the later instant.
Explicit doctor times and Scribe proposals keep their exact instants, including
seconds; the card displays only hours and minutes. Escalation remains due plus
the visible grace.

The user requested automatic deadlines. Missing timing normally produces a proposed `due_at` with a short reason on the existing confirmation card, not a separate request for the doctor to supply every date.

Precedence is the explicit doctor date/time, otherwise the Scribe's contextual proposal validated against the applicable timing policy, otherwise the mission-type default. Record source, reason and policy version. The doctor's confirmation accepts the resulting deadline. Preserve an explicit four-hour instruction exactly; never clamp it to tomorrow. Ask only when the date is ambiguous/invalid or the underlying clinical instruction cannot be executed safely without missing information.

The current proposed operational defaults are TEST 14 days, MONITOR schedule end plus one day, MEDICATION acknowledgment within three days, SEND_RECORDS three days, VISIT 30 days when no date is given, QUESTION 48 hours and TASK seven days. These are draft timing policies to approve and test, not medically validated safe waiting periods. The current inferred-only bounds are one to 180 days. An inferred objective deadline does not authorize inferred dosing, treatment duration, clinical preparation or an unapproved monitoring cadence.

`due_at` is when the objective is wanted. `review_at` is when unfinished/review work must be revisited. `escalation_at = due_at + grace` is when an unmet objective produces a DEADLINE notice. The reconciled recommendation is zero default grace for every mission type, matching the accepted behavior that an unfinished mission reaches the doctor's phone when its date passes. A doctor can explicitly choose grace on the confirmation card or in an approved policy; the card always displays the resulting escalation time. This zero-default change is part of the plan for the final audit, not a claim that the clinical approver separately approved every numeric default. A policy change never retroactively moves an existing explicit doctor instruction without a visible revision.

A result's review clock starts from its fulfilment event; a draft's intake-review time is not reused as if it were the review deadline for a later result. The proposed result-review interval is three days, subject to the approved policy and risk context. QUESTION uses a 48-hour default offset snapped to the policy local clock on that date, with zero grace (11b draft, OWNER_REVIEW_PENDING). Pausing contact does not pause `due_at`, review work or safety incidents; a pause has `resume_at`, bounded by the approved policy.

Every nonterminal mission, pending claim, open review obligation, outstanding check-in and owed message has a next action or an explicit timed exception. Late enrollment cannot silently shift an explicit deadline. A patient who never scans the QR still appears in unfinished-work review and eventually reaches the doctor through the deadline policy.

## Medication follow-up and changes to the active plan

Medication acknowledgments are patient self-reports. A report of stopping or changing a medicine fulfills only the matching current doctor-approved STOP or CHANGE objective; it cannot change the order. Ambiguous drug matches use a patient choice, and a report without a matching instruction is relayed as a question. Doctor DONE notices and the patient plan identify these reports as self-reported.

Start-date clarification is retained for 24 hours under the draft policy. A reply naming today, yesterday, the day before yesterday, a weekday, an ISO date or a number of days ago is interpreted in the patient's timezone; a weekday means its most recent occurrence, including today. Dates within seven days anchor the existing check-in. An older date retains the report with an unknown anchor and leaves the unmet-objective review unresolved. Future or ambiguous dates ask again. An expired date question cannot complete a start; a new start report begins again. Already prompted check-ins keep their original prompt time. These values remain `OWNER_REVIEW_PENDING`.

A reported start barrier is classified deterministically as cost, availability, forgetting, confusion, side-effect experience or another explicit inability. It blocks the matching unfinished START mission and pauses routine contact for one day under the draft policy, preserving the clinical deadline. A later start report resolves the barrier; the first deadline notice includes the patient's words. A barrier reported at the day-three check-in is retained on that follow-up's answer and DONE notice without reopening its fulfilled parent. Requests to change treatment also enter the existing question route. Danger follows the accepted safety path. This records the difficulty and promises no solution; practical resolution remains the Resolver's work.

Doctor confirmation of a START order creates a `MEDICATION_DAY3` FollowUpTask immediately, with its own state and clock, awaiting the effective-start anchor when unknown. START acknowledgment fulfills the report objective and anchors/preserves this existing task. An explicit doctor date can anchor it at confirmation. STOP/CHANGE acknowledgments create no automatic day-three task; an explicit doctor-requested follow-up is a CLINICAL_CHECKIN. It remains scheduled after the parent mission is fulfilled. The confirmation card specifies the check-in's anchor, ordinarily the reported effective start/change time, or the doctor-specified date. An ambiguous actual start date prompts clarification; code does not pretend confirmation time proves the patient started.

The day-3 check-in reports experience and barriers under the safety kernel. A valid answer fulfills the check-in and sends the doctor a second DONE for that order (the first was the start report); a dangerous answer sends DANGER instead. It does not diagnose a side effect or recommend changing treatment. Missing acknowledgment still reaches the doctor by its deadline. Lack of a check-in response remains visible with its own deadline/review outcome. The check-in cannot extend a prescription or imply verified adherence.

Keep active structured orders separate from historical addenda. An amendment identifies the superseded order, new version, exact doctor instruction, effective time and related missions. Confirmation invalidates or recomputes affected future prompts and check-ins; the outbox revalidates order version immediately before sending. A stopped/superseded order must not produce stale "continue" or "start" guidance. Updating history alone does not change an active order. A cancelled check-in does not erase the medication history or any unresolved incident.

A confirmed medication STOP or CHANGE supersedes unfinished medication work for the previous version, cancels its unfulfilled day-three task and resolves only its unmet-objective review in the confirmation transaction. Queued routine prompts for that version are suppressed on the next ordinary Steward command or sweep. Dispatch refuses an inactive order in the interval before that cleanup. History-only changes do not trigger supersession, and other medicines and unscoped prompts do not match an order-scoped suppression.

## Patient consent, reminders and practical support

Clinical disclosure and contact begin only after the claim/consent/binding process in [safety and security](safety-security.md). Contact preferences are distinct from clinical instructions.

Three budgets apply: chase contacts share one patient-wide reservation per local calendar day across all missions; scheduled prompts follow the doctor-confirmed and patient-consented schedule; urgent safety responses have no routine contact budget. Twice-daily monitoring therefore produces two scheduled prompts. Reserve chase capacity atomically and release it only when it is known the send did not leave. Retries of uncertain sends must not silently create extra contact allowances.

Quiet hours defer ordinary chase messages. A scheduled prompt in quiet hours is enabled only after explicit consent to that slot. If consent is absent, disable that reminder slot and create a visible timed review item; do not move a clinical measurement or medication time to fit a contact preference. Emergency guidance and eligible alerts bypass routine quiet hours. After an outage, recompute from current state and summarize missed routine work rather than sending a backlog burst.

Patient "stop" ends routine contact immediately, revokes future routine sends and acknowledges the preference. The clinical order remains until the doctor changes it. Contact status and review obligations stay visible; no response means neither refusal nor verified nonadherence. An unsolicited restart requires explicit renewed consent. A new patient-initiated message still receives appropriate safety screening and a response within the approved policy.

The Concierge offers plan explanations and useful general education. The Resolver addresses cost, access, transport, upload problems, availability and forgetting with bounded questions and verified practical options. It cannot substitute a drug/test, quote unverified prices, promise a booking or change a clinical deadline. If it cannot solve the barrier, the barrier and attempted steps remain in the mission; the doctor sees it on a permitted report or when requesting status. Barrier solving is required even if the chosen places provider needs replacing with reviewed clinic resources.

## One doctor communication path

Only the Liaison's controlled delivery gateway sends unsolicited doctor messages. The Steward determines eligibility and supplies facts. If the model is slow or unavailable, the same gateway uses deterministic templates; safety delivery never waits for generated prose. Direct replies to doctor actions, account approval and claim confirmation are requested interactions, not extra proactive patient-status classes.

| Class | Trigger and required content |
|---|---|
| DANGER | Qualifying safety incident, immediately. Include observed/reported facts, uncertainty, patient identity when confirmed (otherwise the doctor-private unassigned intake reference), source/time, delivery status and action/acknowledgment link. A critical fulfilled result produces one urgent report carrying both facts rather than a redundant DONE push. |
| DONE | Valid objective fulfilment, once per objective/version. State exactly what was received or self-reported and whether clinical review remains outstanding. It does not wait for clinical review. |
| DEADLINE | Unmet objective at its escalation time; overdue question/review/follow-up work; approved pre-visit and unresolved-work reminder subtypes. State what is missing, what was attempted and the action needed. |

Routine progress stays in the dashboard/timeline. All messages distinguish `queued`, `sending`, `provider_accepted`, `uncertain` and `failed`; doctor acknowledgment is a separate event. A provider-accepted send does not prove the doctor read it. Patient-facing urgent guidance does not wait for doctor acknowledgment, and it cannot claim the doctor knows when delivery failed or is uncertain.

A DEADLINE rings once. Its durable card remains until Review, Extend, Close, Cancel or Answer resolves the underlying obligation. "Seen" only records acknowledgment. Unresolved cards produce one doctor-level bundle seven days after the first deadline report and weekly thereafter, through the same DEADLINE gateway. Material change may re-ring the affected card under policy; routine duplicate evidence does not. The doctor-pulled digest lists unresolved work first. Review cards cannot disappear because their parent mission was cancelled or fulfilled.

## Evidence, correction and closure

Accepted evidence retains its source, patient association, date, units, extraction uncertainty and version. The domain timing contract distinguishes the predicate-completing receipt time from later verification time: unverified on-time input is reported as verification pending, and once accepted its timeliness is based on the required source receipts, not model processing latency. A printed name match is an association check, not proof of the sender's identity. Unknown units, conflicting dates or two equally matching missions require clarification. Danger screening happens independently even when attachment/fulfilment is refused.

An explicit correction supersedes the affected evidence and sets `fulfillment_validity = invalidated_pending_review` if it undermines prior fulfilment. It creates timed review work and keeps the previous reported fulfilment in history. It does not silently reopen patient contact; the doctor explicitly reopens/revises the work. A late new upload is screened, stored and offered for association without automatically becoming a correction. A material correction to a prior DONE report uses the same Liaison gateway as a clearly labelled updated report, or DANGER when applicable; it is never represented as another newly fulfilled objective.

The doctor can fulfil review, request evidence, amend an order, extend a deadline, cancel, or close an unsuccessful attempt with a reason. Closing an attempt without evidence cannot assert success. An unresolved safety incident still needs explicit disposition even if the mission is closed or cancelled.

## Full-build acceptance evidence

Required evidence includes all seven mission types; simultaneous missions sharing one patient budget; four-hour and inferred deadlines; old records with historical dates and multiple requested categories; early medication acknowledgment followed by day-3 work; order amendment before a queued send; patient stop; delayed/unclaimed binding; corrected and late evidence; terminal mission with unresolved review; weekly doctor bundles; and crash/restart recovery. [Verification](verification.md) turns these policies into acceptance scenarios. A successful demo path alone does not establish full completion.
