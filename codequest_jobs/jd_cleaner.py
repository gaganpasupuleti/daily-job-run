"""Clean raw scraped JDs into plain text + structured sections/keywords."""

from __future__ import annotations

import html
import re
from typing import Any

# Shared lexicon labels used for must/nice keyword extraction
SKILL_PATTERNS: list[tuple[str, str]] = [
    ("python", "python"),
    ("java", "java"),
    ("javascript", "javascript"),
    ("typescript", "typescript"),
    ("react", "react"),
    ("next.js", "nextjs"),
    ("nextjs", "nextjs"),
    ("django", "django"),
    ("flask", "flask"),
    ("fastapi", "fastapi"),
    ("spring boot", "spring-boot"),
    ("springboot", "spring-boot"),
    ("node.js", "nodejs"),
    ("nodejs", "nodejs"),
    ("sql", "sql"),
    ("postgresql", "postgresql"),
    ("mysql", "mysql"),
    ("mongodb", "mongodb"),
    ("aws", "aws"),
    ("azure", "azure"),
    ("docker", "docker"),
    ("kubernetes", "kubernetes"),
    ("git", "git"),
    ("selenium", "selenium"),
    ("power bi", "power-bi"),
    ("powerbi", "power-bi"),
    ("power apps", "power-apps"),
    ("power automate", "power-automate"),
    ("salesforce", "salesforce"),
    ("apex", "apex"),
    ("dynamics 365", "dynamics-365"),
    ("servicenow", "servicenow"),
    ("machine learning", "machine-learning"),
    ("deep learning", "deep-learning"),
    ("tensorflow", "tensorflow"),
    ("pytorch", "pytorch"),
    ("llm", "llm"),
    ("rag", "rag"),
    ("langchain", "langchain"),
    ("langgraph", "langgraph"),
    ("generative ai", "generative-ai"),
    ("gen ai", "generative-ai"),
    ("rest api", "rest-apis"),
    ("rest apis", "rest-apis"),
    ("html", "html-css"),
    ("css", "html-css"),
    ("excel", "excel"),
    ("tableau", "tableau"),
    ("cybersecurity", "cybersecurity"),
    ("cyber security", "cybersecurity"),
    ("siem", "siem"),
]

HTML_TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t]+")
BLANK_RE = re.compile(r"\n{3,}")
BULLET_RE = re.compile(r"^\s*(?:[-*•●▪►]|\d+[.)])\s+(.+)$")

SECTION_HEADERS = {
    "responsibilities": re.compile(
        r"(?im)^\s*(?:key\s+)?(?:responsibilities|roles?\s*(?:and|&)?\s*responsibilities|"
        r"what you(?:'|)ll do|you will|duties|job description)\s*:?\s*$"
    ),
    "requirements": re.compile(
        r"(?im)^\s*(?:requirements|required\s+skills|must[- ]have|qualifications|"
        r"what we(?:'|)re looking for|eligibility|skills\s+required|desired\s+skills)\s*:?\s*$"
    ),
    "nice": re.compile(
        r"(?im)^\s*(?:nice\s+to\s+have|good\s+to\s+have|preferred|bonus|plus)\s*:?\s*$"
    ),
}


def strip_html(raw: str | None) -> str:
    if not raw:
        return ""
    text = html.unescape(str(raw))
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n", text)
    text = re.sub(r"(?i)</li\s*>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = HTML_TAG_RE.sub(" ", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [WS_RE.sub(" ", ln).strip() for ln in text.split("\n")]
    text = "\n".join(ln for ln in lines if ln)
    return BLANK_RE.sub("\n\n", text).strip()


def _bullets_from_block(block: str) -> list[str]:
    out: list[str] = []
    for ln in block.split("\n"):
        m = BULLET_RE.match(ln)
        if m:
            item = m.group(1).strip(" .;")
            if 8 <= len(item) <= 280:
                out.append(item)
        elif ln and not SECTION_HEADERS["responsibilities"].match(ln) and len(ln) > 20:
            # plain paragraph line under a section
            if len(out) < 12 and len(ln) <= 280:
                out.append(ln.strip(" .;"))
    # dedupe preserve order
    seen: set[str] = set()
    uniq = []
    for x in out:
        key = x.lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(x)
    return uniq[:12]


def split_sections(clean: str) -> dict[str, list[str]]:
    if not clean:
        return {"responsibilities": [], "requirements": [], "nice": []}

    lines = clean.split("\n")
    current = "body"
    buckets: dict[str, list[str]] = {"body": [], "responsibilities": [], "requirements": [], "nice": []}
    for ln in lines:
        matched = None
        for name, pat in SECTION_HEADERS.items():
            if pat.match(ln):
                matched = name
                break
        if matched:
            current = matched
            continue
        buckets[current].append(ln)

    responsibilities = _bullets_from_block("\n".join(buckets["responsibilities"]))
    requirements = _bullets_from_block("\n".join(buckets["requirements"]))
    nice = _bullets_from_block("\n".join(buckets["nice"]))

    # Fallback: pull bullets from whole JD if sections missing
    if not responsibilities and not requirements:
        all_bullets = _bullets_from_block(clean)
        mid = max(1, len(all_bullets) // 2)
        responsibilities = all_bullets[:mid]
        requirements = all_bullets[mid:]

    return {
        "responsibilities": responsibilities,
        "requirements": requirements,
        "nice": nice,
    }


def extract_keywords(text: str) -> tuple[list[str], list[str]]:
    low = f" {text.lower()} "
    found: list[str] = []
    for token, slug in SKILL_PATTERNS:
        if f" {token} " in low or f" {token}." in low or f"/{token}" in low:
            if slug not in found:
                found.append(slug)
    # Must = first half / primary; nice = remainder (also include explicit nice section later)
    if not found:
        return [], []
    split_at = max(1, min(5, (len(found) + 1) // 2))
    return found[:split_at], found[split_at:]


def practice_focus_from_keywords(must: list[str], nice: list[str]) -> list[str]:
    focus = []
    for slug in must + nice:
        if slug not in focus:
            focus.append(slug)
        if len(focus) >= 5:
            break
    if not focus:
        focus = ["programming-fundamentals"]
    return focus


def clean_job_description(raw: str | None, *, title: str = "") -> dict[str, Any]:
    clean = strip_html(raw)
    sections = split_sections(clean)
    blob = " ".join(
        [
            title or "",
            clean,
            " ".join(sections["responsibilities"]),
            " ".join(sections["requirements"]),
            " ".join(sections["nice"]),
        ]
    )
    must, nice_kw = extract_keywords(blob)
    # Prefer nice-section skills as nice keywords when present
    nice_from_section, _ = extract_keywords(" ".join(sections["nice"]))
    for slug in nice_from_section:
        if slug in must:
            must.remove(slug)
        if slug not in nice_kw:
            nice_kw.append(slug)

    if len(clean) >= 400:
        jd_status = "FETCHED"
    elif len(clean) >= 80:
        jd_status = "PARTIAL"
    else:
        jd_status = "UNKNOWN"

    # Clear summary: first 2–3 sentences / ~400 chars of clean text
    summary_src = clean or title
    summary = re.sub(r"\s+", " ", summary_src).strip()
    if len(summary) > 420:
        summary = summary[:417].rstrip() + "..."

    return {
        "jd_clean": clean,
        "jd_responsibilities": sections["responsibilities"],
        "jd_requirements": sections["requirements"],
        "keywords_must": must,
        "keywords_nice": nice_kw,
        "practice_focus": practice_focus_from_keywords(must, nice_kw),
        "jd_fetch_status": jd_status,
        "jd_summary": summary,
    }
