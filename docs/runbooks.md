# Sanad development runbooks

These procedures operate only the synthetic `sanad-dev` stack in us-east-1.
The operator must have the approved account in `AWS_ACCOUNT_ID` and use the
deployment environment. Commands use its Python environment. Clinical activation
remains closed; [clinical readiness](clinical-readiness.md) belongs to the owner.
Each incident below is a separate runbook page.

New restore, records and release-record commands require `--env dev`, print
counts, and default to dry-run. Repeat with `--yes` only to execute the reviewed
operation. Health-report is always read-only. Existing tick, rollback and secrets
commands execute immediately. Never print secrets or put raw logs, exports or
patient content in a public release record.

## Process dies after inbound ACK

1. Run `python -m deploy.ops health-report --env dev`. Read the oldest unfinished
   inbound/media ages, current expired claims and Lambda errors.
2. Run `python -m deploy.ops logs tail --env dev`; inspect sanitized app/relay
   errors. In DynamoDB console → Tables → sanad-dev-data → Explore table items,
   inspect the affected receipt's state and processing claim privately.
3. Once the worker fault is corrected, run `python -m deploy.ops tick fire --env dev`.
   This invokes normal persisted-work recovery; do not delete the receipt or reset
   its generation. A targeted receipt-replay operator command is not available in this build.
4. Confirm the same receipt is completed with its result event IDs, or visibly
   needs attention with its independent review. Check that a duplicate does not
   create another logical action. Resolved means no lost acknowledged input,
   no duplicate accepted mutation and an explicit disposition for failed work.

---

## Media/model failure

1. Run `python -m deploy.ops health-report --env dev` and
   `python -m deploy.ops logs tail --env dev`. Read oldest media work and Lambda errors;
   complete model/tool/ASR error rates are not measurable in this build.
2. The treating doctor opens `/inbox` in Telegram or Needs review in the doctor
   dashboard. Inspect the failed media review and retained original privately.
3. Restore the configured provider's availability or ask the sender to use the
   delivered resend/type request. Run `python -m deploy.ops tick fire --env dev`
   for normal bounded retry. Do not force a failed reading into accepted evidence.
4. Resolved means the input is processed with provenance, or a useful resend/type
   request and an owned unresolved-media review remain visible. A retry limit or
   provider outage is not successful extraction.

---

## Scheduler outage

1. Run `python -m deploy.ops health-report --env dev`. Read last tick acceptance,
   oldest due work via an authorized DynamoDB inspection and missing-work counts.
   Tick acceptance is not a lag measurement.
2. AWS console → EventBridge → Rules → sanad-dev-tick: inspect enabled state,
   one-minute schedule and relay target. Lambda → sanad-dev-relay → Monitor:
   inspect Errors and Throttles; CloudWatch → Alarms: inspect relayerrors.
3. After correcting the outage, run `python -m deploy.ops tick fire --env dev`.
   Repeat a bounded observation after the next scheduled minute; persistent work
   is reclaimed by normal sweeps. Do not alter clinical deadlines or directly
   recreate queued messages.
4. Confirm tick acceptance resumes, overdue work advances or has visible
   disposition, and old routine slots are coalesced/suppressed. Resolved means
   recovery is observed, not merely that the EventBridge rule is enabled.

---

## Unknown Telegram outcome

1. Run `python -m deploy.ops health-report --env dev`; read uncertain attempts in
   the last 24 hours. In the treating doctor's `/inbox`, read the delivery review.
2. Run `python -m deploy.ops webhook info --env dev` and sanitized
   `python -m deploy.ops logs tail --env dev` to distinguish a channel outage.
3. Leave uncertain intent/attempt records intact. Restore connectivity and allow
   `python -m deploy.ops tick fire --env dev` to apply the existing retry policy.
   Ordinary uncertainty goes to review; DANGER has its bounded same-incident retry.
4. The doctor records the permitted review disposition. Resolved means uncertainty
   has an explicit outcome or remains owned. Provider acceptance never proves reading.
   A manual force-resend command is not available in this build.

---

## Patient stop/order change/suspension

1. Read the patient's current contact setting and active orders in the treating
   doctor's dashboard. Inspect accepted and uncertain deliveries separately.
2. For stop, the patient uses Reminders → switch off in the patient browser, or the
   supported stop message in Telegram. For an order change, the treating doctor
   dictates the amendment and confirms its current before/after card.
3. For suspension, use the administrator's `/login admin` → Continue → `/admin`
   → selected doctor → Suspend with reason. These are separate clinical/contact actions.
4. Run `python -m deploy.ops tick fire --env dev` only after the normal actions
   commit. Confirm stale queued guidance is suppressed. Review already in-flight
   accepted/uncertain attempts and arrange any needed doctor-approved correction.
5. Resolved means stale unsent guidance is invalidated and in-flight races have
   a truthful disposition; an order change does not itself renew contact consent.

---

## Compromised account/token

1. Contain affected delivery: administrator → `/admin` → affected doctor →
   Suspend with reason. For a service-wide credential incident, AWS console →
   EventBridge → Rules → sanad-dev-tick → Disable; Lambda → sanad-dev-app →
   Configuration → Concurrency → reserve 0, after recording previous settings.
   Wait for already-running invocations to finish; these switches do not recall sends.
2. The affected account uses Telegram `/logout` or browser Sign out everywhere
   where it still controls the verified identity. If identity is lost, use the
   account recovery procedure; do not silently rebind it.
3. For bot compromise, rotate the dev bot token in BotFather, update the local
   deployment input privately, then `python -m deploy.ops secrets set --env dev`.
   This command preserves existing generated webhook/tick secrets; forced rotation
   of those values is not available as a dedicated operator command in this build.
   An authorized operator can use SSM → Parameter Store → /sanad/dev/ → selected
   SecureString → Edit to replace a compromised value without logging it.
4. A reviewed application deployment is required to refresh configuration:
   `python -m deploy.deploy --env dev --image <known-good-sha256-digest>`.
   Restore recorded concurrency for the smoke run under the incident owner's
   explicit resumption decision. Run `python -m deploy.ops webhook register --env dev`
   for a rotated bot/webhook secret. Check smokes, authentication and sanitized logs.
5. Restore the schedule and reinstate the doctor only after identity checks and
   authorization. If administrator configuration changes A → B → A, revoke A's
   old sessions with Sign out everywhere; old epochs can otherwise revive within TTL.
   Resolved means old credentials/sessions refuse access and new authentication passes.

---

## Bad release

1. Run `python -m deploy.ops health-report --env dev` and inspect sanitized logs.
   Identify the live image and last passing distinct image in
   `deploy/releases/dev.json`; do not use a mutable tag.
2. If ongoing work is harmful, contain automation using the EventBridge and
   Lambda concurrency paths in Compromised account/token. Record previous settings.
3. After authorization and compatible-schema confirmation, restore app concurrency
   for smokes and run `python -m deploy.rollback --env dev`.
   The command selects the previous passing distinct image and runs deployment smokes.
4. Read the appended release-history entry: expected digest, stored schema and
   every smoke status. A failure leaves the deployed image visible; there is no
   implicit rollback of valid events and no automatic schema migration.
5. Resolved means a compatible known-good image passes smokes, persisted work
   recovers, and the incident owner explicitly restores any paused automation.
   Data repair beyond these commands is not available in this build.

---

## Restore

1. Choose a new lowercase suffix. Preview
   `python -m deploy.restore --env dev --into <suffix> --at <aware-ISO-instant>`.
   Omit `--at` to use five minutes before command start. Check source, target,
   restore time and table count. The target must start sanad-dev-data-restore-.
2. Authorized architect rehearsal: repeat with `--yes`; save its printed JSON
   lines as `docs/evidence/restore-dev-<date>.jsonl`.
   No transport is constructed. Wait for the restore to become active and tagged.
3. Read all five partition-class counts, matching-key stored-version differences,
   oldest unfinished receipts/due work, and eligible/suppressed outbound counts
   on both sides. Queued retries use the imported freshness policy.
   A consistent scan is not an atomic cross-table snapshot.
4. Privately compare restored consent, binding, suspension and order epochs against
   current live authority. Old authority can predate revocations. Do not repoint
   the app, send from the restored table or treat eligibility counts as resume approval.
   Promotion/revocation repair is not available in this build.
5. Preview cleanup with `python -m deploy.restore --env dev --into <suffix> --delete`;
   repeat with `--yes`. A missing/mismatched ownership tag refuses deletion.
   If a restore succeeded but tagging failed, inspect its provenance in DynamoDB
   and CloudTrail; the command refuses adoption or deletion of an untagged table.
6. Resolved means isolated restore and reconciliation evidence are recorded and
   the owned rehearsal table is removed or explicitly retained.
   Run `python -m deploy.release_record --env dev`, then `--yes` to write the
   release record after review. Live promotion requires a separate owner decision.

---

## Doctor unavailable

1. The treating doctor or designated operational responder checks `/inbox` and
   the dashboard Needs review queue; danger, fulfillment and review are separate.
2. If access must be suspended, administrator → `/admin` → doctor → Suspend
   with reason. Preserve ownership and coverage reviews.
3. Use the clinic's owner-approved external fallback/coverage process and tell
   patients its actual availability. A configurable clinic-fallback editor,
   automatic transfer and substitute-clinician role are not available in this build.
4. Resolved means coverage is explicitly restored by an authorized owner decision
   or outstanding responsibilities remain visibly assigned. Never promise a response
   time or silently transfer a patient's chart.

---

## Cost/provider limit

1. Run `python -m deploy.release_record --env dev` to check live budget inputs and
   `python -m deploy.ops health-report --env dev` for worker symptoms.
   AWS console → Billing and Cost Management → Budgets → sanad-dev-monthly:
   read actual spend and notification configuration. The budget is not a hard cap.
2. In the configured Gemini project's console, inspect model quota/billing.
   AWS → Bedrock → model access and CloudWatch → Lambda throttles help distinguish
   provider allowance from compute limits. Full cross-provider cost measurement
   is not available in this build.
3. Preserve receipts and reviews. An owner-approved allowance change can restore
   availability. A model/provider substitution needs a reviewed code/config release;
   it is not an arbitrary operator toggle. Use containment from Compromised
   account/token if service-wide automation must stop.
4. Resolved means allowed capacity is restored and pending work recovers, or bounded
   failure and clinic fallback are explicit. Budget alarms alone do not stop spend,
   and blocking costly processing does not establish clinical safety coverage.

---

## Doctor approval and suspension

1. Applicant sends `/start` privately to the dev bot and completes the application.
2. Administrator uses the delivered Approve/Reject application buttons, or
   `/login admin` → Continue → `/admin` → application → Approve/Reject.
   Verify the exact application/account and enter the required reason.
3. For an approved account use `/admin` → doctor → Suspend/Reinstate with reason.
   Confirm the new status. Suspension revokes clinical authorization; reinstatement
   does not imply permission to enroll real patients.

## Patient invitation and re-invitation

1. The approved treating doctor creates/confirms the patient card or sends
   `/qr <patient name or short ID>`; resolve ambiguous matches on the selection card.
2. Confirm name and short ID privately before sharing the invitation. Reissue with
   the same `/qr` flow when a new invitation is needed; old generations are revoked.
3. Patient follows the invitation, accepts consent and supplies intended-person
   proof; the doctor uses the delivered claim-confirmation action.
   Resolved means the intended binding is active, never merely that a QR opened.
   A bound identity transfer is not available in this build.

## Account recovery

1. With the original verified identity, request a fresh `/login` (administrator:
   `/login admin`). Use Sign out everywhere or `/logout` to revoke old sessions.
2. For lost/mistaken identity, suspend affected doctor access through `/admin` and
   contact the owner through the established private recovery process.
   A complete operator UI for lost patient identity re-binding is not available in this build.
3. Do not edit a SubjectBinding or mint a replacement patient to bypass ownership.
   Resolved means identity is independently verified and old access is revoked;
   unsupported recovery remains blocked and recorded.

## Policy updates

1. Read the relevant versioned policy and owner decision before changing behavior.
   Numerical clinical policy approval remains part F and an activation gate.
2. Runtime editing of clinical thresholds/retention policy is not available in this build.
   Prepare a separate reviewed contract and deploy only its approved release.
   Doctor-specific digest time/packing can be changed today with `/digest` or
   doctor dashboard → Preferences.
3. Confirm the intended policy/source revision and deployed smokes in the release
   record; do not call a policy clinically approved because its tests pass.

## Opt-out

1. The patient opens the patient browser → Reminders → switch off, or sends a supported
   stop request to the bot. Read the acknowledgment and current reminder preference.
2. Confirm routine contact is disabled. Clinical consent, order status, danger and
   outstanding doctor reviews remain separate. Full consent-withdrawal administration
   is not available in this build; route a withdrawal request to the owner.
3. Resolved means the requested routine opt-out is recorded and unsent routine work
   is invalidated; it does not mean the chart or its unresolved obligations vanished.

## Safety review

1. Treating doctor opens `/inbox` or dashboard → Needs review and reads the incident
   source/evidence privately. Use fresh actions for the current version.
2. Acknowledge records receipt of the review only. Use the offered resolve-incident
   disposition with an explicit reason after clinical assessment; do not infer
   assessment from delivery acceptance or elapsed time.
3. Confirm the exact review's resolved state and disposition; unrelated result and
   mission reviews remain open. If the action is unavailable, keep responsibility
   visible and contact the owner; no operator raw-row resolution is provided.

## Stop and resume of contact

1. Patient browser → Reminders: switch off or choose a supported snooze duration.
   Confirm the saved state and expiry. Stopping reminders is separate from stopping medication.
2. Resume through the browser's consent confirmation or the bot's delivered
   single-use resume action. Do not edit consent/version fields in DynamoDB.
3. Confirm current consent, binding, doctor coverage and active orders permit new
   routine guidance. A rejected resume remains stopped; no old missed reminders
   should be forced through.

## Retention

1. Clinical retention periods and backup/export handling require an owner decision.
   Automatic age-based clinical retention deletion is not available in this build.
2. CloudWatch → Log groups: inspect retention for the two Lambda groups and build
   group. DynamoDB → sanad-dev-data → Backups: inspect PITR; release-record collects both.
3. Use patient export/deletion below for a specifically authorized patient.
   TTL on disposable sessions is not a clinical-retention scheduler. Deletion does
   not erase historical PITR backups or doctor-owned retained material.

## Export

1. Verify the requesting doctor, exact patient and authorized private destination.
   Preview `python -m deploy.records export --env dev --doctor <id> --patient <id> --out <new-private-dir>`.
2. Read row/media counts and left-in-place doctor records. Repeat with `--yes`.
   The directory is private; JSON and original media bytes are unredacted.
   Generated local filenames map to original S3 keys in records.json.
   Explicitly linked intake originals are included; unrelated intake material is excluded.
3. Confirm records.json and each listed media file exist. Transfer only through the
   doctor's approved private process. An incomplete export retains a private partial
   directory and exits nonzero; use a new directory after resolving the error.

## Deletion

1. Verify the authorized doctor/patient target and retention/export decision.
   Quiesce relevant activity first; this operator command does not fence running
   writers. Suspend/stop access as appropriate and, for a maintenance window,
   use the service-wide containment and drain steps above.
2. Preview `python -m deploy.records delete --env dev --doctor <id> --patient <id>`.
   Read patient/global/name row counts, versioned S3 object/delete-marker counts
   and doctor-scoped records left in place.
3. Repeat with `--yes`. A tenant AuditEvent records patient ID and counts before
   media deletion; then rows are deleted and both are rescanned.
4. Resolved means verification reports zero patient rows/media. A retry after a
   recorded deletion reports zero and succeeds; an unknown patient refuses.
   S3 failure retains patient rows; reappearing data exits nonzero and requires
   stopping the writer before retrying. Doctor-owned drafts/reviews/offers and
   historical backups remain; broader erasure is not available in this build.
