# LEGACY

`run_scheduled.py` is a thin wrapper around `codequest_jobs/run_jobready_daily.py`.

The other Python modules in this directory are frozen copies of the old CodeQuest validator. They are not the production implementation. Do not patch link checking, Indeed checks, enrichment, or catalog writes here.

`push_validated_to_job_ready.py` is retired. It delegates to `codequest_jobs/push_validated_to_job_ready.py`, which exits with code 2 without contacting a database.
