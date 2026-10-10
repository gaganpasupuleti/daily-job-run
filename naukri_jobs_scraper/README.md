# Job source collectors

`run_all_sources.py` returns normalized jobs. By itself it writes an optional JSON artifact and does not open CodeQuest or the old scraper database.

The daily command is:

```bash
python codequest_jobs/run_jobready_daily.py
```

Role family names and keywords live in `families.json` in this directory.

`--legacy-write-local` and `--legacy-write-codequest` are deprecated and are not the default.
