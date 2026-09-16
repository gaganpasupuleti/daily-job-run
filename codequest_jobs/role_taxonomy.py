"""CodeQuest ROLE_* taxonomy (synced from learn-coding-through job_role_seed)."""

from __future__ import annotations

from datetime import datetime, timezone

from psycopg2.extras import Json

from db import connect

JOB_ROLES: list[dict[str, str | None]] = [
    {"role_id": "ROLE_JAVA_BACKEND", "role_name": "Java Backend Developer", "role_family": "java-backend", "description": "Backend services with Java and Spring ecosystem."},
    {"role_id": "ROLE_PYTHON_DEV", "role_name": "Python Developer", "role_family": "python-dev", "description": "Application development with Python frameworks and APIs."},
    {"role_id": "ROLE_DATA_ANALYST", "role_name": "Data Analyst", "role_family": "data-analyst", "description": "SQL, reporting, dashboards, and business analytics."},
    {"role_id": "ROLE_POWERBI_ANALYST", "role_name": "Power BI Analyst", "role_family": "powerbi-analyst", "description": "Power BI modeling, DAX, and visualization."},
    {"role_id": "ROLE_FRONTEND_REACT", "role_name": "Frontend React Developer", "role_family": "frontend-react", "description": "React UI development and frontend integration."},
    {"role_id": "ROLE_FULLSTACK_WEB", "role_name": "Full Stack Web Developer", "role_family": "fullstack-web", "description": "End-to-end web application development."},
    {"role_id": "ROLE_QA_TESTING", "role_name": "QA / Testing Engineer", "role_family": "qa-testing", "description": "Manual and automated software testing."},
    {"role_id": "ROLE_DATA_ENGINEER", "role_name": "Data Engineer", "role_family": "data-engineer", "description": "Data pipelines, ETL, and platform engineering."},
    {"role_id": "ROLE_ML_AI", "role_name": "ML / Data Science Engineer", "role_family": "ml-ai", "description": "Classic machine learning, model training, and data science engineering."},
    {"role_id": "ROLE_GEN_AI", "role_name": "Generative AI / LLM Engineer", "role_family": "gen-ai", "description": "LLMs, RAG, and generative AI application development."},
    {"role_id": "ROLE_AI_ENGINEER", "role_name": "Applied AI / Prompt Engineer", "role_family": "ai-engineer", "description": "Applied AI engineering, prompt engineering, AI interns/developers, and AI product implementation."},
    {"role_id": "ROLE_AGENTIC_AI", "role_name": "Agentic AI Engineer", "role_family": "agentic-ai", "description": "AI agents, multi-agent systems, and autonomous agent workflows."},
    {"role_id": "ROLE_IT_SUPPORT", "role_name": "IT Support / Helpdesk", "role_family": "it-support", "description": "L1/L2 support, troubleshooting, and ticketing."},
    {"role_id": "ROLE_SERVICENOW", "role_name": "ServiceNow Developer / Admin", "role_family": "servicenow", "description": "ServiceNow platform configuration and development."},
    {"role_id": "ROLE_BUSINESS_ANALYST", "role_name": "Business Analyst", "role_family": "business-analyst", "description": "Requirements, process analysis, and stakeholder coordination."},
    {"role_id": "ROLE_CYBER_SECURITY", "role_name": "Cyber Security Analyst", "role_family": "cyber-security", "description": "SOC, infosec, vulnerability assessment, and security operations."},
    {"role_id": "ROLE_SALESFORCE", "role_name": "Salesforce Developer / Admin", "role_family": "salesforce-crm", "description": "Salesforce CRM development, Apex, Lightning, and administration."},
    {"role_id": "ROLE_DYNAMICS_CRM", "role_name": "Microsoft Dynamics 365 CRM", "role_family": "dynamics-crm", "description": "Dynamics 365 CRM configuration, plugins, and customization."},
    {"role_id": "ROLE_POWER_PLATFORM", "role_name": "Power Platform Developer", "role_family": "power-platform", "description": "Power Apps, Power Automate, and Microsoft low-code solutions."},
    {"role_id": "ROLE_OTHER_REVIEW", "role_name": "Other (needs review)", "role_family": "other-review", "description": "Ambiguous or cross-role postings requiring manual review."},
]

LEVEL_BANDS = [
    {"suffix": "FRESHER", "experience_level": "fresher", "min_years": 0, "max_years": 1},
    {"suffix": "ENTRY", "experience_level": "entry", "min_years": 1, "max_years": 2},
    {"suffix": "EXPERIENCED", "experience_level": "experienced", "min_years": 2, "max_years": None},
]

# Default core skills per role for quiz generation
CORE_SKILLS: dict[str, list[str]] = {
    "ROLE_JAVA_BACKEND": ["java", "spring-boot", "sql", "rest-apis"],
    "ROLE_PYTHON_DEV": ["python", "django", "fastapi", "sql"],
    "ROLE_DATA_ANALYST": ["sql", "excel", "data-analysis", "power-bi"],
    "ROLE_POWERBI_ANALYST": ["power-bi", "dax", "excel", "sql"],
    "ROLE_FRONTEND_REACT": ["react", "javascript", "html-css", "typescript"],
    "ROLE_FULLSTACK_WEB": ["javascript", "react", "nodejs", "sql"],
    "ROLE_QA_TESTING": ["manual-testing", "selenium", "test-automation", "jira"],
    "ROLE_DATA_ENGINEER": ["python", "sql", "etl", "spark"],
    "ROLE_ML_AI": ["python", "machine-learning", "pandas", "sql"],
    "ROLE_GEN_AI": ["python", "llm", "rag", "generative-ai"],
    "ROLE_AI_ENGINEER": ["python", "prompt-engineering", "llm", "ai-engineering"],
    "ROLE_AGENTIC_AI": ["python", "agentic-ai", "langgraph", "llm"],
    "ROLE_IT_SUPPORT": ["troubleshooting", "ticketing", "windows", "networking"],
    "ROLE_SERVICENOW": ["servicenow", "itsm", "javascript", "workflows"],
    "ROLE_BUSINESS_ANALYST": ["requirements", "sql", "excel", "uat"],
    "ROLE_CYBER_SECURITY": ["cybersecurity", "soc-operations", "siem", "networking"],
    "ROLE_SALESFORCE": ["salesforce", "apex", "crm", "lightning"],
    "ROLE_DYNAMICS_CRM": ["dynamics-365", "crm", "power-platform", "c-sharp"],
    "ROLE_POWER_PLATFORM": ["power-apps", "power-automate", "power-bi", "dataverse"],
    "ROLE_OTHER_REVIEW": ["programming-fundamentals"],
}

FAMILY_TO_ROLE = {str(r["role_family"]): str(r["role_id"]) for r in JOB_ROLES}
ROLE_NAMES = {str(r["role_id"]): str(r["role_name"]) for r in JOB_ROLES}


def job_role_levels_seed() -> list[dict]:
    rows = []
    for role in JOB_ROLES:
        role_id = str(role["role_id"])
        for band in LEVEL_BANDS:
            rows.append(
                {
                    "role_level_id": f"{role_id}_{band['suffix']}",
                    "role_id": role_id,
                    "experience_level": band["experience_level"],
                    "min_years": band["min_years"],
                    "max_years": band["max_years"],
                }
            )
    return rows


def ensure_taxonomy() -> dict[str, int]:
    """Idempotent upsert of job_roles + job_role_levels."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    roles_n = 0
    levels_n = 0
    with connect() as conn:
        with conn.cursor() as cur:
            for role in JOB_ROLES:
                skills = CORE_SKILLS.get(str(role["role_id"]), [])
                cur.execute(
                    """
                    INSERT INTO job_roles (
                        role_id, role_name, role_family, description,
                        core_skills, active, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, TRUE, %s, %s)
                    ON CONFLICT (role_id) DO UPDATE SET
                        role_name = EXCLUDED.role_name,
                        role_family = EXCLUDED.role_family,
                        description = EXCLUDED.description,
                        core_skills = COALESCE(job_roles.core_skills, EXCLUDED.core_skills),
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        role["role_id"],
                        role["role_name"],
                        role["role_family"],
                        role["description"],
                        Json(skills),
                        now,
                        now,
                    ),
                )
                roles_n += 1
            for level in job_role_levels_seed():
                cur.execute(
                    """
                    INSERT INTO job_role_levels (
                        role_level_id, role_id, experience_level,
                        min_years, max_years, active, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, TRUE, %s, %s)
                    ON CONFLICT (role_level_id) DO UPDATE SET
                        experience_level = EXCLUDED.experience_level,
                        min_years = EXCLUDED.min_years,
                        max_years = EXCLUDED.max_years,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        level["role_level_id"],
                        level["role_id"],
                        level["experience_level"],
                        level["min_years"],
                        level["max_years"],
                        now,
                        now,
                    ),
                )
                levels_n += 1
        conn.commit()
    return {"roles": roles_n, "levels": levels_n}
