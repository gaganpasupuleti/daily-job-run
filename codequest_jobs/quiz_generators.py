"""Generate MCQ + workbench (HackerRank-style stored) questions for ROLE_* packs."""

from __future__ import annotations

from typing import Any

from role_taxonomy import CORE_SKILLS, ROLE_NAMES


def _family_token(role_id: str) -> str:
    return role_id.replace("ROLE_", "")


def pack_id_for(role_id: str, experience_level: str, week: int = 1) -> str:
    level = experience_level.upper()
    if level not in ("FRESHER", "ENTRY", "EXPERIENCED"):
        level = "FRESHER"
    return f"QP_{_family_token(role_id)}_{level}_W{week:02d}"


def generate_questions(role_id: str, experience_level: str, skills: list[str] | None = None) -> list[dict[str, Any]]:
    """Return quiz_pack_questions-shaped dicts (without quiz_pack_id / question_id)."""
    role_name = ROLE_NAMES.get(role_id, role_id)
    skills = skills or CORE_SKILLS.get(role_id, ["programming-fundamentals"])
    primary = skills[0] if skills else "programming-fundamentals"
    difficulty = {
        "fresher": "easy",
        "entry": "medium",
        "experienced": "hard",
    }.get(experience_level, "easy")

    questions: list[dict[str, Any]] = [
        {
            "question_type": "MCQ",
            "skill_tag": primary[:64],
            "difficulty": difficulty,
            "question": f"Which skill is most central for a {role_name} fresher role?",
            "option_a": primary,
            "option_b": "graphic-design",
            "option_c": "video-editing",
            "option_d": "hardware-assembly",
            "correct_option": "A",
            "explanation": f"{primary} is a core skill for {role_name}.",
            "workbench_type": None,
            "starter_code_or_query": None,
            "expected_output": None,
        },
        {
            "question_type": "SCENARIO",
            "skill_tag": (skills[1] if len(skills) > 1 else primary)[:64],
            "difficulty": difficulty,
            "question": (
                f"A hiring manager asks you to explain a recent project relevant to {role_name}. "
                "What should you emphasize?"
            ),
            "option_a": "Problem, your approach, tools used, and measurable outcome",
            "option_b": "Only the company name",
            "option_c": "Unrelated hobbies",
            "option_d": "Salary expectations only",
            "correct_option": "A",
            "explanation": "STAR-style answers with tools and outcomes map well to fresher interviews.",
            "workbench_type": None,
            "starter_code_or_query": None,
            "expected_output": None,
        },
    ]

    # Workbench / coding challenges (stored; judge later in CodeQuest)
    if role_id in (
        "ROLE_PYTHON_DEV",
        "ROLE_DATA_ENGINEER",
        "ROLE_ML_AI",
        "ROLE_GEN_AI",
        "ROLE_AI_ENGINEER",
        "ROLE_AGENTIC_AI",
    ):
        questions.append(
            {
                "question_type": "PYTHON_WORKBENCH",
                "skill_tag": "python",
                "difficulty": difficulty,
                "question": "Write a function `normalize(nums)` that returns a new list with each value divided by the max (return [] if empty).",
                "option_a": None,
                "option_b": None,
                "option_c": None,
                "option_d": None,
                "correct_option": None,
                "explanation": "Handle empty input; avoid division by zero by returning [].",
                "workbench_type": "PYTHON",
                "starter_code_or_query": (
                    "def normalize(nums):\n"
                    "    # TODO: implement\n"
                    "    pass\n\n"
                    "if __name__ == '__main__':\n"
                    "    print(normalize([1, 2, 4]))\n"
                ),
                "expected_output": "[0.25, 0.5, 1.0]",
            }
        )
    elif role_id in ("ROLE_JAVA_BACKEND", "ROLE_FULLSTACK_WEB"):
        questions.append(
            {
                "question_type": "JAVA_WORKBENCH",
                "skill_tag": "java",
                "difficulty": difficulty,
                "question": "Complete `sumEven` to return the sum of even integers in the array.",
                "option_a": None,
                "option_b": None,
                "option_c": None,
                "option_d": None,
                "correct_option": None,
                "explanation": "Iterate and add values where n % 2 == 0.",
                "workbench_type": "JAVA",
                "starter_code_or_query": (
                    "public class Solution {\n"
                    "  public static int sumEven(int[] nums) {\n"
                    "    // TODO\n"
                    "    return 0;\n"
                    "  }\n"
                    "  public static void main(String[] args) {\n"
                    "    System.out.println(sumEven(new int[]{1,2,3,4}));\n"
                    "  }\n"
                    "}\n"
                ),
                "expected_output": "6",
            }
        )
    elif role_id in ("ROLE_DATA_ANALYST", "ROLE_POWERBI_ANALYST", "ROLE_BUSINESS_ANALYST"):
        questions.append(
            {
                "question_type": "SQL_WORKBENCH",
                "skill_tag": "sql",
                "difficulty": difficulty,
                "question": "Write SQL to return distinct department names from employees ordered alphabetically.",
                "option_a": None,
                "option_b": None,
                "option_c": None,
                "option_d": None,
                "correct_option": None,
                "explanation": "Use SELECT DISTINCT ... ORDER BY.",
                "workbench_type": "SQL",
                "starter_code_or_query": (
                    "-- table: employees(id, name, department)\n"
                    "SELECT ...\n"
                ),
                "expected_output": "distinct department list A→Z",
            }
        )
    elif role_id == "ROLE_FRONTEND_REACT":
        questions.append(
            {
                "question_type": "MCQ",
                "skill_tag": "react",
                "difficulty": difficulty,
                "question": "In React, which hook is used for state in a function component?",
                "option_a": "useState",
                "option_b": "useRouter",
                "option_c": "useSQL",
                "option_d": "useClass",
                "correct_option": "A",
                "explanation": "useState is the primary state hook for function components.",
                "workbench_type": None,
                "starter_code_or_query": None,
                "expected_output": None,
            }
        )
    else:
        questions.append(
            {
                "question_type": "MCQ",
                "skill_tag": primary[:64],
                "difficulty": difficulty,
                "question": f"What is a good first practice when preparing for a {role_name} interview?",
                "option_a": "Practice fundamentals and build a small demo project",
                "option_b": "Memorize unrelated trivia only",
                "option_c": "Skip reading the job description",
                "option_d": "Avoid using version control",
                "correct_option": "A",
                "explanation": "Fundamentals plus a demo project signal readiness for fresher roles.",
                "workbench_type": None,
                "starter_code_or_query": None,
                "expected_output": None,
            }
        )

    return questions
