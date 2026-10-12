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
    between: str = "",
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
        f"{between}"
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


def apply_anchor(href_attribute: str) -> str:
    return f'<a class="apply-button apply-button--link top-card-layout__cta--primary"{href_attribute}> Apply </a>'


class EndpointOriginTests(unittest.TestCase):
    OPEN = {"job": {"jobId": NAUKRI_ID, "jobStatus": "active"}}
    CLOSED = {"job": {"jobId": NAUKRI_ID, "jobStatus": "expired"}}

    def test_naukri_wrong_host_is_not_conclusive(self) -> None:
        for host in ("evil.example", "naukri.com.evil.example", "m.naukri.com", "naukri.com"):
            url = f"https://{host}/jobapi/v1/job/{NAUKRI_ID}"
            self.assertEqual(naukri(self.CLOSED, final_url=url), ("inconclusive", "naukri_redirected"), host)
        userinfo = f"https://www.naukri.com@evil.example/jobapi/v1/job/{NAUKRI_ID}"
        self.assertEqual(naukri(self.OPEN, final_url=userinfo), ("inconclusive", "naukri_redirected"))

    def test_naukri_plain_http_is_not_conclusive(self) -> None:
        url = f"http://www.naukri.com/jobapi/v1/job/{NAUKRI_ID}"
        self.assertEqual(naukri(self.OPEN, final_url=url), ("inconclusive", "naukri_redirected"))

    def test_naukri_unexpected_port_is_not_conclusive(self) -> None:
        for port in ("8443", "80", "abc"):
            url = f"https://www.naukri.com:{port}/jobapi/v1/job/{NAUKRI_ID}"
            self.assertEqual(naukri(self.OPEN, final_url=url), ("inconclusive", "naukri_redirected"), port)

    def test_linkedin_plain_http_is_not_conclusive(self) -> None:
        url = GUEST.replace("https://", "http://", 1)
        self.assertEqual(linkedin(linkedin_page(), final_url=url), ("inconclusive", "linkedin_redirected"))
        self.assertEqual(linkedin("", status=404, final_url=url), ("inconclusive", "linkedin_redirected"))

    def test_linkedin_unexpected_port_is_not_conclusive(self) -> None:
        url = GUEST.replace("www.linkedin.com", "www.linkedin.com:8443", 1)
        self.assertEqual(linkedin(linkedin_page(), final_url=url), ("inconclusive", "linkedin_redirected"))

    def test_linkedin_other_origin_or_generic_page_is_not_conclusive(self) -> None:
        for url in (
            GUEST.replace("www.linkedin.com", "evil.example", 1),
            GUEST.replace("https://www.linkedin.com", "https://www.linkedin.com@evil.example", 1),
            "https://www.linkedin.com/",
            "https://www.linkedin.com/jobs",
        ):
            self.assertEqual(linkedin(linkedin_page(), final_url=url), ("inconclusive", "linkedin_redirected"), url)

    def test_correct_linkedin_endpoint_is_evaluated_normally(self) -> None:
        for url in (GUEST, f"{GUEST}?trk=public_jobs", GUEST.replace("www.linkedin.com", "www.linkedin.com:443", 1)):
            self.assertEqual(linkedin(linkedin_page(), final_url=url), ("confirmed_active", "linkedin_identity_apply"), url)
            self.assertEqual(linkedin(linkedin_page(closed=True, apply=""), final_url=url)[0], "confirmed_expired", url)

    def test_correct_naukri_endpoint_with_open_status_is_active(self) -> None:
        self.assertEqual(naukri(self.OPEN), ("confirmed_active", "naukri_explicit_open"))
        bare = f"https://www.naukri.com/jobapi/v1/job/{NAUKRI_ID}"
        self.assertEqual(naukri(self.OPEN, final_url=bare), ("confirmed_active", "naukri_explicit_open"))

    def test_correct_naukri_endpoint_with_closed_status_is_expired(self) -> None:
        self.assertEqual(naukri(self.CLOSED), ("confirmed_expired", "naukri_explicit_closed"))


class FunctionalApplyTests(unittest.TestCase):
    def assert_nonfunctional(self, apply: str) -> None:
        self.assertEqual(linkedin(linkedin_page(apply=apply)), ("inconclusive", "linkedin_apply_nonfunctional"), apply)

    def test_anchor_without_href_is_not_open(self) -> None:
        self.assert_nonfunctional(apply_anchor(""))

    def test_anchor_with_empty_href_is_not_open(self) -> None:
        self.assert_nonfunctional(apply_anchor(' href=""'))
        self.assert_nonfunctional(apply_anchor(' href="   "'))

    def test_anchor_with_hash_href_is_not_open(self) -> None:
        self.assert_nonfunctional(apply_anchor(' href="#"'))
        self.assert_nonfunctional(apply_anchor(' href="#apply"'))

    def test_anchor_with_javascript_href_is_not_open(self) -> None:
        self.assert_nonfunctional(apply_anchor(' href="javascript:void(0)"'))
        self.assert_nonfunctional(apply_anchor(' href=" JavaScript:void(0)"'))

    def test_anchor_with_inert_destination_is_not_open(self) -> None:
        for href in ("about:blank", "data:text/html,apply", "mailto:jobs@example.test", GUEST, "?trk=x"):
            self.assert_nonfunctional(apply_anchor(f' href="{href}"'))
        for href in ("https://www.linkedin.com/login?session_redirect=x", "https://www.linkedin.com/signup/cold-join"):
            self.assert_nonfunctional(apply_anchor(f' href="{href}"'))

    def test_disabled_apply_button_is_not_open(self) -> None:
        self.assertEqual(linkedin(linkedin_page(apply=DISABLED_APPLY_BUTTON)), ("inconclusive", "linkedin_apply_disabled"))

    def test_aria_disabled_apply_button_is_not_open(self) -> None:
        aria = APPLY_BUTTON.replace("<button ", '<button aria-disabled="TRUE" ', 1)
        self.assertEqual(linkedin(linkedin_page(apply=aria)), ("inconclusive", "linkedin_apply_disabled"))
        anchor = apply_anchor(' href="https://careers.example.test/apply" aria-disabled="true"')
        self.assertEqual(linkedin(linkedin_page(apply=anchor)), ("inconclusive", "linkedin_apply_disabled"))

    def test_recognized_linkedin_apply_button_is_open(self) -> None:
        self.assertEqual(linkedin(linkedin_page()), ("confirmed_active", "linkedin_identity_apply"))
        enabled = APPLY_BUTTON.replace("<button ", '<button aria-disabled="false" ', 1)
        self.assertEqual(linkedin(linkedin_page(apply=enabled))[0], "confirmed_active")

    def test_apply_button_without_recognized_action_is_not_open(self) -> None:
        self.assert_nonfunctional(APPLY_BUTTON.replace(' data-modal="job-details-topcard-apply-modal"', "", 1))
        self.assert_nonfunctional(APPLY_BUTTON.replace("job-details-topcard-apply-modal", "share-modal", 1))
        self.assert_nonfunctional('<button data-modal="job-details-topcard-apply-modal"> Apply </button>')
        self.assert_nonfunctional('<a href="https://careers.example.test/apply"> Apply now </a>')
        self.assert_nonfunctional(APPLY_BUTTON.replace(" Apply ", " Continue ", 1))

    def test_offsite_http_or_https_apply_anchor_is_open(self) -> None:
        for href in (
            "https://careers.example.test/apply?id=1",
            "http://careers.example.test/apply",
            "/jobs/view/externalApply/4475277813",
        ):
            self.assertEqual(
                linkedin(linkedin_page(apply=apply_anchor(f' href="{href}"'))),
                ("confirmed_active", "linkedin_identity_apply"),
                href,
            )

    def test_closed_marker_with_nonfunctional_apply_is_closed(self) -> None:
        page = linkedin_page(closed=True, apply=apply_anchor(' href="#"'))
        self.assertEqual(linkedin(page), ("confirmed_expired", "linkedin_closed_marker"))


class TopCardScopeTests(unittest.TestCase):
    def test_apply_outside_top_card_is_not_open(self) -> None:
        for place in ("between", "after"):
            page = linkedin_page(apply="", **{place: f"<div>{APPLY_BUTTON}</div>"})
            self.assertEqual(linkedin(page), ("inconclusive", "linkedin_no_apply_evidence"), place)

    def test_hidden_apply_inside_top_card_is_not_open(self) -> None:
        for wrapper in ("<div hidden>", '<div style="display: none">', '<div style="visibility:hidden">'):
            page = linkedin_page(apply=f"{wrapper}{APPLY_BUTTON}</div>")
            self.assertEqual(linkedin(page), ("inconclusive", "linkedin_no_apply_evidence"), wrapper)
        self.assertEqual(linkedin(linkedin_page(apply=APPLY_BUTTON.replace("<button ", "<button hidden ", 1)))[0], "inconclusive")

    def test_aria_hidden_apply_inside_top_card_is_not_open(self) -> None:
        page = linkedin_page(apply=f'<div aria-hidden="true">{APPLY_BUTTON}</div>')
        self.assertEqual(linkedin(page), ("inconclusive", "linkedin_no_apply_evidence"))
        hidden_label = APPLY_BUTTON.replace(" Apply ", ' <span aria-hidden="true">Apply</span> ', 1)
        self.assertEqual(linkedin(linkedin_page(apply=hidden_label)), ("inconclusive", "linkedin_apply_nonfunctional"))

    def test_apply_inside_template_or_script_is_not_open(self) -> None:
        for wrapper, closing in (("<template>", "</template>"), ("<noscript>", "</noscript>"), ("<script>var x = '", "';</script>")):
            page = linkedin_page(apply=f"{wrapper}{APPLY_BUTTON}{closing}")
            self.assertEqual(linkedin(page), ("inconclusive", "linkedin_no_apply_evidence"), wrapper)

    def test_closed_marker_outside_top_card_does_not_expire(self) -> None:
        self.assertEqual(linkedin(linkedin_page(between=CLOSED_FIGURE)), ("confirmed_active", "linkedin_identity_apply"))
        self.assertEqual(linkedin(linkedin_page(apply="", between=CLOSED_FIGURE)), ("inconclusive", "linkedin_no_apply_evidence"))
        hidden = f'<div aria-hidden="true">{CLOSED_FIGURE}</div>'
        self.assertEqual(linkedin(linkedin_page(top=hidden)), ("confirmed_active", "linkedin_identity_apply"))

    def test_top_card_closed_marker_without_functional_apply_is_closed(self) -> None:
        self.assertEqual(linkedin(linkedin_page(closed=True, apply="")), ("confirmed_expired", "linkedin_closed_marker"))
        text_only = '<p class="topcard__flavor">No longer\n   accepting applications</p>'
        self.assertEqual(linkedin(linkedin_page(apply="", top=text_only)), ("confirmed_expired", "linkedin_closed_marker"))

    def test_top_card_functional_apply_without_closure_is_open(self) -> None:
        nested = APPLY_BUTTON.replace(" Apply ", " <span>Apply</span> ", 1)
        self.assertEqual(linkedin(linkedin_page(apply=nested)), ("confirmed_active", "linkedin_identity_apply"))

    def test_top_card_with_closure_and_functional_apply_is_a_conflict(self) -> None:
        self.assertEqual(linkedin(linkedin_page(closed=True)), ("inconclusive", "linkedin_conflict"))

    def test_unclear_top_card_boundary_is_not_conclusive(self) -> None:
        second = '<section class="top-card-layout">Another job</section>'
        self.assertEqual(linkedin(linkedin_page(after=second)), ("inconclusive", "linkedin_top_card_ambiguous"))
        unterminated = linkedin_page().replace("</section>", "", 1)
        self.assertEqual(linkedin(unterminated), ("inconclusive", "linkedin_top_card_ambiguous"))
        overrun = f'<main>{linkedin_page().replace("</section>", "</main>", 1)}'
        self.assertEqual(linkedin(overrun), ("inconclusive", "linkedin_top_card_ambiguous"))

    def test_hidden_top_card_copy_is_ignored(self) -> None:
        copy = f'<template><section class="top-card-layout">{CLOSED_FIGURE}</section></template>'
        self.assertEqual(linkedin(linkedin_page(after=copy)), ("confirmed_active", "linkedin_identity_apply"))


UNCLOSED_APPLY_BUTTON = APPLY_BUTTON.removesuffix("</button>")
UNCLOSED_APPLY_LINK = '<a class="apply-button apply-button--link" href="https://careers.example.test/apply"> Apply '


class InheritedDisabledApplyTests(unittest.TestCase):
    """Phase 0 closure: disabled state is inherited from a disabled ancestor."""

    def test_button_inside_disabled_fieldset_is_not_open(self) -> None:
        page = linkedin_page(apply=f"<fieldset disabled>{APPLY_BUTTON}</fieldset>")
        self.assertEqual(linkedin(page), ("inconclusive", "linkedin_apply_disabled"))

    def test_nested_descendants_of_disabled_fieldset_are_not_open(self) -> None:
        page = linkedin_page(apply=f"<fieldset disabled><div><span>{APPLY_BUTTON}</span></div></fieldset>")
        self.assertEqual(linkedin(page), ("inconclusive", "linkedin_apply_disabled"))
        nested = f'<fieldset disabled=""><fieldset><div>{APPLY_BUTTON}</div></fieldset></fieldset>'
        self.assertEqual(linkedin(linkedin_page(apply=nested)), ("inconclusive", "linkedin_apply_disabled"))

    def test_link_inside_disabled_fieldset_is_not_open(self) -> None:
        anchor = '<a class="apply-button apply-button--link" href="https://careers.example.test/apply"> Apply </a>'
        page = linkedin_page(apply=f"<fieldset disabled>{anchor}</fieldset>")
        self.assertEqual(linkedin(page), ("inconclusive", "linkedin_apply_disabled"))

    def test_aria_disabled_ancestor_is_not_open(self) -> None:
        page = linkedin_page(apply=f'<div aria-disabled="true"><span>{APPLY_BUTTON}</span></div>')
        self.assertEqual(linkedin(page), ("inconclusive", "linkedin_apply_disabled"))

    def test_direct_disabled_and_aria_disabled_remain_disabled(self) -> None:
        self.assertEqual(linkedin(linkedin_page(apply=DISABLED_APPLY_BUTTON)), ("inconclusive", "linkedin_apply_disabled"))
        aria = APPLY_BUTTON.replace("<button ", '<button aria-disabled="true" ', 1)
        self.assertEqual(linkedin(linkedin_page(apply=aria)), ("inconclusive", "linkedin_apply_disabled"))

    def test_enabled_button_after_closed_disabled_fieldset_is_open(self) -> None:
        apply = f"<fieldset disabled><button> Save </button></fieldset>{APPLY_BUTTON}"
        self.assertEqual(linkedin(linkedin_page(apply=apply)), ("confirmed_active", "linkedin_identity_apply"))

    def test_disabled_fieldset_elsewhere_does_not_affect_top_card(self) -> None:
        form = f"<fieldset disabled><div>{APPLY_BUTTON}</div></fieldset>"
        for place in ("between", "after"):
            page = linkedin_page(**{place: form})
            self.assertEqual(linkedin(page), ("confirmed_active", "linkedin_identity_apply"), place)
        top = "<fieldset disabled><input type='text'></fieldset>"
        self.assertEqual(linkedin(linkedin_page(top=top)), ("confirmed_active", "linkedin_identity_apply"))

    def test_enabled_fieldset_does_not_disable(self) -> None:
        page = linkedin_page(apply=f"<fieldset>{APPLY_BUTTON}</fieldset>")
        self.assertEqual(linkedin(page), ("confirmed_active", "linkedin_identity_apply"))


class MalformedApplyTests(unittest.TestCase):
    """Phase 0 closure: an Apply control counts only if its own end tag closed it."""

    def test_unclosed_apply_button_is_not_open(self) -> None:
        page = linkedin_page(apply=UNCLOSED_APPLY_BUTTON)
        self.assertEqual(linkedin(page), ("inconclusive", "linkedin_apply_malformed"))

    def test_unclosed_apply_link_is_not_open(self) -> None:
        page = linkedin_page(apply=UNCLOSED_APPLY_LINK)
        self.assertEqual(linkedin(page), ("inconclusive", "linkedin_apply_malformed"))

    def test_apply_closed_only_by_outer_top_card_is_not_open(self) -> None:
        page = (
            '<section class="top-card-layout container-lined">'
            '<h2 class="top-card-layout__title">Python Developer</h2>'
            f'<div class="top-card-layout__cta-container">{UNCLOSED_APPLY_BUTTON}'
            "</section>"
            f'<code id="decoratedJobPostingId" style="display: none"><!--"{JOB_ID}"--></code>'
        )
        self.assertEqual(linkedin(page), ("inconclusive", "linkedin_apply_malformed"))
        stray = APPLY_BUTTON.replace("</button>", "</a>", 1)
        self.assertEqual(linkedin(linkedin_page(apply=stray)), ("inconclusive", "linkedin_apply_malformed"))

    def test_properly_closed_apply_still_confirms_active(self) -> None:
        self.assertEqual(linkedin(linkedin_page()), ("confirmed_active", "linkedin_identity_apply"))
        nested = APPLY_BUTTON.replace(" Apply ", " <span>Apply</span> ", 1)
        self.assertEqual(linkedin(linkedin_page(apply=nested)), ("confirmed_active", "linkedin_identity_apply"))
        self_closing_icon = APPLY_BUTTON.replace("></icon>", " />", 1).replace("</icon>", "", 1)
        self.assertEqual(linkedin(linkedin_page(apply=self_closing_icon)), ("confirmed_active", "linkedin_identity_apply"))

    def test_malformed_hidden_apply_does_not_affect_result(self) -> None:
        for wrapper in ("<div hidden>", '<div style="display:none">', "<template>"):
            closing = "</template>" if wrapper == "<template>" else "</div>"
            page = linkedin_page(apply=f"{wrapper}{UNCLOSED_APPLY_BUTTON}{closing}{APPLY_BUTTON}")
            self.assertEqual(linkedin(page), ("confirmed_active", "linkedin_identity_apply"), wrapper)

    def test_malformed_apply_beside_valid_apply_is_not_open(self) -> None:
        page = linkedin_page(apply=f"{APPLY_BUTTON}<div>{UNCLOSED_APPLY_LINK}</div>")
        self.assertEqual(linkedin(page), ("inconclusive", "linkedin_apply_malformed"))

    def test_unclosed_non_apply_wrapper_does_not_block_valid_apply(self) -> None:
        page = linkedin_page(apply=f"<p><span>{APPLY_BUTTON}")
        self.assertEqual(linkedin(page), ("confirmed_active", "linkedin_identity_apply"))


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
