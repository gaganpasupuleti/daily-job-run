"""Subject types for route evidence (separate from lifecycle ledger observations)."""

from __future__ import annotations

SUBJECT_LISTING = "listing"
SUBJECT_REQUISITION = "employer_requisition"
SUBJECT_APPLICATION_ROUTE = "application_route"

ALL_SUBJECTS = frozenset({SUBJECT_LISTING, SUBJECT_REQUISITION, SUBJECT_APPLICATION_ROUTE})
