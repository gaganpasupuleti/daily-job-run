"""Upsert quiz_packs + questions; attach quiz_pack_id on job_enrichments."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from db import connect
from quiz_generators import generate_questions, pack_id_for
from role_taxonomy import FAMILY_TO_ROLE, ROLE_NAMES, ensure_taxonomy


def _role_level_pairs_needing_packs(*, family: str | None = None) -> list[tuple[str, str, str]]:
    """Return (role_id, role_level_id, experience_level) from enrichments missing packs or for refresh."""
    with connect() as conn:
        with conn.cursor() as cur:
            sql = """
                SELECT DISTINCT e.actual_role_id, e.role_level_id, e.experience_level
                FROM job_enrichments e
                JOIN scraped_jobs s ON s.job_id = e.job_id
                WHERE COALESCE(s.link_status, 'active') = 'active'
            """
            params: list[Any] = []
            if family:
                role_id = FAMILY_TO_ROLE.get(family)
                if role_id:
                    sql += " AND (s.ingest_profile = %s OR e.actual_role_id = %s)"
                    params.extend([family, role_id])
                else:
                    sql += " AND s.ingest_profile = %s"
                    params.append(family)
            cur.execute(sql, params)
            return [(r[0], r[1], r[2]) for r in cur.fetchall()]


def upsert_pack(role_id: str, role_level_id: str, experience_level: str, skills: list[str] | None = None) -> dict[str, Any]:
    ensure_taxonomy()
    qpid = pack_id_for(role_id, experience_level, week=1)
    questions = generate_questions(role_id, experience_level, skills)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    title = f"{ROLE_NAMES.get(role_id, role_id)} — {experience_level} Week 1"

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quiz_packs (
                    quiz_pack_id, role_id, role_level_id, week_number, title,
                    difficulty, question_count, linked_jobs_count, active,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, 1, %s, %s, %s, 0, TRUE, %s, %s)
                ON CONFLICT (quiz_pack_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    question_count = EXCLUDED.question_count,
                    updated_at = EXCLUDED.updated_at,
                    active = TRUE
                """,
                (
                    qpid,
                    role_id,
                    role_level_id,
                    title[:200],
                    {"fresher": "easy", "entry": "medium", "experienced": "hard"}.get(
                        experience_level, "easy"
                    ),
                    len(questions),
                    now,
                    now,
                ),
            )
            # Replace questions for idempotent packs (keep pack id stable)
            cur.execute("DELETE FROM quiz_pack_questions WHERE quiz_pack_id = %s", (qpid,))
            for q in questions:
                cur.execute(
                    """
                    INSERT INTO quiz_pack_questions (
                        quiz_pack_id, question_type, skill_tag, difficulty, question,
                        option_a, option_b, option_c, option_d, correct_option,
                        explanation, workbench_type, starter_code_or_query, expected_output,
                        active, created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        TRUE, %s, %s
                    )
                    """,
                    (
                        qpid,
                        q["question_type"],
                        q["skill_tag"],
                        q["difficulty"],
                        q["question"],
                        q["option_a"],
                        q["option_b"],
                        q["option_c"],
                        q["option_d"],
                        q["correct_option"],
                        q["explanation"],
                        q["workbench_type"],
                        q["starter_code_or_query"],
                        q["expected_output"],
                        now,
                        now,
                    ),
                )
            cur.execute(
                """
                UPDATE job_enrichments
                SET quiz_pack_id = %s, updated_at = %s
                WHERE role_level_id = %s
                  AND (quiz_pack_id IS NULL OR quiz_pack_id = '')
                """,
                (qpid, now, role_level_id),
            )
            linked = cur.rowcount
            # also re-attach all for this level
            cur.execute(
                """
                UPDATE job_enrichments
                SET quiz_pack_id = %s, updated_at = %s
                WHERE role_level_id = %s
                """,
                (qpid, now, role_level_id),
            )
            linked = cur.rowcount
            cur.execute(
                """
                UPDATE quiz_packs
                SET linked_jobs_count = %s, updated_at = %s
                WHERE quiz_pack_id = %s
                """,
                (linked, now, qpid),
            )
        conn.commit()

    return {
        "quiz_pack_id": qpid,
        "questions": len(questions),
        "linked_jobs": linked,
        "role_id": role_id,
        "role_level_id": role_level_id,
    }


def run_quiz_packs(*, family: str | None = None) -> dict[str, Any]:
    pairs = _role_level_pairs_needing_packs(family=family)
    if not pairs:
        # Still ensure fresher packs for core roles if enrichments empty
        pairs = [
            ("ROLE_PYTHON_DEV", "ROLE_PYTHON_DEV_FRESHER", "fresher"),
        ]

    results = []
    for role_id, role_level_id, exp in pairs:
        results.append(upsert_pack(role_id, role_level_id, exp))

    by_role: dict[str, int] = defaultdict(int)
    for r in results:
        by_role[r["role_id"]] += 1

    return {
        "packs_upserted": len(results),
        "packs": results,
        "roles": dict(by_role),
    }
