import json
import os
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

from db import TABLE_NAME

load_dotenv(".env", override=True)
conn = psycopg2.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()

cur.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}")
total = cur.fetchone()[0]

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

print(f"database: production")
print(f"table: {TABLE_NAME}")
print(f"db_total_rows: {total}")
print(f"db_tagged_with_family: {tagged}")
print(f"db_untagged: {untagged}")
print(f"db_distinct_families: {families}")
print(f"db_latest_scrape: {latest}")
