"""Naukri search keywords for CodeQuest role families (fresher + intern)."""

from __future__ import annotations

import json
from pathlib import Path

CATALOG = Path(__file__).resolve().parents[2] / "Role_Reports" / "catalog.json"

# Two keywords per family: fresher-focused + intern-focused
FAMILY_KEYWORDS: dict[str, list[str]] = {
    "java-backend": ["java developer fresher", "java intern"],
    "python-dev": ["python developer fresher", "python intern"],
    "frontend-react": ["react developer fresher", "frontend developer intern"],
    "fullstack-web": ["full stack developer fresher", "full stack intern"],
    "qa-testing": ["qa engineer fresher", "software testing intern"],
    "data-analyst": ["data analyst fresher", "data analyst intern"],
    "powerbi-analyst": ["power bi developer fresher", "power bi intern"],
    "data-engineer": ["data engineer fresher", "data engineer intern"],
    "ml-ai": ["machine learning engineer fresher", "ml intern"],
    "gen-ai": ["generative ai fresher", "gen ai intern"],
    "ai-engineer": ["ai engineer fresher", "prompt engineer intern", "ai developer intern"],
    "agentic-ai": ["agentic ai fresher", "llm engineer intern"],
    "it-support": ["it support fresher", "helpdesk intern"],
    "servicenow": ["servicenow developer fresher", "servicenow intern"],
    "business-analyst": ["business analyst fresher", "business analyst intern"],
    "cyber-security": ["cyber security fresher", "cybersecurity intern"],
    "salesforce-crm": ["salesforce developer fresher", "salesforce intern"],
    "dynamics-crm": ["dynamics 365 fresher", "dynamics crm intern"],
    "power-platform": ["power platform developer fresher", "power automate intern"],
}


def load_families() -> list[dict]:
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    catalog_names = {fam["id"]: fam["name"] for fam in data["families"]}
    fallback_names = {
        "ai-engineer": "Applied AI / Prompt Engineer",
    }
    families = []
    for fam_id, keywords in FAMILY_KEYWORDS.items():
        families.append(
            {
                "id": fam_id,
                "name": catalog_names.get(fam_id) or fallback_names.get(fam_id, fam_id),
                "keywords": keywords,
            }
        )
    return families
