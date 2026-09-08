# Agent roles and execution contract

Version: 1.0 · Reconciled full-build specification · Strands with deterministic application control.

## Personal continuity without permanent autonomous processes

Each doctor and patient experiences a consistent Sanad assistant with isolated context. Reusable role definitions create bounded agent instances for an individual turn/job; no immortal process or independent medical memory is required per person. Provider clients can be reused, but mutable agent context cannot be shared across people.

Sessions preserve conversational continuity. They never own the patient record, active orders, mission state, clock or notification policy. The application supplies authenticated scope, consent, current facts and versions on every turn and protects shared sessions with the same fencing/version discipline as the patient work. Closing a conversation cannot erase outstanding mission or review work.

## Six required roles

| Role | Responsibility | Scoped reads | Typed outputs/proposal tools | Boundaries |
|---|---|---|---|---|
| Scribe | Doctor free-text/voice/prescription workflow, patient lookup and record/plan changes | Authenticated doctor's panel search; selected patient's current orders, history and requested evidence | `find_patient`, `get_record`, read-only `lookup_drug`, `propose_new_patient`, `propose_update`, `propose_missions`, read-only `Answer` | Writes are proposals shown on a confirmation card. Ambiguous patient selection requires a choice. No cross-doctor search or invented prescription. |
| Concierge | Consistent patient conversation, active-plan explanation, general education and question handling | Own approved plan, relevant mission/evidence status, preferences and reviewed education sources | `ConciergeAnswer`, patient-report/clarification proposal, QUESTION-ticket proposal, intent for Steward routing | No other patient's records, private doctor notes, diagnosis or treatment change. Every generated patient sentence passes the output gates. |
| Coordinator | Choose the next useful permitted move for a mission over time | Current mission, evidence completeness, patient response/barrier, policy and event context | `schedule_next_contact`, `request_missing_evidence`, `classify_barrier`, `escalate_barrier`, `mark_evidence_received`, `close_verified_mission`, `pause_mission` proposals | Code computes clocks, completion and eligibility. Tool names do not grant state mutation. Cannot bypass limits, invent evidence, move a clinical deadline or close review obligations. |
| Resolver | Solve practical obstacles within the doctor's plan | Current mission, patient-stated barrier, stated area where consented, verified clinic/resources results | `ask_patient`, `find_places`, proposed `reschedule_visit`, `resume_chase`, `hand_to_doctor`; `BarrierProposal` with attempted steps | No substitute drug/test, unverified price/booking, silent clinical postponement or unsolicited doctor send. A reschedule proposal still needs the applicable human confirmation. |
| Evidence Reader | Extract prescriptions, lab slips, monitor screens and old reports into typed candidates | Only the scoped file, expected request and minimal association metadata | `PhotoReading`/extraction with class, transcription, values, units, date, printed identity, regions and uncertainty | No clinical interpretation, final patient identity decision, automatic order activation or declaration of fulfilment. Unreadable/ambiguous fields remain unknown. |
| Liaison | Compose all unsolicited doctor reports and updates from accepted facts | An authorized fact bundle for one doctor: mission/evidence, uncertainty, attempted work, review status and allowed actions | `ReportDraft` for a preclassified DANGER, DONE or DEADLINE; source/number-preserving summary | Cannot choose a fourth class, add facts, change clinical instructions, send directly, or delay the deterministic safety fallback. |

The Scribe can directly answer a doctor's requested lookup and present a confirmation card through the doctor interaction channel. The single-Liaison rule applies to unsolicited patient-status messages. The outward patient response uses the Concierge/channel composition path; specialist wording is never an unvalidated second bot persona.

Scribe `lookup_drug` is bound to the doctor's vocabulary and a timed public drug
lookup. Source text and patient identity are excluded from requests. Each fact
has source-aligned term pairs and a fixed kind prefix. Code verifies the spoken
fragment and fragment-local values. Vocabulary or phonetic identity removes the
question mark; other anchored English pairs remain visible with (؟) and one
shared, nonblocking confirmation line. Unanchored or invalid pairs retain their
spoken fallback. Mixed term kinds split into separate lines. Confirmation teaches
the displayed English pairs and fallback fragments in the care-plan transaction. Test
names must resolve from their spoken forms. No free `clinical_en` string is
rendered. Redundant bare labels already carried by another fact's displayed
terms are dropped with a metadata-only count. Extraction retries a transient schema/model failure or
a request cue without any supported mission once with a fresh
agent and identical request, within the existing deadline and shared lookup
budget. RxNorm failures preserve metadata and allow the seed or doctor-confirmation
path to proceed. A still-missing request becomes one whole-card blocking issue;
an unrelated correction cannot dismiss it.

A correction retains a previous verified drug name unless disputed. Unsupported
order fields stay blocked, are omitted from order lines, and are quoted once in
the clarification section. Source frequency words remain words.

Contract 16b implements the Coordinator row's `request_missing_evidence` and
`schedule_next_contact` as wording selections only. Code supplies the exact move
set, source-bound identifiers, missing monitoring slots, remaining document
categories from the accepted predicate, visit/report requirements and deadline.
The model orders identifiers; it writes no patient sentence or time. A fresh
scoped Strands instance has no tools, one model turn, one provider attempt and a
six-second ceiling. A durable Steward attempt checkpoint prevents another call
on recovery. `pause_mission` is always refused as redundant; barrier classification,
evidence receipt and verified completion retain their deterministic executors.
Refusal audits retain a typed reason, source version and contact identity without
provider prose. The original template remains the fallback; singleton sets skip
the provider. The Coordinator never changes contact eligibility or timing.

## The Steward owns orchestration and all writes

The deterministic Steward reads the authenticated event, evaluates safety, selects the required role, validates its typed proposal, commits accepted events/state/outbox intent and schedules derived work. Agents do not call one another freely. Fixed application routing can invoke the Evidence Reader for a Scribe input or the Resolver for a Coordinator barrier; the routing itself is code.

Tools that sound mutating return command proposals. The Steward verifies principal/tenant, role, consent, patient binding, order/mission version, required confirmation, current state, evidence predicate, contact budget and policy before a command has effects. A Strands pre-tool guard is defense in depth; the same authority check runs in the tool/command boundary even if the hook is absent.

The deterministic safety path can emit an incident and approved patient guidance without any model call. The Liaison delivery gateway sends eligible urgent facts using a template if report generation has not completed. Ordinary model failure invokes the existing safe code path and creates a visible timed unresolved item where work remains. Failure cannot silently discard an instruction or make an unsupported request appear completed.

## Context and session boundaries

The runtime supplies environment, bot, doctor, patient, actor role, conversation epoch, current lease fencing token and snapshot versions. Session keys include this scope. Model arguments may narrow within it, not supply a replacement tenant. Selected-patient state is explicit and reset when ambiguous. A doctor's "same for Ahmed" cannot resolve to an arbitrary Ahmed.

Build context from active structured orders, current mission state, accepted evidence excerpts, source identifiers, permitted policy and a bounded conversation window. Superseded prescriptions and old documents appear only as labelled history. Do not inject the whole panel or an entire chart when a scoped fact bundle suffices. Patient/OCR text is an untrusted quoted data block, never a runtime instruction.

The Coordinator retains a mission conversation and the Concierge a patient conversation; Scribe/Liaison instances are per turn. Session retention is a policy, not a trigger to drop unresolved work. Fenced storage prevents an old worker from overwriting a replacement's session. Model/network calls run outside database transactions; commit and send revalidate current versions. A version conflict causes a re-read/redecision or safe pending outcome.

## Bounded execution

Each role has explicit wall-clock, tool-iteration, token and cost limits in its released contract. Initial architecture ceilings are one principal reasoning turn with at most two fixed specialist invocations, a finite tool loop and bounded media retries. A provider that repeatedly proposes refused tools stops with a typed failure; it cannot grow its own budget. Real measurements from the model/media spikes set deployable limits before the provider slice is accepted.

The Resolver's initial barrier budget is one clarification question and two places searches per barrier attempt, recorded durably. A later material patient reply can create a new versioned attempt under policy; restarting a worker does not reset the budget. A failed location provider uses reviewed practical resources/help and keeps barrier resolution in the required build. It does not remove the Resolver feature.

Each async media/model/storage operation has a named timeout. A transcription failure asks for text/resend, retains the media reference under retention policy and creates timed follow-up. Budget exhaustion cannot be treated as "no danger" or "patient has no question". Emergency text bypasses ordinary-turn waiting and never waits for a multi-call reasoning budget.

## Required behavior examples

Doctor: "Find Ahmed, add these old medicines, and change his evening tablet to the prescription in this photo." Scribe scopes search, asks if several records match, labels old medicines as history, extracts the proposed active amendment separately and shows the exact old-to-new change. Confirmation creates a new active order version and suppresses stale queued instructions.

Doctor: "Send me her old labs" without a date. Scribe creates SEND_RECORDS with a visible default/contextual deadline and reason on the same card. It does not turn every absent date into another question. The type's historical-date rule remains valid; if the doctor asked for two categories/counts, the predicate requires both.

Patient: "What is LDL?" Concierge explains from the small reviewed source set shipped with the education feature, in the patient's language. "Can I double my tablet?" is a treatment-changing question, handled by the safety/change gate and a timed doctor ticket. A danger signal follows the independent urgent path.

Patient: "The lab costs too much." The Steward routes the barrier to Resolver, which clarifies within budget and offers verified relevant resources without inventing a lower price or changing the requested test. If unresolved, the mission remains blocked with resume/deadline accountability. Only an eligible Liaison report reaches the doctor's phone.

Patient: "I started it yesterday." A valid medication acknowledgment fulfils that objective as self-report. Code separately schedules/preserves the agreed day-3 follow-up from the confirmed effective time. No agent memory is needed to remember it after the acknowledgment mission closes.

## Education is a delivered capability

Concierge owns general education; it does not require a seventh permanent agent. Its implementation slice includes a compact reviewed source manifest, scoped retrieval, Arabic examples, plan/education distinctions and validation cases. Source review is assigned within that slice, not an unowned library prerequisite. Unsupported coverage is explained without fabricated certainty and routed as appropriate. See [safety and security](safety-security.md).

## Qualification and acceptance

Before implementing the dependent live flow, the spikes demonstrate actual pinned Strands/model behavior: typed output success/rejection, guarded tool refusal, session continuation, images, Arabic/English/mixed-language voice and failure/latency behavior. Record exact model/SDK versions and measured limits. A provider's language label alone does not prove Egyptian dictation quality.

Hermetic scripted providers prove deterministic invariants; live provider checks prove integration; the full scenario tests prove the application paths; Clinical review approves content and usefulness. None substitutes for the others. Tests must prove scope containment, unconfirmed-write refusal, no specialist direct send, safety with a model down, version conflicts, useful bounded education and all required roles operating in the full journey.

Implementation ownership follows [roadmap](roadmap.md): provider/media integration in 08, Scribe in 09, Concierge/education in 10, adaptive barrier work in 16 and Liaison/review reporting in 17. Mission clocks and executors remain deterministic across 11–15. All required product slices 00–21 must pass; contest packaging in 22 is a separate checkpoint obligation.
