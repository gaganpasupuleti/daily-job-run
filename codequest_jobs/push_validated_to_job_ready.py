#!/usr/bin/env python3
"""RETIRED. Every invocation exits non-zero without contacting a database.

This command used to copy active rows from the legacy CodeQuest scraped_jobs
table into the JobReady catalog, overwriting validated_jobs.link_status
without a validator observation or a lifecycle ledger row. CodeQuest is not
authoritative for JobReady lifecycle state, so the push is no longer supported
and no flag re-enables it.

Catalog lifecycle changes come from codequest_jobs/run_jobready_daily.py.
Application membership reconciliation is the separate guarded command
codequest_jobs/sync_job_ready_ui.py.
"""

from __future__ import annotations

import sys

RETIRED_MESSAGE = "RETIRED_COMMAND push_validated_to_job_ready"
HELP_TEXT = (
    "push_validated_to_job_ready.py is retired.\n"
    "Pushing CodeQuest scraped_jobs into the JobReady catalog is no longer supported.\n"
    "CodeQuest is not authoritative for JobReady lifecycle state.\n"
    "Catalog lifecycle changes come from codequest_jobs/run_jobready_daily.py.\n"
    "Application membership reconciliation is codequest_jobs/sync_job_ready_ui.py.\n"
    "No arguments or environment variables re-enable this command."
)
EXIT_RETIRED = 2


def main(argv: list[str] | None = None) -> int:
    """Refuse every invocation. Arguments are not parsed or echoed."""
    args = sys.argv[1:] if argv is None else argv
    if any(arg in ("-h", "--help") for arg in args):
        print(HELP_TEXT, flush=True)
    print(RETIRED_MESSAGE, flush=True)
    return EXIT_RETIRED


if __name__ == "__main__":
    raise SystemExit(main())
