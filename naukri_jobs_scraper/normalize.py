"""Shared job shape for Naukri + JobSpy sources."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from naukri_api import NaukriJob


def source_job_id(source: str, job_url: str) -> str:
    """Stable 32-char id: sha256(source:url) — matches CodeQuest scraped_jobs.id."""
    raw = f"{source}:{job_url}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


@dataclass
class UnifiedJob:
    job_id: str
    source: str
    title: str
    company: str = ""
    location: str = ""
    experience: str = ""
    salary: str = ""
    skills: str = ""
    description: str = ""
    job_url: str = ""
    posted_label: str = ""
    search_keyword: str = ""
    experience_filter: int = 0
    role_family: str = ""
    role_family_name: str = ""
    job_type: str = ""
    date_posted: datetime | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    currency: str = ""
    apply_url: str = ""
    # CodeQuest scraped_jobs primary key (always hash of source:url)
    codequest_id: str = ""
    ingest_profile: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.date_posted is not None:
            data["date_posted"] = self.date_posted.isoformat()
        return data


def from_naukri(job: NaukriJob, *, source: str = "naukri") -> UnifiedJob:
    url = (job.job_url or "").strip()
    cq_id = source_job_id(source, url) if url else job.job_id
    return UnifiedJob(
        job_id=job.job_id,
        source=source,
        title=job.title,
        company=job.company or "",
        location=job.location or "",
        experience=job.experience or "",
        salary=job.salary or "",
        skills=job.skills or "",
        description=job.description or "",
        job_url=url,
        posted_label=job.posted_label or "",
        search_keyword=job.search_keyword or "",
        experience_filter=job.experience_filter,
        role_family=job.role_family or "",
        role_family_name=job.role_family_name or "",
        apply_url=url,
        codequest_id=cq_id,
        ingest_profile=job.role_family or "fresher_india",
    )


def from_jobspy_row(
    row: dict[str, Any],
    *,
    search_keyword: str = "",
    role_family: str = "",
    role_family_name: str = "",
) -> UnifiedJob | None:
    source = str(row.get("source") or "").strip().lower()
    job_url = str(row.get("jobUrl") or row.get("job_url") or "").strip()
    if not source or not job_url:
        return None

    job_id = str(row.get("id") or source_job_id(source, job_url))
    apply_url = str(row.get("applyUrl") or row.get("apply_url") or job_url).strip()
    salary_min = row.get("salaryMin") if row.get("salaryMin") is not None else row.get("salary_min")
    salary_max = row.get("salaryMax") if row.get("salaryMax") is not None else row.get("salary_max")
    date_posted = row.get("datePosted") if row.get("datePosted") is not None else row.get("date_posted")
    if isinstance(date_posted, str):
        try:
            date_posted = datetime.fromisoformat(date_posted.replace("Z", "+00:00"))
        except ValueError:
            date_posted = None

    salary_bits: list[str] = []
    if salary_min is not None:
        salary_bits.append(str(salary_min))
    if salary_max is not None:
        salary_bits.append(str(salary_max))
    currency = str(row.get("currency") or "").strip()
    salary = " - ".join(salary_bits)
    if currency and salary:
        salary = f"{salary} {currency}"

    return UnifiedJob(
        job_id=job_id,
        source=source,
        title=str(row.get("title") or "Untitled role").strip(),
        company=str(row.get("company") or "").strip(),
        location=str(row.get("location") or "").strip(),
        experience="",
        salary=salary,
        skills="",
        description=str(row.get("description") or "").strip(),
        job_url=job_url,
        posted_label="",
        search_keyword=search_keyword,
        experience_filter=0,
        role_family=role_family,
        role_family_name=role_family_name,
        job_type=str(row.get("jobType") or row.get("job_type") or "").strip(),
        date_posted=date_posted if isinstance(date_posted, datetime) else None,
        salary_min=float(salary_min) if salary_min is not None else None,
        salary_max=float(salary_max) if salary_max is not None else None,
        currency=currency,
        apply_url=apply_url,
        codequest_id=job_id,
        ingest_profile=role_family or "fresher_india",
    )
