# Operating release records

Generate from the identified synthetic dev stack after the authorized live
rehearsals. No live release record is fabricated by the offline implementation.

1. Preview `python -m deploy.release_record --env dev`. Its JSON includes the safe
   Markdown preview and counts; no file is written.
2. The architect records the isolated restore output as
   `docs/evidence/restore-dev-<date>.jsonl`, then rehearses
   `python -m deploy.rollback --env dev` to the previous passing image and back.
   Both rollback invocations run smokes and append release history.
3. Re-run the preview; inspect every section and open item. Use
   `--restore-evidence docs/evidence/<file>` to select a specific restore.
4. Execute with `--yes` to write/update `docs/releases/dev-<deployed-git-sha>.md`.
   The revision comes from passing release history matched to the live image,
   not from an assumption that the current checkout is deployed.

The owner's optional plain-name parameter is set with
`python -m deploy.ops secrets set --env dev --operator-name "Operator Name"`.
Omitting it preserves an existing value; the record says “not set” until set.
It is excluded from application configuration/version requirements.

Records contain allowlisted metadata, parameter versions (never parameter values
other than the operator's name), restore counts and smoke verdicts. A token, phone
or email in an emitted field refuses the whole write. Provider log messages,
budget subscribers and arbitrary release-history payloads are never copied.

The file links the architect/owner clinical-readiness checklist. Missing alarms,
unfinished rehearsals, owner decisions and activation remaining closed are explicit.
A record is operational evidence, not acceptance, deployment authorization or
clinical validation.
