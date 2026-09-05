# Deployment, operations and clinical readiness

Version 2.0 · Planning only; no infrastructure provisioned or clinical activation authorized

## Environments and owners

Local development uses fake clocks, captured transport, fixture models and the in-memory store; DynamoDB Local verifies adapter transactions. Development deployment uses only synthetic data, a new AWS target and a new Telegram bot. Judge deployment is a separately identified synthetic release. A clinical environment follows the readiness gate below with its own configuration, data and access controls.

The specification defines and the review checks operational contracts; the implementation demonstrates them. The operator owns cloud accounts, allowed spending, clinical decisions and release authorization. Record an operational responder and escalation channel before launch. Provisioning must positively identify new account/region/resource names and refuse legacy Google service/bot targets.

## Budget and access before live calls

Record permitted account/region, deployment principal/roles, model IDs, ASR choice, credit terms, daily/per-turn token limits and spend ceiling. Use least-privilege short-lived deployment access where practical; do not default to broadly privileged permanent keys. Never print credentials.

Forecast the whole ECS resource set, including compute, load balancer, networking/public IP/NAT when used, data transfer, logs, table/storage and model/ASR requests. ECS Express charges for underlying resources, so a container-only estimate is incomplete. [AWS cost model](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/express-service-overview.html)

Budget alarms are notifications, not hard spending caps. Add application model/token/concurrency/rate limits, bounded retries and an operator stop switch. Verify credit service eligibility; no assumed free deployment. Hosting through judging requires coverage to **2026-10-09 03:00 Cairo**. The old project's billing/trial is a separate obligation and must not be changed by this build.

## Deployment and release procedure

1. Accepted code and dependency lock, migrations/index plan, synthetic fixtures, local and store-parity checks.
2. Explicit targets, allowed principal, new bot, secret references and spend approval.
3. Provision infrastructure declaratively or with an idempotent reviewed script; record inventory and costs. Set encryption/private bucket policy, access logging without sensitive URL tokens, backup configuration and retention.
4. Apply compatible schema/index migrations; expose health/readiness and revision. Keep a known-good rollback image/config.
5. Verify service/tick authentication, replay denial, webhook secret and durable ACK behavior before pointing only the new bot at it.
6. Complete real doctor approval/login/claim, Strands tool, voice/image, scheduler, delivery/restart and second-tenant denial checks.
7. Exercise browser/Telegram parity, opt-out, order edits, review/bundle clocks and full original journey. Record measured evidence distinct from fixture results.
8. Rehearse restore/rollback and operator response; resolve severe findings; create release manifest.
9. Publish or submit only under the relevant owner instruction. Keep the frozen judging artifact separate from continuing product development. Real-patient enrollment requires the additional gate below.

## Observability and service objectives

Measure oldest unfinished inbound/media item, scheduler lag, leases expired/reclaimed, missions/obligations without due work, oldest overdue/review item, failed/uncertain deliveries, unacknowledged urgent incidents, model/tool/ASR errors, clarification rate, contact count, opt-outs and cost. Log pseudonymous IDs, versions, timings and verdicts; never prompts, images, transcripts or clinical text. Disable/redact default provider/SDK tracing that captures content.

Initial engineering targets to benchmark: durable webhook ACK under 2 seconds at intended load, readable-text safety handling under 5 seconds, ordinary text response around 15 seconds on a healthy provider, scheduled-work lag under 2 minutes. These are test targets, not achieved SLAs or a clinical emergency-response promise. Media latency has a separate measured budget. A minute trigger alone does not guarantee minute delivery.

Operator alerts contain operational metadata only. Treating-doctor clinical messages still pass the Liaison policy. A configured emergency/clinic fallback must tell patients what the service actually supports, never imply24/7 human monitoring.

## Recovery playbooks

| Incident | Required action and proof |
|---|---|
| Process dies after inbound ACK | Reclaim pending/expired processing receipt with durable payload/media reference; complete one logical action; completed duplicate no-op |
| Media/model failure | Retain input and uncertainty; bounded retry or useful resend/type request; independently timed unresolved-media review |
| Scheduler outage | Recover due work from persistent indexes, re-read current authority, coalesce missed routine contact; preserve original deadlines and unresolved outcomes |
| Unknown Telegram outcome | Keep uncertain state; apply category-specific bounded retry, label repeated danger as same incident; never claim doctor read |
| Patient stop/order change/suspension | Invalidate stale unsent work at dispatch; clinical order and contact consent remain separate; record already-in-flight races and corrective action |
| Compromised account/token | Rotate secret/revoke sessions, pause affected delivery, verify new auth and audit before explicit resumption |
| Bad release | Hold affected automation; roll back to compatible known-good image without erasing valid new events; reviewed data repair if needed |
| Restore | Restore into isolation, compare aggregate/event/work versions, keep sends disabled, dry-run reconciliation and revocation checks, then explicit resume |
| Doctor unavailable | Preserve ownership/review queue, show truthful coverage information and clinic fallback; no silent transfer or promised response |
| Cost/provider limit | Preserve deterministic safety/accountability, reject or queue costly work visibly within limits, notify operator and select reviewed alternative |

Required release record: revision, schema/config/policy/model versions, evidence links, backup/restore run, migration/rollback steps, limits, open issues and accountable operator. The runbook includes approval, invitations, account recovery, policy updates, opt-out, safety review, stop/resume and retention/export/deletion handling.

## Clinical readiness is an owned gate

Slice 21 must produce a concrete checklist and evidence, not a promise to revisit it someday. Qualified clinical reviewers approve supported patients/conditions, numerical policies and educational sources; choose handling for unsupported/pediatric/pregnancy/complex cases instead of extending unvalidated tables implicitly. Define patient consent/withdrawal, identity recovery, authorized access, retention/deletion/export, hosting/data-transfer obligations and coverage/escalation arrangements with appropriate local review.

Clinical/legal review results and decisions must be recorded before actual enrollment; this plan does not assert legal compliance. Start supervised with accountable clinicians and monitor errors, missed/false alerts, patient burden and unresolved-review age. Passing synthetic tests or winning a contest cannot stand in for this gate. Required software remains in the full build even while activation waits for the recorded prerequisites.

## Judge handover

Synthetic judge access must work without an operator attending each run. Separate a clearly labeled public synthetic board from authenticated clinical routes; any reset/demo clock affects only its synthetic environment. Use real integrations in the recorded journey and label compressed time. Setup instructions, architecture diagram and reuse/license inventory describe actual code. Funding/access persists through the verified judging period.
