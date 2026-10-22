"""Shadow decision vocabulary and structured observations for application-route checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Distinct from catalog link_status and from lifecycle_projection labels only by naming parity.
CONFIRMED_OPEN = "confirmed_open"
CONFIRMED_CLOSED = "confirmed_closed"
UNRESOLVED = "unresolved"

APPLICATION_ROUTE_VALIDATOR_VERSION = "application-route-validator-prototype-v1"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class JobValidationTarget:
    """Minimal input for a single validation attempt (no catalog row required)."""

    expected_job_id: str
    source: str
    listing_url: str
    application_url: str | None = None
    job_id: str = "prototype"


@dataclass(frozen=True, slots=True)
class RouteDimension:
    """One axis: listing board, employer requisition, or application route."""

    status: str
    reason: str
    identity_match: bool | None = None


@dataclass(frozen=True, slots=True)
class RouteValidationObservation:
    """Audit-friendly slice of evidence (no page bodies or secrets)."""

    channel: str
    reason: str
    final_url: str | None = None
    expected_identity: str | None = None
    observed_identities: tuple[str, ...] | None = None
    listing: RouteDimension | None = None
    requisition: RouteDimension | None = None
    application_route: RouteDimension | None = None
    observed_at: datetime = field(default_factory=_utc_now)
    validator_version: str = APPLICATION_ROUTE_VALIDATOR_VERSION
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ApplicationRouteValidationResult:
    decision: str
    reason: str
    listing: RouteDimension
    requisition: RouteDimension
    application_route: RouteDimension
    observations: tuple[RouteValidationObservation, ...]
    elapsed_ms: int
    validator_version: str = APPLICATION_ROUTE_VALIDATOR_VERSION
