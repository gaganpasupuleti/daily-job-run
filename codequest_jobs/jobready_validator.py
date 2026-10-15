"""Validate JobReady catalog rows. This module does not read CodeQuest.

Observations are confirmed_active, confirmed_expired, or inconclusive.
A conclusive observation needs provider evidence bound to the requested job:
the response must come from that job's provider endpoint, carry the same job
identity, and state its status. Anything weaker is inconclusive. Generic HTTP
failure is inconclusive. A missing Apply action is not closure.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import httpx

from link_policy import (
    CONFIRMED_ACTIVE,
    CONFIRMED_EXPIRED,
    INCONCLUSIVE,
    UNSUPPORTED_SOURCE,
    evidence_identities,
    evidence_identity,
    normalize_source,
    sanitize_evidence_url,
    source_label,
    valid_http_status,
)
from linkedin_checker import CLOSED_MARKERS, GUEST_URL, HEADERS, linkedin_job_id_from_url

LINKEDIN_IDENTITY_RE = re.compile(
    r'id="decoratedJobPostingId"[^>]*>\s*<!--\s*"?(\d+)"?\s*-->',
    re.IGNORECASE,
)
LINKEDIN_BLOCK_MARKERS = (
    "captcha",
    "authwall",
    "/checkpoint/challenge",
    "security verification",
    "quick security check",
)
LINKEDIN_SOFT_404_MARKERS = ("page not found", "job not found")
LINKEDIN_TOP_CARD_CLASS = "top-card-layout"
LINKEDIN_APPLY_CLASSES = frozenset({"top-card-layout__cta--primary", "apply-button"})
LINKEDIN_APPLY_MODALS = frozenset({"job-details-topcard-apply-modal"})
LINKEDIN_CLOSED_CLASS = "closed-job__flavor--closed"
LINKEDIN_SIGN_IN_PATHS = ("/login", "/signup", "/authwall", "/checkpoint", "/uas/login")
APPLY_TEXT_RE = re.compile(r"\bapply\b", re.IGNORECASE)
NON_RENDERED_RE = re.compile(
    r"<(script|style|template|noscript)\b.*?</\1\s*>|<!--.*?-->",
    re.IGNORECASE | re.DOTALL,
)
NON_RENDERED_TAGS = frozenset({"script", "style", "template", "noscript"})
VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)
HIDDEN_STYLE_RE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)

NAUKRI_DETAIL_URL = "https://www.naukri.com/jobapi/v1/job/{job_id}"
NAUKRI_ID_KEYS = ("jobId", "jobid", "id")
NAUKRI_STATUS_KEYS = ("jobStatus", "status", "applyStatus")
NAUKRI_FLAG_KEYS = ("isExpired", "expired", "isClosed")
NAUKRI_CLOSED_STATUS = {"expired", "closed", "withdrawn", "inactive"}
NAUKRI_OPEN_STATUS = {"active", "open", "live"}

# Names the evidence contract that produced an observation: what a verdict means and what
# each recorded evidence field means. Change it only when one of those changes, never per
# run or per deploy. v2 adds expected_identity and observed_identities; v1 rows predate them.
JOBREADY_EVIDENCE_VERSION = "jobready-validator-v2"


# httpx logs every request URL at INFO, query string and userinfo included, and httpcore
# logs connection detail at DEBUG. Provider redirects can put tokens in those URLs.
TRANSPORT_LOGGERS = ("httpx", "httpcore")


def quiet_transport_logging() -> None:
    """Keep provider transport loggers at WARNING or above, whatever the process configured.

    Applied at import and again before every provider request, so a later INFO
    configuration cannot re-enable raw URL logging. Other loggers are untouched.
    """
    for name in TRANSPORT_LOGGERS:
        transport = logging.getLogger(name)
        if transport.getEffectiveLevel() < logging.WARNING:
            transport.setLevel(logging.WARNING)


quiet_transport_logging()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class LinkObservation:
    job_id: str
    previous_status: str
    observation: str
    provider: str
    reason: str
    # When the provider evidence was observed, not when it is written.
    observed_at: datetime = field(default_factory=_utc_now)
    evidence_version: str = JOBREADY_EVIDENCE_VERSION
    # HTTP provenance: sanitized URLs and the response status, or None when unknown.
    requested_url: str | None = None
    final_url: str | None = None
    http_status: int | None = None
    # Requisition identity evidence in stored form (link_policy.evidence_identity/ies):
    # the id the probe asked for, and the ids the provider response exposed.
    expected_identity: str | None = None
    observed_identities: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ProbeResult:
    """One provider probe. URLs and identities are sanitized on construction, so raw response values never leave it.

    Provenance is set only for a request that was actually made; otherwise it stays None.
    expected_identity is the provider id derived before the probe, or None when none could be.
    observed_identities is None when no response identity was inspected, empty when a usable
    response was inspected and exposed none, and otherwise the ids that response exposed.
    """

    observation: str
    reason: str
    provider: str
    requested_url: str | None = None
    final_url: str | None = None
    http_status: int | None = None
    expected_identity: str | None = None
    observed_identities: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "requested_url", sanitize_evidence_url(self.requested_url))
        object.__setattr__(self, "final_url", sanitize_evidence_url(self.final_url))
        if self.http_status is not None and not valid_http_status(self.http_status):
            raise ValueError("http_status must be an integer HTTP status code")
        object.__setattr__(self, "expected_identity", evidence_identity(self.expected_identity))
        object.__setattr__(self, "observed_identities", evidence_identities(self.observed_identities))


Probe = Callable[[dict[str, Any]], "ProbeResult | tuple[str, str, str]"]


def probe_result(value: ProbeResult | tuple[str, str, str]) -> ProbeResult:
    """Legacy probes return (observation, reason, provider) and carry no HTTP provenance or identity evidence."""
    if isinstance(value, ProbeResult):
        return value
    observation, reason, provider = value
    return ProbeResult(observation, reason, provider)


def interpret_indeed(*, expired: Any, key_configured: bool) -> tuple[str, str]:
    """Only a real boolean from the Indeed API is evidence."""
    if not key_configured:
        return INCONCLUSIVE, "indeed_missing_api_key"
    if expired is True:
        return CONFIRMED_EXPIRED, "indeed_expired_true"
    if expired is False:
        return CONFIRMED_ACTIVE, "indeed_expired_false"
    return INCONCLUSIVE, "indeed_api_error"


def _is_provider_endpoint(final_url: str | None, expected_url: str) -> bool:
    """Same https origin and exact path as the expected job endpoint. The query may differ."""
    if not final_url:
        return False
    try:
        final = urlsplit(final_url)
        expected = urlsplit(expected_url)
        port = final.port
    except ValueError:
        return False
    return (
        final.scheme == "https"
        and expected.scheme == "https"
        and port in (None, 443)
        and final.username is None
        and final.password is None
        and final.hostname is not None
        and final.hostname == expected.hostname
        and final.path.rstrip("/") == expected.path.rstrip("/")
    )


@dataclass
class _Action:
    tag: str
    attrs: dict[str, str | None]
    inherited_disabled: bool
    text: list[str] = field(default_factory=list)
    closed: bool = False


@dataclass
class _Node:
    tag: str
    hidden: bool
    in_card: bool
    root: bool
    disabled: bool
    action: _Action | None


def _hidden_attributes(attrs: dict[str, str | None]) -> bool:
    if "hidden" in attrs:
        return True
    if (attrs.get("aria-hidden") or "").strip().lower() == "true":
        return True
    return bool(HIDDEN_STYLE_RE.search(attrs.get("style") or ""))


def _aria_disabled(attrs: dict[str, str | None]) -> bool:
    return (attrs.get("aria-disabled") or "").strip().lower() == "true"


def _disables_descendants(tag: str, attrs: dict[str, str | None]) -> bool:
    """A disabled fieldset or an aria-disabled element disables everything inside it."""
    return (tag == "fieldset" and "disabled" in attrs) or _aria_disabled(attrs)


class _TopCardParser(HTMLParser):
    """Collect visible text and actions inside the LinkedIn top-card element.

    Hidden subtrees (template, script, style, noscript, hidden, aria-hidden,
    display:none, visibility:hidden) contribute nothing. The top card counts
    only if exactly one visible top-card element exists and its own end tag
    closes it. Likewise an action is closed only by its own end tag, and it
    inherits the disabled state of any disabled ancestor.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[_Node] = []
        self.roots = 0
        self.terminated = 0
        self.overrun = False
        self.closed_class = False
        self.text: list[str] = []
        self.actions: list[_Action] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        parent = self.stack[-1] if self.stack else None
        hidden = (parent is not None and parent.hidden) or tag in NON_RENDERED_TAGS or _hidden_attributes(attributes)
        classes = set((attributes.get("class") or "").split())
        root = not hidden and LINKEDIN_TOP_CARD_CLASS in classes
        if root:
            self.roots += 1
        in_card = (parent is not None and parent.in_card) or root
        inherited_disabled = parent is not None and parent.disabled
        disabled = inherited_disabled or _disables_descendants(tag, attributes)
        action = None
        if in_card and not hidden:
            self.closed_class = self.closed_class or LINKEDIN_CLOSED_CLASS in classes
            if tag in ("a", "button"):
                action = _Action(tag, attributes, inherited_disabled)
                self.actions.append(action)
        if tag not in VOID_TAGS:
            self.stack.append(_Node(tag, hidden, in_card, root, disabled, action))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == tag:
                break
        else:
            return
        if self.stack[index].action is not None:
            self.stack[index].action.closed = True
        if self.stack[index].root:
            self.terminated += 1
        if any(node.root for node in self.stack[index + 1 :]):
            self.overrun = True
        del self.stack[index:]

    def handle_data(self, data: str) -> None:
        node = self.stack[-1] if self.stack else None
        if node is None or node.hidden or not node.in_card:
            return
        self.text.append(data)
        for open_node in reversed(self.stack):
            if open_node.action is not None:
                open_node.action.text.append(data)
                break

    @property
    def confident(self) -> bool:
        return self.roots == 1 and self.terminated == 1 and not self.overrun


def _actionable_href(href: str | None, page_url: str) -> bool:
    """A real http(s) destination other than this page or a LinkedIn sign-in wall."""
    raw = "".join((href or "").split())
    if not raw or raw.startswith("#"):
        return False
    try:
        target = urlsplit(urljoin(page_url, raw))
    except ValueError:
        return False
    page = urlsplit(page_url)
    if target.scheme not in ("http", "https") or not target.hostname:
        return False
    if (target.hostname, target.path.rstrip("/")) == (page.hostname, page.path.rstrip("/")):
        return False
    if target.hostname.endswith("linkedin.com") and target.path.startswith(LINKEDIN_SIGN_IN_PATHS):
        return False
    return True


def _apply_action_state(action: _Action, page_url: str) -> str | None:
    """Return malformed, enabled, disabled, or nonfunctional for an Apply-looking action, else None."""
    classes = set((action.attrs.get("class") or "").split())
    labelled = bool(APPLY_TEXT_RE.search(" ".join(action.text)))
    recognized = bool(classes & LINKEDIN_APPLY_CLASSES)
    if not labelled and not recognized:
        return None
    if not action.closed:
        return "malformed"
    if action.inherited_disabled or "disabled" in action.attrs or _aria_disabled(action.attrs):
        return "disabled"
    if not (labelled and recognized):
        return "nonfunctional"
    if action.tag == "button":
        return "enabled" if action.attrs.get("data-modal") in LINKEDIN_APPLY_MODALS else "nonfunctional"
    return "enabled" if _actionable_href(action.attrs.get("href"), page_url) else "nonfunctional"


def _linkedin_apply_state(actions: list[_Action], page_url: str) -> str:
    """Return malformed, enabled, disabled, nonfunctional, or absent for the top-card Apply actions."""
    states = {_apply_action_state(action, page_url) for action in actions}
    for state in ("malformed", "enabled", "disabled", "nonfunctional"):
        if state in states:
            return state
    return "absent"


def interpret_linkedin(
    *,
    status_code: int | None,
    body: str | None,
    expected_job_id: str,
    final_url: str | None,
    error: str | None = None,
) -> tuple[str, str]:
    """Interpret the LinkedIn guest job endpoint for one requested job id."""
    observation, reason, _identities = interpret_linkedin_evidence(
        status_code=status_code, body=body, expected_job_id=expected_job_id, final_url=final_url, error=error
    )
    return observation, reason


def interpret_linkedin_evidence(
    *,
    status_code: int | None,
    body: str | None,
    expected_job_id: str,
    final_url: str | None,
    error: str | None = None,
) -> tuple[str, str, frozenset[str] | None]:
    """interpret_linkedin plus the page identities it read.

    The identities are None unless a 200 page from the guest endpoint was inspected;
    a status-only verdict such as 404/410 reads no page identity.
    """
    if error or status_code is None:
        return INCONCLUSIVE, "linkedin_connection_failure", None
    endpoint = GUEST_URL.format(job_id=expected_job_id)
    if not _is_provider_endpoint(final_url, endpoint):
        return INCONCLUSIVE, "linkedin_redirected", None
    if status_code in (401, 403, 408, 429, 999) or status_code >= 500:
        return INCONCLUSIVE, f"linkedin_http_{status_code}", None
    if status_code in (404, 410):
        return CONFIRMED_EXPIRED, "linkedin_guest_gone", None
    if status_code != 200:
        return INCONCLUSIVE, f"linkedin_http_{status_code}", None
    html = body or ""
    identities = frozenset(LINKEDIN_IDENTITY_RE.findall(html))
    rendered = NON_RENDERED_RE.sub(" ", html)
    visible = rendered.lower()
    if any(marker in visible for marker in LINKEDIN_BLOCK_MARKERS):
        return INCONCLUSIVE, "linkedin_blocked", identities
    if any(marker in visible for marker in LINKEDIN_SOFT_404_MARKERS):
        return INCONCLUSIVE, "linkedin_soft_404", identities
    if not identities:
        return INCONCLUSIVE, "linkedin_identity_missing", identities
    if identities != {expected_job_id}:
        return INCONCLUSIVE, "linkedin_identity_mismatch", identities
    card = _TopCardParser()
    card.feed(html)
    card.close()
    if card.roots == 0:
        return INCONCLUSIVE, "linkedin_no_status_region", identities
    if not card.confident:
        return INCONCLUSIVE, "linkedin_top_card_ambiguous", identities
    card_text = " ".join(" ".join(card.text).split()).lower()
    closed = card.closed_class or any(marker in card_text for marker in CLOSED_MARKERS)
    apply_state = _linkedin_apply_state(card.actions, endpoint)
    if closed and apply_state == "enabled":
        return INCONCLUSIVE, "linkedin_conflict", identities
    if closed:
        return CONFIRMED_EXPIRED, "linkedin_closed_marker", identities
    if apply_state == "malformed":
        return INCONCLUSIVE, "linkedin_apply_malformed", identities
    if apply_state == "enabled":
        return CONFIRMED_ACTIVE, "linkedin_identity_apply", identities
    if apply_state == "disabled":
        return INCONCLUSIVE, "linkedin_apply_disabled", identities
    if apply_state == "nonfunctional":
        return INCONCLUSIVE, "linkedin_apply_nonfunctional", identities
    return INCONCLUSIVE, "linkedin_no_apply_evidence", identities


def _naukri_flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    return None


def _naukri_status_signals(job: dict[str, Any]) -> tuple[bool, bool]:
    """Return (closed, open) from explicit provider status fields only."""
    closed = opened = False
    for key in NAUKRI_STATUS_KEYS:
        value = job.get(key)
        text = value.strip().lower() if isinstance(value, str) else ""
        closed = closed or text in NAUKRI_CLOSED_STATUS
        opened = opened or text in NAUKRI_OPEN_STATUS
    for key in NAUKRI_FLAG_KEYS:
        flag = _naukri_flag(job.get(key))
        closed = closed or flag is True
        opened = opened or flag is False
    return closed, opened


def interpret_naukri(
    *,
    status_code: int | None,
    payload: Any,
    expected_job_id: str,
    final_url: str | None,
    error: str | None = None,
) -> tuple[str, str]:
    """A Naukri verdict needs the same job id and an explicit status field."""
    observation, reason, _identities = interpret_naukri_evidence(
        status_code=status_code, payload=payload, expected_job_id=expected_job_id, final_url=final_url, error=error
    )
    return observation, reason


def interpret_naukri_evidence(
    *,
    status_code: int | None,
    payload: Any,
    expected_job_id: str,
    final_url: str | None,
    error: str | None = None,
) -> tuple[str, str, frozenset[str] | None]:
    """interpret_naukri plus the job-detail identities it read.

    The identities are None unless a job-detail object from the detail endpoint was
    inspected; an empty detail object reads as no identities.
    """
    if error or status_code is None:
        return INCONCLUSIVE, "naukri_connection_failure", None
    if status_code != 200:
        return INCONCLUSIVE, f"naukri_http_{status_code}", None
    if not _is_provider_endpoint(final_url, NAUKRI_DETAIL_URL.format(job_id=expected_job_id)):
        return INCONCLUSIVE, "naukri_redirected", None
    if not isinstance(payload, dict):
        return INCONCLUSIVE, "naukri_malformed", None
    job = payload.get("job")
    if not isinstance(job, dict):
        job = payload.get("jobDetails")
    if not isinstance(job, dict):
        return INCONCLUSIVE, "naukri_empty_detail", None
    identities = frozenset(str(job[key]).strip() for key in NAUKRI_ID_KEYS if job.get(key) not in (None, ""))
    if not job:
        return INCONCLUSIVE, "naukri_empty_detail", identities
    if not identities:
        return INCONCLUSIVE, "naukri_identity_missing", identities
    if identities != {expected_job_id}:
        return INCONCLUSIVE, "naukri_identity_mismatch", identities
    closed, opened = _naukri_status_signals(job)
    if closed and opened:
        return INCONCLUSIVE, "naukri_conflict", identities
    if closed:
        return CONFIRMED_EXPIRED, "naukri_explicit_closed", identities
    if opened:
        return CONFIRMED_ACTIVE, "naukri_explicit_open", identities
    return INCONCLUSIVE, "naukri_no_status_evidence", identities


class UnsupportedSourceError(ValueError):
    """Rows on sources without a lifecycle probe were passed for validation. Nothing was probed."""

    def __init__(self, sources: list[str]) -> None:
        super().__init__(UNSUPPORTED_SOURCE)
        self.sources = sources


def probe_for(source: Any, probes: dict[str, Probe] | None = None) -> Probe | None:
    """The probe for a source, or None when the source has no lifecycle probe.

    Production uses default_probes(). Injected probes are matched by exact normalized source.
    """
    chosen = probes if probes is not None else default_probes()
    return chosen.get(normalize_source(source))


def validate_candidates(rows: list[dict[str, Any]], probes: dict[str, Probe] | None = None) -> list[LinkObservation]:
    """Return one observation per catalog row. Probes are injectable so tests stay offline.

    Every row must have a probe; otherwise UnsupportedSourceError is raised before any
    probe runs. A probe that raises is recorded as inconclusive for that row only.
    """
    chosen = probes if probes is not None else default_probes()
    unsupported = sorted({source_label(row.get("source")) for row in rows if probe_for(row.get("source"), chosen) is None})
    if unsupported:
        raise UnsupportedSourceError(unsupported)
    observations: list[LinkObservation] = []
    for row in rows:
        source = normalize_source(row.get("source"))
        probe = chosen[source]
        try:
            result = probe_result(probe(row))
        except Exception:
            result = ProbeResult(INCONCLUSIVE, f"{source}_probe_error", source)
        observations.append(
            LinkObservation(
                job_id=row["job_id"],
                previous_status=row.get("link_status") or "active",
                observation=result.observation,
                provider=result.provider,
                reason=result.reason,
                observed_at=_utc_now(),
                requested_url=result.requested_url,
                final_url=result.final_url,
                http_status=result.http_status,
                expected_identity=result.expected_identity,
                observed_identities=result.observed_identities,
            )
        )
    return observations


def default_probes() -> dict[str, Probe]:
    """Provider lifecycle probes. The keys are link_policy.SUPPORTED_LIFECYCLE_SOURCES; there is no fallback."""
    return {
        "indeed": probe_indeed,
        "linkedin": probe_linkedin,
        "naukri": probe_naukri,
    }


def probe_indeed(row: dict[str, Any]) -> ProbeResult:
    from indeed_checker import INDEED_GRAPHQL, indeed_jk_from_url, lookup_indeed_job

    quiet_transport_logging()
    jk = indeed_jk_from_url(row.get("job_url") or "")
    if not (os.getenv("INDEED_API_KEY") or "").strip():
        observation, reason = interpret_indeed(expired=None, key_configured=False)
        return ProbeResult(observation, reason, "indeed", expected_identity=jk)
    if not jk:
        return ProbeResult(INCONCLUSIVE, "indeed_missing_job_key", "indeed")
    expired, returned_keys = lookup_indeed_job(jk)
    observation, reason = interpret_indeed(expired=expired, key_configured=True)
    # The helper does not expose its response URL or status, so only the endpoint it posted to is known.
    return ProbeResult(
        observation,
        reason,
        "indeed",
        requested_url=INDEED_GRAPHQL,
        expected_identity=jk,
        observed_identities=returned_keys,
    )


def probe_linkedin(row: dict[str, Any]) -> ProbeResult:
    quiet_transport_logging()
    job_id = linkedin_job_id_from_url(row.get("job_url") or "")
    if not job_id:
        return ProbeResult(INCONCLUSIVE, "linkedin_unrecognized_url", "linkedin")
    endpoint = GUEST_URL.format(job_id=job_id)
    try:
        with httpx.Client(timeout=8.0, follow_redirects=True, headers=HEADERS) as client:
            response = client.get(endpoint)
    except httpx.HTTPError:
        observation, reason = interpret_linkedin(
            status_code=None, body=None, expected_job_id=job_id, final_url=None, error="connection"
        )
        return ProbeResult(observation, reason, "linkedin", requested_url=endpoint, expected_identity=job_id)
    final_url = str(response.url)
    observation, reason, identities = interpret_linkedin_evidence(
        status_code=response.status_code,
        body=response.text,
        expected_job_id=job_id,
        final_url=final_url,
    )
    return ProbeResult(
        observation,
        reason,
        "linkedin",
        requested_url=endpoint,
        final_url=final_url,
        http_status=response.status_code,
        expected_identity=job_id,
        observed_identities=identities,
    )


def probe_naukri(row: dict[str, Any]) -> ProbeResult:
    from link_checker import _naukri_job_id_from_url

    quiet_transport_logging()
    job_id = _naukri_job_id_from_url(row.get("job_url") or "")
    if not job_id:
        return ProbeResult(INCONCLUSIVE, "naukri_unrecognized_url", "naukri")
    requested = None
    try:
        import sys
        from pathlib import Path

        scraper = Path(__file__).resolve().parents[1] / "naukri_jobs_scraper"
        if str(scraper) not in sys.path:
            sys.path.append(str(scraper))
        from naukri_api import JOB_DETAIL_URL, NaukriSearchClient, generate_nkparam

        endpoint = JOB_DETAIL_URL.format(job_id=job_id)
        client = NaukriSearchClient(delay_seconds=0.2)
        try:
            # Set only once the detail request is issued; a failed session warm-up is not one.
            requested = endpoint
            response = client._client.get(
                endpoint,
                params={"microsite": "y", "src": "jobsearchDesk", "xp": "1", "px": "1"},
                headers={
                    "Accept": "application/json",
                    "appid": "121",
                    "systemid": "Naukri",
                    "clientid": "d3skt0p",
                    "nkparam": generate_nkparam("jd"),
                },
            )
        finally:
            client.close()
    except Exception:
        observation, reason = interpret_naukri(
            status_code=None, payload=None, expected_job_id=job_id, final_url=None, error="connection"
        )
        return ProbeResult(observation, reason, "naukri", requested_url=requested, expected_identity=job_id)
    payload = None
    if response.status_code == 200:
        try:
            payload = response.json()
        except Exception:
            payload = None
    final_url = str(response.url)
    observation, reason, identities = interpret_naukri_evidence(
        status_code=response.status_code,
        payload=payload,
        expected_job_id=job_id,
        final_url=final_url,
    )
    return ProbeResult(
        observation,
        reason,
        "naukri",
        requested_url=requested,
        final_url=final_url,
        http_status=response.status_code,
        expected_identity=job_id,
        observed_identities=identities,
    )
