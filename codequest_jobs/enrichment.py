"""Rules-v1 role classification.

classify_job_row is pure and used by the JobReady daily run.
Database writes to job_enrichments and practice items are LEGACY and are not
part of the daily pipeline.
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from psycopg2.extras import Json

from db import connect, ensure_jd_schema
from jd_cleaner import clean_job_description
from practice_items import upsert_practice_for_enrichments
from role_taxonomy import ROLE_NAMES, ensure_taxonomy

OUTPUT_DIR = Path(__file__).resolve().parent / "output"

TITLE_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("ROLE_POWER_PLATFORM", ("power apps", "powerapps", "power automate", "power platform", "canvas app", "power fx", "model-driven app")),
    ("ROLE_POWERBI_ANALYST", ("power bi", "powerbi", " dax", "power-bi")),
    ("ROLE_SALESFORCE", ("salesforce", "sfdc", "apex developer", "salesforce developer", "salesforce admin", "lightning developer")),
    ("ROLE_DYNAMICS_CRM", ("dynamics 365", "dynamics crm", "microsoft dynamics", " d365", "crm developer")),
    ("ROLE_CYBER_SECURITY", ("cyber security", "cybersecurity", "soc analyst", "information security", "infosec", "security analyst", "security operations", "cyber defense", "soc ")),
    ("ROLE_DATA_ANALYST", ("data analyst", "reporting analyst", "bi analyst", "mis analyst", "analytics analyst")),
    ("ROLE_DATA_ENGINEER", ("data engineer", " etl", "pipeline engineer", "spark", "airflow")),
    ("ROLE_AGENTIC_AI", (
        "agentic ai", "ai agent", "ai agents", "multi-agent", "multi agent", "autonomous agent",
        "langgraph", "crewai", "autogen", "agent workflow", "agentic workflow", "agent developer",
    )),
    ("ROLE_GEN_AI", (
        "gen ai", "genai", "generative ai", "llm engineer", "llm developer", "large language model",
        "rag engineer", "rag developer", " chatgpt", " openai", " llm ",
    )),
    ("ROLE_AI_ENGINEER", (
        "prompt engineer", "prompt engineering", "ai engineer", "ai developer", "ai intern",
        "ai/ml intern", "ai / ml", "ai-ml", "applied ai", "ai trainee", "jr .ai", "jr.ai",
        "artificial intelligence", " ai internship", "ai automation",
    )),
    ("ROLE_ML_AI", (
        "machine learning", "ml engineer", "data scientist", "data science engineer",
        "deep learning", "nlp engineer", "computer vision", "tensorflow", "pytorch", "scikit-learn",
        "ml intern", "ai/ml",
    )),
    ("ROLE_JAVA_BACKEND", ("java developer", "java engineer", "spring boot", "springboot", " java ", "java backend")),
    ("ROLE_PYTHON_DEV", ("python developer", "python engineer", " django", " flask", " fastapi", " python ")),
    ("ROLE_FRONTEND_REACT", (
        "react developer", "react.js", "next.js", "nextjs", "frontend developer", "front end developer",
        "front-end", "frontend engineer", " ui developer", " react ", "javascript developer", "typescript developer",
    )),
    ("ROLE_QA_TESTING", ("qa engineer", "quality assurance", "software tester", "manual testing", "test engineer", "selenium")),
    ("ROLE_SERVICENOW", ("servicenow", "itsm developer", "itsm admin")),
    ("ROLE_IT_SUPPORT", ("it support", "helpdesk", "help desk", "desktop support", " l1 support", " l2 support")),
    ("ROLE_BUSINESS_ANALYST", ("business analyst", "functional analyst", "requirements analyst", " brd ", " uat ")),
    ("ROLE_FULLSTACK_WEB", ("full stack", "fullstack", "mern", "software engineer", "software developer", "sde", "web developer")),
]

# Prefer scrape ingest_profile / family slug when present
FAMILY_HINTS = {
    "java-backend": "ROLE_JAVA_BACKEND",
    "python-dev": "ROLE_PYTHON_DEV",
    "frontend-react": "ROLE_FRONTEND_REACT",
    "fullstack-web": "ROLE_FULLSTACK_WEB",
    "qa-testing": "ROLE_QA_TESTING",
    "data-analyst": "ROLE_DATA_ANALYST",
    "powerbi-analyst": "ROLE_POWERBI_ANALYST",
    "data-engineer": "ROLE_DATA_ENGINEER",
    "ml-ai": "ROLE_ML_AI",
    "gen-ai": "ROLE_GEN_AI",
    "ai-engineer": "ROLE_AI_ENGINEER",
    "agentic-ai": "ROLE_AGENTIC_AI",
    "it-support": "ROLE_IT_SUPPORT",
    "servicenow": "ROLE_SERVICENOW",
    "business-analyst": "ROLE_BUSINESS_ANALYST",
    "cyber-security": "ROLE_CYBER_SECURITY",
    "salesforce-crm": "ROLE_SALESFORCE",
    "dynamics-crm": "ROLE_DYNAMICS_CRM",
    "power-platform": "ROLE_POWER_PLATFORM",
}

SKILL_LEXICON: dict[str, tuple[str, str]] = {
    "java": ("lang", "java"),
    "python": ("lang", "python"),
    "javascript": ("lang", "javascript"),
    "typescript": ("lang", "typescript"),
    "sql": ("req", "sql"),
    "react": ("fw", "react"),
    "spring": ("fw", "spring-boot"),
    "django": ("fw", "django"),
    "flask": ("fw", "flask"),
    "fastapi": ("fw", "fastapi"),
    "node.js": ("fw", "nodejs"),
    "nodejs": ("fw", "nodejs"),
    "excel": ("tool", "excel"),
    "power bi": ("tool", "power-bi"),
    "powerbi": ("tool", "power-bi"),
    "git": ("tool", "git"),
    "jira": ("tool", "jira"),
    "selenium": ("tool", "selenium"),
    "aws": ("tool", "aws"),
    "azure": ("tool", "azure"),
    "mysql": ("db", "mysql"),
    "postgresql": ("db", "postgresql"),
    "mongodb": ("db", "mongodb"),
    "html": ("req", "html-css"),
    "css": ("req", "html-css"),
    "rest api": ("req", "rest-apis"),
    "machine learning": ("req", "machine-learning"),
    "testing": ("req", "manual-testing"),
    "salesforce": ("tool", "salesforce"),
    "apex": ("lang", "apex"),
    "power apps": ("tool", "power-apps"),
    "power automate": ("tool", "power-automate"),
    "dynamics 365": ("tool", "dynamics-365"),
    "cybersecurity": ("req", "cybersecurity"),
    "cyber security": ("req", "cybersecurity"),
    "langchain": ("fw", "langchain"),
    "langgraph": ("fw", "langgraph"),
    "generative ai": ("req", "generative-ai"),
    "gen ai": ("req", "generative-ai"),
    "llm": ("req", "llm"),
    "rag": ("req", "rag"),
    "prompt engineering": ("req", "prompt-engineering"),
    "tensorflow": ("fw", "tensorflow"),
    "pytorch": ("fw", "pytorch"),
    "agentic": ("req", "agentic-ai"),
}

PREP_TOPICS = {
    "java": "java-basics",
    "python": "python-fundamentals",
    "javascript": "javascript-es6",
    "sql": "sql-queries",
    "react": "react-components",
    "html-css": "html-css",
    "excel": "excel-pivot",
    "power-bi": "power-bi-basics",
    "manual-testing": "testing-fundamentals",
    "rest-apis": "api-basics",
    "machine-learning": "ml-basics",
    "generative-ai": "gen-ai-basics",
    "llm": "llm-fundamentals",
    "rag": "rag-basics",
    "agentic-ai": "agentic-ai-basics",
    "cybersecurity": "security-fundamentals",
    "salesforce": "salesforce-basics",
    "power-apps": "power-platform-basics",
}

YEARS_RE = re.compile(r"(\d+)\+?\s*(?:years?|yrs?)", re.I)
FRESHER_KW = ("fresher", "graduate", "trainee", "intern", "entry level", "entry-level", "0-1 year", "0 to 1")
ENTRY_KW = ("junior", "associate", "1 year", "1-2 year", "1 to 2", "1 yr")
EXP_KW = ("senior", "lead", "architect", "principal", "5+ year", "5 year", "4 year", "3-5 year")


def _score_roles(text: str) -> list[tuple[str, float]]:
    scores: list[tuple[str, float]] = []
    for role_id, patterns in TITLE_RULES:
        hits = sum(1 for p in patterns if p in text)
        if hits:
            scores.append((role_id, min(0.95, 0.55 + hits * 0.15)))
    scores.sort(key=lambda x: -x[1])
    return scores


def _experience_band(text: str, profile: str | None) -> tuple[str, str]:
    m = YEARS_RE.search(text)
    if m:
        yrs = int(m.group(1))
        if yrs <= 1:
            return "fresher", "FRESHER"
        if yrs <= 2:
            return "entry", "ENTRY"
        return "experienced", "EXPERIENCED"
    if profile == "internship_india" or " intern" in f" {text}":
        return "fresher", "FRESHER"
    if profile in FAMILY_HINTS or any(k in text for k in FRESHER_KW):
        # sister ingest_profile is often family id like python-dev
        if profile == "entry_level_india" or any(k in text for k in ENTRY_KW):
            return "entry", "ENTRY"
        return "fresher", "FRESHER"
    if profile == "entry_level_india" or any(k in text for k in ENTRY_KW):
        return "entry", "ENTRY"
    if profile == "experienced_manual_india" or any(k in text for k in EXP_KW):
        return "experienced", "EXPERIENCED"
    return "fresher", "FRESHER"


def _extract_skills(text: str) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {
        "required_skills": [],
        "good_to_have_skills": [],
        "tools": [],
        "programming_languages": [],
        "databases": [],
        "frameworks": [],
    }
    counts: Counter[str] = Counter()
    bucket_map = {
        "req": "required_skills",
        "good": "good_to_have_skills",
        "tool": "tools",
        "lang": "programming_languages",
        "db": "databases",
        "fw": "frameworks",
    }
    for token, (bucket, slug) in SKILL_LEXICON.items():
        if token in text:
            counts[slug] += text.count(token)
            key = bucket_map[bucket]
            if slug not in buckets[key]:
                buckets[key].append(slug)
    if not buckets["required_skills"] and counts:
        buckets["required_skills"] = [s for s, _ in counts.most_common(3)]
    return buckets


def classify_job_row(row: dict[str, Any]) -> dict[str, Any]:
    title = row.get("title") or ""
    description = row.get("description") or ""
    company = row.get("company") or ""
    profile = row.get("ingest_profile")
    link_status = row.get("link_status") or "active"
    text = f"{title} {description} {row.get('job_type') or ''}".lower()

    scores = _score_roles(text)
    exp_level, exp_suffix = _experience_band(text, profile)

    # Boost family hint from sister scrape ingest_profile, but do not
    # override a clear AI title match with a non-AI scrape family.
    AI_ROLES = {"ROLE_AI_ENGINEER", "ROLE_GEN_AI", "ROLE_ML_AI", "ROLE_AGENTIC_AI"}
    if profile in FAMILY_HINTS:
        hinted = FAMILY_HINTS[profile]
        strong_ai = bool(scores and scores[0][0] in AI_ROLES and scores[0][1] >= 0.7)
        if strong_ai and hinted not in AI_ROLES:
            notes_prefix = f"Kept title AI match over scrape hint {profile}. "
        else:
            notes_prefix = ""
            if not scores or scores[0][0] != hinted:
                scores = [(hinted, 0.8)] + [(r, s) for r, s in scores if r != hinted]
    else:
        notes_prefix = ""

    if not scores:
        role_id = "ROLE_OTHER_REVIEW"
        confidence = 0.35
        needs_review = True
        notes = "No role family keyword match; mapped to ROLE_OTHER_REVIEW."
    else:
        role_id, confidence = scores[0]
        needs_review = False
        notes = f"{notes_prefix}rules-v1 → {role_id} (score={confidence:.2f})."
        if len(scores) > 1 and scores[1][1] >= scores[0][1] - 0.1:
            confidence = min(confidence, 0.55)
            needs_review = True
            notes += f" Ambiguous vs {scores[1][0]}."
        if role_id == "ROLE_FULLSTACK_WEB" and confidence < 0.75:
            needs_review = True

    cleaned = clean_job_description(description, title=title)
    # Prefer keywords from cleaned JD; fall back to lexicon on title+raw
    skills = _extract_skills(text)
    if cleaned["keywords_must"]:
        skills["required_skills"] = list(
            dict.fromkeys(cleaned["keywords_must"] + skills["required_skills"])
        )[:10]
    if cleaned["keywords_nice"]:
        skills["good_to_have_skills"] = list(
            dict.fromkeys(cleaned["keywords_nice"] + skills["good_to_have_skills"])
        )[:10]
    if not skills["required_skills"]:
        skills["required_skills"] = ["programming-fundamentals"]
        confidence = min(confidence, 0.65)
        needs_review = True

    jd_status = cleaned["jd_fetch_status"]
    if jd_status in ("UNKNOWN", "PARTIAL"):
        confidence = min(confidence, 0.69)
        needs_review = True

    if confidence < 0.70:
        needs_review = True

    prep = [PREP_TOPICS[s] for s in skills["required_skills"][:5] if s in PREP_TOPICS]
    role_name = ROLE_NAMES.get(role_id, role_id)
    summary = cleaned["jd_summary"] or f"{role_name} role at {company or 'company'}."
    if cleaned["jd_clean"]:
        summary = f"{role_name} at {company or 'company'}. {summary}"[:2000]
    else:
        summary = f"Title/metadata classification: {title} at {company or 'company'}."
        needs_review = True

    return {
        "job_id": row["job_id"],
        "actual_role_id": role_id,
        "actual_role_name": role_name,
        "role_level_id": f"{role_id}_{exp_suffix}",
        "experience_level": exp_level,
        "job_live_status": "LIVE" if link_status == "active" else "UNKNOWN",
        "jd_fetch_status": jd_status,
        "jd_summary": summary[:2000],
        "jd_clean": cleaned["jd_clean"],
        "jd_responsibilities": cleaned["jd_responsibilities"],
        "jd_requirements": cleaned["jd_requirements"],
        "keywords_must": cleaned["keywords_must"] or skills["required_skills"][:5],
        "keywords_nice": cleaned["keywords_nice"] or skills["good_to_have_skills"][:5],
        "practice_focus": cleaned["practice_focus"],
        "required_skills": skills["required_skills"],
        "good_to_have_skills": skills["good_to_have_skills"],
        "tools": skills["tools"],
        "programming_languages": skills["programming_languages"],
        "databases": skills["databases"],
        "frameworks": skills["frameworks"],
        "student_preparation_topics": prep,
        "quiz_pack_id": None,
        "mapping_confidence": round(float(confidence), 3),
        "manual_review_needed": needs_review,
        "approved_status": "NEEDS_REVIEW" if needs_review else "PENDING",
    }


def fetch_active_jobs(*, days: int | None = 14, limit: int = 0, family: str | None = None) -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            sql = """
                SELECT job_id, title, company, location, job_type, description,
                       link_status, ingest_profile, source, created_at
                FROM scraped_jobs
                WHERE COALESCE(link_status, 'active') = 'active'
                  AND job_id IS NOT NULL
            """
            params: list[Any] = []
            if days is not None and days > 0:
                cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
                sql += " AND created_at >= %s"
                params.append(cutoff)
            if family:
                role_id = FAMILY_HINTS.get(family)
                sql += " AND (ingest_profile = %s OR ingest_profile = %s)"
                params.extend([family, role_id or family])
            sql += " ORDER BY created_at DESC"
            if limit > 0:
                sql += " LIMIT %s"
                params.append(limit)
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def upsert_enrichments(rows: list[dict[str, Any]]) -> tuple[int, int]:
    if not rows:
        return 0, 0
    ensure_jd_schema()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    inserted = updated = 0
    with connect() as conn:
        with conn.cursor() as cur:
            for idx, r in enumerate(rows, start=1):
                cur.execute("SELECT 1 FROM job_enrichments WHERE job_id = %s", (r["job_id"],))
                exists = cur.fetchone() is not None
                cur.execute(
                    """
                    INSERT INTO job_enrichments (
                        job_id, actual_role_id, actual_role_name, role_level_id,
                        experience_level, job_live_status, jd_fetch_status, jd_summary,
                        jd_clean, jd_responsibilities, jd_requirements,
                        keywords_must, keywords_nice, practice_focus,
                        required_skills, good_to_have_skills, tools, programming_languages,
                        databases, frameworks, student_preparation_topics, quiz_pack_id,
                        mapping_confidence, manual_review_needed, approved_status,
                        created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s
                    )
                    ON CONFLICT (job_id) DO UPDATE SET
                        actual_role_id = EXCLUDED.actual_role_id,
                        actual_role_name = EXCLUDED.actual_role_name,
                        role_level_id = EXCLUDED.role_level_id,
                        experience_level = EXCLUDED.experience_level,
                        job_live_status = EXCLUDED.job_live_status,
                        jd_fetch_status = EXCLUDED.jd_fetch_status,
                        jd_summary = EXCLUDED.jd_summary,
                        jd_clean = EXCLUDED.jd_clean,
                        jd_responsibilities = EXCLUDED.jd_responsibilities,
                        jd_requirements = EXCLUDED.jd_requirements,
                        keywords_must = EXCLUDED.keywords_must,
                        keywords_nice = EXCLUDED.keywords_nice,
                        practice_focus = EXCLUDED.practice_focus,
                        required_skills = EXCLUDED.required_skills,
                        good_to_have_skills = EXCLUDED.good_to_have_skills,
                        tools = EXCLUDED.tools,
                        programming_languages = EXCLUDED.programming_languages,
                        databases = EXCLUDED.databases,
                        frameworks = EXCLUDED.frameworks,
                        student_preparation_topics = EXCLUDED.student_preparation_topics,
                        mapping_confidence = EXCLUDED.mapping_confidence,
                        manual_review_needed = EXCLUDED.manual_review_needed,
                        approved_status = EXCLUDED.approved_status,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        r["job_id"],
                        r["actual_role_id"],
                        r["actual_role_name"],
                        r["role_level_id"],
                        r["experience_level"],
                        r["job_live_status"],
                        r["jd_fetch_status"],
                        r["jd_summary"],
                        r.get("jd_clean") or "",
                        Json(r.get("jd_responsibilities") or []),
                        Json(r.get("jd_requirements") or []),
                        Json(r.get("keywords_must") or []),
                        Json(r.get("keywords_nice") or []),
                        Json(r.get("practice_focus") or []),
                        Json(r["required_skills"]),
                        Json(r["good_to_have_skills"]),
                        Json(r["tools"]),
                        Json(r["programming_languages"]),
                        Json(r["databases"]),
                        Json(r["frameworks"]),
                        Json(r["student_preparation_topics"]),
                        r.get("quiz_pack_id"),
                        r["mapping_confidence"],
                        r["manual_review_needed"],
                        r["approved_status"],
                        now,
                        now,
                    ),
                )
                if exists:
                    updated += 1
                else:
                    inserted += 1
                if idx % 100 == 0:
                    conn.commit()
                    print(f"  enrich progress: {idx}/{len(rows)}", flush=True)
        conn.commit()
    return inserted, updated


def run_enrichment(
    *,
    days: int | None = 14,
    limit: int = 0,
    family: str | None = None,
) -> dict[str, Any]:
    ensure_taxonomy()
    ensure_jd_schema()
    print("Fetching active jobs for enrichment...", flush=True)
    jobs = fetch_active_jobs(days=days, limit=limit, family=family)
    print(f"Classifying {len(jobs)} jobs...", flush=True)
    classified = [classify_job_row(j) for j in jobs if j.get("job_id")]
    print(f"Upserting {len(classified)} enrichments...", flush=True)
    inserted, updated = upsert_enrichments(classified)
    print("Writing practice items...", flush=True)
    practice = upsert_practice_for_enrichments(classified)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    csv_path = OUTPUT_DIR / f"enrichment_{stamp}.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "job_id", "actual_role_id", "actual_role_name", "role_level_id",
                "experience_level", "jd_fetch_status", "keywords_must",
                "mapping_confidence", "manual_review_needed",
            ],
        )
        writer.writeheader()
        for r in classified:
            writer.writerow(
                {
                    "job_id": r["job_id"],
                    "actual_role_id": r["actual_role_id"],
                    "actual_role_name": r["actual_role_name"],
                    "role_level_id": r["role_level_id"],
                    "experience_level": r["experience_level"],
                    "jd_fetch_status": r["jd_fetch_status"],
                    "keywords_must": "|".join(r.get("keywords_must") or []),
                    "mapping_confidence": r["mapping_confidence"],
                    "manual_review_needed": r["manual_review_needed"],
                }
            )

    role_counts = Counter(r["actual_role_id"] for r in classified)
    clean_n = sum(1 for r in classified if (r.get("jd_clean") or "").strip())
    return {
        "jobs_classified": len(classified),
        "inserted": inserted,
        "updated": updated,
        "with_jd_clean": clean_n,
        "practice": practice,
        "csv": str(csv_path),
        "role_breakdown": dict(role_counts),
    }
