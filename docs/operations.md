# Deployment, operations and clinical readiness

Version 2.1 · 2026-09-06 · Synthetic AWS development deployment accepted (contract 07); no clinical activation

## Environments and owners

Local development uses fake clocks, captured transport, fixture models and the in-memory store; DynamoDB Local verifies adapter transactions. Development deployment uses only synthetic data, a new AWS target and a new Telegram bot. Judge deployment is a separately identified synthetic release. A clinical environment follows the readiness gate below with its own configuration, data and access controls.

The specification defines and the review checks operational contracts; the implementation demonstrates them. The operator owns cloud accounts, allowed spending, clinical decisions and release authorization. Record an operational responder and escalation channel before launch. Provisioning must positively identify new account/region/resource names and refuse legacy Google service/bot targets.

## Development deployment, contract 07

The owner authorized this development deployment and rollback on 2026-09-06. Account and region were verified through the AWS default credential chain; the account remains `FREE / ACTIVE` in `us-east-1`. Only synthetic fixtures were used. Contract 07 was accepted by the architect on 2026-09-06.

| Environment | State and endpoint |
|---|---|
| Local | In-memory store or DynamoDB Local; captured transport |
| dev | Stack `sanad-dev`; [HTTPS health](https://btx35drwcqejwxymdcwfzwku5m0cynoz.lambda-url.us-east-1.on.aws/health); real SSM/DynamoDB, Lambda workers and minute ticks |
| judge | Not deployed |
| Clinical | Not activated |

| Stack output / setting | Development value |
|---|---|
| Function URL | `https://btx35drwcqejwxymdcwfzwku5m0cynoz.lambda-url.us-east-1.on.aws/` |
| App / relay | `sanad-dev-app` / `sanad-dev-relay` |
| Table | `sanad-dev-data` |
| Bucket | `sanad-dev-<owner-account>-us-east-1` (account component redacted) |
| ECR repository / CodeBuild project | `sanad-dev-app` / `sanad-dev-build` |
| Image digest | `sha256:c4a7faa1bc09f7ac84d2dba3270875bbf5d9b5fe7fd91a5e411d8cf95bdf1866` (checkpoint `c831929`, deployed 2026-09-08 21:52 with all six smoke groups passing, first cold request 6.38 s). It carries slices 13, 16a, 16b, 11g and 11h plus the doctor-language fix. **Three images were needed for that one fix**: `e955233` at `525d62f` rendered the card in English but saved the proposal's language from the raw preference; `315a454` at `66b3664` fixed five sites in the Scribe turn and added a source rail over that module; neither changed what a doctor received, because `card_intents` re-read the raw preference from `accounts.language()` and re-rendered the finished English card into Arabic. The override now lives in that accessor, and the guard is a test on the delivered message rather than on the source. |
| Open observation | The tick smoke on this release reported `sweep {examined 17, handled 0, deferred 17, budget_exhausted true}`. The budget is 100 items or 10 s; it stopped on **seconds**, not items. Innocent explanations exist (three days of accumulated synthetic work, all 17 legitimately not yet due, cold-start time inside the first sweep) but the shape also matches a starved queue, where due work at the back of the lane never runs. Measure with repeated ticks before treating it as a fault. |
| Source archive SHA-256 | `357e56d60a5098790c99e3c10fdedb02d6622b0d82c9ee6b66ae02d8ed5ef725` |
| Stack revision | `1e84abb9b2b8cd422f36d27035c47dbd2dce82c878bb1eaf91cb242fcb0279ad` |
| Code / stored schema version | `1` / `1` |
| Parameter prefix | `/sanad/dev/` (values withheld) |
| App limits | ARM64, 3,008 MB, 120 s; no reserved concurrency |
| Relay limits | ARM64, Python 3.12 zip, 128 MB, 25 s; no reserved concurrency |
| Account concurrency | 10 total, 10 unreserved |
| Alarms / budget | App Errors and Throttles, relay Errors over 5 minutes; $20 monthly account budget with one configured email subscriber |

The 23 stack resources include the build infrastructure, bucket, table, functions, URL permissions, schedule, roles, log groups, alarms and budget. Seven parameters are owned by `ops.py`: `bot-token`, `webhook-secret`, `tick-secret`, `admin-telegram-id` (SecureString), and `public-base-url`, `bot-username`, `budget-email` (String). The app and relay can decrypt only through SSM using its default managed key. The app's table role includes `ConditionCheckItem` for transaction read guards; the first live smoke exposed that missing permission, and it was fixed before a passing release was recorded.

Use the two-pass operator sequence in [README](../README.md#deploy). A bootstrap stack without an image records application checks as skipped; it does not constitute a passing deployment. The app pass writes the function URL to SSM and requires health, tick/replay/forgery, webhook persistence/replay/worker completion, two-tenant store isolation, browser headers/CSRF and cost checks. Configuration revision uses parameter versions and modification timestamps, so recreating a parameter at version 1 also refreshes cached Lambda configuration.

[Release history](../deploy/releases/dev.json) records every attempt, including the initial smoke failure, a passing first image, a passing second image, a real rollback to the first image, restoration of the final image and no-op verification. The real rollback restored `sha256:6c0d13328b9dbbdf9de06f67824f6bd90fcd4d91f42aef0bdcfcf18b350abd9d` with all six smoke groups passing; the final image above was then restored. The new bot's webhook is registered on this stack's `/tg` URL, with zero pending updates at verification. No real account was approved or patient activated by these checks. Tenant smoke uses a separate synthetic bot namespace in the real table; the live tick excludes other bot namespaces.

The final image measured 73,251,440 bytes in ECR (compressed) and 199,688,097 bytes through `docker image inspect` (uncompressed). First health after introducing that image took 4.1626 s; its warm check took 0.1715 s. The first ever public health request on the initial image took 7.4146 s. These are end-to-end request timings, not clinical SLAs or isolated CPU initialization measurements. Every passing warm health measurement was below 1 s.

The actual CloudWatch inputs and 30-day Lambda forecast are embedded in each release record and in [contract 07's report](contracts/07-aws-development-deployment.md#report). The forecast reports both 30 times the observed 24-hour window and 43,200 minute-scheduled app/relay calls using measured mean durations at the ARM rate. The observation window includes deployment and smoke traffic; it is not a full day of steady operation. ECR, CodeBuild, DynamoDB, S3, logs, alarms and future model/media usage are separate costs. The credit API reported $139.56 remaining during verification. The $20 budget is a notification, not a hard cap.

The replaced spike function and URL, disabled rule and targets, ECR repository/images, CodeBuild project, both roles/policies, bucket and two log groups were removed. Bucket cleanup removed two versions/objects; read-only checks confirmed the principal spike resources absent. No frozen Google resources were accessed.

Deleting the development stack intentionally removes its table, functions, logs, build project, ECR images/repository, roles, schedule, budget and alarms. The versioned bucket and seven parameters remain. After stack deletion, run `python -m deploy.cleanup retained-bucket --env dev` to remove versions, delete markers and multipart uploads and then the bucket; run `python -m deploy.ops secrets delete --env dev` to remove the parameters. The bucket command refuses to operate while the stack still exists. Preserve required evidence and exports before teardown. Do not use these deletion commands for ordinary rollback.

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
