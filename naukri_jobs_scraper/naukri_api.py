"""Simple HTTP client for Naukri job search API (no browser)."""

from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass, asdict
from typing import Any

import httpx
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MFwwDQYJKoZIhvcNAQEBBQADSwAwSAJBALrlQ+djR0RjJwBF1xuisHmdFv334MIm
K6LgzJhmLhN7B5yuEyaKoasgXQk3+OQglsOaBxEJ0j5PcTL3nbOvt80CAwEAAQ==
-----END PUBLIC KEY-----"""

JOB_SEARCH_URL = "https://www.naukri.com/jobapi/v3/search"
JOB_DETAIL_URL = "https://www.naukri.com/jobapi/v1/job/{job_id}"

FRESHER_TITLE_RE = re.compile(
    r"\b(intern(ship)?|fresher|trainee|entry[- ]?level|graduate|junior|jr\.|"
    r"l0\b|l1\b|level\s*0|0\s*yrs?|0\s*to\s*0|apprentice)\b",
    re.I,
)

SENIOR_TITLE_RE = re.compile(
    r"\b(senior|sr\.|lead|manager|principal|director|head of|architect|"
    r"5\+\s*years?|7\+\s*years?|10\+\s*years?)\b",
    re.I,
)


@dataclass
class NaukriJob:
    job_id: str
    title: str
    company: str
    location: str
    experience: str
    salary: str
    skills: str
    description: str
    job_url: str
    posted_label: str
    search_keyword: str
    experience_filter: int
    role_family: str = ""
    role_family_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def generate_nkparam(page_type: str = "srp") -> str:
    key = RSA.import_key(PUBLIC_KEY)
    cipher = PKCS1_v1_5.new(key)
    timestamp = int(time.time() * 1000)
    plaintext = f"v0|{timestamp}|121_{page_type}"
    encrypted = cipher.encrypt(plaintext.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


def _placeholder_label(raw: dict, ptype: str) -> str:
    for item in raw.get("placeholders", []):
        if item.get("type") == ptype:
            return item.get("label") or ""
    return ""


def _build_seo_key(keyword: str, location: str, page: int) -> str:
    kw_slug = keyword.strip().lower().replace(".", "-dot-").replace(" ", "-").replace("+", "-").strip("-")
    if location.strip():
        loc_slug = location.strip().lower().replace(" ", "-")
        return f"{kw_slug}-jobs-in-{loc_slug}-{page}"
    return f"{kw_slug}-jobs-{page}"


def parse_job(raw: dict, keyword: str, experience_filter: int) -> NaukriJob:
    job_id = str(raw.get("jobId") or raw.get("id") or "")
    jd_url = raw.get("jdURL") or ""
    if jd_url and not jd_url.startswith("http"):
        jd_url = f"https://www.naukri.com{jd_url}"

    return NaukriJob(
        job_id=job_id,
        title=raw.get("title") or raw.get("jobTitle") or "",
        company=raw.get("companyName") or raw.get("company") or "",
        location=_placeholder_label(raw, "location"),
        experience=_placeholder_label(raw, "experience"),
        salary=_placeholder_label(raw, "salary"),
        skills=raw.get("tagsAndSkills") or "",
        description=(raw.get("jobDescription") or "").strip(),
        job_url=jd_url,
        posted_label=raw.get("footerPlaceholderLabel") or "",
        search_keyword=keyword,
        experience_filter=experience_filter,
    )


def is_fresher_or_intern(job: NaukriJob) -> bool:
    blob = f"{job.title} {job.experience} {job.description[:300]}"
    if SENIOR_TITLE_RE.search(blob):
        return False
    if FRESHER_TITLE_RE.search(blob):
        return True
    # Naukri experience filter 0 usually means 0 years
    return job.experience_filter == 0 and (
        "0 Yrs" in job.experience or "0-0" in job.experience or not job.experience
    )


class NaukriSearchClient:
    def __init__(self, delay_seconds: float = 1.5):
        self.delay_seconds = delay_seconds
        self._client = httpx.Client(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        self._client.get("https://www.naukri.com/")

    def close(self) -> None:
        self._client.close()

    def search_page(
        self,
        keyword: str,
        *,
        location: str = "india",
        page: int = 1,
        experience: int = 0,
        results_per_page: int = 20,
        job_age_days: int = 15,
    ) -> list[dict]:
        headers = {
            "Accept": "application/json",
            "Accept-Language": "en-IN,en;q=0.9",
            "appid": "109",
            "systemid": "109",
            "clientid": "d3skt0p",
            "gid": "LOCATION,INDUSTRY,EDUCATION,FAREA_ROLE",
            "nkparam": generate_nkparam("srp"),
            "referer": "https://www.naukri.com/",
            "origin": "https://www.naukri.com",
        }
        params = {
            "noOfResults": results_per_page,
            "urlType": "search_by_keyword",
            "searchType": "adv",
            "keyword": keyword,
            "k": keyword,
            "pageNo": page,
            "experience": experience,
            "jobAge": job_age_days,
            "seoKey": _build_seo_key(keyword, location, page),
            "src": "jobsearchDesk",
            "nignbevent_src": "jobsearchDeskGNB",
        }
        if location:
            params["location"] = location

        response = self._client.get(JOB_SEARCH_URL, params=params, headers=headers)
        if response.status_code == 403:
            raise RuntimeError("Naukri blocked the request (403). Retry in a few minutes.")
        if response.status_code == 406:
            raise RuntimeError("Naukri requires captcha (406). Run again from a home IP.")
        if response.status_code == 400 and "pageNo" in response.text:
            return []
        if response.status_code != 200:
            raise RuntimeError(f"Naukri API error {response.status_code}: {response.text[:300]}")

        data = response.json()
        return data.get("jobDetails") or data.get("jobs") or []

    def search_jobs(
        self,
        keyword: str,
        *,
        location: str = "india",
        max_pages: int = 3,
        experience: int = 0,
        fresher_only: bool = True,
        results_per_page: int = 20,
    ) -> list[NaukriJob]:
        seen: set[str] = set()
        jobs: list[NaukriJob] = []

        for page in range(1, max_pages + 1):
            if page > 1:
                time.sleep(self.delay_seconds)

            raw_jobs = self.search_page(
                keyword,
                location=location,
                page=page,
                experience=experience,
                results_per_page=results_per_page,
            )
            if not raw_jobs:
                break

            for raw in raw_jobs:
                parsed = parse_job(raw, keyword, experience)
                if not parsed.job_id or parsed.job_id in seen:
                    continue
                if fresher_only and not is_fresher_or_intern(parsed):
                    continue
                seen.add(parsed.job_id)
                jobs.append(parsed)

        return jobs

    def check_job_status(self, job_id: str) -> str:
        """Return 'active', 'expired', or 'unknown' for a single Naukri job ID."""
        headers = {
            "Accept": "application/json",
            "Accept-Language": "en-IN,en;q=0.9",
            "appid": "121",
            "systemid": "Naukri",
            "clientid": "d3skt0p",
            "nkparam": generate_nkparam("jd"),
            "referer": "https://www.naukri.com/",
            "origin": "https://www.naukri.com",
        }
        params = {
            "microsite": "y",
            "src": "jobsearchDesk",
            "xp": "1",
            "px": "1",
        }
        url = JOB_DETAIL_URL.format(job_id=job_id)
        try:
            response = self._client.get(url, params=params, headers=headers)
        except httpx.HTTPError:
            return "unknown"

        if response.status_code in (404, 410):
            return "expired"
        if response.status_code in (403, 406):
            return "unknown"
        if response.status_code == 500:
            # Naukri often returns HTML "System Error" for removed/expired JDs.
            body = response.text.lower()
            if "system error" in body or "job not found" in body or "not found" in body:
                return "expired"
            return "unknown"
        if response.status_code != 200:
            return "unknown"

        try:
            data = response.json()
        except Exception:
            return "unknown"

        job = data.get("job") or data.get("jobDetails") or data
        if not isinstance(job, dict):
            return "unknown"

        # Naukri marks closed / withdrawn listings in several fields.
        closed_flags = [
            job.get("jobStatus"),
            job.get("status"),
            job.get("applyStatus"),
            str(job.get("isExpired", "")),
            str(job.get("expired", "")),
            str(job.get("isClosed", "")),
        ]
        blob = " ".join(str(x).lower() for x in closed_flags if x is not None)
        if any(token in blob for token in ("expired", "closed", "inactive", "withdrawn", "true")):
            return "expired"

        title = job.get("title") or job.get("jobTitle") or ""
        if not title and not job.get("jobId"):
            return "expired"
        return "active"
