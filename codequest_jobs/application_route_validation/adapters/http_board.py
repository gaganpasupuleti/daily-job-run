"""HTTP lifecycle interpretation reused from jobready_validator (listing/board plane)."""

from __future__ import annotations

import json
from typing import Any

from application_route_validation.contracts import RouteValidationObservation
from application_route_validation.decision import dimension_from_lifecycle
from jobready_validator import interpret_linkedin_evidence, interpret_naukri_evidence
from link_policy import INCONCLUSIVE


def observe_listing_http(
    *,
    source: str,
    expected_job_id: str,
    status_code: int | None,
    body: str | None,
    payload: Any,
    final_url: str | None,
    error: str | None = None,
) -> RouteValidationObservation:
    src = source.strip().lower()
    if src == "linkedin":
        observation, reason, identities = interpret_linkedin_evidence(
            status_code=status_code,
            body=body,
            expected_job_id=expected_job_id,
            final_url=final_url,
            error=error,
        )
        identity_match = None
        if identities is not None:
            identity_match = expected_job_id in identities and len(identities) == 1
        listing = dimension_from_lifecycle(observation, reason, identity_match)
        return RouteValidationObservation(
            channel="http_linkedin_listing",
            reason=reason,
            final_url=final_url,
            expected_identity=expected_job_id,
            observed_identities=tuple(sorted(identities)) if identities else None,
            listing=listing,
        )
    if src == "naukri":
        if payload is None and body:
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = None
        observation, reason, identities = interpret_naukri_evidence(
            status_code=status_code,
            payload=payload,
            expected_job_id=expected_job_id,
            final_url=final_url,
            error=error,
        )
        identity_match = None
        if identities is not None:
            identity_match = identities == frozenset({expected_job_id})
        listing = dimension_from_lifecycle(observation, reason, identity_match)
        return RouteValidationObservation(
            channel="http_naukri_api",
            reason=reason,
            final_url=final_url,
            expected_identity=expected_job_id,
            observed_identities=tuple(sorted(identities)) if identities else None,
            listing=listing,
        )
    return RouteValidationObservation(
        channel="http_unsupported",
        reason="unsupported_source",
        expected_identity=expected_job_id,
        listing=dimension_from_lifecycle(INCONCLUSIVE, "unsupported_source", None),
    )
