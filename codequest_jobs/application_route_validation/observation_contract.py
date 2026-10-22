"""Versioned immutable route-evidence observations (separate from lifecycle ledger)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from application_route_validation.subjects import SUBJECT_APPLICATION_ROUTE, SUBJECT_LISTING, SUBJECT_REQUISITION
from link_policy import sanitize_evidence_url

ROUTE_EVIDENCE_CONTRACT_VERSION = "jobready-route-evidence-v1"
ROUTE_POLICY_VERSION = "jobready-route-policy-v1"

CONFIRMED_OPEN = "confirmed_open"
CONFIRMED_CLOSED = "confirmed_closed"
UNRESOLVED = "unresolved"

VALID_SUBJECTS = frozenset({SUBJECT_LISTING, SUBJECT_REQUISITION, SUBJECT_APPLICATION_ROUTE})
VALID_OUTCOMES = frozenset({CONFIRMED_OPEN, CONFIRMED_CLOSED, UNRESOLVED})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_evidence_ref() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True, slots=True)
class IdentityNamespace:
    namespace: str
    value: str

    def encoded(self) -> str:
        return f"{self.namespace}:{self.value}"


@dataclass(frozen=True, slots=True)
class IdentityLinkEvidence:
    link_type: str
    detail: str


@dataclass(frozen=True, slots=True)
class RouteObservation:
    evidence_ref: str
    run_id: str
    job_id: str
    subject_type: str
    subject_id: str
    route_id: str
    provider: str
    adapter_version: str
    policy_version: str
    observed_at: datetime
    outcome: str
    reason: str
    expected_identity: IdentityNamespace | None = None
    observed_identities: tuple[IdentityNamespace, ...] = ()
    identity_link: IdentityLinkEvidence | None = None
    destination_self_match: bool | None = None
    catalog_link_assured: bool | None = None
    requested_url: str | None = None
    final_url: str | None = None
    http_status: int | None = None
    supporting_refs: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.subject_type not in VALID_SUBJECTS:
            raise ValueError("invalid subject_type")
        if self.outcome not in VALID_OUTCOMES:
            raise ValueError("invalid outcome")
        if not self.evidence_ref:
            raise ValueError("evidence_ref required")
        if self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": ROUTE_EVIDENCE_CONTRACT_VERSION,
            "evidence_ref": self.evidence_ref,
            "run_id": self.run_id,
            "job_id": self.job_id,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "route_id": self.route_id,
            "provider": self.provider,
            "adapter_version": self.adapter_version,
            "policy_version": self.policy_version,
            "observed_at": self.observed_at.isoformat(),
            "outcome": self.outcome,
            "reason": self.reason,
            "expected_identity": self.expected_identity.encoded() if self.expected_identity else None,
            "observed_identities": [item.encoded() for item in self.observed_identities],
            "identity_link": (
                {"link_type": self.identity_link.link_type, "detail": self.identity_link.detail}
                if self.identity_link
                else None
            ),
            "destination_self_match": self.destination_self_match,
            "catalog_link_assured": self.catalog_link_assured,
            "requested_url": sanitize_evidence_url(self.requested_url),
            "final_url": sanitize_evidence_url(self.final_url),
            "http_status": self.http_status,
            "supporting_refs": list(self.supporting_refs),
            "extra": dict(self.extra),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)
