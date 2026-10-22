"""JSONL-serializable route evidence records (no catalog writes)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from application_route_validation.contracts import APPLICATION_ROUTE_VALIDATOR_VERSION
from application_route_validation.subjects import SUBJECT_APPLICATION_ROUTE, SUBJECT_LISTING, SUBJECT_REQUISITION
from link_policy import sanitize_evidence_url


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class SubjectEvidenceRecord:
    job_id: str
    subject_type: str
    subject_id: str
    route_id: str
    provider: str
    adapter_version: str
    observed_at: datetime
    outcome: str
    reason: str
    expected_identity: str | None = None
    observed_identities: tuple[str, ...] | None = None
    identity_link_type: str | None = None
    requested_url: str | None = None
    final_url: str | None = None
    http_status: int | None = None
    evidence_ref: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        payload = asdict(self)
        payload["observed_at"] = self.observed_at.isoformat()
        payload["requested_url"] = sanitize_evidence_url(self.requested_url)
        payload["final_url"] = sanitize_evidence_url(self.final_url)
        return json.dumps(payload, sort_keys=True)


@dataclass(frozen=True, slots=True)
class ShadowJobSummary:
    job_id: str
    source: str
    listing_outcome: str
    requisition_outcome: str
    application_route_outcome: str
    route_ready: str
    route_ready_reason: str
    browser_attempted: bool
    browser_skip_reason: str
    listing_http_reason: str
    elapsed_ms: int
    records: tuple[SubjectEvidenceRecord, ...]


def subject_id_for_listing(source: str, expected: str) -> str:
    return f"{source}:{expected}"


def subject_id_for_route(host: str, path_hint: str) -> str:
    return f"{host}:{path_hint}"
