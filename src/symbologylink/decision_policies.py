from __future__ import annotations

import json
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Iterable


ENTITY_PATHWAYS = (
    "exact_cik",
    "exact_lei",
    "exact_figi",
    "exact_isin",
    "exact_cusip",
    "ticker_and_exchange",
    "exact_name_and_domain",
    "exact_domain",
    "exact_name",
    "fuzzy_name_and_country",
    "fuzzy_name_only",
    "customer_rule",
    "human_override",
    "relationship_traversal",
    "brand_inference",
    "ticker_only",
    "unknown",
)


@dataclass(frozen=True, slots=True)
class DecisionPolicy:
    allow_auto_match: bool = False
    minimum_match_score: float = 0.98
    allow_review: bool = True
    review_minimum_score: float = 0.80
    require_no_conflicts: bool = True
    require_active_security: bool = False

    def __post_init__(self) -> None:
        for name in ("allow_auto_match", "allow_review", "require_no_conflicts", "require_active_security"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be true or false.")
        for name in ("minimum_match_score", "review_minimum_score"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a number between 0 and 1.")
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1.")
        if self.minimum_match_score < self.review_minimum_score:
            raise ValueError("minimum_match_score cannot be lower than review_minimum_score.")

    def decide(self, match_score: float, has_conflict: bool = False, active_security: bool = True) -> str:
        # Exact-identifier, temporal, and ambiguity conflicts are safety gates,
        # not evidence that can be outweighed by a larger aggregate score.
        if has_conflict and self.require_no_conflicts:
            return "review_required"
        active_requirement_failed = self.require_active_security and not active_security
        if self.allow_auto_match and match_score >= self.minimum_match_score and not active_requirement_failed:
            return "matched"
        if self.allow_review and (match_score >= self.review_minimum_score or active_requirement_failed):
            return "review_required"
        return "unmatched"


def _policy(**overrides: Any) -> DecisionPolicy:
    return replace(DecisionPolicy(), **overrides)


def default_decision_policies() -> dict[str, DecisionPolicy]:
    exact = _policy(allow_auto_match=True)
    review_only = _policy(allow_auto_match=False)
    return {
        "exact_cik": exact,
        "exact_lei": exact,
        "exact_figi": exact,
        "exact_isin": exact,
        "exact_cusip": exact,
        "ticker_and_exchange": _policy(allow_auto_match=True, require_active_security=True),
        "exact_name_and_domain": _policy(allow_auto_match=True),
        "exact_domain": review_only,
        "exact_name": review_only,
        "fuzzy_name_and_country": review_only,
        "fuzzy_name_only": review_only,
        "customer_rule": _policy(allow_auto_match=True, minimum_match_score=0, review_minimum_score=0),
        "human_override": _policy(allow_auto_match=True, minimum_match_score=0, review_minimum_score=0),
        "relationship_traversal": _policy(allow_auto_match=False, review_minimum_score=0),
        "brand_inference": _policy(allow_auto_match=False, review_minimum_score=0),
        "ticker_only": _policy(allow_auto_match=False, review_minimum_score=0),
        "unknown": review_only,
    }


def _load_structured(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("YAML decision policies require: pip install 'symbologylink[rules]'") from exc
    return yaml.safe_load(text)


class DecisionPolicySet:
    def __init__(self, policies: dict[str, DecisionPolicy] | None = None):
        self.policies = policies or default_decision_policies()

    @classmethod
    def load(cls, path: str | Path | None) -> "DecisionPolicySet":
        defaults = default_decision_policies()
        if not path:
            return cls(defaults)
        policy_path = Path(path)
        if not policy_path.exists():
            raise ValueError(f"Decision policy file does not exist: {policy_path}")
        value = _load_structured(policy_path) or {}
        if not isinstance(value, dict):
            raise ValueError("Decision policy configuration must be an object.")
        if "decision_policies" in value:
            unknown_top_level = sorted(set(value) - {"decision_policies"})
            if unknown_top_level:
                raise ValueError(f"Unknown decision policy configuration key(s): {', '.join(unknown_top_level)}")
        raw_policies = value.get("decision_policies", value)
        if not isinstance(raw_policies, dict):
            raise ValueError("decision_policies must be an object keyed by pathway.")
        unknown_pathways = sorted(set(raw_policies) - set(ENTITY_PATHWAYS))
        if unknown_pathways:
            raise ValueError(f"Unknown decision pathway(s): {', '.join(unknown_pathways)}")
        valid_fields = {item.name for item in fields(DecisionPolicy)}
        for pathway, raw_policy in raw_policies.items():
            if not isinstance(raw_policy, dict):
                raise ValueError(f"Decision policy {pathway!r} must be an object.")
            unknown_fields = sorted(set(raw_policy) - valid_fields)
            if unknown_fields:
                raise ValueError(f"Unknown field(s) for decision policy {pathway!r}: {', '.join(unknown_fields)}")
            defaults[pathway] = replace(defaults[pathway], **raw_policy)
        return cls(defaults)

    @classmethod
    def legacy(cls, auto_match_threshold: float, review_threshold: float) -> "DecisionPolicySet":
        policy = DecisionPolicy(
            allow_auto_match=True,
            minimum_match_score=auto_match_threshold,
            allow_review=True,
            review_minimum_score=review_threshold,
            require_no_conflicts=True,
        )
        return cls({pathway: policy for pathway in ENTITY_PATHWAYS})

    def policy_for(self, pathway: str | None) -> DecisionPolicy:
        return self.policies.get(pathway or "unknown", self.policies["unknown"])

    def decide(self, pathway: str | None, match_score: float, has_conflict: bool = False, active_security: bool = True) -> str:
        return self.policy_for(pathway).decide(match_score, has_conflict, active_security)


def _evidence_value(evidence: Any, name: str, default: Any = None) -> Any:
    if isinstance(evidence, dict):
        return evidence.get(name, default)
    return getattr(evidence, name, default)


def derive_primary_pathway(evidence: Iterable[Any]) -> str:
    values = list(evidence)
    evidence_types = {_evidence_value(item, "type") for item in values}
    if "human_override" in evidence_types:
        return "human_override"
    if "reusable_rule_match" in evidence_types:
        return "customer_rule"

    identifier_details: set[str] = set()
    for item in values:
        if _evidence_value(item, "type") in {"identifier_match", "security_identifier_match"}:
            detail = str(_evidence_value(item, "detail", "")).strip().lower()
            identifier_details.add(detail)
    for field in ("cik", "lei", "figi", "isin", "cusip"):
        if field in identifier_details:
            return f"exact_{field}"
    if "ticker and exchange" in identifier_details:
        return "ticker_and_exchange"
    if "brand_inference" in evidence_types:
        return "brand_inference"

    exact_name = any(
        _evidence_value(item, "type") == "name_similarity" and _evidence_value(item, "similarity") == 1.0
        for item in values
    )
    fuzzy_name = any(
        _evidence_value(item, "type") == "name_similarity" and (_evidence_value(item, "similarity") or 0) < 1.0
        for item in values
    )
    exact_domain = "domain_match" in evidence_types
    country_match = "country_match" in evidence_types
    if exact_name and exact_domain:
        return "exact_name_and_domain"
    if exact_domain:
        return "exact_domain"
    if exact_name:
        return "exact_name"
    if fuzzy_name and country_match:
        return "fuzzy_name_and_country"
    if fuzzy_name:
        return "fuzzy_name_only"
    if "relationship_resolution" in evidence_types:
        return "relationship_traversal"
    if "ticker only" in identifier_details:
        return "ticker_only"
    return "unknown"
