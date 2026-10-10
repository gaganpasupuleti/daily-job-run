"""LEGACY. Job-specific practice items (MCQ / coding / SQL) from clean keywords."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from db import connect


def _mcq(skill: str, difficulty: str) -> dict[str, Any]:
    return {
        "skill_tag": skill[:64],
        "item_type": "MCQ",
        "prompt": f"Which statement best matches practical use of `{skill}` in a fresher role?",
        "starter_code_or_query": None,
        "expected_output": None,
        "option_a": f"Use `{skill}` to solve a clear, scoped task and explain the trade-offs",
        "option_b": f"Avoid `{skill}` completely in interviews",
        "option_c": "Only memorize tool logos",
        "option_d": "Skip reading the job description",
        "correct_option": "A",
        "explanation": f"Interviewers look for applied understanding of {skill}, not buzzwords.",
        "difficulty": difficulty,
    }


def _coding(skill: str, difficulty: str, role_hint: str) -> dict[str, Any]:
    if skill in ("sql", "postgresql", "mysql"):
        return {
            "skill_tag": skill[:64],
            "item_type": "SQL",
            "prompt": (
                "Write SQL to return the top 5 departments by employee count "
                "from table employees(id, name, department)."
            ),
            "starter_code_or_query": (
                "-- employees(id, name, department)\n"
                "SELECT department, COUNT(*) AS cnt\n"
                "FROM employees\n"
                "-- complete query\n"
            ),
            "expected_output": "department + cnt, ordered by cnt DESC, limit 5",
            "option_a": None,
            "option_b": None,
            "option_c": None,
            "option_d": None,
            "correct_option": None,
            "explanation": "Group by department, order by count, limit 5.",
            "difficulty": difficulty,
        }

    lang = "python"
    if skill in ("java", "spring-boot"):
        lang = "java"
    elif skill in ("javascript", "typescript", "react", "nodejs"):
        lang = "javascript"

    if lang == "java":
        return {
            "skill_tag": skill[:64],
            "item_type": "CODING",
            "prompt": f"[{role_hint}] Complete `uniqueCount` to return how many unique strings are in the array (skill focus: {skill}).",
            "starter_code_or_query": (
                "import java.util.*;\n"
                "public class Solution {\n"
                "  public static int uniqueCount(String[] words) {\n"
                "    // TODO\n"
                "    return 0;\n"
                "  }\n"
                "}\n"
            ),
            "expected_output": "count of unique values",
            "option_a": None,
            "option_b": None,
            "option_c": None,
            "option_d": None,
            "correct_option": None,
            "explanation": "Use a HashSet and return its size.",
            "difficulty": difficulty,
        }

    if lang == "javascript":
        return {
            "skill_tag": skill[:64],
            "item_type": "CODING",
            "prompt": f"[{role_hint}] Implement `uniqueCount(words)` returning the number of unique strings (skill focus: {skill}).",
            "starter_code_or_query": (
                "function uniqueCount(words) {\n"
                "  // TODO\n"
                "}\n"
                "console.log(uniqueCount(['a','b','a']));\n"
            ),
            "expected_output": "2",
            "option_a": None,
            "option_b": None,
            "option_c": None,
            "option_d": None,
            "correct_option": None,
            "explanation": "Use a Set and return size.",
            "difficulty": difficulty,
        }

    return {
        "skill_tag": skill[:64],
        "item_type": "CODING",
        "prompt": (
            f"[{role_hint}] Write `normalize(nums)` that divides each value by the max. "
            f"Return [] if empty. Practice focus: {skill}."
        ),
        "starter_code_or_query": (
            "def normalize(nums):\n"
            "    # TODO\n"
            "    pass\n\n"
            "if __name__ == '__main__':\n"
            "    print(normalize([1, 2, 4]))\n"
        ),
        "expected_output": "[0.25, 0.5, 1.0]",
        "option_a": None,
        "option_b": None,
        "option_c": None,
        "option_d": None,
        "correct_option": None,
        "explanation": "Guard empty input; divide by max(nums).",
        "difficulty": difficulty,
    }


def build_practice_items(enrichment: dict[str, Any]) -> list[dict[str, Any]]:
    focus = list(enrichment.get("practice_focus") or enrichment.get("keywords_must") or [])
    if not focus:
        focus = ["programming-fundamentals"]
    difficulty = {
        "fresher": "easy",
        "entry": "medium",
        "experienced": "hard",
    }.get(str(enrichment.get("experience_level") or "fresher"), "easy")
    role_hint = str(enrichment.get("actual_role_name") or enrichment.get("actual_role_id") or "Developer")

    items: list[dict[str, Any]] = []
    # 1 MCQ on primary skill
    items.append(_mcq(focus[0], difficulty))
    # 1 coding/SQL on primary (or second) skill
    code_skill = focus[0]
    for s in focus:
        if s in ("sql", "postgresql", "mysql", "python", "java", "javascript", "typescript", "react"):
            code_skill = s
            break
    items.append(_coding(code_skill, difficulty, role_hint))
    # 1 MCQ on secondary focus if available
    if len(focus) > 1:
        items.append(_mcq(focus[1], difficulty))
    return items[:4]


def replace_practice_items(job_id: str, items: list[dict[str, Any]]) -> int:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM job_practice_items WHERE job_id = %s", (job_id,))
            for it in items:
                cur.execute(
                    """
                    INSERT INTO job_practice_items (
                        job_id, skill_tag, item_type, prompt,
                        starter_code_or_query, expected_output,
                        option_a, option_b, option_c, option_d, correct_option,
                        explanation, difficulty, active, created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, TRUE, %s, %s
                    )
                    """,
                    (
                        job_id,
                        it["skill_tag"],
                        it["item_type"],
                        it["prompt"],
                        it.get("starter_code_or_query"),
                        it.get("expected_output"),
                        it.get("option_a"),
                        it.get("option_b"),
                        it.get("option_c"),
                        it.get("option_d"),
                        it.get("correct_option"),
                        it.get("explanation") or "",
                        it.get("difficulty") or "easy",
                        now,
                        now,
                    ),
                )
        conn.commit()
    return len(items)


def upsert_practice_for_enrichments(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Replace practice items for many jobs in a single DB connection."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    total_items = 0
    jobs = 0
    with connect() as conn:
        with conn.cursor() as cur:
            for r in rows:
                jid = r.get("job_id")
                if not jid:
                    continue
                items = build_practice_items(r)
                cur.execute("DELETE FROM job_practice_items WHERE job_id = %s", (jid,))
                for it in items:
                    cur.execute(
                        """
                        INSERT INTO job_practice_items (
                            job_id, skill_tag, item_type, prompt,
                            starter_code_or_query, expected_output,
                            option_a, option_b, option_c, option_d, correct_option,
                            explanation, difficulty, active, created_at, updated_at
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, %s,
                            %s, %s, %s, %s, %s,
                            %s, %s, TRUE, %s, %s
                        )
                        """,
                        (
                            jid,
                            it["skill_tag"],
                            it["item_type"],
                            it["prompt"],
                            it.get("starter_code_or_query"),
                            it.get("expected_output"),
                            it.get("option_a"),
                            it.get("option_b"),
                            it.get("option_c"),
                            it.get("option_d"),
                            it.get("correct_option"),
                            it.get("explanation") or "",
                            it.get("difficulty") or "easy",
                            now,
                            now,
                        ),
                    )
                total_items += len(items)
                jobs += 1
                if jobs % 100 == 0:
                    conn.commit()
                    print(f"  practice progress: {jobs} jobs / {total_items} items", flush=True)
        conn.commit()
    return {"jobs": jobs, "items": total_items}
