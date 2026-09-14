# Sanad Domain and Store Contract

Version: 1.0 · Proposed reconciled schema for the final audit · 2026-09-05

This blueprint is implementation input after final audit; it is not executable code or a released contract. Where slice 01's accepted code refines a shape (creation factories, no-op semantics, review action table, follow-up legality, policy fields), the binding wording is the A01–A24 addendum in [contract 01](contracts/01-domain-transitions-and-deadlines.md) and the code under `src/sanad/domain/`. [architecture.md](architecture.md) owns the system flow. Contract 00 defines types only; contract 01 implements transitions/deadlines; 02–03 implement storage and recovery. Later contracts extend this schema explicitly before adding behavior. Existing `pending_review`/`done` execution terminology is superseded by the split below; DONE remains a message class.

## Shared types and invariants

All IDs are opaque strings. Telegram IDs are lossless decimal strings, not floating-point numbers. Instants are timezone-aware UTC; user-facing relative timing also records the source timezone (IANA name), original local expression and anchor instant. Never infer an omitted clinical quantity or demographic.

Every persisted record has `id`, `entity_type`, `version` (positive monotonic integer), `created_at`, `updated_at`. Clinical/operational records also carry server-established `doctor_id` and, when patient-related, `patient_id`; no model or browser selects these scope fields. Immutable version/event records are append-only. Mutable aggregates are replaced conditionally by a new version, not mutated through shared in-memory objects.

Observation is the immutable input view of an InboundReceipt: `observation_id` and `source_observation_id` reference its ID, not an undocumented second payload store. Input before doctor approval belongs to restricted account/operational scope. A doctor's as-yet-unassigned patient dictation belongs to IntakeDraft scope; patient_id is absent until an explicit record association is confirmed.

Shared values:

| Type | Fields / contract |
|---|---|
| Principal | subject: string; actor_kind: unknown/admin/doctor/patient/system; verified_roles: immutable set of role strings; permissions: immutable set of action strings; bot_id, user_id, session_id, doctor_id, patient_id: optional strings; auth_epoch: optional nonnegative integer. Only authentication/dispatch constructs an authoritative instance. Unknown actors have no clinical permissions. |
| PatientScope | doctor_id, patient_id; validated against Principal and Patient owner on every operation |
| TenantScope | doctor_id; the doctor's authorized board/intake scope; PatientScope adds patient_id and never weakens tenant authorization |
| ObservationRef | observation_id: string referencing InboundReceipt; contains no clinical acceptance flag |
| CandidateRef | candidate_id: string; version: positive integer; identifies an interpretation, never an accepted fact by its type |
| AcceptedFactRef | fact_kind: clinical_fact/evidence/care_order; fact_id: string; version: positive integer; acceptance and scope are rechecked against the authoritative record, never established by constructing this reference |
| TimingProposal | proposed_due_at, source (scribe/default), reason, timezone, anchor_time, anchor_kind, policy_version, source_observation_ref; no treatment quantity or duration authority; schema-validated before doctor confirmation |
| WorkerCapability | service_subject, permitted_lanes/actions, resolved_scope, auth_expiry, invocation_id; internal authentication plus per-record scope checks, never supplied by an agent model |
| VersionRef | entity_type, id, version; an OrderRef additionally names order_id and order_version |
| Quantity | raw_value, raw_unit, normalized_value, normalized_unit, conversion_rule_id; unknown remains null and cannot pass a unit-dependent predicate |
| Provenance | source_observation_id, actor_kind, actor_id, source_kind (doctor_statement/patient_report/document_observation/clinician_confirmed), source_span/source_region optional, observed_at optional, received_at, model_id/prompt_version/extraction_version optional, validation_rule_ids, confidence optional, confirmed_by/confirmed_at optional |
| WorkClock | next_action_at, work_lane, work_shard, work_generation, attempt_count, last_error_code optional; mandatory on every unfinished work record |
| ProcessingClaim | owner, generation, expires_at, claimed_at; generation increases on each takeover; active ownership is checked on every write |
| CommandEnvelope | command_id, Principal, PatientScope optional, expected_versions, expected_auth_epoch, expected_binding/consent/delivery/safety epochs where relevant, payload, requested_at |
| CommandResult | accepted(event_ids, resulting_versions) / needs_confirmation / stale_version / forbidden / unsupported / invalid_input; duplicate command returns the original accepted result |
| ContactSlot | slot_id, kind (chase/scheduled/urgent), requested_at, allowed_window_end, source_order_version, source_mission/followup_id, quiet_hours_exception_id optional; an expired clinical slot never means “take it now” |

### Exact foundation value shapes (contract 00)

These value objects are frozen, reject unknown fields and confer no authority by construction. Identifier strings are nonblank, versions are positive integers, epochs are nonnegative integers, and supplied datetimes are aware and normalized to UTC. Optional values default to absent; collection defaults are empty immutable tuples/sets.

- `Principal`: `subject: str`; `actor_kind: unknown|admin|doctor|patient|system`; `verified_roles: frozenset[admin|doctor|patient]`; `permissions: frozenset[str]`; optional string `bot_id,user_id,session_id,doctor_id,patient_id`; optional integer `auth_epoch`. Unknown actors have no verified roles, permissions or clinical scope. Patient actors require doctor_id and patient_id; doctor actors require doctor_id. Authorized admin+doctor roles may coexist; a patient role cannot coexist with either. System authority comes from WorkerCapability, not fabricated verified_roles.
- `TenantScope(doctor_id: str)`; `PatientScope(doctor_id: str, patient_id: str)`. The latter requires both values. Later service methods validate them against current authority.
- `VersionRef(entity_type: str, id: str, version: int)`; `ObservationRef(observation_id: str)`; `CandidateRef(candidate_id: str, version: int)`; `AcceptedFactRef(fact_kind: clinical_fact|evidence|care_order, fact_id: str, version: int)`. Cross-record existence/ownership is verified by the store, not by constructing a reference.
- `TimingProposal`: `proposed_due_at: datetime`, `source: scribe|default`, `reason: str`, `timezone: str` validated as an IANA zone, `anchor_time: datetime`, `anchor_kind: observation_received|doctor_reference_time|schedule_end|reported_effective_start|reported_effective_change`, `policy_version: str`, `source_observation_ref: ObservationRef`. Reason/policy are nonblank. This object does not resolve deadlines or contain a treatment quantity.
- `Provenance`: `source_observation_id: str`, `actor_kind` using the Principal enum, `actor_id: str`, `source_kind: doctor_statement|patient_report|document_observation|clinician_confirmed`, `received_at: datetime`; optional `observed_at: datetime`, `model_id,prompt_version,extraction_version: str`, `source_span: TextSpan|AudioSpan`, `source_region: ImageRegion`, `confidence: float` within 0–1, `confirmed_by: str`, `confirmed_at: datetime`; `validation_rule_ids: tuple[str,...]`. Confirmation fields are both present or both absent; their presence does not convert patient_report into clinician_confirmed.
- `TextSpan`: `kind="text"`, `start:int`, `end:int`, zero-based Unicode code-point offsets with 0 <= start < end.
- `AudioSpan`: `kind="audio"`, `start_ms:int`, `end_ms:int`, 0 <= start_ms < end_ms.
- `ImageRegion`: `asset_ref:str`, `coordinate_space="normalized_source"`, `x,y,width,height:float`, with x,y >= 0, width,height > 0, x+width <= 1 and y+height <= 1. The referenced immutable image defines the coordinate space; a normalized derivative must use its own asset reference.

No confidence is invented when a parser/provider supplies none. These are source references; no extraction, clinical inference, principal authentication or full persisted entity implementation belongs to contract 00.

ClinicalFact payloads and mission details are discriminated typed objects. A generic JSON bag cannot bypass validation. The approved contract for each executor defines its exact predicate/fields. The following fields are the common required blueprint, not permission to invent clinical values.

## Identity, consent and patient records

Contract 11c adds `NameMemory` in exact doctor `TenantScope` and clinic
`AccountScope(bot_id=Doctor.telegram_bot_id)`: Latin name, generic identity,
drug/test/finding kind, at most ten spoken forms, observed strengths, confirmation
count, last confirmation time and source. IDs hash the scope partition, kind and normalized Latin
name; doctor and clinic rows have distinct record identities. Doctor and clinic vocabulary updates are conditionally written alongside
the confirmed card; replay cannot increment counts twice and an interrupted
transaction cannot save the care plan without its name updates. Clinic rows
contain vocabulary only, without patient or contributing-doctor identities.

`NameCache` contains validated public RxNorm name/generic/strength results,
hashed query identity and explicit 30-day expiry in a fixed tenant-independent
account partition. Application reads enforce expiry; TTL is cleanup only.
Neither cache nor memory constitutes a clinical order. The Scribe's narrow
transaction guard permits only these named cross-partition vocabulary writes.
`GET /api/names` returns only the signed-in doctor's memory.

Dictation facts retain spoken `text` and optional proposed `name_latin`. The v8
fact categories include `finding` and `complaint`, also preserved on confirmed
ClinicalFact records. Unknown extraction categories normalize to `history` rather
than discarding the fact; all other schema and numeric validation still applies.
The code-owned prefix set includes `Finding` for a finding without ECG/Echo cues.
The Arabic v8 model schema omits legacy `clinical_en`, `clinical_kind` and `terms`
fields; these remain readable in stored records and photo corrections. The English
schema exposes `clinical_en` as a candidate, while code derives its accepted wording
from the same source-aligned resolver. Code builds the fact's
fixed prefix and aligned `terms` from the single resolver. `NameReading` holds only
that resolved display or the plain spoken fallback. An unsupported English guess
cannot be learned. Finding memory continues to strip numeric results and may
hold a doctor-confirmed Arabic fallback; drug/test memory keeps its Latin constraint.
TEST `clinical_en` is populated by code from resolved analytes, preserving its
existing stored projection. A supported repeated vital request becomes MONITOR
with a typed schedule and the `monitor` evidence predicate. Unsupported metrics
or an unparsable cadence retain the normalized TASK instruction and `doctor_task`
patient-report predicate, with receipt-relative duration or the TASK default.
Other mission text stays spoken. Existing TASK records remain unchanged.

`Proposal.language` snapshots the doctor's language for card rendering. Doctor,
Patient and presentation defaults share `domain/language.py`'s owner-review-pending
`default_language = "en"`. `/lang en|ar` accepts only an authenticated approved
doctor's own language change, version and update time, atomically with receipt,
audit and reply; it cannot change any authority field. Patient binding copies the
owning doctor's language. Existing bound patients keep their saved language.

Contract 11k adds `Proposal.source_partition`: the original canonical dictation's
end offset, plus each correction reply's start/end offsets, authorising proposal
ID/version and code-owned answer slots. All offsets are zero-based, end-exclusive
Unicode code-point offsets into the retained `source_text`. Source-matching
resolver aliases are snapshotted for later occurrence replay. The source partition
is included in the evidence fingerprint; it is never reconstructed from punctuation
or obtained by reading the live pending card. Legacy corrected proposals without
this authority fail closed.

`FieldEvidence` may additionally identify the correction's proposal ID/version.
Preparation, evidence production and revalidation use the same occurrence binder
for previous values. It represents missing target quantities and shared same-brand
name occurrences explicitly. The old value shown in a diff and an unchanged field
retained in a new instruction have separate evidence permissions. A missing target
dose remains absent and blocks creation of its `CareOrderVersion` and CHANGE mission.
Existing order versions and heads retain their accepted schema and atomic transition
rules; no migration of clinical records is performed.

An OrderCandidate may retain `previous_drug` and `previous_dose` for an explicitly
spoken same-family brand change. Code verifies both the source and ingredient
family before using them; unused previous numeric fields retain numeric clarification.
A new patient's stated previous values do not create a fabricated prior prescription.
An existing order retains its head identity and immutable previous versions.

Contract 11e adds `Proposal.single_source`, a tuple of code-owned `family:index`
references, defaulting to empty for older records. Model-supplied bookkeeping is
ignored. Merge disagreements persist as blocking `ProposalIssue` records with
`code=extraction_conflict`, the affected `item`, optional `field`, the quoted question
and its numeric alternatives. Both surviving and conflicting evidence pass the
existing numeric guards, including discarded medication and patient numbers. Primary
history and missions are retained; secondary-only facts/missions and raw questions
are ignored. Overlapping primary facts fold with coherent metadata remapping.
Oversized cards retain every order and mission and reveal additional history via
Edit after the first six lines; atomic confirmation limits remain enforced. Metadata is
remapped when preparation removes or converts items. Unanswered conflicts survive
reloading and corrections; an answer to a different field does not settle them.
These fields change no clinical lifecycle or NameMemory/NameCache record.
The original observation remains provenance.
A correction keeps the proposal ID and increments its version, retains original
patient/expiry, and rotates every callback. A pending reply-choice stores private
source data until the doctor chooses correction or new patient. Superseded
numeric fragments are tracked explicitly so answering a dose question does not
create a new unassigned-number question for its old value.
Corrections retain verified readings for undisputed drug identities while updating
their dose observations. A full compound-dose answer retires its associated
compressed source prefix even when the model had already proposed the blocked
full dose. Unsupported order fields remain in the candidate and validation issues
for review; presentation omits them from order lines and quotes them once as doubts.
The code-owned `request_missing` ProposalIssue uses `item=all` and `blocked=True`
when a transcript has a request cue but the merged candidate has no supported
mission. It is persisted with the proposal, removes the confirm action and is
rechecked by the existing whole-card confirmation guard. An unrelated reply
retains the block through the original source; supplying the requested mission
clears it on a new card version. No inferred mission is written. Redundant bare
fact labels are removed before persistence; surviving readings and issue indices
are remapped together and the original source remains in provenance.

| Entity | Fields beyond common metadata / rules |
|---|---|
| Doctor | telegram_bot_id, telegram_user_id, private_chat_id, name, specialty, city, language, timezone, status (pending/approved/rejected/suspended/revoked), approved_by/at optional, auth_epoch, policy_version, clinical_policy_version optional, approval_reference optional |
| DoctorPolicy | policy_version, timezone, quiet_hours, daily_chase_limit, per_mission_chase_limit, evidence_request_limit, barrier_question/search_limits, default_deadlines by kind, default_grace_seconds=0, permitted_inference_bounds, draft_review_interval, result_review_interval, pause_max_interval, followup_response_window, incident_coverage_policy_id, knowledge_scope_ids, session_idle/absolute_ttl, invitation_ttl; versioned and applicability-scoped |
| Application | bot_id, telegram_user_id, private_chat_id, claimed_name/specialty/city, status (pending/approved/rejected), reviewer_id/at, approval_reference, decision_reason (admin_rejected/unverified or null; absent stored values default to null); pending applications have operational review ownership |
| SubjectBinding | key(bot_id, telegram_user_id), role_set, doctor_id optional, patient_id optional, status (active/frozen/revoked), binding_epoch; admin+doctor may coexist, patient role is exclusive of clinician/admin roles |
| Patient | immutable doctor_id, display_name, identifiers allowed by policy, date_of_birth/age/sex optional with provenance, timezone, language, contact_status (awaiting_link/active/paused/opted_out/unreachable/frozen), active_binding_id optional, consent_id/version optional, record_version, delivery_epoch, safety_epoch, lease_generation, lease_owner/expiry optional, current_plan_id/version optional; clinical history is typed facts, not unversioned active orders |
| Consent | patient_id, binding_id, version, policy_text_version, offer_claim_id/offer_generation and language (absent for legacy agreements), accepted_at/by, permitted_channels, routine_contact_enabled, urgent_response_policy_id, scheduled_slot_consents, quiet_hours, withdrawn_at optional; changing it increments Patient.delivery_epoch |
| Invitation | token_hash, doctor_id, patient_id, issued_by, expires_at, state (issued/claimed/consumed/revoked/expired), pending_claim_id optional, consumed_at optional, review_at, WorkClock while unfinished; proposed default expiry 24 hours, shown to doctor |
| PatientClaim | invitation_id, candidate_subject, minimal_claim_identifier, consent_version, offer_generation and append-only consent_offers (generation, text_version, language, short_text, full_text, configuration, digest), proof_method/reference, doctor_confirmed_by/at optional, state (pending/approved/rejected/expired), review_at, WorkClock; no record disclosure while pending |
| PatientBinding | patient_id, subject_key, status (active/frozen/revoked), binding_epoch, claim_id, consent_id/version, doctor_confirmed_by/at; doctor/patient ownership fixed at activation |
| LoginExchange | token_hash, intended_role/subject, binding_id optional, auth_epoch, consent_version optional, issued_at, expires_at (doctor default 10 minutes), state (issued/consumed/revoked/expired), consumed_at optional; GET is non-consuming, POST atomic consumption only |
| WebSession | session_hash, role, subject, doctor_id, patient_id/binding_id optional, auth_epoch, binding_epoch/consent_version optional, csrf_secret_ref, issued_at, last_seen_at, idle_expires_at, absolute_expires_at, revoked_at optional; no PHI in cookie |
| ClinicalFact | category (condition/allergy/history/medication_history/demographic/patient_report/finding/complaint), typed payload, Provenance, effective_at optional, visibility (doctor_private/patient_released), supersedes_fact_id optional; immutable accepted facts |
| CareOrderVersion | order_id, order_version, type, structured_instruction, Provenance, confirmed_by/at, effective_from, effective_to only if prescribed, supersedes_version optional; immutable and sufficient for its supported executor |
| CareOrderHead | order_id, current_order_version, status (active/stopped/superseded), delivery_epoch, changed_by/at; current snapshot references the active version only |
| CarePlan | plan_id, plan_version, order_refs, mission_ids, followup_ids, status (proposed/confirmed/superseded), confirmed_by/at optional, source_proposal_id; a confirmed batch explicitly records accepted and deferred items |
| Proposal | patient_candidate_ids, selected_patient_id optional, proposed fact/order/mission deltas, base_versions, source_observation_ids, created_by_agent/command, validation_results, expires_at, confirmation_nonce_hash, status (pending/confirmed/rejected/expired), review_at, WorkClock while pending |
| IntakeDraft | owner_doctor_id, source_receipt_ids, media_work_ids, proposal_id optional, selected_patient_id optional, state (pending/associated/rejected/expired), safety_epoch, ProcessingClaim, review_at, WorkClock until associated/rejected or handed to timed disposition; unassigned material remains doctor-private |

Each consent offer freezes its rendered text and configuration for the claim's
lifetime. Refresh appends a generation without altering earlier offers. Callback
tokens bind the offer generation; their action key is claim, generation and action.
ReadConsentTerms commits its receipt and one full-text outbox intent without
revising the claim or consuming agreement. Terms delivery follows the offer
generation across acceptance, but stops at confirmation or closure. Agreement
references the frozen offer; legacy agreements have no invented text. Patient
agreement reads derive their patient and consent solely from the live session.

Invitation confirmation transaction checks: valid unreplayed invitation/claim, verified private subject, consent, doctor authority, unchanged patient owner/version, absent conflicting global SubjectBinding, and current invitation generation. It writes consumption, approved claim, PatientBinding, global SubjectBinding and patient contact/consent references together. Reissue revokes pending claims. No automatic merge or reassignment occurs.

Lost/mistaken binding resolution uses explicit authenticated commands, records a reason, freezes old access, increments relevant epochs and revokes exchanges/sessions. A clinician can own a patient before that patient is linked; clinical disclosure and routine contact still require final consent/binding. The mission deadline is never silently shifted to compensate for late enrollment.

Contract 10 adds `ReportFactPayload` to patient-released `ClinicalFact` values: medication start, day-three reply, raw reading or repeated-question attachment, always retaining patient-report provenance. `Reading` keeps the exact quoted measurement span separately from parsed value/unit and policy judgment. Patient routine-contact flags and snooze expiry are separate from clinical consent; a preference revision updates Patient, PatientProfile, Consent and PatientBinding together and increments delivery epoch. Hashed, expiring `PatientAction` choices bind renewed reminder consent or a specific current START/quiet slot to the patient, receipt and authority epochs. No model can provide these mutations.

A doctor removes a patient through an authenticated, typed-name confirmation. The
patient profile records `removed_at`, `removed_by` and `purge_due_at` (30 days later).
The atomic `RemovePatient` write withdraws consent, rejects a pending claim, revokes
outstanding invitations and an active binding, freezes contact and increments the
authority epochs. It also records one `PatientRemoved` event and a durable
`PatientRemoval` operational task. No record or media is deleted at this step.

The recovery task suppresses queued and uncertain routine patient messages before
cancelling eligible open missions and follow-ups in bounded transactions. Messages
already sending may arrive. Questions, reviews, incidents, fulfilled parents and
missions with a danger history remain available to the doctor. The roster and
patient-task tiles omit removed patients, while the inbox and direct record remain
readable. Routine patient contact and new plans are refused; review, question,
evidence and record-correction work remains possible. Question replies are saved
with removal suppression. A delayed receipt retains its original identity and
completes as `removed_patient_unbound`; pending media ends `removed` without a fetch.
The fixed emergency reply remains available without routing new care to the doctor.
Undo and deletion across media versions/backups require later contracts.

## Missions, follow-up and review

Contract 16a adds `Mission.barrier_attempts`, an ordered tuple of typed,
doctor/patient/mission-scoped `BarrierAttempt` values. Each attempt has a sequence,
revision, originating receipt, patient words, stated area and its receipt source,
expiry, durable reasoning/question/search counters, attempted steps and outcomes,
cached source-backed place fields, and resolved/unresolved/handed-to-doctor state.
Expiry stops further work; it does not delete the record or resolve the mission.
Only a different accepted problem category or an accepted answer to the requested
fact opens another attempt. The same category appends without resetting the budget
or pause. A rejected area ends the complete/asked attempt through the existing
unresolved and handoff path; it cannot spend a second question. Area exclusions
reuse the danger normalizer and clinical vocabularies solely to reject search
terms, never to choose a category. A receipt checkpoint retains its processing
claim and work clock and may atomically add the verified problem outcome;
completion remains the final patient-turn transaction.

Contract 28 adds a receipt-bound `BarrierReservation` (at most two attempts,
screened text digest and optional transcript reference) and `BarrierOutcome`
(accepted/none/uncertain/failure, two typed readings, verified quote offsets,
model IDs, prompt version and model/patient_choice provenance). Both readers
return zero to three problems; only one agreeing, asserted patient problem with
two valid citations is accepted. Empty lists from both readers mean none. Any
failed citation, negation/experiencer check, disagreement or multiple targets
means uncertain. Category and target `PatientAction` tokens preserve the message,
receipt, target version, epochs, 30-minute expiry and single-use selection. Web
messages select targets by number and categories by exact option words or number.

Contract 14 adds `medication_stop`, `medication_change`, `start_date` and `barrier` to `ReportFactPayload`, with optional `barrier_type`, `effective_start` and `anchor_unknown`. They retain patient provenance and the exact target version. `PatientProfile.pending_start_clarification` contains `mission_ref`, `asked_at` and `expires_at`; a patient reply can change only that field and normal revision metadata, without changing consent, recipient, epochs or lease data. Medication `PatientAction` choices retain the screened `medication_report_text`, including for voice input, and keep the existing receipt, single-use and authority bindings.

The medication executor emits the existing `BarrierRecorded`, `BarrierResolved` and `OrderSuperseded` events. The existing blocked shape retains a bounded `resume_at` and barrier reason; supersession ends unfinished work. A day-three barrier is a fact referenced by the fulfilled FollowUpTask, not an event on its already fulfilled parent. STOP/CHANGE acknowledgments never create an automatic day-three task. An accepted recent anchor whose deadline is already due retains that deadline and schedules an immediate accountability wake. No legal transition-table row is added.

| Entity | Fields beyond common metadata / rules |
|---|---|
| Mission | kind (TEST/MONITOR/MEDICATION/SEND_RECORDS/VISIT/QUESTION/TASK), title, typed details, objective_predicate, order_refs, state, fulfillment_validity, fulfillment_event_id optional, fulfilled_at optional, objective_received_at optional, timeliness (undetermined/on_time/late), evidence_refs, confirmed_at/by optional, source_proposal_id optional, due_at, due_source (doctor/scribe/default), due_reason, timing_anchor, original_time_expression optional, timezone, grace_seconds, escalation_at, review_at, resume_at optional, next_contact_at optional, deadline_generation, WorkClock while nonterminal, contact_count/unanswered_delivered_count/evidence_request_count, barrier_type/reason optional, danger_history flag, latest_deadline_notice_event_id optional |
| FollowUpTask | kind (MEDICATION_DAY3/CLINICAL_CHECKIN), parent_mission_id, order_refs, confirmed_by/at, anchor_kind (reported_effective_start/reported_effective_change/doctor_specified_date), anchor_time/source_ref optional while awaiting_anchor, prompt_at/due_at optional only while awaiting_anchor, review_at (always required), response_predicate, source_report_ids, state (awaiting_anchor/scheduled/waiting_response/fulfilled/overdue/contact_suppressed/cancelled), suppression_reason optional, consent_slot_id, review_obligation_id optional, done_intent_id optional (fulfilled creates a DONE:FULFILLMENT intent per Decision 012), WorkClock until fulfilled/cancelled or atomically handed to that independently timed ReviewObligation |
| ReviewObligation | source_type/id/version, review_kind (result_review/correction_disposition/incident_response/unmet_objective/question_answer/media_failure/delivery_failure/binding_review/coverage_review/followup_disposition/evidence_association/intake_clarification), unique_source_key, owner_doctor_id, patient_id optional, source_mission_id optional, state (open/acknowledged/resolved), review_at, next_action_at, coverage_status (covered/blocked), acknowledged_by/at optional, resolved_by/at/reason/action_event_id optional, first_notice_at optional, last_material_change_version, WorkClock until resolved |
| Card | card_class (DANGER/DONE/DEADLINE/CONFIRM/QUESTION/INFO), source_event_id, source_version, review_obligation_ids, allowed_action_types with expected_versions, acknowledged_at optional, resolved_at optional, rendered_payload_ref, delivery_intent_ids; mutable presentation derived from immutable events and current obligations |
| IntakeConcern | intake_id, unique_source_key, source_receipt/evidence_refs, rule_family/version, severity, facts_ref, state (open/associated/resolved), owner_doctor_id, review_obligation_id, associated_patient_id/event_id optional, prior_delivery_refs; doctor-private concern before patient association, independently timed through its ReviewObligation |
| Incident | unique_source_key, observation/evidence_id and version, rule_family/version, severity, verified_status (unverified/verified), facts_ref, state (open/resolved), resolution_event_id optional, raised_at, review_obligation_id, alert_intent_ids; source dedupe does not suppress a later independent event |
| BundleSchedule | `src/sanad/store/records.py`: TenantScope, doctor_id, generation, next_action_at, last_provider_accepted_at optional, pending_intent_id optional, OperationalClock in the bundle lane; interval is the single draft ContactPolicy's 7 days. Unresolved eligibility is queried from obligations and rechecked before send |
| ContactReservation | patient_id, local_day or explicit slot_id, budget_kind, source_intent_id, state (reserved/consumed/released), fence_generation, reserved_at, delivery_attempt_id optional; one atomic key owns each allowed budget slot |

`Mission.review_status` is a **derived projection**, not another writable clinical decision: `not_required | pending | acknowledged | reviewed | correction_requested`. Pending/acknowledged/resolved ReviewObligations determine it; an open correction takes priority over older reviewed evidence. A done-looking execution card cannot hide open reviews.

`fulfillment_validity` is `not_fulfilled | valid | invalidated_pending_review`. Only state=fulfilled plus validity=valid and a currently satisfied objective predicate may produce a new DONE notice. Invalidated historical fulfillment stays in audit and cannot trigger clinical reassurance, resume contact or repeat DONE until an explicit reviewed correction/reopen disposition establishes a new valid version.

A correction to a previously delivered report is a separate proposed `DONE:CORRECTION` purpose under Decision 004, not a new fulfillment announcement. It requires the exact previous report/source version and accepted correcting version, explicitly states that the previous conclusion changed, and creates review even when validity is invalidated_pending_review. If danger rules apply it uses DANGER instead. Deduplication keys include the correcting version; this exception never labels an unmet objective completed.

Review source uniqueness is `(doctor, source_type, source_id, source_version, review_kind)`. Retrying creation returns the same obligation. A materially new version creates a new source key; resolving an old version never resolves a new one by accident. Cardinality is per review kind because one evidence version may require result review and an independent incident response. An incident has exactly one incident-response obligation.

### Mission details by kind

Contract 13 adds a backward-compatible `MonitorDetails.readings` projection.
Each `MonitorReading` references an immutable clinical fact or evidence version,
its row index, observed and received instants, the normalized reported value,
and one slot index or null for an extra. Replacements retain prior source links
and never fill another slot on legacy tolerance-3h plans. New window-next-v1
plans store timezone and cadence; acceptance fills the observed window's slot,
else its empty successor, else an extra. Value-only corrections preserve assignment;
detach frees it and restore assigns against current occupancy without displacement.
Distinct occupied indices establish coverage;
missing values are never synthesized. The store admits a patient MONITOR
revision only when its exact projection follows from accepted sources and the
current receipt. Patient choices retain the screened reading and original time.
Old TASK missions are not migrated.

| Kind | Required executable details and objective |
|---|---|
| TEST | named tests/analytes or imaging request, collection window, identity matching policy, units/accepted representations, completeness predicate; preparation only if doctor supplied. Verified required results fulfill; missing/contradictory pieces stay incomplete. |
| MONITOR | metric, units, dated slots/windows, required coverage, duplicate/late policy, approved target/safety references, consented prompt slots; valid correctly assigned observations satisfy explicit coverage, never silence. |
| MEDICATION | action START/STOP/CHANGE, exact CareOrderVersion, patient-report predicate; START requires the patient explicitly reporting the confirmed action. It does not prove ingestion/adherence. For START, independently create MEDICATION_DAY3 on doctor confirmation, with an explicit anchor or awaiting_anchor state and required review clock. START acknowledgment anchors/preserves it. STOP/CHANGE create no automatic day-three task; only an explicitly doctor-requested CLINICAL_CHECKIN adds follow-up. |
| SEND_RECORDS | categories, historical period optional, required document count/completeness; correct readable historical documents fulfill without the TEST post-order date rule. |
| VISIT | requested/booking/attendance/report objective chosen explicitly, requested date/window, approved preparation optional, pre_visit_brief_at if enabled; a booking report is not attendance. |
| QUESTION | patient's original question, source observation, owner doctor, due_at=(created_at+48 hours) snapped to the default local clock on that date (11b draft), zero default grace; automatically opened support ticket, not a treatment order; answer command fulfills. |
| TASK | supported task category, exact doctor instruction, measurable completion/report predicate, required patient response, optional approved symptom/self-care/preparation questions. Contract 15 records unsupported on-behalf actions as confirmed requests with `completion_rule=unsupported_action`, a `doctor_task` predicate and deadline, without a patient chase or executor. Unsafe or ambiguous clinical instructions retain their existing confirmation guards. |

A generic task cannot execute arbitrary model-generated code or substitute for an unsupported treatment. Symptom checks, self-care/preparation and administrative requests remain required supported workflows when explicitly doctor-defined and validated by the clinical policy.

Contract 15 extends `ReportFactPayload.report_kind` with `visit_booking`, `visit_attendance`, `visit_report_pending`, and `task_done`. Optional `detail` records self-report limitations, nonattendance or early/late timing; `original_receipt_id` preserves the selected report's source time when a later callback resolves ambiguity. The fact's provenance still identifies the authenticated accepting receipt. `PatientAction` adds `visit_report`/`task_report` and private `report_text`, retaining scope, target version, expiry and binding/consent/delivery epochs. Window-only VISIT revisions can change only `details.window_start/window_end` and revision metadata. These are local-day UTC boundaries, independent of `due_at` and `escalation_at`.

`QuestionDetails` adds private `held_answer`, `held_answer_by`, `held_answer_ready`, `held_answer_amendment_ref` and `held_answer_consumed_at`. Readiness and the accepted amendment reference commit with Scribe confirmation. The separate answer transaction resolves the review, fulfils the ticket and queues only the fixed plan-update notice; terminal disposition consumes the hold without deleting its doctor-visible words. A later reopen cannot reuse the consumed hold. New holds reset consumption explicitly. Patient plan projection exposes the original question, never these held words.

`OutboundIntent` adds hashed TASK accept/reopen tokens and `task_action_expires_at`; a stable per-intent `DoctorAccepted`/`DoctorTaskReopened` audit key consumes either button in the clinical transaction. It also carries `question_listing_token`, `question_listing_targets` and `question_listing_expires_at` on the saved doctor-intake listing reply. Its conversation sequence fixes the current page's numbering for one hour, with six tickets per page. The accepted two-step doctor transaction preserves scope: clinical PatientScope command first, tenant receipt completion second. No cross-scope receipt guard is relaxed.

## Mission state and event contract

States: `proposed`, `awaiting_link`, `open`, `waiting_patient`, `blocked`, `unreachable`, `overdue`, `fulfilled`, `cancelled`, `closed_unfulfilled`, `superseded`.

Terminal execution states: fulfilled, cancelled, closed_unfulfilled, superseded. No clock transition creates terminal success or clinical resolution. All other states carry a next action and review time. `blocked` means blocked execution/contact with a reason and bounded resume/review time; patient contact opt-out is an orthogonal Patient status, not mission cancellation.

Aliases used in the table: A = open/waiting_patient/blocked/unreachable/overdue; U = awaiting_link plus A; T = the four terminal states. An event not listed for a state is illegal unless it is an explicit state-preserving event below. `transition` returns a new aggregate plus required event/work/review effects, or a typed rejection; it does not write or send.

The mission event enum is:

`PROPOSAL_CREATED`, `CONFIRM_MISSION`, `CREATE_SUPPORT_TICKET`, `PATIENT_BOUND`, `CONTACT_SCHEDULED`, `CONTACT_ACCEPTED`, `PATIENT_REPLIED`, `BARRIER_RECORDED`, `BARRIER_RESOLVED`, `CONTACT_EXHAUSTED`, `PAUSE_CONTACT`, `RESUME_CONTACT`, `DEADLINE_REACHED`, `OBJECTIVE_FULFILLED`, `DOCTOR_EXTEND`, `DOCTOR_CANCEL`, `DOCTOR_CLOSE_UNFULFILLED`, `DOCTOR_REOPEN`, `ORDER_SUPERSEDED`, `CORRECT_ACCEPTED_EVIDENCE`, `VALIDATE_CORRECTION`, `LATE_INPUT_RECORDED`, `EVIDENCE_ASSOCIATED`, `REVIEW_ACKNOWLEDGED`, `REVIEW_RESOLVED`, `SAFETY_INCIDENT_RAISED`, `CONTACT_PREFERENCE_CHANGED`.

| Event | Allowed source | Result and guard |
|---|---|---|
| PROPOSAL_CREATED | creation only | proposed with administrative review clock; unconfirmed clinical fields remain candidates |
| CONFIRM_MISSION | proposed | open when consented binding is active, else awaiting_link; only doctor-authorized, complete valid instructions; establish immutable confirmed_at and order refs |
| CREATE_SUPPORT_TICKET | creation only | open QUESTION from authenticated patient lane, no doctor tap; create linked question-answer obligation, 48-hour default offset at the policy local clock, no treatment order |
| PATIENT_BOUND | awaiting_link/overdue | from awaiting_link: open before escalation_at, otherwise overdue; from overdue: remain overdue while activating the confirmed binding. Preserve due time and outstanding deadline review; consent/binding proof required. Repeated binding is idempotent, never an implicit extension |
| CONTACT_SCHEDULED | open/waiting_patient | State preserving; set next_contact_at, slot_id, template_id and kind (chase/scheduled). Emit a routine_prompt to the patient only when emit=True and next_contact_at <= now; a future event only arms work. Clinical instants are unchanged. |
| CONTACT_ACCEPTED | open/waiting_patient/unreachable/overdue | waiting_patient if not overdue, else overdue; count only a provider-accepted permitted contact, never queued/uncertain output |
| PATIENT_REPLIED | A | open if before escalation_at, else overdue; reset relevant unanswered sequence only; raw reply alone never fulfills |
| BARRIER_RECORDED | A | blocked before escalation, otherwise overdue with barrier; set reason/resume_at within policy and preserve due_at |
| BARRIER_RESOLVED | blocked/overdue | open before escalation, otherwise overdue; valid supported resolution and current consent required for subsequent contact |
| CONTACT_EXHAUSTED | open/waiting_patient/blocked/unreachable | unreachable after the defined exhausted delivered-contact outcome; retain due/review clocks and disposition |
| PAUSE_CONTACT | U | keep awaiting_link if unlinked, otherwise blocked or overdue; set reason and resume_at, bounded by policy; pause cannot stop deadline/review clocks |
| RESUME_CONTACT | blocked/overdue | open before escalation or overdue after; reject missing consent, stopped order or suspended doctor; rejection has timed review, never unowned limbo |
| DEADLINE_REACHED | U | overdue when now >= escalation_at and predicate not fulfilled; one notice per deadline generation, create/reuse unmet-objective review; repeat wake is idempotent |
| OBJECTIVE_FULFILLED | U | fulfilled with validity=valid after deterministic predicate over accepted current evidence; create result review when required and DONE intent, or merge completion facts into same-source DANGER |
| DOCTOR_EXTEND | U | open if consented/bound, otherwise awaiting_link; explicit new future due, visible grace and incremented deadline generation; resolve only prior unmet-deadline disposition, preserve result/incident reviews |
| DOCTOR_CANCEL | proposed/U | cancelled with explicit authorized reason; suppress routine work and create/reuse disposition where needed; never resolves incident/result reviews implicitly |
| DOCTOR_CLOSE_UNFULFILLED | U | closed_unfulfilled with explicit reason and no fulfillment; refused if danger_history=true, requiring explicit cancellation/disposition instead |
| DOCTOR_REOPEN | fulfilled/cancelled/closed_unfulfilled | open or awaiting_link using an explicit new confirmed version, current active orders, new due/review and predicate; no automatic reopening from incoming data; superseded old orders cannot be revived |
| ORDER_SUPERSEDED | proposed/U | superseded only in a doctor-confirmed revision transaction with explicit successor or cancellation; suppress stale routine output and retain independent reviews |
| CORRECT_ACCEPTED_EVIDENCE | any existing state | state unchanged; supersede the specified evidence version, set invalidated_pending_review if prior fulfillment fails, cancel stale intents, create correction review; never start contact |
| VALIDATE_CORRECTION | fulfilled | remains fulfilled; doctor-reviewed current predicate must be satisfied to restore valid, with superseding event; otherwise remains invalidated until explicit reopen/disposition |
| LATE_INPUT_RECORDED | any existing state | state unchanged; retain observation, screen danger, create association/review work; never auto-attach as corrective evidence |
| EVIDENCE_ASSOCIATED | U | state unchanged; accepted association alone does not fulfill; OBJECTIVE_FULFILLED is separately checked |
| REVIEW_ACKNOWLEDGED / REVIEW_RESOLVED | any existing state | execution unchanged; update exact ReviewObligation by authorized action and criteria; no time-based resolution |
| SAFETY_INCIDENT_RAISED | any existing state | execution unchanged; independent incident/review, danger_history=true when applied under safe aggregate concurrency; urgent incident commit itself does not wait for ordinary lease |
| CONTACT_PREFERENCE_CHANGED | any existing state | execution unchanged; epochs/status invalidate unsent routine contact; unfinished follow-ups/reviews retain ownership |

Event effects are one command transaction when they touch current truth. Incident persistence happens independently before any optional mission annotation; `danger_history` safety guard also queries linked incident records so a delayed projection cannot permit unsafe close. For an overdue mission whose evidence is later verified, OBJECTIVE_FULFILLED records receipt-based timeliness separately from fulfillment time; delayed processing alone never proves late patient submission.

FollowUpTask events are CONFIRM_FOLLOWUP, ANCHOR_CONFIRMED, PROMPT_SCHEDULED, PROMPT_ACCEPTED, RESPONSE_RECEIVED, FOLLOWUP_DEADLINE, SUPPRESS_FOLLOWUP_CONTACT, CANCEL_FOLLOWUP. Confirmation creates awaiting_anchor when actual start/change date is unknown, otherwise scheduled from the explicit doctor date. ANCHOR_CONFIRMED records the patient-reported effective date or doctor-approved date and schedules the independent task. Scheduled → waiting_response only after accepted prompt; valid response → fulfilled plus a DONE:FULFILLMENT intent (merged into a same-source DANGER when danger rules apply); deadline → overdue plus review and a DEADLINE notice; suppression → contact_suppressed plus timed disposition; explicit cancellation → cancelled while existing clinical/incident reviews survive. No parent mission terminal state performs these transitions automatically.

| Follow-up event | Allowed source | Result and guard |
|---|---|---|
| PROMPT_SCHEDULED | scheduled | State preserving at or after prompt_at; emit patient_day3_prompt to the explicit slot. Never count delivery or change prompt_at/due_at. |
| PROMPT_ACCEPTED | scheduled | waiting_response only on provider acceptance; queued or uncertain prompts count nothing. |

DoctorExtend appends the prior explicit/inferred deadline and its original source, reason, anchor, expression, zone and policy to Mission.timing_history before applying the new doctor time, preserving provenance across long-horizon restarts.

Contract 11 persists Mission.last_patient_reply_at, first_chase_accepted_at and last_chase_accepted_at and PatientProfile.last_chase_accepted_at. ContactAccepted carries the provider acceptance instant and chase/scheduled kind; delayed feedback for an already accepted send updates contact counters while preserving a later terminal or blocked execution state and any subsequent patient reply. Accepted patient turns apply the existing PatientReplied event to remaining active missions, resetting unanswered counts without fulfillment; scheduled MONITOR prompts count delivered contact but cannot exhaust the chase ladder. ContactScheduled carries an explicit emit flag and expiry; future planning does not emit. The scheduler exposes a typed ineligibility reason separately from the optional ContactPlan. Its operational projections and missed-window audit never edit clinical instants. Computed local contact times use the later valid instant for a DST fold/gap; explicit doctor timing continues to reject ambiguous local input. Confirmation and intended-person binding prime discovery work without moving clinical deadlines. Existing monitor consent IDs from contract 10 remain recognized alongside the canonical monitor:<mission>:<index> IDs.

Review events are CREATE_REVIEW, ACKNOWLEDGE_REVIEW, RESOLVE_REVIEW, BLOCK_COVERAGE, RESTORE_COVERAGE, MATERIAL_CHANGE. Acknowledge preserves open responsibility; only an authorized resolution with the required source versions/reason ends it. Superseding a reviewed source creates a fresh obligation when needed.

### Evidence receipt and verification timing

Contract 12 implements immutable `Evidence` versions and a separately versioned
`EvidenceHead` in the patient's partition. Each evidence version retains its
receipt and PatientMedia reference, patient-scoped byte hash, classified category,
printed name/date hints, both reader results, disagreements, graded rows, shift
and identity flags, association provenance and predicate results. A unique hash
record identifies the original evidence for byte-identical resends without
expiring. The head names the selected immutable version and its
candidate/accepted/rejected/superseded state. Only `_EvidenceTurn`,
`RecordObjectiveFulfilled`, `AssociateEvidence` and `RejectEvidence` may commit
these records through the Steward. Corrections/supersession remain slice 19.

Binding addenda 2–3 add `accepted_pending_identity` to the evidence association
and head states. It is attributable retention on a selected mission, not fulfilled
work or identity proof. The `identity_unverifiable` observation flag remains in
history; only the owning doctor's explicit `confirm_identity` action adds
`identity_confirmed`. Partial papers also retain this pending state so another
receipt cannot silently use their unconfirmed identity. The pure evaluator can
check their joint content; fulfillment requires every paper in the completing
set to have matching or doctor-confirmed identity. Confirmation of a partial
paper alone cannot satisfy missing content. Each paper's review has the existing
24-hour clock, single-use version/epoch-bound confirm and reject buttons, and
equivalent session API actions. Rejection clears association, retains observation
and danger, resolves the evidence-owned review, and asks the existing patient name
question; a patient yes only returns it for doctor review. Mission result review
is created by fulfillment, separately from this evidence identity review.
An on-time pending identity continues T17 wording until decided. No post-fulfillment
detachment is introduced. The accepted Patient schema has no separate Latin-name
field: matching uses the stored display name, including any stored Latin tokens,
and never invents a transliteration or treats arbitrary identifiers as names.

| Evidence evaluator | Required result in contract 12 |
|---|---|
| `test` | `lab_result`; canonical analytes with readable values and recognized explicit units (a missing clinical baseline remains `cannot_judge` without invalidating the observed value); confirmed all/any coverage accumulated only within this mission; printed date inside any collection window |
| `send_records` | Requested category coverage, historical period when present, and distinct-document count; pre-order dates are permitted |
| `visit` | Only `report_received`, with a report/discharge/imaging category; other objectives retain their patient-report predicates |
| `task_evidence` | Readable evidence stays pending until explicit doctor acceptance |
| `monitor` | The shared text/photo predicate checks accepted reading-to-slot links; every required slot must be filled. Missing slots have explicit `slot:<index>` identifiers. Unverified identity, disagreement and incompatible units cannot supply coverage. |

Binding addendum 1 limits a photo to one document: multiple-document cues retain
one candidate with `one_document_per_photo`, with no inferred grouping. The
coarse reader type, caption, expected categories and printed item cues feed the
handwritten classification table; ambiguity is `other` with association review.
Missing/unknown units, disputed fields and shifted rows cannot establish
fulfilment. The completing set removes noncontributing/redundant accepted
documents in descending receipt-time order whenever the unchanged evaluator
still succeeds; the original observations remain retained. This preserves
the earliest complete receipt set when extraction occurs out of order. Single-use hashed choices carry evidence/mission versions and current
binding/consent epochs; clarification lasts 24 hours. The released draft limits
remain three candidates per receipt, five pages per turn, one shared normalized
name token after honorific removal, and an unbounded duplicate window. The
single-photo interface yields at most one candidate in this slice.

Active `value_alert` versions are additional patient-specific thresholds, never
replacements for kernel rules. Comparison uses the kernel's quantity conversion;
an unknown or incomparable unit records `alert_unit_mismatch`. An invalid looser
alert is refused at write time and ignored at grading time. Kernel-critical and
alert facts for one row share one incident. Deterministic doctor-only context
contains active medication fields, condition facts and the previous accepted
value/date; it changes neither the observed value nor its clinical verdict.

For an accepted predicate-completing evidence set, store `objective_received_at = max(received_at of the source receipts actually required by that predicate)`, alongside `fulfilled_at` (the later verification/acceptance transaction time). Collection/sample-date requirements are separate predicate checks. `timeliness` is `undetermined` until the set is verified, then `on_time` if objective_received_at <= due_at, otherwise `late`. Grace affects notification time, not whether the target date was met.

At escalation_at, an unverified on-time candidate cannot prove fulfillment. The deadline report states “evidence received; verification pending” with the actual missing/failed processing step, never that the patient failed to submit. It creates/retains timed processing review. Once verification succeeds, update the original deadline disposition with on-time/late facts, preserve the original message/audit, and create independent clinical result review. A correction retains historical timing and recomputes the current version explicitly; it never rewrites a previous event.

## Clock calculations

Contract 11b draft policy (`OWNER_REVIEW_PENDING`): type-default deadlines use
`DoctorTimingPolicy.default_deadline_local_time="10:00"`. Calculate anchor plus
the type offset, take that date in the policy timezone, then resolve 10:00 on
that date through `local_to_utc`. Computed folds/gaps choose the later valid
instant. Scribe proposals and explicit doctor instants retain their full
precision. Grace is still added to the resulting due instant.

`due_at` is the objective target. `escalation_at = due_at + grace_seconds`; reconciled default grace is zero for every type. A doctor-configured nonzero grace is explicit on the confirmation card. Inferred-only bounds of 1–180 days validate timing candidates, never clamp explicit clinical instructions. Original local time/anchor/source/reason is retained. Invalid explicit or clinically incomplete instructions ask on the existing card; no guessed dose/duration.

`review_at` is the administrative accountability deadline; it is not a new prescribed test/medication date. Proposed, awaiting-link, blocked and unreachable states all retain it. `resume_at` is a bounded proposed contact pause ending, never a replacement due time. ReviewObligation.review_at is independent of Mission.review_at: a result review is due at fulfilled_at plus the approved result-review interval (proposed three days), never three days after the original draft. It remains visible after execution ends.

For each nonterminal mission, `next_action_at = min(eligible next_contact_at, due escalation if unreported, review_at, resume_at when present)`. Already-handled deadlines are excluded using deadline generation/event state; they cannot create a hot loop. Every handled wake atomically records its disposition and a strictly future next action or terminal execution state. A contact cap/quiet-hour/opt-out check excludes only contact from this minimum, not review/deadline work.

Independent records contribute separate due-index entries: each ReviewObligation, FollowUpTask, BundleSchedule, receipt, media job and outbox item. Their clocks do not depend on a terminal mission retaining a wake. The day-three anchor rule is printed on confirmation: use the reported effective start/change date, or an explicit doctor date. Plan confirmation time is never evidence the patient started. Awaiting-anchor tasks retain review_at and next_action_at, ask for the missing date and create disposition if unanswered. Once anchored, prompt_at is anchor plus three days; a later correction requires an explicit source/version update and revalidation. The response window and clinical policy are separately approved defaults.

Bundle eligibility starts seven days after the first accepted DEADLINE notice for an unresolved item. One per-doctor BundleSchedule collects eligible items and sends at most one accepted weekly bundle. Only accepted delivery advances last_provider_accepted_at; uncertain delivery takes the bounded uncertainty/review path. A doctor-bundle delivery failure uses a tenant-owned, patient-unassigned delivery_failure ReviewObligation with source_type=outbound_intent; its review clock re-arms through the same pure review transition. A provably suppressed generation is retired before new work uses another logical key, without changing the last accepted interval. Each candidate obligation is re-read, with resolved items omitted. An empty bundle clears its current due work; a new eligible obligation re-arms it transactionally.

OutboundIntent.contact_feedback is not_applicable, pending or applied. Mission/follow-up routine prompts start pending; provider acceptance retains a delivery clock until the idempotent contact:<intent_id> Steward command and conditional feedback marker complete. Recovery never sends accepted intents again. DeliveryResolution can carry an exact-version obligation stamp and a guarded BundleSchedule revision alongside the accepted attempt; a stamp conflict is re-read once and durable recovery retains acceptance if contention persists.

ReviewObligation `first_notice_at` records provider acceptance, not queuing or acknowledgment. Its original `review_at` remains unchanged for overdue visibility; after each handled review wake, `next_action_at` becomes the next policy accountability check or material-change work time. Routine rechecks do not re-ring individual cards. BundleSchedule handles the approved weekly message; incident response retains its own earlier policy clock. A fulfilled or contact-suppressed follow-up may drop its execution clock only when any required unresolved review is durably linked and independently scheduled in the same transaction.

Index delays or a missed tick do not lose deadlines: due records stay due until atomically handled. Sweep metrics show oldest overdue work, claims expired, processing failures and coverage blocks. Periodic reconciliation rechecks mandatory clocks against canonical status using paginated tenant partitions and repairs missing derived entries under version checks; TTL is not the scheduler.

## Inbound, media and delivery schema

For PDFs, MediaWork carries an ordered page manifest. Evidence and doctor drafts
reference those pages; merged reads retain both independent reader slots and
blocked page indices. Repeated lab rows retain all source page indices; conflicting
values on the same date require review. Each complete page, draft, proposal and
evidence item is bounded to 350 KiB, and merged reads to 300 KiB. Oversized reports
retain private page data and images for doctor review. Parent completion shares
the fenced transaction that records the outcome; extraction alone is insufficient.

| Entity | Required fields / lifecycle |
|---|---|
| InboundReceipt | transport_key (bot+update_id, or session+client_command_id), source_subject/chat, channel, kind, immutable payload or protected payload_ref, provider_media_handle optional, received_at, safety_screen_state/policy_version, state (pending/processing/completed/needs_attention), ProcessingClaim, WorkClock until completed, result_event_ids; scope established by ingress, never payload claims |
| MediaWork | receipt_id, provider_handle_ref, source_blob_ref optional, normalized_blob_ref optional, byte_hash optional, mime/size/duration, stage (fetch/normalize/extract/associate), state (pending/processing/completed/needs_attention), ProcessingClaim, WorkClock, last_error/resend_intent_id optional |
| DocumentPageWork | Doctor/patient or private intake scope; parent MediaWork, receipt and source hash; page index, image hash and renderer version; independently fenced render/read/commit stages and media clock; paired reads or private read reference; terminal committed, blank, unreadable or duplicate disposition. Policy refresh retains the prior page and creates separately checkpointed reads of its immutable image. |
| PhotoReceipt | authenticated_scope (patient scope or owning-doctor IntakeDraft), content_hash, processing_version, media_work_id, evidence_id optional, state (pending/processing/completed/needs_attention), source_receipt_ids; deduping cannot cross scopes or hide unfinished work |
| Evidence | observation_id, source_blob_ref, normalized_blob_ref, content_hash, category, printed_identity/date optional, association_state (unmatched/candidate/accepted_pending_identity/accepted/rejected), patient_match_provenance, extracted_values with units, required_predicate_results, accepted_by/at optional, supersedes_evidence_id/version optional, Provenance; immutable accepted versions, current head separately points to chosen version |
| EvidenceHead | evidence_id, current_version, status (candidate/accepted_pending_identity/accepted/rejected/superseded), association refs; explicit correction is the only supersession authority |
| Candidate | observation_id, candidate_kind, typed payload, Provenance, validation_results, status (proposed/accepted/rejected/needs_confirmation), source_versions; never direct current-order authority |
| AuditEvent | event_id, command_id, scope, event_type, aggregate_refs, before/after_versions, actor, accepted_at, policy_versions, source_refs, protected payload_ref optional; immutable, no mutable card.resolved field |
| CommandReceipt | command_id, scope, payload_digest, accepted_result, accepted_at; reused ID with different payload is rejected |
| OutboundIntent | logical_key, source_event_ids/versions, scope_kind, audience, recipient_ref, notification_purpose, variant-specific authority/freshness fields (defined below), eligibility_class, payload_ref, payload_digest, conversation_sequence, slot_id optional, expires_at, status (queued/sending/provider_accepted/uncertain/failed/suppressed), delivery_claim, accepted_message_id/at optional, retry_count, uncertain_retry_count, last_error, suppression_reason optional, WorkClock until accepted/suppressed or transferred to timed review |
| DeliveryAttempt | intent_id, attempt_id, lease_generation, freshness_snapshot, started_at, ended_at optional, outcome (started/provider_accepted/uncertain/definite_failure), provider_message_id optional, redacted_error_code; persisted attempt-start makes an interrupted send uncertain, not automatically unsent |
| SessionSnapshot | session_key (Coordinator+mission or Concierge+patient), patient_id, bounded turn records/summary reference, source_order_versions, safety_epoch, fence_generation, session_version; memory only, fenced writes required |
| OperationalIssue | kind, affected_scope refs without unnecessary clinical payload, owner_role/id, status (open/acknowledged/resolved), due_at, reason, WorkClock; covers infrastructure/account work without silently granting admin clinical access |
| ReplayNonce | issuer/key_id, nonce_digest, timestamp, expires_at; conditional insert prevents tick/callback replay |

### Outbound scope and purpose variants

`SuppressRoutineIntents.order_refs` is optional. When supplied, only queued routine intents with an intersecting order reference are suppressed; empty or absent intent refs do not match. Omitting the effect's scope preserves the existing patient-wide behavior. The patient store guard requires a declared scope plus an inactive order or the exact accepted barrier projection. Medication STOP DONE notices retain their completed mission source but carry no active-order guidance reference, allowing the doctor to receive the self-report about the stopped order.

OutboundIntent has common delivery metadata plus a discriminated `scope_kind` and `audience`; patient-only fields are not fabricated for account/intake messages. Common fields include `recipient_ref` (server-resolved verified private channel subject/chat), `notification_purpose` (solicited_reply, DANGER, DONE:FULFILLMENT, DONE:CORRECTION, DEADLINE, routine_prompt or patient_safety_response), source versions, payload reference/hash, expiry and delivery/work state.

| Variant | Required authority/freshness fields and guard |
|---|---|
| account → applicant/admin/doctor | bot_id, recipient_subject, application/account source version, recipient role/status and auth_epoch when an approved account exists. Application acknowledgments may reach the verified pending applicant; approval actions still need current admin authority. No patient fields or clinical payload |
| doctor → owning doctor bundle | TenantScope only; approved doctor, configured bot, private chat and recipient auth epoch; DEADLINE/bundle purpose, generation-bound logical key, no patient authority fields. Re-read unresolved obligations with first_notice_at at least seven days old; suppress bundle_empty when none remain. Render payload from those reads at send time. |
| intake → owning doctor | doctor_id, intake_id, intake_safety_epoch, current approved-doctor auth_epoch, source concern/proposal versions. No patient_id, patient binding, consent or order is invented |
| patient → owning doctor | doctor_id, patient_id, approved recipient-doctor auth_epoch, authoritative patient ownership, source mission/evidence/review versions and purpose-specific predicates. Patient opt-out, missing binding or cancelled mission cannot suppress valid doctor accountability; patient-only routine consent is not a prerequisite |
| patient → patient routine | doctor_id/patient_id, active recipient PatientBinding+epoch, current doctor coverage/status, consent_version, patient delivery/safety epochs, active order refs and relevant mission/follow-up/evidence versions, valid slot/expiry. Stop or stale instruction suppresses the send |
| patient → patient safety response | verified source subject and binding/identity context, source incident/policy version, recipient and current safety epoch. The approved immediate response is independent of routine contact consent and doctor transport; frozen coverage cannot authorize chart disclosure or individualized stale orders. Unlinked/unknown users receive only the permitted general service/safety response |

Only the purpose DONE:FULFILLMENT checks for current valid completion of its source mission or FollowUpTask. Proposed DONE:CORRECTION checks the previously provider-accepted report and accepted correcting version, and may explicitly invalidate its earlier conclusion. All variants retain source dedupe and current recipient authorization. Intake outbound intents/attempts use their owning intake partition; account replies use account/operational scope. Dispatch records the applicable variant's freshness snapshot, never a fake patient consent token.

All pending receipts and MediaWork retain enough source information to resume after restart. Before S3 persistence, the recoverable provider handle can be re-resolved; failure to retrieve becomes a resend request and timed exception. Never mark processing completed until either its accepted effects or its explicit failure/disposition have been durably committed. One receipt may produce multiple bounded subcommands with unique keys; completion records every subcommand outcome.

Delivery lease expiration with an attempt started but no terminal receipt becomes uncertain. Lease expiration before any attempt-start can safely requeue. Retries honor provider retry guidance and bounded policy. Daily chase is consumed after acceptance; reservations remain held while uncertain. Release only proven-unsent work. Recipient changes and version changes suppress stale queued intents; already-started requests follow the architecture's documented unavoidable network-race handling.

## Transaction and Store interfaces

All interfaces require a server-created Principal or WorkerCapability. A global index is never permission to read all tenants. Store implementations use the same conditional-write behavior in memory, DynamoDB Local and AWS. Domain modules import protocols/types only; boto3 belongs to the adapter.

| Protocol operation | Semantics |
|---|---|
| authorize(principal, action, scope) | current approval/binding/auth/consent checks; no ID alone grants access |
| get_patient / list_patients / get_order_head / get_evidence / list_review | scope-required reads, pagination, non-disclosing forbidden results; global index results are reauthorized |
| accept_inbound(transport_key, normalized_payload) | put-if-absent durable receipt and initial WorkClock; return existing completion or unfinished state; never erase pending input |
| claim_work(record_key, expected_version, owner, now, ttl) | conditional lease takeover only when eligible/expired; increment generation, return fenced token |
| acquire_patient(scope, owner, now, ttl) | shared ordinary lease generation; acquire does not authorize clinical action by itself |
| acquire_intake(principal, intake_id, owner, now, ttl) | owning-doctor fenced processing before a patient is selected; confirmation acquires existing patient authority or atomically creates a new patient |
| commit_command(envelope, expected_fence, mutations, events, intents, work_updates, receipt_completion) | one atomic transaction checking scope, versions, auth/consent/safety/delivery epochs and fence; writes CommandReceipt; returns same result on duplicate |
| raise_intake_concern(principal, intake_id, unique_source_key, facts, policy) | approved owning-doctor scope; source dedupe, IntakeConcern, independent ReviewObligation, intake safety_epoch increment and Liaison intent without patient ID; explicit later association links source/review and rechecks safety/delivery |
| raise_incident(scope, unique_source_key, facts, policy) | independent urgent transaction: source dedupe, Incident, ReviewObligation, safety_epoch increment and urgent intents; no ordinary patient-lease dependency |
| confirm_claim(principal, claim_id, expected_versions) | atomic invitation consumption, binding uniqueness, consent, patient ownership, proof and role checks |
| query_due(lane, shard, through, cursor, limit) | service-only index query; return candidates/cursor, then authoritative checks/claims before effects |
| reserve_contact(scope, slot_or_day, intent_id, expected_fence) | conditional single budget reservation; current consent/order validity still checked at send |
| start_delivery(intent_id, expected_versions, owner, now) | delivery lease plus freshness/authorization check and persisted attempt-start; suppress invalid intent; no network inside transaction |
| complete_delivery(attempt_id, outcome, provider_id) | conditional attempt result, next retry/review, contact reservation outcome; acceptance distinct from doctor acknowledgment |
| load_session / commit_session(scope, key, expected_session_version, fence) | fenced bounded memory; stale callbacks cannot write |
| create_or_get_review(unique_source_key, due, owner) | source-key uniqueness, independent WorkClock and BundleSchedule update when eligible |
| resolve_review(principal, obligation_id, expected_version, action) | checks exact source/action criteria; audit resolution; never cascade from mission cancellation |
| reconcile_partition(capability, doctor/patient partition, cursor, limit) | bounded canonical-state audit/repair of required clocks, unresolved refs and derived indexes; no silent clinical state changes |

DynamoDB transaction item/size limits are enforced before accepting a doctor batch. Bound the number of items on a confirmation; a larger dictation is split into clearly numbered independently confirmed sub-batches or a proposed manifest workflow with explicit commit visibility, never partially acknowledged as one atomic commit. Critical commands and their outbox/event writes must fit one supported transaction. Exact safe batch sizing is verified against the pinned adapter in contract 02.

S3 and DynamoDB cannot be one transaction. Upload immutable source to a generated staging/object key first, verify its checksum, then transactionally link it to the evidence. Unlinked blobs are reconciled under retention policy; missing blobs create timed exceptions. Never delete accepted evidence on a failed DB attempt. Authorized media reads validate doctor/patient ownership before streaming or issuing a tightly scoped expiring URL.

## DynamoDB keys and access patterns

One table, string `PK`/`SK`. Key constructors live only in the store adapter and include authenticated scope. Names/phones/medical text are never partition keys. Separate global account/binding records from clinical partitions.

| Record group | PK | SK / notes |
|---|---|---|
| Doctor profile/policy/applications/bundle | D#doctor_id | PROFILE, POLICY#version, APPLICATION#id, BUNDLE; policy versions immutable |
| Patient partition | D#doctor_id#P#patient_id | PROFILE; FACT#id; ORDER#id#HEAD or #V#version; PLAN#id#V#version; MISSION#id; FOLLOWUP#id; REVIEW#id; INCIDENT#id; CARD#id; EVIDENCE#id#HEAD or #V#version; SESSION#role#id |
| Unassigned doctor intake | D#doctor_id#INTAKE#intake_id | PROFILE, PROPOSAL#id, MEDIA#id, PHOTO#hash#processing_version, CONCERN#id, REVIEW#id; physician-private until confirmed patient association; no S3 rename is required for a scope-checked immutable reference |
| Tenant timeline | same patient PK | EVENT#UTC-time#event_id; immutable; stable pagination tie-breaker |
| Invitation/login lookup | TOKEN#purpose#token_hash | META; scope references only; conditional state transition, application expiry enforced |
| Global subject uniqueness | SUBJECT#bot_id#telegram_user_id | BINDING; no clinical payload; admin+doctor role set permitted, patient exclusivity enforced |
| Web session | SESSION#session_hash | META; explicit auth/binding checks on every request |
| Inbound dedupe | IN#transport#transport_key_digest | META; protected payload and WorkClock, derive clinical scope independently |
| Media/photo processing | D#doctor_id#P#patient_id | MEDIA#id; PHOTO#content_hash#processing_version; content dedupe confined to patient |
| Outbox/attempts | D#doctor_id#P#patient_id, D#doctor_id#INTAKE#intake_id, or account/operational scope | OUT#intent_id; ATTEMPT#intent_id#attempt_id |
| Logical command/outbound/review uniqueness | appropriate patient/doctor PK | CMD#command_id; OUTKEY#logical_key_digest; REVIEWKEY#source_key_digest; conditional marker and actual record in same transaction |
| Contact budget | same patient PK | CONTACT#local-day#budget-slot or CONTACT#scheduled-slot-id; conditionally owned |
| Nonce/operational account work | OPS#service | NONCE#digest, ISSUE#id; clinical detail omitted unless independently authorized |

Indexes:

- **GSI_DOCTOR_PATIENTS:** patient PROFILE only; partition D#doctor_id, sort normalized_name#patient_id. Supports the doctor's board/search pages; name ambiguity still needs confirmation. Enforce access before querying and before returned content.
- **GSI_DUE:** every unfinished record with WorkClock; partition lane#shard, sort fixed-width UTC next_action_at#entity_type#id. Lanes separate urgent, ingress/media, delivery, mission/followup, review/bundle and operational work. Stable hash sharding is configured once per deployment; query every configured shard with pagination. Urgent work is attempted promptly on ingress and also recovered by this index.
- **GSI_REVIEW:** unresolved ReviewObligation only; partition D#doctor_id, sort review_at#obligation_id. Patient_id stays projected for filtering/view grouping; resolved entries disappear only after an authorized resolution. This index is visibility, not independent truth.

The canonical WorkClock lives on the actual mission/receipt/review/etc record; it is projected automatically to GSI_DUE, not copied into an unrelated queue whose absence loses a task. Immediate strongly consistent key reads resolve index lag/stale entries. A bounded reconciliation pass over tenant canonical partitions detects malformed/missing mandatory WorkClocks; no full-table patient scan runs every minute.

TTL applies only to genuinely disposable expired exchanges, sessions, replay/dedupe markers after the supported replay horizon, and explicitly approved operational retention. Authorization never waits for asynchronous TTL deletion. Clinical events, evidence, open review and unresolved processing do not expire merely because a timer elapsed. Retention/export/deletion is a separate authenticated policy workflow, with coordinated media/backups and minimal audit retention.

## Acceptance evidence required across contracts

These are behavior checks to distribute through their owning contracts, not tests already run:

- Receipt/ACK then crash; retry after an incomplete photo receipt; expired claims; accepted mutation then crash before send; send accepted with lost response; stale lease/session callback after takeover.
- Concurrent reply/tick/two mission wakes share one ordinary fence and one chase reservation; urgent second message bypasses the slow turn and invalidates reassurance; queued old-order output is suppressed at send.
- Stolen/expired/replayed QR, wrong claimant, group-chat enrollment, cross-doctor ID/media/query, token preview, suspended session and callback replay; no history before consent plus doctor claim confirmation.
- Four-hour explicit deadline unchanged, zero default grace, inferred reason visible, pause/opt-out/unlinked deadline still handled; expired review remains indexed after mission cancellation/fulfillment.
- START acknowledged on day one sends DONE and day-three follow-up still due after restart; an answered day-three check-in sends a second DONE; order stop/opt-out blocks the prompt and creates disposition; silent check-in never counts as adherence or completed review.
- Fulfilled objective creates DONE before review; dangerous fulfillment sends one urgent report; correction invalidates prior fulfillment without automatic reopen; late new upload never silently corrects; acknowledgment does not resolve an incident.
- Seven-day bundle contains unresolved eligible items once per doctor; resolved/cross-tenant items excluded; uncertain send does not claim acknowledgment; a missed tick catches up without a stale reminder burst.
- Full supported patient journey and every required mission/agent capability remain in integration acceptance; a contest date cannot turn a missing executor into accepted completion.

Contract 10 permits an `AnchorConfirmed` on a contact-suppressed MEDICATION_DAY3 task to record the reported start and derive its original timing while retaining suppression and independent review. It cannot restart contact. Patient web reading projections expose the raw report rather than the stored policy grading metadata.


## Review notice offers (contract 17)

`liaison_notice` and `review_offer` live in the owning doctor partition under
`LIAISON_NOTICE#id` and `REVIEW_OFFER#sha256` keys. A notice retains the actual
payload, intent/attempt identity, recipient subject/bot/epoch and displayed review
snapshots. An offer contains no clinical content: it binds one action to that
notice, exact review scope/version, immutable source identity/version, material
version and the source revision read for display. Random 256-bit callback values
are hashed for lookup. Application expiry is enforced without relying on TTL.

Only final-notice delivery completion issues these records together, conditioned
on the active attempt, current doctor authority and displayed source reads.
Definite failure and unsent outcomes cannot issue them. Accepted/uncertain delivery
is separate from reading. A short review transaction consumes the offer with the
permitted domain transition, audit and replay record; every authorization check
runs again. Patient reviews retain patient leases. Exact tenant delivery failures
and exact intake sources use their independently guarded patientless branch.
No fabricated patient profile, patient lease or executor effect is admitted.

`OutboundIntent.review_listing` retains the selected inbox snapshots and its
one-hour expiry. A durable `LIAISON_MODEL_ATTEMPT` event/command reservation grants
one bounded proposal attempt per logical intent, without granting action authority.
The first accepted DEADLINE's metadata stamp and initial offer issuance share the
same conditional transaction. Subsequent actions never refresh stale snapshots.


## Accepted correcting versions (contract 19)

`Correction` is immutable, patient-scoped, and separately attributed with origin
`authorized_correction`, doctor Principal, reason, exact predecessor/successor refs,
before/after values, server-computed predicate results, affected mission ids, retained
monitor links, prior report ids and instruction exposure states. It does not reuse
the proposal-only FieldEvidence correction validator.

Evidence advances its existing head and appends a new Evidence version. Explicit
`detached` versions have a matching detached head, correction id, predecessor link
and reason; accepted_by/at are absent. The old mission id stays as provenance, not
an active association. No detached version contributes to an evaluator. Original
readers, observation/media ids, hashes, source regions and clinical received times
are retained. Evidence-derived reading summaries advance their FactHead atomically.

ClinicalFact corrections append a new id with root_fact_id, supersedes_fact_id and
correction_id. FactHead selects its current ref and accepted/detached status. Current
views use the head; history keeps all versions. Evidence-derived summaries direct
correction to their canonical Evidence source, preventing divergent values.
Monitoring links retain original observation/receipt times and recover removed slots
when later corrected; the latest observation's ordering remains unchanged.

One correction-disposition review covers a bounded dependency inventory. Reviewed
ValidateCorrection checks the exact current correction/review, recomputes all its
invalidated missions, and atomically restores validity and resolves review. An old
correction/review cannot validate its successor. A separate CorrectionResponse
records a doctor decision without changing fulfilment or sending a patient message.
CorrectionOffer holds the actor, exact mission ref, patient epochs, explicit due_at,
reason, preview, expiry and consumption time for confirmed reopening.

Direct correction transactions allow at most 24 combined output records and
expected-version references, and 512 KiB serialized outputs, before store overhead.
The store additionally enforces 100 operations, 400 KiB per item and 4 MiB. Larger
fan-out fails atomically without advancing current truth. Scribe amendments retain
the same store limits and one final revision per record.

## Reusable doctor answers (contract 17d)

`ReuseOffer` and `ReusableAnswer` are versioned tenant records under
`REUSE_OFFER#id` and `REUSABLE_ANSWER#id`. Offers retain exact accepted answer and
question text, doctor id/auth epoch, source patient/mission and post-answer
version, original listing token, one-hour expiry and explicit consumption actor
command/time. Reusable answers retain normalized and original question text,
exact answer text, source patient/mission and creation time. No patient or model
can create either record. The shared store reconstructs the authorized command's
complete write set and conditionally checks cross-partition versions before
admitting their writes. Application expiry, not TTL, controls offer validity.

`OutboundIntent.question_bindings` adds mission versions and optional reusable
answer id/version beside the existing patient/mission target pairs. Existing
records without bindings remain readable; sending a proposal from an unbound
legacy listing is refused. `AuditEvent.question_command` optionally retains the
immutable private payload of a committed doctor question action for receipt
recovery. These fields are excluded from model representations and are not public
patient projections. No clinical event or transition schema changes.


## Administrator identity (18b checkpoint 2)

`AdminAccount` is a versioned bot AccountScope row at `ADMIN_ACCOUNT#<configured
Telegram user id>` with `auth_epoch`, creation/update timestamps and no revoked_at.
Only initial admin link issuance creates it; revocation increments the epoch.
The configured identity, not the presence of this row, grants the admin role.
Authorization exposes `admin_epoch` independently of the doctor's `auth_epoch`.

LoginExchange admits `admin_login` and intended_role `admin`; TokenHead admits
that purpose. Admin exchanges and WebSessions require doctor_id and all patient,
binding and consent fields to be null. Doctor/patient sessions still require a
nonblank doctor id and their original role-specific fields. `WebSession` is typed
by its doctor-id value: existing clinical consumers retain the string variant;
identity decoding uses `AnyWebSession` (`str | None`) with the same role validators.
Both variants serialize the same `web_session` record family. No migration or
weakened clinical decoder is required.

Account commands optionally carry selected session_role and admin_epoch; omitted
browser metadata does not change existing Telegram command fingerprints. Browser
principals cannot omit this role at commit. Admin mutation checks condition the
AdminAccount version along with the account target versions. The browser inbound
receipt has transport/channel `web-admin`; account audit events optionally carry
that channel. Clinical event payloads and their command semantics are unchanged.

## Patient browser controls (contract 18d)

`OutboundIntent.delivered_text` is optional private text, absent for legacy rows
and pending delivery. Patient delivery completion may set it once to the exact
rendered text. Only patient/provider-accepted records admit a value; subsequent
completion cannot replace it. Missing text does not authorize reconstruction.
The existing status name also represents web store-and-show acceptance, with a
`web:<intent-id>` acceptance reference rather than a Telegram message ID.

Web message and preference receipts retain the originating `web_session_id` in
their private payload, use channel `web`, and transport `web-message` or
`web-preference`. Their deterministic transport key includes the session ID,
command ID and body digest. The existing callback payload carries only the
hashed patient-action token, with no fabricated Telegram callback-query ID.

A browser-command reservation is an operational CAS item under the bot account
partition: `WEB_COMMAND#sha256(session-id:command-id)`, version 1, body digest,
and TTL equal to the originating session's absolute expiry. It contains no
patient text. A crash after reservation can retry the same receipt; only
`accept_inbound` confers durable receipt acceptance. Expiry authorization never
waits for TTL cleanup.

A `SetContactPreference` command carrying its receipt's originating session ID
may atomically advance that session's consent version alongside the existing
Patient/Consent/PatientBinding/PatientProfile update. The session's identity,
CSRF secret, binding, authority, expiry and revocation fields cannot change.
The guard verifies live authority and conditions the exact old session version.
Other sessions keep their previous consent snapshot and existing refusal.

`patient_receipts(PatientScope)` exposes only owned inbound records, including
historical transport-key records. The current DynamoDB implementation uses a
filtered, internally paginated consistent scan and ownership-checked key reads;
API pagination bounds returned conversation entries rather than scan cost.
No global receipt key or cursor grants clinical access. A future indexed access
pattern must account for historical rows before replacing this reader.
