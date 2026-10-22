"""Append-only route evidence (file JSONL and optional Postgres). Never calls apply_link_observations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from application_route_validation.observation_contract import RouteObservation


class RouteEvidenceStore:
    def append(self, observation: RouteObservation) -> None:
        raise NotImplementedError


class JsonlRouteEvidenceStore(RouteEvidenceStore):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, observation: RouteObservation) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(observation.to_json() + "\n")

    def load_all(self) -> list[RouteObservation]:
        if not self.path.exists():
            return []
        rows: list[RouteObservation] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(observation_from_dict(json.loads(line)))
        return rows


def observation_from_dict(payload: dict) -> RouteObservation:
    from application_route_validation.observation_contract import IdentityLinkEvidence, IdentityNamespace
    from datetime import datetime

    expected = payload.get("expected_identity")
    expected_ns = None
    if expected and ":" in expected:
        ns, val = expected.split(":", 1)
        expected_ns = IdentityNamespace(ns, val)
    observed = tuple(
        IdentityNamespace(*item.split(":", 1))
        for item in payload.get("observed_identities") or []
        if ":" in item
    )
    link = payload.get("identity_link")
    link_ev = IdentityLinkEvidence(link["link_type"], link["detail"]) if link else None
    return RouteObservation(
        evidence_ref=payload["evidence_ref"],
        run_id=payload["run_id"],
        job_id=payload["job_id"],
        subject_type=payload["subject_type"],
        subject_id=payload["subject_id"],
        route_id=payload["route_id"],
        provider=payload["provider"],
        adapter_version=payload["adapter_version"],
        policy_version=payload["policy_version"],
        observed_at=datetime.fromisoformat(payload["observed_at"]),
        outcome=payload["outcome"],
        reason=payload["reason"],
        expected_identity=expected_ns,
        observed_identities=observed,
        identity_link=link_ev,
        destination_self_match=payload.get("destination_self_match"),
        catalog_link_assured=payload.get("catalog_link_assured"),
        requested_url=payload.get("requested_url"),
        final_url=payload.get("final_url"),
        http_status=payload.get("http_status"),
        supporting_refs=tuple(payload.get("supporting_refs") or ()),
        extra=dict(payload.get("extra") or {}),
    )


def append_many(store: RouteEvidenceStore, items: Iterable[RouteObservation]) -> int:
    count = 0
    for item in items:
        store.append(item)
        count += 1
    return count
