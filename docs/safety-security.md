# Identity, privacy and clinical safety

Version: 1.0 · Reconciled full-build specification · No clinical or technical safety certification is implied; release evidence is recorded per contract.

## Doctor approval and access

An inbound Telegram identity comes from the verified Bot API payload and a private chat, never a name, username or a message saying "I am a doctor". `/start` creates an application. The administrator's authenticated action approves the account; approval records who acted, when and the resulting role/version. Applications cannot grant admin privileges. Group messages do not expose patient information or create clinical bindings.

Every application route, tool and store query derives doctor scope from the authenticated server-side principal. Patient IDs in model arguments or URLs cannot change that scope. A doctor can search, read and modify only their own panel. Media serving, exports, callbacks, dashboard filters and read-only agent tools enforce the same boundary. Administrative approval access is account metadata by default, not permission to browse all clinical records.

Suspension/revocation increments the doctor's authorization epoch, invalidates login exchanges/sessions and rejects new commands. Unsent routine work is suppressed, affected patients and outstanding obligations enter operational review, and audit history is preserved. No patient is automatically transferred to another doctor. Suspension does not erase an outstanding safety incident.

## Dashboard login and sessions

The current design uses a random one-time exchange delivered to the approved doctor's Telegram account. `/d/<token>` renders a neutral Continue page. Only the explicit POST consumes the token, preventing ordinary link previews from burning it. Store the token hash, enforce ten-minute expiry and single-use consumption, rotate the session, and redirect to a clean dashboard URL.

Use Secure, HttpOnly, SameSite cookies; CSRF protection for writes; server-side revocation and an authorization-epoch check on every request. Reject expired exchanges and stale sessions after suspension. The transient exchange is the sole permitted doctor URL secret: redact or suppress it in proxy/access/application logs, telemetry, analytics and error reporting; use a no-referrer policy and no third-party content on the exchange page. Do not place lasting credentials in URLs or localStorage.

Reissuing a link revokes previous unconsumed exchanges as specified by the login contract. Callback actions also validate actor, target ownership, version, expiry and one-time action semantics. A captured old confirmation cannot update a newly amended record. Authentication and ownership checks apply before serving patient media, including any temporary object-download capability.

## Patient QR, consent and exclusive binding

The QR contains a random opaque invitation token, no clinical data. Store only its hash, expiry and lifecycle state. Reissue/revoke invalidates the old invitation. A scan shows the intended doctor's identity and a neutral claim flow; it does not reveal the existing patient's name, diagnosis, medications or history.

The intended patient submits a minimal claim and explicit consent. The doctor then confirms through an authenticated action that this claimant is the intended patient. Until both occur, the claim releases no clinical data and cannot start routine contact. This extra confirmation is an identity check; it does not require the doctor to reapprove the already confirmed plan.

Final binding is atomic across the invitation, claim, patient and a global `(environment, bot, Telegram subject)` binding index. One bot account subject cannot bind to two patient records or two doctors. Reject expiry, replay, second claims and existing conflicting bindings without exposing another doctor's details. Competing claims are resolved through the explicit identity-recovery workflow, never by "first scanner gets the chart".

Browser use requires an authenticated patient session after the claim is approved; possession of the burned invitation is not continuing access. The browser claim must remain tied to the verified patient identity and approved claim, with its own revocable cookie. The contract must prove the browser-to-Telegram handoff and cannot assume a web request establishes a Telegram identity. A browser-only judge account uses an explicitly isolated synthetic enrollment flow, never a weaker clinical binding path.

One private identity represents one patient in the agreed first release. Caregiver, account-loss and accidental-binding situations freeze sensitive access/contact until authenticated recovery. A claim such as "I'm his wife" does not grant access. Recovery preserves history and requires an explicit doctor/admin action; it is not an automatic merge or transfer.

## Consent and contact preferences

Before disclosure, explain the AI role, the relationship with the named doctor, which data will be processed, the involved channel/cloud/model providers, routine contact schedule, limitations, opt-out, retention and clinic contact. Keep a versioned consent record. Telegram bot messaging must not be presented as end-to-end encrypted clinical communication.

Doctor-confirmed scheduled prompts require patient consent to their cadence and any quiet-hour exception. A disabled reminder does not alter medication or measurement timing. Patient stop immediately suppresses routine queued and future messages; the clinical plan and outstanding review obligations remain visible. The bot can screen and respond to later patient-initiated messages under the approved safety/consent policy. Renewed proactive contact requires explicit renewed consent.

## Independent safety processing

Durably record permitted inbound work before acknowledging receipt. The safety lane examines all permitted text and captions before conversational caps, ordinary leases, agent calls or conversation routing. A new urgent text can be evaluated while another turn is waiting on transcription/model work. Routine concurrency protection cannot block safety classification or the urgent delivery gateway.

Deterministic approved symptom/concept and numeric rules can raise concern. Bounded model interpretation may add concern, never lower a deterministic concern. Known values require metric, unit, collection/context and applicable policy. A raw number without a known unit is not silently normalized into an accepted clinical measurement.

Voice and images need bounded extraction before their hidden content can be screened. Screen available captions immediately, prioritize extraction, and record processing failures with a timed unresolved-media item and a patient resend/type route. Do not claim an unreadable image or failed transcript was clinically screened. A possible critical result with uncertain identity/unit/legibility can create an urgent unverified concern under an approved rule, while remaining unaccepted evidence; uncertainty must not be converted into reassurance.

Patient urgent guidance uses approved local-care wording immediately when its rule triggers. It does not wait for a language model, doctor delivery or acknowledgment. The doctor alert uses the same controlled Liaison gateway, with a deterministic fallback when prose generation is unavailable. Include source and uncertainty; do not assert a diagnosis. State delivery accurately: queued/uncertain/failed is not "your doctor knows", and provider acceptance is not a claim that the doctor read it.

Safety incidents are durable objects with their own owner, delivery state, acknowledgment, review time, next action and resolution. Completing or cancelling a mission does not resolve an incident. Notification failure remains actionable through retry/reconciliation and operational coverage. Repeated uncertain urgent delivery follows an explicit bounded policy and labels the same incident rather than presenting duplicate new incidents.

## Clinical boundaries and useful education

The bot cannot autonomously diagnose, prescribe, change/stop a medication, substitute a drug/test, choose preparation instructions, resolve an interaction or declare a result clinically clear. The doctor can issue those instructions within their own clinical practice; Sanad must capture, preview and confirm the exact instruction before it becomes active structured data. Patient questions or photographed old prescriptions cannot issue new orders.

Useful general education is part of the required patient feature. Deliver a small reviewed source set with that slice: concise disease explanations, the purpose of doctor-ordered monitoring/tests, interpretation of plan terminology and practical adherence questions within scope. Record source URL/title, retrieval/review date, reviewer, permitted topics, excerpt/summary provenance and version. This is a limited initial content responsibility, not a prerequisite to author an encyclopedic library.

The Concierge may retrieve from that approved set and explain the active plan in the patient's language. It distinguishes general information from personalized instructions, traces plan-specific numbers and medications to active orders, and provides understandable source labels where useful. If coverage is missing, explain the limitation and create an appropriate question/review path. Do not manufacture a source or suppress every useful answer because no large library exists. No patient-identifying details are sent into public web searches.

The source set and translated examples require clinical review as part of feature acceptance. Include adversarial cases: disguised dose changes, unsupported reassurance, an old order quoted as current, a patient claiming clinician authority, ambiguous symptoms and numerically plausible but wrong medication advice. The output validator is one enforceable boundary, not a proof that arbitrary generated health text is correct.

Supported cohorts, red-flag protocols, analyte thresholds/units, emergency wording, clinician-response expectations and fallback coverage must have explicit versioned approval. Copied legacy tables retain provenance and tests; copying does not validate them for new cohorts or specialties. Synthetic policy fixtures let development proceed. Clinical operation requires approved policies for the intended use; a missing protocol cannot be treated as a normal/safe result.

## Untrusted inputs, files and outputs

Verify webhook authenticity; deduplicate with durable receipts; validate server-side schemas and actor/target ownership. File handling uses declared product limits, MIME/content checks, safe decoders, bounded image processing/transcoding, time/memory limits and non-executable storage. Do not fetch arbitrary model-provided URLs or execute attachments. The first media contract covers text, voice and images; any additional file format requires its own supported parser and rejection behavior.

Reject unsupported/oversized media with a useful resend route and preserve a timed unresolved request where needed. Input rejection cannot discard already visible danger in its caption/text. Provider file URLs, bot tokens, signed capabilities and clinical content are excluded from ordinary logs.

Treat all patient/doctor text, OCR, transcription and model output as untrusted data. Escape Telegram/HTML output, generate links server-side, check callback replay and stale revisions, and defend against prompt injection, role spoofing, IDOR, CSRF and session fixation. Trace metadata records roles, tool outcomes, latency, model/policy versions and event IDs by default; raw patient text/images are not a telemetry requirement.

Versioned active orders are the sole executable clinical view. History remains clearly labelled. The sender checks consent, identity, order and mission versions immediately before delivering a queued instruction; a stale item is cancelled/recomputed through the Steward, not sent because it was valid when queued.

## Data lifecycle and operational release

Separate synthetic development/judge environments from clinical deployment. No real identifiers in the repository, test assets, videos, screenshots or public traces. Store evidence privately, authorize each access and retain its provenance. Export/delete/retention/account-recovery requests use authenticated workflows; a conversational suggestion is not authorization for irreversible record deletion.

The full build includes backup/restore verification, restore-time reconciliation, access revocation, retention/deletion behavior, monitored failed delivery, operational ownership and incident response. Before clinical use, confirm applicable privacy/medical duties, controller/operator responsibilities, provider terms and processing regions with qualified review, then approve consent and clinic response coverage. This is an operational acceptance gate within the required full build, not permission to stop at a synthetic demonstration or claim legal compliance.

Language/accessibility checks include the actual Arabic/English mix used by the doctor and patients, drug names and numbers in voice notes, readable mobile consent, and safety messages understood by the intended patient. A live provider demonstration, hermetic tests and clinical review establish different evidence and must be reported separately.
