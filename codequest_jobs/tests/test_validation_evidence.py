"""Validator evidence gates. Pure tests: no network and no database.

The LinkedIn fixtures are synthetic and follow the structure of the public
guest job endpoint: a top card with the primary Apply action or a closed-job
figure, then the description, and a hidden decoratedJobPostingId.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
SCRAPER = REPO / "naukri_jobs_scraper"
for _entry in (str(SCRAPER), str(ROOT)):
    while _entry in sys.path:
        sys.path.remove(_entry)
    sys.path.insert(0, _entry)

import link_policy  # noqa: E402
from indeed_checker import parse_indeed_results  # noqa: E402
from jobready_validator import (  # noqa: E402
    interpret_indeed,
    interpret_linkedin,
    interpret_naukri,
    validate_candidates,
)
from linkedin_checker import GUEST_URL  # noqa: E402

JOB_ID = "4475277813"
GUEST = GUEST_URL.format(job_id=JOB_ID)
NAUKRI_ID = "051026501163"
NAUKRI_DETAIL = f"https://www.naukri.com/jobapi/v1/job/{NAUKRI_ID}?microsite=y&src=jobsearchDesk"

APPLY_BUTTON = (
    '<button class="sign-up-modal__outlet top-card-layout__cta top-card-layout__cta--primary btn-md btn-primary" '
    'data-modal="job-details-topcard-apply-modal"> Apply '
    '<icon data-svg-class-name="apply-button__offsite-apply-icon-svg"></icon></button>'
)
DISABLED_APPLY_BUTTON = (
    '<button class="top-card-layout__cta top-card-layout__cta--primary btn-md btn-primary" disabled> Apply </button>'
)
SAVE_BUTTON = '<button class="top-card-layout__cta top-card-layout__cta--secondary btn-md btn-secondary"> Save </button>'
CLOSED_FIGURE = (
    '<figure class="closed-job closed-job__flavor topcard__flavor-row">'
    '<figcaption class="closed-job__flavor--closed">No longer accepting applications</figcaption></figure>'
)


def linkedin_page(
    *,
    apply: str = APPLY_BUTTON,
    closed: bool = False,
    identity: str | None = JOB_ID,
    top: str = "",
    after: str = "",
) -> str:
    identity_html = (
        f'<code id="decoratedJobPostingId" style="display: none"><!--"{identity}"--></code>' if identity else ""
    )
    return (
        '<section class="top-card-layout container-lined">'
        '<h2 class="top-card-layout__title">Python Developer</h2>'
        f"{CLOSED_FIGURE if closed else ''}{top}"
        f'<div class="top-card-layout__cta-container">{apply}{SAVE_BUTTON}</div>'
        "</section>"
        '<div class="description__text description__text--rich">'
        '<div class="show-more-less-html__markup">Build Python APIs for a fresher team.</div></div>'
        f"{after}{identity_html}"
    )


def linkedin(body: str, *, status: int = 200, final_url: str | None = GUEST, expected: str = JOB_ID):
    return interpret_linkedin(status_code=status, body=body, expected_job_id=expected, final_url=final_url)


def naukri(payload, *, status: int = 200, final_url: str | None = NAUKRI_DETAIL):
    return interpret_naukri(status_code=status, payload=payload, expected_job_id=NAUKRI_ID, final_url=final_url)


class LinkedInEvidenceTests(unittest.TestCase):
    def test_identity_bound_enabled_apply_is_open(self) -> None:
        self.assertEqual(linkedin(linkedin_page()), ("confirmed_active", "linkedin_identity_apply"))

    def test_offsite_apply_link_is_open(self) -> None:
        anchor = '<a class="apply-button apply-button--link" href="https://careers.example.test/apply"> Apply </a>'
        self.assertEqual(linkedin(linkedin_page(apply=anchor))[0], "confirmed_active")

    def test_disabled_apply_is_not_open(self) -> None:
        self.assertEqual(linkedin(linkedin_page(apply=DISABLED_APPLY_BUTTON)), ("inconclusive", "linkedin_apply_disabled"))
        aria = APPLY_BUTTON.replace("<button ", '<button aria-disabled="true" ', 1)
        self.assertEqual(linkedin(linkedin_page(apply=aria)), ("inconclusive", "linkedin_apply_disabled"))

    def test_missing_apply_is_neither_open_nor_closed(self) -> None:
        self.assertEqual(linkedin(linkedin_page(apply="")), ("inconclusive", "linkedin_no_apply_evidence"))

    def test_job_not_found_top_card_is_not_open(self) -> None:
        page = linkedin_page(top='<p class="topcard__flavor">Job not found</p>')
        self.assertEqual(linkedin(page), ("inconclusive", "linkedin_soft_404"))

    def test_captcha_and_authwall_are_not_open(self) -> None:
        captcha = linkedin_page(top='<div class="topcard__flavor">Please complete this captcha to continue</div>')
        self.assertEqual(linkedin(captcha), ("inconclusive", "linkedin_blocked"))
        authwall = linkedin_page(after='<div class="authwall-join-form">Join to view</div>')
        self.assertEqual(linkedin(authwall), ("inconclusive", "linkedin_blocked"))

    def test_identity_missing_or_mismatched_is_not_conclusive(self) -> None:
        self.assertEqual(linkedin(linkedin_page(identity=None)), ("inconclusive", "linkedin_identity_missing"))
        self.assertEqual(linkedin(linkedin_page(identity="999")), ("inconclusive", "linkedin_identity_mismatch"))
        closed_other = linkedin_page(closed=True, apply="", identity="999")
        self.assertEqual(linkedin(closed_other), ("inconclusive", "linkedin_identity_mismatch"))
        two = linkedin_page(after='<code id="decoratedJobPostingId"><!--"999"--></code>')
        self.assertEqual(linkedin(two), ("inconclusive", "linkedin_identity_mismatch"))

    def test_closure_in_status_region_is_closed(self) -> None:
        self.assertEqual(linkedin(linkedin_page(closed=True, apply="")), ("confirmed_expired", "linkedin_closed_marker"))

    def test_closure_with_enabled_apply_is_a_conflict(self) -> None:
        self.assertEqual(linkedin(linkedin_page(closed=True)), ("inconclusive", "linkedin_conflict"))

    def test_closure_text_outside_status_region_is_ignored(self) -> None:
        similar = '<section class="similar-jobs"><li>Other job: No longer accepting applications</li></section>'
        self.assertEqual(linkedin(linkedin_page(after=similar))[0], "confirmed_active")
        self.assertEqual(linkedin(linkedin_page(apply="", after=similar)), ("inconclusive", "linkedin_no_apply_evidence"))
        hidden = '<template><figcaption class="closed-job__flavor--closed">No longer accepting applications</figcaption></template>'
        self.assertEqual(linkedin(linkedin_page(top=hidden))[0], "confirmed_active")

    def test_applicant_counts_are_never_closure(self) -> None:
        for text in ("0 applicants", "Be among the first 25 applicants", "Over 200 applicants"):
            page = linkedin_page(apply="", top=f'<span class="num-applicants__caption">{text}</span>')
            self.assertEqual(linkedin(page)[0], "inconclusive", text)

    def test_page_without_status_region_is_not_conclusive(self) -> None:
        body = f'<html><body>Jobs search {APPLY_BUTTON}<code id="decoratedJobPostingId"><!--"{JOB_ID}"--></code></body></html>'
        self.assertEqual(linkedin(body), ("inconclusive", "linkedin_no_status_region"))

    def test_redirect_away_from_the_job_endpoint_is_not_conclusive(self) -> None:
        for final_url in (
            "https://www.linkedin.com/login?session_redirect=jobs",
            "https://www.linkedin.com/jobs/search/?keywords=python",
            GUEST_URL.format(job_id="999"),
            None,
        ):
            self.assertEqual(linkedin(linkedin_page(), final_url=final_url), ("inconclusive", "linkedin_redirected"))
            self.assertEqual(linkedin("", status=404, final_url=final_url)[0], "inconclusive")

    def test_http_failures_are_not_conclusive(self) -> None:
        for status in (401, 403, 408, 429, 500, 503, 999):
            self.assertEqual(linkedin(linkedin_page(), status=status)[0], "inconclusive", status)
        error = interpret_linkedin(status_code=None, body=None, expected_job_id=JOB_ID, final_url=None, error="timeout")
        self.assertEqual(error, ("inconclusive", "linkedin_connection_failure"))

    def test_guest_endpoint_gone_for_the_same_job_is_closed(self) -> None:
        self.assertEqual(linkedin("", status=404), ("confirmed_expired", "linkedin_guest_gone"))
        self.assertEqual(linkedin("", status=410)[0], "confirmed_expired")


class NaukriEvidenceTests(unittest.TestCase):
    def test_explicit_closed_with_matching_id_is_closed(self) -> None:
        payload = {"job": {"jobId": NAUKRI_ID, "title": "Python Developer", "jobStatus": "Expired"}}
        self.assertEqual(naukri(payload), ("confirmed_expired", "naukri_explicit_closed"))
        flagged = {"jobDetails": {"jobId": NAUKRI_ID, "isExpired": True}}
        self.assertEqual(naukri(flagged)[0], "confirmed_expired")

    def test_explicit_open_with_matching_id_is_open(self) -> None:
        self.assertEqual(naukri({"job": {"jobId": NAUKRI_ID, "jobStatus": "active"}}), ("confirmed_active", "naukri_explicit_open"))
        self.assertEqual(naukri({"job": {"jobId": NAUKRI_ID, "isExpired": False}})[0], "confirmed_active")
        self.assertEqual(naukri({"job": {"jobId": NAUKRI_ID, "isExpired": "false"}})[0], "confirmed_active")

    def test_title_alone_is_not_open(self) -> None:
        payload = {"job": {"jobId": NAUKRI_ID, "title": "Python Developer"}}
        self.assertEqual(naukri(payload), ("inconclusive", "naukri_no_status_evidence"))

    def test_identity_is_required(self) -> None:
        self.assertEqual(naukri({"job": {"title": "Python Developer", "jobStatus": "expired"}}), ("inconclusive", "naukri_identity_missing"))
        other = {"job": {"jobId": "999", "jobStatus": "expired"}}
        self.assertEqual(naukri(other), ("inconclusive", "naukri_identity_mismatch"))

    def test_conflicting_flags_are_not_conclusive(self) -> None:
        payload = {"job": {"jobId": NAUKRI_ID, "jobStatus": "active", "isExpired": True}}
        self.assertEqual(naukri(payload), ("inconclusive", "naukri_conflict"))

    def test_unrecognized_flag_values_are_ignored(self) -> None:
        for value in ("1", 1, 0, None, "yes", ""):
            payload = {"job": {"jobId": NAUKRI_ID, "isExpired": value}}
            self.assertEqual(naukri(payload), ("inconclusive", "naukri_no_status_evidence"), repr(value))

    def test_empty_or_malformed_detail_is_not_conclusive(self) -> None:
        self.assertEqual(naukri({"job": []}), ("inconclusive", "naukri_empty_detail"))
        self.assertEqual(naukri({"job": {}}), ("inconclusive", "naukri_empty_detail"))
        self.assertEqual(naukri([{"jobId": NAUKRI_ID}]), ("inconclusive", "naukri_malformed"))
        self.assertEqual(naukri(None, status=500), ("inconclusive", "naukri_http_500"))

    def test_redirect_away_from_the_detail_endpoint_is_not_conclusive(self) -> None:
        payload = {"job": {"jobId": NAUKRI_ID, "jobStatus": "expired"}}
        for final_url in ("https://www.naukri.com/", "https://www.naukri.com/jobapi/v1/job/999", None):
            self.assertEqual(naukri(payload, final_url=final_url), ("inconclusive", "naukri_redirected"))


class IndeedEvidenceTests(unittest.TestCase):
    def test_only_real_booleans_are_evidence(self) -> None:
        self.assertEqual(interpret_indeed(expired=True, key_configured=True)[0], "confirmed_expired")
        self.assertEqual(interpret_indeed(expired=False, key_configured=True)[0], "confirmed_active")
        for value in (None, "false", "true", 0, 1, ""):
            self.assertEqual(interpret_indeed(expired=value, key_configured=True)[0], "inconclusive", repr(value))
        self.assertEqual(interpret_indeed(expired=True, key_configured=False)[0], "inconclusive")

    def test_parse_requires_boolean_flag_for_the_requested_key(self) -> None:
        payload = {
            "data": {
                "jobData": {
                    "results": [
                        {"job": {"key": "aaaa", "expired": False}},
                        {"job": {"key": "bbbb", "expired": True}},
                        {"job": {"key": "cccc"}},
                        {"job": {"key": "dddd", "expired": None}},
                        {"job": {"key": "eeee", "expired": "false"}},
                        {"job": {"key": "zzzz", "expired": False}},
                    ]
                }
            }
        }
        result = parse_indeed_results(payload, ["aaaa", "bbbb", "cccc", "dddd", "eeee", "ffff"])
        self.assertEqual(
            result,
            {"aaaa": False, "bbbb": True, "cccc": None, "dddd": None, "eeee": None, "ffff": None},
        )

    def test_conflicting_duplicate_results_are_unknown(self) -> None:
        payload = {
            "data": {
                "jobData": {
                    "results": [
                        {"job": {"key": "aaaa", "expired": False}},
                        {"job": {"key": "aaaa", "expired": True}},
                        {"job": {"key": "bbbb", "expired": True}},
                        {"job": {"key": "bbbb", "expired": True}},
                    ]
                }
            }
        }
        self.assertEqual(parse_indeed_results(payload, ["aaaa", "bbbb"]), {"aaaa": None, "bbbb": True})

    def test_malformed_graphql_is_unknown(self) -> None:
        for payload in (
            {"errors": [{"message": "bad request"}]},
            {"data": None},
            {"data": {"jobData": {"results": "nope"}}},
            {"data": {"jobData": {"results": [None, "x", {"job": "x"}]}}},
            ["unexpected"],
            None,
        ):
            self.assertEqual(parse_indeed_results(payload, ["aaaa"]), {"aaaa": None}, repr(payload))


class ValidationRunTests(unittest.TestCase):
    def test_probe_error_is_inconclusive_for_that_row_only(self) -> None:
        def broken(_row):
            raise ValueError("unexpected markup")

        rows = [
            {"job_id": "CQJ-1", "source": "linkedin", "link_status": "active"},
            {"job_id": "CQJ-2", "source": "naukri", "link_status": "active"},
        ]
        probes = {"linkedin": broken, "naukri": lambda row: ("confirmed_expired", "fixture", "naukri")}
        observations = validate_candidates(rows, probes=probes)
        self.assertEqual(
            [(item.job_id, item.observation, item.reason) for item in observations],
            [("CQJ-1", "inconclusive", "linkedin_probe_error"), ("CQJ-2", "confirmed_expired", "fixture")],
        )

    def test_expired_row_reactivates_only_on_identity_bound_open_evidence(self) -> None:
        pages = {
            "weak": linkedin_page(apply=""),
            "disabled": linkedin_page(apply=DISABLED_APPLY_BUTTON),
            "other": linkedin_page(identity="999"),
            "strong": linkedin_page(),
        }

        def probe(row):
            observation, reason = linkedin(pages[row["page"]])
            return observation, reason, "linkedin"

        rows = [
            {"job_id": f"CQJ-{name}", "source": "linkedin", "link_status": "expired", "page": name}
            for name in pages
        ]
        observations = validate_candidates(rows, probes={"linkedin": probe})
        statuses = {item.job_id: link_policy.next_link_status(item.previous_status, item.observation) for item in observations}
        self.assertEqual(
            statuses,
            {"CQJ-weak": "expired", "CQJ-disabled": "expired", "CQJ-other": "expired", "CQJ-strong": "active"},
        )


if __name__ == "__main__":
    unittest.main()
