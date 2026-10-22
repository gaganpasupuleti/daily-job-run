"""Shadow validation: lifecycle HTTP probes + selective browser route checks. No DB writes."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from application_route_validation.adapters.greenhouse_board import (
    ADAPTER_VERSION as GH_ADAPTER_VERSION,
    observe_greenhouse_board_playwright,
)
from application_route_validation.adapters.naukri_listing_page import (
    ADAPTER_VERSION as NAUKRI_ADAPTER_VERSION,
    observe_naukri_listing_playwright,
)
from application_route_validation.browser_safety import resolve_public_host
from application_route_validation.contracts import CONFIRMED_CLOSED, CONFIRMED_OPEN, UNRESOLVED, RouteDimension
from application_route_validation.adapters.greenhouse_board import expected_post_id
from application_route_validation.decision import dimension_from_lifecycle
from application_route_validation.identity_assurance import assurance_for_greenhouse_route
from application_route_validation.subject_decision import decide_subjects
from application_route_validation.evidence_records import (
    ShadowJobSummary,
    SubjectEvidenceRecord,
    subject_id_for_listing,
    subject_id_for_route,
)
from application_route_validation.identity import greenhouse_post_from_apply_url, link_board_to_greenhouse, listing_identity
from application_route_validation.route_policy import should_verify_application_route
from application_route_validation.subjects import SUBJECT_APPLICATION_ROUTE, SUBJECT_LISTING
from jobready_validator import probe_for, probe_result
from link_policy import CONFIRMED_ACTIVE, CONFIRMED_EXPIRED, normalize_source


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _listing_dimension(probe) -> RouteDimension:
    identity_match: bool | None = None
    if probe.expected_identity and probe.observed_identities is not None:
        identity_match = probe.expected_identity in probe.observed_identities
    return dimension_from_lifecycle(probe.observation, probe.reason, identity_match)


def _outcome_from_observation(observation: str) -> str:
    if observation == CONFIRMED_ACTIVE:
        return CONFIRMED_OPEN
    if observation == CONFIRMED_EXPIRED:
        return CONFIRMED_CLOSED
    return UNRESOLVED


def validate_job_shadow(
    row: dict[str, Any],
    *,
    browser_page: Any | None = None,
    enable_browser: bool = True,
    browser_timeout_ms: int = 15000,
) -> ShadowJobSummary:
    """Run listing HTTP probe and optional browser route verification. Never writes catalog."""
    started = time.perf_counter()
    source = normalize_source(row.get("source"))
    job_url = (row.get("job_url") or "").strip()
    apply_url = (row.get("apply_url") or job_url).strip()
    job_id = row["job_id"]

    probe_fn = probe_for(source)
    if probe_fn is None:
        listing = RouteDimension(UNRESOLVED, "unsupported_source", None)
        elapsed = int((time.perf_counter() - started) * 1000)
        return ShadowJobSummary(
            job_id=job_id,
            source=source,
            listing_outcome=listing.status,
            requisition_outcome=UNRESOLVED,
            application_route_outcome=UNRESOLVED,
            route_ready=UNRESOLVED,
            route_ready_reason="unsupported_source",
            browser_attempted=False,
            browser_skip_reason="unsupported_source",
            listing_http_reason="unsupported_source",
            elapsed_ms=elapsed,
            records=(),
        )

    probe = probe_result(probe_fn(row))
    listing = _listing_dimension(probe)
    requisition = RouteDimension(UNRESOLVED, "not_independently_verified", None)
    application_route = RouteDimension(UNRESOLVED, "not_collected", None)

    records: list[SubjectEvidenceRecord] = []
    listing_ref = listing_identity(source, job_url)
    expected = probe.expected_identity or (listing_ref.value if listing_ref else "")
    route_id = subject_id_for_listing(source, expected or "unknown")
    records.append(
        SubjectEvidenceRecord(
            job_id=job_id,
            subject_type=SUBJECT_LISTING,
            subject_id=route_id,
            route_id=route_id,
            provider=probe.provider,
            adapter_version="jobready-validator-v2",
            observed_at=_utc_now(),
            outcome=_outcome_from_observation(probe.observation),
            reason=probe.reason,
            expected_identity=probe.expected_identity,
            observed_identities=probe.observed_identities,
            requested_url=probe.requested_url,
            final_url=probe.final_url,
            http_status=probe.http_status,
            evidence_ref=str(uuid.uuid4()),
        )
    )

    should_browser, browser_trigger = should_verify_application_route(
        source=source,
        job_url=job_url,
        apply_url=apply_url,
        listing_observation=probe.observation,
        listing_reason=probe.reason,
    )
    browser_attempted = False
    browser_skip_reason = browser_trigger if not should_browser else "pending"

    if enable_browser and should_browser and browser_page is not None:
        ok, safety_reason = resolve_public_host(apply_url)
        if not ok:
            browser_skip_reason = safety_reason
        else:
            gh = greenhouse_post_from_apply_url(apply_url)
            if gh is not None:
                browser_attempted = True
                browser_skip_reason = "ran"
                obs = observe_greenhouse_board_playwright(browser_page, apply_url, browser_timeout_ms)
                if obs.application_route:
                    application_route = obs.application_route
                link = link_board_to_greenhouse(listing_ref, gh, apply_url)
                records.append(
                    SubjectEvidenceRecord(
                        job_id=job_id,
                        subject_type=SUBJECT_APPLICATION_ROUTE,
                        subject_id=f"greenhouse:{gh.value}",
                        route_id=subject_id_for_route(urlsplit(apply_url).hostname or "greenhouse", gh.value),
                        provider="greenhouse_board",
                        adapter_version=GH_ADAPTER_VERSION,
                        observed_at=obs.observed_at,
                        outcome=application_route.status,
                        reason=obs.reason,
                        expected_identity=obs.expected_identity,
                        observed_identities=obs.observed_identities,
                        identity_link_type=link.link_type if link else None,
                        final_url=obs.final_url,
                        http_status=obs.extra.get("http_status"),
                        evidence_ref=str(uuid.uuid4()),
                    )
                )
            elif browser_trigger == "naukri_rendered_page_experiment" and source == "naukri":
                ok_job, _ = resolve_public_host(job_url)
                if ok_job:
                    browser_attempted = True
                    browser_skip_reason = "ran"
                    obs = observe_naukri_listing_playwright(browser_page, job_url, expected, browser_timeout_ms)
                    if obs.listing:
                        listing = obs.listing
                    records.append(
                        SubjectEvidenceRecord(
                            job_id=job_id,
                            subject_type=SUBJECT_LISTING,
                            subject_id=route_id,
                            route_id=route_id,
                            provider="naukri",
                            adapter_version=NAUKRI_ADAPTER_VERSION,
                            observed_at=obs.observed_at,
                            outcome=listing.status,
                            reason=obs.reason,
                            expected_identity=expected,
                            observed_identities=obs.observed_identities,
                            final_url=obs.final_url,
                            http_status=obs.extra.get("http_status"),
                            evidence_ref=str(uuid.uuid4()),
                        )
                    )
            else:
                browser_skip_reason = "no_adapter_for_host"
    elif not enable_browser:
        browser_skip_reason = "browser_disabled"

    if application_route.status == UNRESOLVED and probe.observation == CONFIRMED_ACTIVE:
        application_route = listing
        requisition = listing

    catalog_link_assured: bool | None = None
    if application_route.status != UNRESOLVED and greenhouse_post_from_apply_url(apply_url):
        post_id, _ = expected_post_id(apply_url)
        observed = application_route.identity_match
        assurance = assurance_for_greenhouse_route(
            source=source,
            job_url=job_url,
            apply_url=apply_url,
            expected_post_id=post_id or "",
            observed_post_id=post_id if observed else None,
        )
        catalog_link_assured = assurance.catalog_link_assured

    subject = decide_subjects(
        listing=listing,
        requisition=requisition,
        application_route=application_route,
        catalog_link_assured=catalog_link_assured,
    )
    route_ready, route_reason = subject.route_ready, subject.route_ready_reason

    elapsed = int((time.perf_counter() - started) * 1000)
    return ShadowJobSummary(
        job_id=job_id,
        source=source,
        listing_outcome=listing.status,
        requisition_outcome=requisition.status,
        application_route_outcome=application_route.status,
        route_ready=route_ready,
        route_ready_reason=route_reason,
        browser_attempted=browser_attempted,
        browser_skip_reason=browser_skip_reason,
        listing_http_reason=probe.reason,
        elapsed_ms=elapsed,
        records=tuple(records),
    )
