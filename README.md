# Daily job run

daily-job-run collects and maintains the source job catalog used by JobReady. JobReady is its only production data destination.

The pipeline scrapes configured boards, normalizes and dedupes the rows, classifies them in memory, and upserts `validated_jobs` in the JobReady catalog. A later observation updates `synced_at`. `scraped_created_at` stays at the first time that source identity was stored. `updated_at` moves only when the catalog content or link status changes. Confirmed closed jobs stay in the catalog with `link_status = expired`. A miss in today's scrape is not expiration.

## Daily command

```bash
python codequest_jobs/run_jobready_daily.py
```

That command scrapes, upserts the catalog, then validates a rotating slice of `validated_jobs`. The default validation budget is 200 rows. Up to half of that budget is reserved for new jobs and re-observed jobs that have never received a conclusive check. The rest of the budget walks the oldest `link_checked_at`, so a normal day keeps moving through the catalog. `--full-audit` checks every row on a supported source. `--validation-limit` changes the budget.

Only LinkedIn, Naukri, and Indeed have a lifecycle validator (`SUPPORTED_LIFECYCLE_SOURCES` in `codequest_jobs/link_policy.py`). A source matches only after the same rule in Python and SQL: trim ASCII space, tab, newline, vertical tab, form feed, and carriage return from both ends, then lowercase A-Z. Values such as `linkedin.com` stay unsupported. Every other source, including Google, Foundit, a blank source, and any new source, is unsupported. Unsupported rows are never selected, so they use no validation budget, including the priority half. They are never probed and get no ledger row, and their `link_status`, `link_checked_at`, `last_link_observation`, and confirmed timestamps do not change. Unsupported does not mean closed, open, or failed: no lifecycle evidence exists for that source. The daily report lists them as `catalog_unsupported_rows` and `catalog_unsupported_by_source`; `catalog_supported_rows` + `catalog_unsupported_rows` = `catalog_total`. `validation_selected` is the number of supported rows selected, and `validated_checked`, `confirmed_active`, `confirmed_expired`, and `unknown_or_inconclusive` count only those rows.

A job found by search starts active. Seeing it in search again does not change `link_status`. An expired row stays expired until the validator returns `confirmed_active` observed after the row's last confirmed closure. Only `confirmed_expired` observed after the row's last confirmed open marks a row expired. Link checks update `link_checked_at` and do not move `synced_at`.

A conclusive check needs evidence for the same job. The response must come from that job's provider endpoint over HTTPS on the provider's own host and default port, without a redirect elsewhere. It must carry the same job id and state its status:

- LinkedIn reads only the visible content of the page's single top-card element. Hidden, template, and script content does not count. A job is `confirmed_active` only when the top card has a working Apply action: LinkedIn's apply-modal button, or an Apply link to a real http or https destination. It is `confirmed_expired` when the top card says the job is closed or the guest endpoint returns 404 or 410. These are inconclusive:
  - a disabled, missing, or inert Apply action, such as a missing href, `#`, or `javascript:`;
  - a captcha or sign-in wall;
  - a "not found" page;
  - closed text outside the top card;
  - a page where the top card cannot be identified.
- Naukri needs an explicit status field for the same job id. A title alone is inconclusive.
- Indeed needs a true or false `expired` flag for the requested job key. A missing, null, or text value is inconclusive.
- Google, Foundit, and other sources are unsupported, not inconclusive. `audit_job_ready.py` reports them as `unsupported` with reason `unsupported_source`, without a request, and counts them apart from `checked`.

Inconclusive keeps the previous `link_status`. The daily report counts each observation and reason under `validation_reasons`.

Before the first run, and after a schema review:

```bash
python codequest_jobs/run_jobready_daily.py --preflight
python codequest_jobs/run_jobready_daily.py --apply-schema
```

`--preflight` is read-only. It checks that the catalog is reachable, `validated_jobs` exists, required columns and indexes are present, and source identities are not duplicated. It prints `host`, `port`, and database name, not a connection string. `--apply-schema` applies idempotent catalog DDL and refuses when duplicate identities exist. A normal daily run refuses if that schema is missing. It does not create the table itself.

### Lifecycle evidence schema (Phase 1)

`--apply-schema` creates the lifecycle evidence objects. The daily run and `--preflight` require them: a missing or incomplete ledger (columns, foreign key, observation check, or indexes) is reported as `SCHEMA_REQUIRED` / `PREFLIGHT_FAILED`. The daily run never creates them.

Each validation batch is one transaction. It appends one ledger row per observation and updates `link_status`, `link_checked_at`, `last_link_observation`, and the confirmed timestamps. If either part fails, both roll back, and the journal shows `validation_write` `committed: false`. Ledger rows carry the journal `run_id`, the probe's `provider` and `reason`, and `evidence_version` (`jobready-validator-v2` for validator results; rows written before identity evidence carry `jobready-validator-v1`). `previous_link_status` is the catalog status the observation was applied to. A batch holds at most one observation per job. The runtime only inserts ledger rows; it never updates or deletes them. Ledger rows also carry HTTP provenance in `requested_url`, `final_url`, and `http_status`; they are written in the same transaction and roll back with the rest. The daily report adds `lifecycle_observations_recorded`.

HTTP provenance is evidence only. It does not change how observations are interpreted, which `link_status` they produce, the evidence version, or the report.

- Stored URLs are sanitized. They keep the `http` or `https` scheme, hostname, a non-default port, and path. Userinfo, query string, and fragment are never stored. A URL that cannot be represented safely is stored as `NULL`.
- `http_status` is an integer from 100 through 999. 999 is accepted because LinkedIn uses it for bot checks.
- The writer refuses a batch with an unsanitized URL or an invalid status before any SQL runs.
- LinkedIn: `requested_url` is the guest job endpoint once the request is made, including when the connection fails. `final_url` and `http_status` come from the HTTP response when there is one, including redirects, rate limits, and soft 404s.
- Naukri: `requested_url` is the job detail endpoint once the detail request is issued. If the session warm-up fails before that request, it stays `NULL`. `final_url` and `http_status` come from the HTTP response when there is one, including an empty detail response.
- Indeed: `requested_url` is the GraphQL endpoint only when an API request is attempted, which needs `INDEED_API_KEY` and a parsed job key. `final_url` and `http_status` stay `NULL` because the Indeed helper does not expose its response.
- Generic sources, unrecognized job URLs, probe errors, and injected observations (`jobready-injected-v1`) store `NULL` provenance.
- The validator keeps the `httpx` and `httpcore` loggers at `WARNING` or stricter, so request URLs are not printed in logs.

Lifecycle transport for LinkedIn, Naukri, and Indeed uses bounded, source-aware retries (`codequest_jobs/retry_policy.py`). Retries are transport-only: provider open/closed verdict rules are unchanged. Retry exhaustion stays `inconclusive`; it does not confirm closure. `Retry-After` is honored within a configured maximum delay. Internal retries do not create extra lifecycle observations—one validation still yields one ledger row. Provenance reflects the final HTTP response used for the observation.

Ledger rows also carry requisition identity evidence, written in the same row and transaction. Like provenance, it explains a verdict and never changes one.

- `expected_identity` is the provider job id the probe asked for: the LinkedIn job id, the Naukri job id, or the Indeed `jk` parsed from the catalog URL. It is `NULL` when no id could be parsed.
- `observed_identities` is what the provider response itself exposed. It is never filled from the requested URL, the expected id, or catalog columns. `NULL` means no response identity was inspected: no request, a connection failure, a non-200 or redirected response, a status-only verdict such as LinkedIn 404/410, a malformed payload, or no Indeed results list. `{}` means a usable response was inspected and carried no id. Otherwise it holds the ids found: LinkedIn page ids, Naukri `jobId`/`jobid`/`id` values, or every Indeed key returned, requested or not.
- Ids are deduplicated, sorted, and at most 16. A value that is not a plain id (letters, digits, `_`, `-`, at most 64 characters) is stored only as `[redacted]`, so URLs, query strings, markup, and tokens are never stored.
- Rows written before these columns, injected observations, legacy tuple probes, and probe errors store `NULL` in both.

- `jobready_link_observations` is append-only. Each validation attempt is one row, so repeated identical results are kept. The only uniqueness is `id`. `job_id` references `validated_jobs(job_id)` with `ON DELETE RESTRICT`, so a job that has evidence cannot be deleted. `observation` is exactly `confirmed_active`, `confirmed_expired`, or `inconclusive`. Rows hold metadata only, never page bodies, headers, cookies, tokens, or credentials.
- `observed_at` is when the provider was probed. `recorded_at` is when the row was stored.
- `validated_jobs.last_confirmed_open_at` and `last_confirmed_closed_at` are the latest `observed_at` of trusted `confirmed_active` and `confirmed_expired` evidence. They never move backward when older evidence arrives. `inconclusive` never advances either timestamp.
- Confirmed evidence changes `link_status` only when its `observed_at` is strictly later than the opposite confirmed timestamp. Older or simultaneous contradictory evidence is still recorded and still advances its own timestamp, but the ledger row's `resulting_link_status` equals its `previous_link_status` and the row keeps its status. A row with no opposite confirmed timestamp follows the observation, as before.
- No backfill. Only validator observations made after this runtime is active create ledger rows or set the timestamps. They are not derived from `link_status`, `link_checked_at`, `last_link_observation`, `synced_at`, or `updated_at`.
- Recording evidence changes no visibility or membership behavior. `link_status` follows the ordering rule above.
- The foreign key needs `validated_jobs.job_id` to be unique, which `SCHEMA_SQL` declares. If a catalog lacks that constraint, `--apply-schema` fails and applies nothing.

### Shadow lifecycle projection (D16)

`codequest_jobs/lifecycle_projection.py` is a pure, read-only decision layer over immutable lifecycle observations. It does not write to the ledger, change `link_status`, implement freshness/TTL, or decide eligibility/publication.

- Shadow vocabulary is `confirmed_open`, `confirmed_closed`, or `unresolved` (not catalog `active`/`expired`).
- Authority is an explicit policy object. The default policy treats LinkedIn, Naukri, and Indeed as one canonical class with no invented relative ranking. Injected and unknown providers are non-authoritative.
- Resolution combines observation time and authority: an observation dominates an opposing one only when it is at least as authoritative and at least as recent, with at least one dimension strictly stronger. Equal-time or authority/time disagreements return `unresolved` with `review_required`.
- Inconclusive rows are latest-attempt context only. They do not open, close, or create conflicts.
- Output references evidence ids (or synthetic refs in tests) so later eligibility export can explain the decision.

### Shadow evidence freshness (D18)

`codequest_jobs/lifecycle_freshness.py` is a pure, read-only evidence-age layer over D16 `LifecycleProjection`. It does not write to the ledger, change `link_status`, or decide final eligibility or publication (D17).

- Positive confirmed-open freshness uses an explicit `LifecycleFreshnessPolicy` input (including `confirmed_open_ttl`). No production TTL is chosen in code.
- TTL expiry means stale/held positive evidence, not employer-closed. Confirmed closed from D16 stays closed regardless of an older open timestamp.
- D16 unresolved conflicts become `review_hold`; absent or non-authoritative conclusive evidence becomes `unconfirmed`. Fresh timestamps do not bypass D16 contradictions.
- Unsupported lifecycle sources use the same capability rules as `link_policy`. Evaluation time is always supplied by the caller for reproducibility.

### Versioned shadow eligibility export (D17)

`codequest_jobs/eligibility_export.py` emits contract `jobready-eligibility-v1` per-job shadow eligibility artifacts by composing D16 lifecycle projection and D18 freshness. It does not write catalog or application membership, publish jobs, or choose a production TTL.

- Eligible only when lifecycle is authoritatively `confirmed_open`, freshness is `fresh`, D16 review is not required, and every prerequisite is explicitly `true`. Unknown prerequisites hold.
- Stale positive evidence is `held`, not employer-closed. Unsupported or conflicting lifecycle evidence holds for review.
- Authoritative open freshness anchors derive from controlling D16 `confirmed_active` observations, not catalog summary timestamps or `link_status`.

Experience buckets stored for JobReady are `Internship`, `Fresher`, `Entry (1-2 yrs)`, `Experienced`, and `Unknown / Unclassified`.

Required configuration: `JOB_READY_DATABASE_URL`.

Set `INDEED_API_KEY` when Indeed expiry checks should run. Leave it empty to skip those checks.

Application membership is separate. The command prints `would_archive` and `would_reactivate` before any application write. Pass `--sync-application --confirm-target host:port/database` only when `JOB_READY_APP_DATABASE_URL` should be updated. The confirmation is the host, port, and database name. It is not a connection string. Membership includes catalog rows whose `link_status` is `active`. A write is refused when an archive or reactivation is at least 100 jobs and at least 10 percent of the integration-owned active application jobs. There is no bypass flag. A source-wide scrape failure also refuses the membership write and does not expire unrelated catalog rows.

One run holds a PostgreSQL advisory lock on the catalog. A second run exits while that lock is held.

Each successful run writes the daily report `codequest_jobs/output/jobready_daily_YYYYMMDD_HHMMSS.json`. The report is the business summary: counts, sources, validation reasons.

## Run journal

Every attempted daily run also writes a run journal, `codequest_jobs/output/runs/<run_id>.json`. The journal is execution history. It is written before the lock is taken and rewritten at every stage start and end, so it exists even when the run fails, and it says where a run stopped. `run_id` looks like `20261012T021500Z_<short commit>_<random>` and sorts by start time (UTC). Each write goes to a temp file in the same folder, is flushed to disk, and replaces the journal in one step, so the file is always complete JSON. The journal is local only. Nothing is written to a database.

Run fields: `run_id`, `started_at`, `lock_acquired_at` (set once the run holds the daily-run lock), `finished_at`, `terminal_state`, `git_commit`, `command`, `effective_mode`, `validation_limit`, `full_audit`, `apply_schema`, `membership_sync_requested`, the scrape options, `failed_stage`, `failure_code`, `failure_message`, and `report_path` after a successful report.

Stages, in order: `preflight` (lock and schema check), `scrape`, `catalog_plan`, `catalog_write`, `validation_select`, `validation_probe`, `validation_write`, `application_sync`, `report`. Each stage has `status`, `started_at`, `finished_at`, `elapsed_sec`, `committed`, `counters`, `skip_reason`, `failure_code`, and `failure_message`. `status` is one of `not_started`, `running`, `completed`, `failed`, or `skipped`. `application_sync` is `skipped` when `--sync-application` was not passed.

`terminal_state`:

- `running`: the run has not finished, or the process died before it could record an outcome.
- `completed`: every stage completed or was skipped and the report was written. Exiting without an exception is not enough.
- `failed`: a stage failed. The command still exits non-zero; the journal records the failure and does not swallow it.
- `abandoned`: this run recorded `lock_acquired_at`, so it held the daily-run lock, and a later run then took the same lock and found this journal still `running`. The later run first writes its own `lock_acquired_at`, then checks earlier journals. The stage that was running is marked `failed` with `RUN_ABANDONED` and `committed: null`. A `running` journal without `lock_acquired_at` may belong to a live run still waiting for the lock, so it is never marked abandoned. A run that cannot get the lock fails with `RUN_LOCK_HELD` and does not touch other journals.

`committed` says whether that stage's own database transaction committed:

- `true`: the commit succeeded (`catalog_write`, `validation_write`, applied `application_sync`).
- `false`: the stage writes but did not commit. A failed `catalog_write` was rolled back; a failed `validation_write` or refused membership sync left no change.
- `null`: the stage does not write (`preflight`, `scrape`, `catalog_plan`, `validation_select`, `validation_probe`, `report`, a dry-run `application_sync`, any `skipped` stage), or the outcome is unknown (an `abandoned` stage).

The run is not one transaction. A failure in `validation_probe` after `catalog_write` committed leaves the catalog rows in place, and the journal shows `catalog_write` `committed: true`. Nothing resumes or replays a run from its journal. The next daily run starts from the beginning.

To inspect a failed or partial run, open the newest file in `codequest_jobs/output/runs/`. Read `terminal_state`, then `failed_stage` and its `failure_code`, then the `committed` value of every write stage before it. `report_path` is set only when the report was written. A report that failed to write shows `report` as `failed`.

Journals hold sanitized metadata only: flags, counts, timings, and structured failure codes. They never contain connection strings, passwords, API keys, Authorization headers, tokens, or the `--confirm-target` value. Failure messages are short and redacted, and they never include raw stack traces. `--preflight` and `--apply-schema` do not write a journal.

## Sources

Naukri, Indeed, LinkedIn, Google, and Foundit, for the role families in `naukri_jobs_scraper/families.json`.

## Legacy commands

The modules in [LEGACY.md](LEGACY.md) still talk to CodeQuest or the old `naukri_scraper` database. Do not use them for the daily catalog. `link_validator_service/run_scheduled.py` only delegates to the command above.

`codequest_jobs/prune_job_ready_now.py` is retired. It exits with `RETIRED_COMMAND prune_job_ready_now` and touches no database. CodeQuest does not decide which catalog jobs survive.

`codequest_jobs/audit_job_ready.py` is a read-only catalog audit. Neither it nor `codequest_jobs/validate_links.py` writes JobReady lifecycle state; their old write options (`--prune-job-ready`, `--sync-job-ready-every`, `--confirm-target`) exit with `JOBREADY_SYNC_RETIRED`. Only the daily command changes `link_status`.
