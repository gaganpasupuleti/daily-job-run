"""Separate destination self-match from catalog-to-destination linkage."""

from __future__ import annotations

from dataclasses import dataclass

from application_route_validation.identity import IdentityLink, IdentityRef, link_board_to_greenhouse, listing_identity
from application_route_validation.observation_contract import IdentityLinkEvidence, IdentityNamespace


@dataclass(frozen=True, slots=True)
class IdentityAssurance:
    """destination_self_match: apply_url post id matches rendered destination id.

    catalog_link_assured: explicit apply_url linkage from board id to destination id (not title/company).
    """

    destination_self_match: bool | None
    catalog_link_assured: bool | None
    link: IdentityLinkEvidence | None


def assurance_for_greenhouse_route(
    *,
    source: str,
    job_url: str,
    apply_url: str,
    expected_post_id: str,
    observed_post_id: str | None,
) -> IdentityAssurance:
    board = listing_identity(source, job_url)
    dest_match: bool | None = None
    if observed_post_id is not None:
        dest_match = expected_post_id == observed_post_id
    gh_ref = IdentityRef("greenhouse:job_post", expected_post_id) if expected_post_id else None
    link_obj = (
        link_board_to_greenhouse(
            IdentityRef(board.namespace, board.value) if board else None,
            gh_ref,
            apply_url,
        )
        if board and gh_ref
        else None
    )
    link_evidence = (
        IdentityLinkEvidence(link_type=link_obj.link_type, detail=link_obj.evidence) if link_obj else None
    )
    # apply_url pointer is catalog evidence; it does not prove the employer requisition matches board metadata.
    catalog_assured = link_evidence is not None and dest_match is True
    return IdentityAssurance(
        destination_self_match=dest_match,
        catalog_link_assured=catalog_assured,
        link=link_evidence,
    )
