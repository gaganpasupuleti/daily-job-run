"""When browser verification is allowed (not on every HTTP inconclusive)."""

from __future__ import annotations

from urllib.parse import urlsplit

from application_route_validation.identity import greenhouse_post_from_apply_url
from link_policy import CONFIRMED_ACTIVE, INCONCLUSIVE

# Configuration gaps: browser cannot fix these.
_CONFIG_SKIP_REASONS = frozenset(
    {
        "indeed_missing_api_key",
        "greenhouse_auth_missing",
    }
)

# Hard stops: retrying in a browser is not permitted / not useful.
_ACCESS_BLOCK_REASONS = frozenset(
    {
        "linkedin_captcha",
        "linkedin_authwall",
        "linkedin_bot_block",
        "linkedin_rate_limited",
    }
)

_NAUKRI_RENDER_EXPERIMENT_REASONS = frozenset({"naukri_empty_detail"})


def _normalized_urls(job_url: str, apply_url: str | None) -> tuple[str, str]:
    job = (job_url or "").strip()
    apply = (apply_url or job).strip()
    return job, apply


def _same_listing_route(job_url: str, apply_url: str) -> bool:
    try:
        a = urlsplit(job_url)
        b = urlsplit(apply_url)
    except ValueError:
        return False
    return a.scheme == b.scheme and a.netloc == b.netloc and a.path.rstrip("/") == b.path.rstrip("/")


def should_verify_application_route(
    *,
    source: str,
    job_url: str,
    apply_url: str | None,
    listing_observation: str,
    listing_reason: str,
) -> tuple[bool, str]:
    """Return whether Playwright may run on apply_url for additional route evidence."""
    job_url, apply = _normalized_urls(job_url, apply_url)
    if not apply:
        return False, "no_apply_url"

    if listing_reason in _CONFIG_SKIP_REASONS:
        # External employer routes may still be verified (e.g. Indeed listing + Greenhouse apply).
        if greenhouse_post_from_apply_url(apply) is None and _same_listing_route(job_url, apply):
            return False, "listing_config_skip"

    if listing_reason in _ACCESS_BLOCK_REASONS:
        return False, "listing_access_block"

    if greenhouse_post_from_apply_url(apply) is not None:
        return True, "greenhouse_board_route"

    if not _same_listing_route(job_url, apply):
        return True, "external_application_host"

    if source.strip().lower() == "naukri" and listing_reason in _NAUKRI_RENDER_EXPERIMENT_REASONS:
        return True, "naukri_rendered_page_experiment"

    if listing_observation == CONFIRMED_ACTIVE and not _same_listing_route(job_url, apply):
        return True, "verify_external_apply_after_open_listing"

    if listing_observation == INCONCLUSIVE and source.strip().lower() == "linkedin":
        return False, "linkedin_browser_not_enabled"

    return False, "browser_not_needed"
