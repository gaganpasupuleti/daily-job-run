"""Role families owned by this repository.

Family names and keywords are stored in families.json beside this module.
"""

from __future__ import annotations

import json
from pathlib import Path

FAMILIES_PATH = Path(__file__).resolve().parent / "families.json"


def load_families() -> list[dict]:
    data = json.loads(FAMILIES_PATH.read_text(encoding="utf-8"))
    families = []
    for row in data["families"]:
        families.append(
            {
                "id": row["id"],
                "name": row["name"],
                "keywords": list(row["keywords"]),
            }
        )
    return families
