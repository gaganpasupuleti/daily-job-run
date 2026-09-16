import json
import os
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

from db import TABLE_NAME

load_dotenv(".env.testing", override=True)
conn = psycopg2.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()

cur.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}")
try:
    total = cur.fetchone()[0]
except psycopg2.errors.UndefinedTable:
    cur.execute("SELECT COUNT(*) FROM naukri_jobs")
    total = cur.fetchone()[0]
    TABLE_NAME = "naukri_jobs"

cur.execute(
    f"SELECT COUNT(*) FROM {TABLE_NAME} WHERE role_family IS NOT NULL AND role_family <> ''"
)
tagged = cur.fetchone()[0]

cur.execute(
    f"SELECT COUNT(*) FROM {TABLE_NAME} WHERE role_family IS NULL OR role_family = ''"
)
untagged = cur.fetchone()[0]

cur.execute(f"SELECT COUNT(DISTINCT role_family) FROM {TABLE_NAME} WHERE role_family IS NOT NULL")
families = cur.fetchone()[0]

cur.execute(f"SELECT MAX(scraped_at) FROM {TABLE_NAME}")
latest = cur.fetchone()[0]

conn.close()

json_files = sorted(
    Path("output").glob("naukri_18_families_*.json"),
    key=lambda p: p.stat().st_mtime,
    reverse=True,
)
latest_json = json_files[0] if json_files else None
json_count = 0
if latest_json:
    data = json.loads(latest_json.read_text(encoding="utf-8"))
    json_count = data["meta"]["total_unique_jobs"]
    print(f"latest_json: {latest_json.name}")
    print(f"json_unique_jobs: {json_count}")

print(f"table: {TABLE_NAME}")
print(f"db_total_rows: {total}")
print(f"db_tagged_with_family: {tagged}")
print(f"db_untagged: {untagged}")
print(f"db_distinct_families: {families}")
print(f"db_latest_scrape: {latest}")
