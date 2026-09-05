# Sanad Domain and Store Contract

Version: 1.0 · Proposed reconciled schema for the final audit · 2026-09-05

This blueprint is implementation input after final audit; it is not executable code or a released contract. [architecture.md](architecture.md) owns the system flow. Contract 00 defines types only; contract 01 implements transitions/deadlines; 02–03 implement storage and recovery. Later contracts extend this schema explicitly before adding behavior. Existing `pending_review`/`done` execution terminology is superseded by the split below; DONE remains a message class.

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

| Entity | Fields beyond common metadata / rules |
|---|---|
| Doctor | telegram_bot_id, telegram_user_id, private_chat_id, name, specialty, city, language, timezone, status (pending/approved/rejected/suspended/revoked), approved_by/at optional, auth_epoch, policy_version, clinical_policy_version optional, approval_reference optional |
| DoctorPolicy | policy_version, timezone, quiet_hours, daily_chase_limit, per_mission_chase_limit, evidence_request_limit, barrier_question/search_limits, default_deadlines by kind, default_grace_seconds=0, permitted_inference_bounds, draft_review_interval, result_review_interval, pause_max_interval, followup_response_window, incident_coverage_policy_id, knowledge_scope_ids, session_idle/absolute_ttl, invitation_ttl; versioned and applicability-scoped |
| Application | bot_id, telegram_user_id, private_chat_id, claimed_name/specialty/city, status (pending/approved/rejected), reviewer_id/at, approval_reference; pending applications have operational review ownership |
| SubjectBinding | key(bot_id, telegram_user_id), role_set, doctor_id optional, patient_id optional, status (active/frozen/revoked), binding_epoch; admin+doctor may coexist, patient role is exclusive of clinician/admin roles |
| Patient | immutable doctor_id, display_name, identifiers allowed by policy, date_of_birth/age/sex optional with provenance, timezone, language, contact_status (awaiting_link/active/paused/opted_out/unreachable/frozen), active_binding_id optional, consent_id/version optional, record_version, delivery_epoch, safety_epoch, lease_generation, lease_owner/expiry optional, current_plan_id/version optional; clinical history is typed facts, not unversioned active orders |
| Consent | patient_id, binding_id, version, policy_text_version, accepted_at/by, permitted_channels, routine_contact_enabled, urgent_response_policy_id, scheduled_slot_consents, quiet_hours, withdrawn_at optional; changing it increments Patient.delivery_epoch |
| Invitation | token_hash, doctor_id, patient_id, issued_by, expires_at, state (issued/claimed/consumed/revoked/expired), pending_claim_id optional, consumed_at optional, review_at, WorkClock while unfinished; proposed default expiry 24 hours, shown to doctor |
| PatientClaim | invitation_id, candidate_subject, minimal_claim_identifier, consent_version, proof_method/reference, doctor_confirmed_by/at optional, state (pending/approved/rejected/expired), review_at, WorkClock; no record disclosure while pending |
| PatientBinding | patient_id, subject_key, status (active/frozen/revoked), binding_epoch, claim_id, consent_id/version, doctor_confirmed_by/at; doctor/patient ownership fixed at activation |
| LoginExchange | token_hash, intended_role/subject, binding_id optional, auth_epoch, consent_version optional, issued_at, expires_at (doctor default 10 minutes), state (issued/consumed/revoked/expired), consumed_at optional; GET is non-consuming, POST atomic consumption only |
| WebSession | session_hash, role, subject, doctor_id, patient_id/binding_id optional, auth_epoch, binding_epoch/consent_version optional, csrf_secret_ref, issued_at, last_seen_at, idle_expires_at, absolute_expires_at, revoked_at optional; no PHI in cookie |
| ClinicalFact | category (condition/allergy/history/medication_history/demographic/patient_report), typed payload, Provenance, effective_at optional, visibility (doctor_private/patient_released), supersedes_fact_id optional; immutable accepted facts |
| CareOrderVersion | order_id, order_version, type, structured_instruction, Provenance, confirmed_by/at, effective_from, effective_to only if prescribed, supersedes_version optional; immutable and sufficient for its supported executor |
| CareOrderHead | order_id, current_order_version, status (active/stopped/superseded), delivery_epoch, changed_by/at; current snapshot references the active version only |
| CarePlan | plan_id, plan_version, order_refs, mission_ids, followup_ids, status (proposed/confirmed/superseded), confirmed_by/at optional, source_proposal_id; a confirmed batch explicitly records accepted and deferred items |
| Proposal | patient_candidate_ids, selected_patient_id optional, proposed fact/order/mission deltas, base_versions, source_observation_ids, created_by_agent/command, validation_results, expires_at, confirmation_nonce_hash, status (pending/confirmed/rejected/expired), review_at, WorkClock while pending |
| IntakeDraft | owner_doctor_id, source_receipt_ids, media_work_ids, proposal_id optional, selected_patient_id optional, state (pending/associated/rejected/expired), safety_epoch, ProcessingClaim, review_at, WorkClock until associated/rejected or handed to timed disposition; unassigned material remains doctor-private |

Invitation confirmation transaction checks: valid unreplayed invitation/claim, verified private subject, consent, doctor authority, unchanged patient owner/version, absent conflicting global SubjectBinding, and current invitation generation. It writes consumption, approved claim, PatientBinding, global SubjectBinding and patient contact/consent references together. Reissue revokes pending claims. No automatic merge or reassignment occurs.

Lost/mistaken binding resolution uses explicit authenticated commands, records a reason, freezes old access, increments relevant epochs and revokes exchanges/sessions. A clinician can own a patient before that patient is linked; clinical disclosure and routine contact still require final consent/binding. The mission deadline is never silently shifted to compensate for late enrollment.

## Missions, follow-up and review

| Entity | Fields beyond common metadata / rules |
|---|---|
| Mission | kind (TEST/MONITOR/MEDICATION/SEND_RECORDS/VISIT/QUESTION/TASK), title, typed details, objective_predicate, order_refs, state, fulfillment_validity, fulfillment_event_id optional, fulfilled_at optional, objective_received_at optional, timeliness (undetermined/on_time/late), evidence_refs, confirmed_at/by optional, source_proposal_id optional, due_at, due_source (doctor/scribe/default), due_reason, timing_anchor, original_time_expression optional, timezone, grace_seconds, escalation_at, review_at, resume_at optional, next_contact_at optional, deadline_generation, WorkClock while nonterminal, contact_count/unanswered_delivered_count/evidence_request_count, barrier_type/reason optional, danger_history flag, latest_deadline_notice_event_id optional |
| FollowUpTask | kind (MEDICATION_DAY3/CLINICAL_CHECKIN), parent_mission_id, order_refs, confirmed_by/at, anchor_kind (reported_effective_start/reported_effective_change/doctor_specified_date), anchor_time/source_ref optional while awaiting_anchor, prompt_at/due_at optional only while awaiting_anchor, review_at (always required), response_predicate, source_report_ids, state (awaiting_anchor/scheduled/waiting_response/fulfilled/overdue/contact_suppressed/cancelled), suppression_reason optional, consent_slot_id, review_obligation_id optional, done_intent_id optional (fulfilled creates a DONE:FULFILLMENT intent per Decision 012), WorkClock until fulfilled/cancelled or atomically handed to that independently timed ReviewObligation |
| ReviewObligation | source_type/id/version, review_kind (result_review/correction_disposition/incident_response/unmet_objective/question_answer/media_failure/delivery_failure/binding_review/coverage_review/followup_disposition/evidence_association/intake_clarification), unique_source_key, owner_doctor_id, patient_id optional, source_mission_id optional, state (open/acknowledged/resolved), review_at, next_action_at, coverage_status (covered/blocked), acknowledged_by/at optional, resolved_by/at/reason/action_event_id optional, first_notice_at optional, last_material_change_version, WorkClock until resolved |
| Card | card_class (DANGER/DONE/DEADLINE/CONFIRM/QUESTION/INFO), source_event_id, source_version, review_obligation_ids, allowed_action_types with expected_versions, acknowledged_at optional, resolved_at optional, rendered_payload_ref, delivery_intent_ids; mutable presentation derived from immutable events and current obligations |
| IntakeConcern | intake_id, unique_source_key, source_receipt/evidence_refs, rule_family/version, severity, facts_ref, state (open/associated/resolved), owner_doctor_id, review_obligation_id, associated_patient_id/event_id optional, prior_delivery_refs; doctor-private concern before patient association, independently timed through its ReviewObligation |
| Incident | unique_source_key, observation/evidence_id and version, rule_family/version, severity, verified_status (unverified/verified), facts_ref, state (open/resolved), resolution_event_id optional, raised_at, review_obligation_id, alert_intent_ids; source dedupe does not suppress a later independent event |
| BundleSchedule | doctor_id, next_action_at, last_provider_accepted_at optional, generation, pending_bundle_intent_id optional, interval_seconds (7 days by recorded policy), WorkClock; unresolved eligibility is queried from obligations and rechecked before send |
| ContactReservation | patient_id, local_day or explicit slot_id, budget_kind, source_intent_id, state (reserved/consumed/released), fence_generation, reserved_at, delivery_attempt_id optional; one atomic key owns each allowed budget slot |

`Mission.review_status` is a **derived projection**, not another writable clinical decision: `not_required | pending | acknowledged | reviewed | correction_requested`. Pending/acknowledged/resolved ReviewObligations determine it; an open correction takes priority over older reviewed evidence. A done-looking execution card cannot hide open reviews.

`fulfillment_validity` is `not_fulfilled | valid | invalidated_pending_review`. Only state=fulfilled plus validity=valid and a currently satisfied objective predicate may produce a new DONE notice. Invalidated historical fulfillment stays in audit and cannot trigger clinical reassurance, resume contact or repeat DONE until an explicit reviewed correction/reopen disposition establishes a new valid version.

A correction to a previously delivered report is a separate proposed `DONE:CORRECTION` purpose under Decision 004, not a new fulfillment announcement. It requires the exact previous report/source version and accepted correcting version, explicitly states that the previous conclusion changed, and creates review even when validity is invalidated_pending_review. If danger rules apply it uses DANGER instead. Deduplication keys include the correcting version; this exception never labels an unmet objective completed.

Review source uniqueness is `(doctor, source_type, source_id, source_version, review_kind)`. Retrying creation returns the same obligation. A materially new version creates a new source key; resolving an old version never resolves a new one by accident. Cardinality is per review kind because one evidence version may require result review and an independent incident response. An incident has exactly one incident-response obligation.

### Mission details by kind

| Kind | Required executable details and objective |
|---|---|
| TEST | named tests/analytes or imaging request, collection window, identity matching policy, units/accepted representations, completeness predicate; preparation only if doctor supplied. Verified required results fulfill; missing/contradictory pieces stay incomplete. |
| MONITOR | metric, units, dated slots/windows, required coverage, duplicate/late policy, approved target/safety references, consented prompt slots; valid correctly assigned observations satisfy explicit coverage, never silence. |
| MEDICATION | action START/STOP/CHANGE, exact CareOrderVersion, patient-report predicate; START requires the patient explicitly reporting the confirmed action. It does not prove ingestion/adherence. For START, independently create MEDICATION_DAY3 on doctor confirmation, with an explicit anchor or awaiting_anchor state and required review clock. START acknowledgment anchors/preserves it. STOP/CHANGE create no automatic day-three task; only an explicitly doctor-requested CLINICAL_CHECKIN adds follow-up. |
| SEND_RECORDS | categories, historical period optional, required document count/completeness; correct readable historical documents fulfill without the TEST post-order date rule. |
| VISIT | requested/booking/attendance/report objective chosen explicitly, requested date/window, approved preparation optional, pre_visit_brief_at if enabled; a booking report is not attendance. |
| QUESTION | patient's original question, source observation, owner doctor, due_at=created_at+48 hours, zero default grace; automatically opened support ticket, not a treatment order; answer command fulfills. |
| TASK | supported task category, exact doctor instruction, measurable completion/report predicate, required patient response, optional approved symptom/self-care/preparation questions. Unsupported clinical actions stay proposed with timed clarification. |

A generic task cannot execute arbitrary model-generated code or substitute for an unsupported treatment. Symptom checks, self-care/preparation and administrative requests remain required supported workflows when explicitly doctor-defined and validated by the clinical policy.

## Mission state and event contract

States: `proposed`, `awaiting_link`, `open`, `waiting_patient`, `blocked`, `unreachable`, `overdue`, `fulfilled`, `cancelled`, `closed_unfulfilled`, `superseded`.

Terminal execution states: fulfilled, cancelled, closed_unfulfilled, superseded. No clock transition creates terminal success or clinical resolution. All other states carry a next action and review time. `blocked` means blocked execution/contact with a reason and bounded resume/review time; patient contact opt-out is an orthogonal Patient status, not mission cancellation.

Aliases used in the table: A = open/waiting_patient/blocked/unreachable/overdue; U = awaiting_link plus A; T = the four terminal states. An event not listed for a state is illegal unless it is an explicit state-preserving event below. `transition` returns a new aggregate plus required event/work/review effects, or a typed rejection; it does not write or send.

The mission event enum is:

`PROPOSAL_CREATED`, `CONFIRM_MISSION`, `CREATE_SUPPORT_TICKET`, `PATIENT_BOUND`, `CONTACT_ACCEPTED`, `PATIENT_REPLIED`, `BARRIER_RECORDED`, `BARRIER_RESOLVED`, `CONTACT_EXHAUSTED`, `PAUSE_CONTACT`, `RESUME_CONTACT`, `DEADLINE_REACHED`, `OBJECTIVE_FULFILLED`, `DOCTOR_EXTEND`, `DOCTOR_CANCEL`, `DOCTOR_CLOSE_UNFULFILLED`, `DOCTOR_REOPEN`, `ORDER_SUPERSEDED`, `CORRECT_ACCEPTED_EVIDENCE`, `VALIDATE_CORRECTION`, `LATE_INPUT_RECORDED`, `EVIDENCE_ASSOCIATED`, `REVIEW_ACKNOWLEDGED`, `REVIEW_RESOLVED`, `SAFETY_INCIDENT_RAISED`, `CONTACT_PREFERENCE_CHANGED`.

| Event | Allowed source | Result and guard |
|---|---|---|
| PROPOSAL_CREATED | creation only | proposed with administrative review clock; unconfirmed clinical fields remain candidates |
| CONFIRM_MISSION | proposed | open when consented binding is active, else awaiting_link; only doctor-authorized, complete valid instructions; establish immutable confirmed_at and order refs |
| CREATE_SUPPORT_TICKET | creation only | open QUESTION from authenticated patient lane, no doctor tap; create linked question-answer obligation, 48-hour due, no treatment order |
| PATIENT_BOUND | awaiting_link/overdue | from awaiting_link: open before escalation_at, otherwise overdue; from overdue: remain overdue while activating the confirmed binding. Preserve due time and outstanding deadline review; consent/binding proof required. Repeated binding is idempotent, never an implicit extension |
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

FollowUpTask events are CONFIRM_FOLLOWUP, ANCHOR_CONFIRMED, PROMPT_ACCEPTED, RESPONSE_RECEIVED, FOLLOWUP_DEADLINE, SUPPRESS_FOLLOWUP_CONTACT, CANCEL_FOLLOWUP. Confirmation creates awaiting_anchor when actual start/change date is unknown, otherwise scheduled from the explicit doctor date. ANCHOR_CONFIRMED records the patient-reported effective date or doctor-approved date and schedules the independent task. Scheduled → waiting_response only after accepted prompt; valid response → fulfilled plus a DONE:FULFILLMENT intent (merged into a same-source DANGER when danger rules apply); deadline → overdue plus review and a DEADLINE notice; suppression → contact_suppressed plus timed disposition; explicit cancellation → cancelled while existing clinical/incident reviews survive. No parent mission terminal state performs these transitions automatically.

Review events are CREATE_REVIEW, ACKNOWLEDGE_REVIEW, RESOLVE_REVIEW, BLOCK_COVERAGE, RESTORE_COVERAGE, MATERIAL_CHANGE. Acknowledge preserves open responsibility; only an authorized resolution with the required source versions/reason ends it. Superseding a reviewed source creates a fresh obligation when needed.

### Evidence receipt and verification timing

For an accepted predicate-completing evidence set, store `objective_received_at = max(received_at of the source receipts actually required by that predicate)`, alongside `fulfilled_at` (the later verification/acceptance transaction time). Collection/sample-date requirements are separate predicate checks. `timeliness` is `undetermined` until the set is verified, then `on_time` if objective_received_at <= due_at, otherwise `late`. Grace affects notification time, not whether the target date was met.

At escalation_at, an unverified on-time candidate cannot prove fulfillment. The deadline report states “evidence received; verification pending” with the actual missing/failed processing step, never that the patient failed to submit. It creates/retains timed processing review. Once verification succeeds, update the original deadline disposition with on-time/late facts, preserve the original message/audit, and create independent clinical result review. A correction retains historical timing and recomputes the current version explicitly; it never rewrites a previous event.

## Clock calculations

`due_at` is the objective target. `escalation_at = due_at + grace_seconds`; reconciled default grace is zero for every type. A doctor-configured nonzero grace is explicit on the confirmation card. Inferred-only bounds of 1–180 days validate timing candidates, never clamp explicit clinical instructions. Original local time/anchor/source/reason is retained. Invalid explicit or clinically incomplete instructions ask on the existing card; no guessed dose/duration.

`review_at` is the administrative accountability deadline; it is not a new prescribed test/medication date. Proposed, awaiting-link, blocked and unreachable states all retain it. `resume_at` is a bounded proposed contact pause ending, never a replacement due time. ReviewObligation.review_at is independent of Mission.review_at: a result review is due at fulfilled_at plus the approved result-review interval (proposed three days), never three days after the original draft. It remains visible after execution ends.

For each nonterminal mission, `next_action_at = min(eligible next_contact_at, due escalation if unreported, review_at, resume_at when present)`. Already-handled deadlines are excluded using deadline generation/event state; they cannot create a hot loop. Every handled wake atomically records its disposition and a strictly future next action or terminal execution state. A contact cap/quiet-hour/opt-out check excludes only contact from this minimum, not review/deadline work.

Independent records contribute separate due-index entries: each ReviewObligation, FollowUpTask, BundleSchedule, receipt, media job and outbox item. Their clocks do not depend on a terminal mission retaining a wake. The day-three anchor rule is printed on confirmation: use the reported effective start/change date, or an explicit doctor date. Plan confirmation time is never evidence the patient started. Awaiting-anchor tasks retain review_at and next_action_at, ask for the missing date and create disposition if unanswered. Once anchored, prompt_at is anchor plus three days; a later correction requires an explicit source/version update and revalidation. The response window and clinical policy are separately approved defaults.

Bundle eligibility starts seven days after the first accepted DEADLINE notice for an unresolved item. One per-doctor BundleSchedule collects eligible items and sends at most one accepted weekly bundle. Only accepted delivery advances last_provider_accepted_at; uncertain delivery takes the bounded uncertainty/review path. Each candidate obligation is re-read, with resolved items omitted. An empty bundle clears its current due work; a new eligible obligation re-arms it transactionally.

ReviewObligation `first_notice_at` records provider acceptance, not queuing or acknowledgment. Its original `review_at` remains unchanged for overdue visibility; after each handled review wake, `next_action_at` becomes the next policy accountability check or material-change work time. Routine rechecks do not re-ring individual cards. BundleSchedule handles the approved weekly message; incident response retains its own earlier policy clock. A fulfilled or contact-suppressed follow-up may drop its execution clock only when any required unresolved review is durably linked and independently scheduled in the same transaction.

Index delays or a missed tick do not lose deadlines: due records stay due until atomically handled. Sweep metrics show oldest overdue work, claims expired, processing failures and coverage blocks. Periodic reconciliation rechecks mandatory clocks against canonical status using paginated tenant partitions and repairs missing derived entries under version checks; TTL is not the scheduler.

## Inbound, media and delivery schema

| Entity | Required fields / lifecycle |
|---|---|
| InboundReceipt | transport_key (bot+update_id, or session+client_command_id), source_subject/chat, channel, kind, immutable payload or protected payload_ref, provider_media_handle optional, received_at, safety_screen_state/policy_version, state (pending/processing/completed/needs_attention), ProcessingClaim, WorkClock until completed, result_event_ids; scope established by ingress, never payload claims |
| MediaWork | receipt_id, provider_handle_ref, source_blob_ref optional, normalized_blob_ref optional, byte_hash optional, mime/size/duration, stage (fetch/normalize/extract/associate), state (pending/processing/completed/needs_attention), ProcessingClaim, WorkClock, last_error/resend_intent_id optional |
| PhotoReceipt | authenticated_scope (patient scope or owning-doctor IntakeDraft), content_hash, processing_version, media_work_id, evidence_id optional, state (pending/processing/completed/needs_attention), source_receipt_ids; deduping cannot cross scopes or hide unfinished work |
| Evidence | observation_id, source_blob_ref, normalized_blob_ref, content_hash, category, printed_identity/date optional, association_state (unmatched/candidate/accepted/rejected), patient_match_provenance, extracted_values with units, required_predicate_results, accepted_by/at optional, supersedes_evidence_id/version optional, Provenance; immutable accepted versions, current head separately points to chosen version |
| EvidenceHead | evidence_id, current_version, status (candidate/accepted/rejected/superseded), association refs; explicit correction is the only supersession authority |
| Candidate | observation_id, candidate_kind, typed payload, Provenance, validation_results, status (proposed/accepted/rejected/needs_confirmation), source_versions; never direct current-order authority |
| AuditEvent | event_id, command_id, scope, event_type, aggregate_refs, before/after_versions, actor, accepted_at, policy_versions, source_refs, protected payload_ref optional; immutable, no mutable card.resolved field |
| CommandReceipt | command_id, scope, payload_digest, accepted_result, accepted_at; reused ID with different payload is rejected |
| OutboundIntent | logical_key, source_event_ids/versions, scope_kind, audience, recipient_ref, notification_purpose, variant-specific authority/freshness fields (defined below), eligibility_class, payload_ref, payload_digest, conversation_sequence, slot_id optional, expires_at, status (queued/sending/provider_accepted/uncertain/failed/suppressed), delivery_claim, accepted_message_id/at optional, retry_count, uncertain_retry_count, last_error, suppression_reason optional, WorkClock until accepted/suppressed or transferred to timed review |
| DeliveryAttempt | intent_id, attempt_id, lease_generation, freshness_snapshot, started_at, ended_at optional, outcome (started/provider_accepted/uncertain/definite_failure), provider_message_id optional, redacted_error_code; persisted attempt-start makes an interrupted send uncertain, not automatically unsent |
| SessionSnapshot | session_key (Coordinator+mission or Concierge+patient), patient_id, bounded turn records/summary reference, source_order_versions, safety_epoch, fence_generation, session_version; memory only, fenced writes required |
| OperationalIssue | kind, affected_scope refs without unnecessary clinical payload, owner_role/id, status (open/acknowledged/resolved), due_at, reason, WorkClock; covers infrastructure/account work without silently granting admin clinical access |
| ReplayNonce | issuer/key_id, nonce_digest, timestamp, expires_at; conditional insert prevents tick/callback replay |

### Outbound scope and purpose variants

OutboundIntent has common delivery metadata plus a discriminated `scope_kind` and `audience`; patient-only fields are not fabricated for account/intake messages. Common fields include `recipient_ref` (server-resolved verified private channel subject/chat), `notification_purpose` (solicited_reply, DANGER, DONE:FULFILLMENT, DONE:CORRECTION, DEADLINE, routine_prompt or patient_safety_response), source versions, payload reference/hash, expiry and delivery/work state.

| Variant | Required authority/freshness fields and guard |
|---|---|
| account → applicant/admin/doctor | bot_id, recipient_subject, application/account source version, recipient role/status and auth_epoch when an approved account exists. Application acknowledgments may reach the verified pending applicant; approval actions still need current admin authority. No patient fields or clinical payload |
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
