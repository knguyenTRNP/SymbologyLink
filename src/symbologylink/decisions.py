from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .models import EntityMatchInput
from .normalize import normalize_country, normalize_domain, normalize_name
from .providers import ProviderCandidate
from .validity import period, validate_periods


def _load_structured(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("YAML rules require the 'rules' extra: pip install 'symbologylink[rules]'") from exc
    return yaml.safe_load(text)


def _date_applies(observation: str | None, valid_from: str | None, valid_to: str | None) -> bool:
    if not observation:
        return not valid_from and not valid_to
    value = date.fromisoformat(observation)
    return (not valid_from or value >= date.fromisoformat(valid_from)) and (not valid_to or value <= date.fromisoformat(valid_to))


def _periods_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_start = date.fromisoformat(left["valid_from"]) if left.get("valid_from") else date.min
    left_end = date.fromisoformat(left["valid_to"]) if left.get("valid_to") else date.max
    right_start = date.fromisoformat(right["valid_from"]) if right.get("valid_from") else date.min
    right_end = date.fromisoformat(right["valid_to"]) if right.get("valid_to") else date.max
    return max(left_start, right_start) <= min(left_end, right_end)


def _stable_signature(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _condition_matches(record: EntityMatchInput, conditions: dict[str, Any]) -> bool:
    name = record.entityName or record.legalName or record.brandName
    for field, clause in conditions.items():
        if field in {"entity_name", "entityName", "legal_name", "brand_name"}:
            value = name
        else:
            value = getattr(record, field, None)
        if not isinstance(clause, dict):
            clause = {"exact": clause}
        if "exact" in clause and value != clause["exact"]:
            return False
        if "exact_any" in clause and value not in clause["exact_any"]:
            return False
        if "normalized_exact" in clause and normalize_name(value) != normalize_name(clause["normalized_exact"]):
            return False
        if "normalized_exact_any" in clause and normalize_name(value) not in {normalize_name(item) for item in clause["normalized_exact_any"]}:
            return False
        if "domain" in clause and normalize_domain(value) != normalize_domain(clause["domain"]):
            return False
        if "country" in clause and normalize_country(value) != normalize_country(clause["country"]):
            return False
    return True


@dataclass(slots=True)
class Decision:
    source: str
    action: str
    candidate: ProviderCandidate | None
    reason: str | None = None
    version: str | None = None


def _candidate_from_result(result: dict[str, Any], provider: str) -> ProviderCandidate:
    entity = result.get("entity") or result
    security = result.get("security")
    parent = result.get("public_parent") or result.get("publicParent")
    relationship_graph = result.get("relationship_graph") or result.get("relationshipGraph") or {}
    relationships = result.get("relationships") or relationship_graph.get("edges") or []
    entity_periods = list(result.get("entity_periods") or result.get("entityPeriods") or entity.get("validityPeriods") or [])
    if not entity_periods and (result.get("valid_from") or result.get("valid_to")):
        entity_periods = [period("entity_existence", result.get("valid_from"), result.get("valid_to"), provider=provider)]
    security_periods = list(result.get("security_periods") or result.get("securityPeriods") or (security or {}).get("validityPeriods") or [])
    if security and not security_periods and (security.get("validFrom") or security.get("validTo")):
        security_periods = [period("security_listing", security.get("validFrom"), security.get("validTo"), provider=provider, securityId=security.get("securityId") or security.get("internal_security_id"))]
    return ProviderCandidate(
        entity_id=entity.get("entity_id") or entity.get("entityId") or "",
        canonical_name=entity.get("canonical_name") or entity.get("canonicalName") or "",
        entity_type=entity.get("entity_type") or entity.get("entityType") or "legal_entity",
        provider=provider,
        identifiers={key: str(value) for key, value in (result.get("identifiers") or {}).items()},
        security=security,
        public_parent=parent,
        relationships=list(relationships),
        valid_from=result.get("valid_from"),
        valid_to=result.get("valid_to"),
        entity_periods=entity_periods,
        security_periods=security_periods,
    )


class RuleSet:
    def __init__(self, rules: list[dict[str, Any]] | None = None):
        self.rules = sorted(rules or [], key=lambda item: int(item.get("priority", 100)), reverse=True)
        self.validate()

    @classmethod
    def load(cls, path: str | Path | None) -> "RuleSet":
        if not path:
            return cls()
        rule_path = Path(path)
        if not rule_path.exists():
            return cls()
        value = _load_structured(rule_path) or []
        return cls(value.get("rules", value) if isinstance(value, dict) else value)

    def validate(self) -> None:
        ids = set()
        for rule in self.rules:
            if not rule.get("id"):
                raise ValueError("Every rule requires an id.")
            if rule["id"] in ids:
                raise ValueError(f"Duplicate rule id: {rule['id']}")
            ids.add(rule["id"])
            if not isinstance(rule.get("conditions"), dict) or not isinstance(rule.get("result"), dict):
                raise ValueError(f"Rule {rule['id']} requires conditions and result objects.")
            validate_periods([{"validFrom": rule.get("valid_from"), "validTo": rule.get("valid_to")}], f"Rule {rule['id']} applicability")
            candidate = _candidate_from_result(rule["result"], f"rule:{rule['id']}")
            validate_periods(candidate.entity_periods, f"Rule {rule['id']} entity")
            validate_periods(candidate.security_periods, f"Rule {rule['id']} security")
            if candidate.security_periods and not candidate.security:
                raise ValueError(f"Rule {rule['id']} supplies security periods without a security.")
            validate_periods(candidate.relationships, f"Rule {rule['id']} relationships")
            for relationship in candidate.relationships:
                validate_periods(relationship.get("periods"), f"Rule {rule['id']} relationship periods")
        active = [rule for rule in self.rules if rule.get("enabled", True)]
        for index, left in enumerate(active):
            for right in active[index + 1:]:
                if int(left.get("priority", 100)) != int(right.get("priority", 100)):
                    continue
                if _stable_signature(left.get("conditions")) != _stable_signature(right.get("conditions")):
                    continue
                if not _periods_overlap(left, right):
                    continue
                left_outcome = {"action": left.get("action", "match"), "result": left.get("result")}
                right_outcome = {"action": right.get("action", "match"), "result": right.get("result")}
                if _stable_signature(left_outcome) != _stable_signature(right_outcome):
                    raise ValueError(
                        f"Conflicting equal-priority rules {left['id']} and {right['id']} "
                        "have the same conditions and overlapping validity periods."
                    )

    def resolve(self, record: EntityMatchInput) -> Decision | None:
        for rule in self.rules:
            if not rule.get("enabled", True):
                continue
            if not _date_applies(record.observationDate, rule.get("valid_from"), rule.get("valid_to")):
                continue
            if _condition_matches(record, rule["conditions"]):
                action = rule.get("action", "match")
                candidate = _candidate_from_result(rule["result"], f"rule:{rule['id']}") if action == "match" else None
                return Decision(f"rule:{rule['id']}", action, candidate, rule.get("description"), rule.get("version"))
        return None


class OverrideStore:
    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None

    def list(self) -> list[dict[str, Any]]:
        if not self.path or not self.path.exists():
            return []
        rows = []
        for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid override JSON on line {line_number}: {exc.msg}") from exc
        return rows

    def append(self, decision: dict[str, Any]) -> dict[str, Any]:
        if not self.path:
            raise ValueError("An override path is not configured.")
        if not decision.get("pattern") or not decision.get("action") or not decision.get("reviewer"):
            raise ValueError("Overrides require pattern, action, and reviewer.")
        validate_periods([{"validFrom": decision.get("valid_from"), "validTo": decision.get("valid_to")}], "Override applicability")
        if decision.get("action") == "match":
            candidate = _candidate_from_result(decision.get("result") or {}, f"override:{decision.get('reviewer')}")
            validate_periods(candidate.entity_periods, "Override entity")
            validate_periods(candidate.security_periods, "Override security")
            if candidate.security_periods and not candidate.security:
                raise ValueError("Override supplies security periods without a security.")
            validate_periods(candidate.relationships, "Override relationships")
            for relationship in candidate.relationships:
                validate_periods(relationship.get("periods"), "Override relationship periods")
        decision = {**decision, "created_at": decision.get("created_at") or datetime.now(timezone.utc).isoformat()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(decision, separators=(",", ":")) + "\n")
        return decision

    def resolve(self, record: EntityMatchInput) -> Decision | None:
        for item in reversed(self.list()):
            if not _date_applies(record.observationDate, item.get("valid_from"), item.get("valid_to")):
                continue
            if _condition_matches(record, item["pattern"]):
                candidate = _candidate_from_result(item.get("result") or {}, f"override:{item.get('reviewer')}") if item["action"] == "match" else None
                return Decision("human_override", item["action"], candidate, item.get("reason"), item.get("mapping_version"))
        return None
