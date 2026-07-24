from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any

from .decisions import _periods_overlap
from .ingest import IngestionError, read_records
from .models import EntityMatchInput
from .providers import MatchProvider, ProviderCandidate, ProviderCapabilities, TrustLevel
from .validity import period, validate_periods


class RelationshipType(str, Enum):
    SUBSIDIARY_OF = "subsidiary_of"
    OWNED_BY = "owned_by"
    BRAND_OF = "brand_of"
    DIVISION_OF = "division_of"
    OPERATED_BY = "operated_by"
    ULTIMATE_PARENT_OF = "ultimate_parent_of"
    MINORITY_OWNED_BY = "minority_owned_by"
    FORMERLY_OWNED_BY = "formerly_owned_by"


@dataclass(frozen=True, slots=True)
class EntityRelationship:
    relationship_id: str
    child_entity_id: str
    parent_entity_id: str
    relationship_type: RelationshipType
    valid_from: str | None = None
    valid_to: str | None = None
    ownership_percentage: float | None = None
    source: str | None = None
    source_record_id: str | None = None
    trust_level: TrustLevel = TrustLevel.AUTHORITATIVE
    is_direct: bool = True

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["relationship_type"] = self.relationship_type.value
        value["trust_level"] = self.trust_level.value
        return value


@dataclass(frozen=True, slots=True)
class RelationshipValidationIssue:
    row_number: int
    code: str
    message: str
    field: str | None = None
    severity: str = "error"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RelationshipValidationReport:
    rows: int
    relationships: list[EntityRelationship] = field(default_factory=list)
    issues: list[RelationshipValidationIssue] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        errors = [issue.to_dict() for issue in self.issues if issue.severity == "error"]
        warnings = [issue.to_dict() for issue in self.issues if issue.severity == "warning"]
        return {
            "valid": self.valid,
            "rows": self.rows,
            "relationships": len(self.relationships),
            "errors": errors,
            "warnings": warnings,
        }


class RelationshipValidationError(IngestionError):
    def __init__(self, report: RelationshipValidationReport):
        self.report = report
        errors = [issue for issue in report.issues if issue.severity == "error"]
        detail = "; ".join(f"row {item.row_number} [{item.code}]: {item.message}" for item in errors)
        super().__init__(f"Relationship master validation failed with {len(errors)} error(s): {detail}")


CANONICAL_RELATIONSHIP_FIELDS = set(EntityRelationship.__dataclass_fields__)
REQUIRED_RELATIONSHIP_FIELDS = {"relationship_id", "child_entity_id", "parent_entity_id", "relationship_type"}
MUTUALLY_EXCLUSIVE_PARENT_TYPES = {
    RelationshipType.SUBSIDIARY_OF,
    RelationshipType.OWNED_BY,
    RelationshipType.BRAND_OF,
    RelationshipType.DIVISION_OF,
}
CYCLE_RELATIONSHIP_TYPES = MUTUALLY_EXCLUSIVE_PARENT_TYPES | {
    RelationshipType.OPERATED_BY,
    RelationshipType.ULTIMATE_PARENT_OF,
}


def _load_document(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        value = json.loads(text)
    else:
        try:
            import yaml
        except ImportError as exc:
            raise IngestionError("YAML relationship mappings require: pip install 'symbologylink[rules]'") from exc
        value = yaml.safe_load(text)
    if not isinstance(value, dict):
        raise IngestionError("Relationship configuration must be an object.")
    return value


def load_relationship_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    value = _load_document(config_path)
    block = value.get("relationship_master", value)
    if not isinstance(block, dict):
        raise IngestionError("relationship_master must be an object.")
    unknown = sorted(set(block) - {"path", "columns", "mapping", "trust_level"})
    if unknown:
        raise IngestionError(f"Unknown relationship_master configuration key(s): {', '.join(unknown)}")
    result = dict(block)
    if result.get("path"):
        configured_path = Path(str(result["path"]))
        result["path"] = str(configured_path if configured_path.is_absolute() else config_path.parent / configured_path)
    return result


def load_relationship_mapping(path: str | Path | None) -> dict[str, str]:
    if not path:
        return {field: field for field in CANONICAL_RELATIONSHIP_FIELDS}
    value = _load_document(Path(path))
    block = value.get("relationship_master", value)
    raw = block.get("columns") or block.get("mapping") or block
    if not isinstance(raw, dict) or not raw:
        raise IngestionError("Relationship column mapping must be a non-empty object.")
    if set(raw) <= CANONICAL_RELATIONSHIP_FIELDS:
        mapping = {str(canonical): str(source) for canonical, source in raw.items()}
    elif set(raw.values()) <= CANONICAL_RELATIONSHIP_FIELDS:
        mapping = {str(canonical): str(source) for source, canonical in raw.items()}
    else:
        unknown = sorted((set(raw) - CANONICAL_RELATIONSHIP_FIELDS) | (set(raw.values()) - CANONICAL_RELATIONSHIP_FIELDS))
        raise IngestionError(f"Unknown relationship mapping field(s): {', '.join(map(str, unknown))}")
    duplicate_sources = sorted({source for source in mapping.values() if list(mapping.values()).count(source) > 1})
    if duplicate_sources:
        raise IngestionError(f"Relationship mapping reuses source column(s): {', '.join(duplicate_sources)}")
    return mapping


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _boolean(value: Any, default: bool = True) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    raise ValueError(f"expected a boolean, found {value!r}")


def validate_relationship_file(
    input_path: str | Path,
    mapping_path: str | Path | None = None,
    *,
    mapping: dict[str, str] | None = None,
    default_trust_level: str | TrustLevel = TrustLevel.AUTHORITATIVE,
    entity_ids: set[str] | None = None,
) -> RelationshipValidationReport:
    rows = list(read_records(input_path))
    report = RelationshipValidationReport(rows=len(rows))
    if not rows:
        report.issues.append(RelationshipValidationIssue(0, "empty_file", "The relationship file contains no records."))
        return report
    columns = {str(column) for row in rows for column in row}
    mapping = dict(mapping or load_relationship_mapping(mapping_path))
    missing_mappings = sorted(REQUIRED_RELATIONSHIP_FIELDS - set(mapping))
    for field_name in missing_mappings:
        report.issues.append(RelationshipValidationIssue(0, "missing_mapping", f"No source column is mapped to {field_name}.", field_name))
    for canonical, source in mapping.items():
        if canonical not in CANONICAL_RELATIONSHIP_FIELDS:
            report.issues.append(RelationshipValidationIssue(0, "unknown_mapping", f"Unknown canonical relationship field {canonical!r}.", canonical))
        elif source not in columns and canonical in REQUIRED_RELATIONSHIP_FIELDS:
            report.issues.append(RelationshipValidationIssue(0, "missing_source_column", f"Mapped source column {source!r} is absent.", canonical))
    try:
        configured_trust = default_trust_level if isinstance(default_trust_level, TrustLevel) else TrustLevel(str(default_trust_level))
    except ValueError:
        report.issues.append(RelationshipValidationIssue(0, "invalid_trust_level", f"Unsupported trust level {default_trust_level!r}.", "trust_level"))
        configured_trust = TrustLevel.AUTHORITATIVE

    parsed: list[tuple[int, EntityRelationship]] = []
    for row_number, row in enumerate(rows, 1):
        canonical = {field_name: row.get(source) for field_name, source in mapping.items() if field_name in CANONICAL_RELATIONSHIP_FIELDS}
        relationship_id = _clean(canonical.get("relationship_id"))
        child_id = _clean(canonical.get("child_entity_id"))
        parent_id = _clean(canonical.get("parent_entity_id"))
        for field_name, value in (("relationship_id", relationship_id), ("child_entity_id", child_id), ("parent_entity_id", parent_id)):
            if not value:
                report.issues.append(RelationshipValidationIssue(row_number, "missing_identifier", f"{field_name} is required.", field_name))
        if child_id and parent_id and child_id == parent_id:
            report.issues.append(RelationshipValidationIssue(row_number, "self_reference", "child_entity_id and parent_entity_id must differ.", "parent_entity_id"))
        try:
            relationship_type = RelationshipType(str(canonical.get("relationship_type") or "").strip().lower())
        except ValueError:
            relationship_type = None
            allowed = ", ".join(item.value for item in RelationshipType)
            report.issues.append(RelationshipValidationIssue(row_number, "invalid_relationship_type", f"Expected one of: {allowed}.", "relationship_type"))
        valid_from, valid_to = _clean(canonical.get("valid_from")), _clean(canonical.get("valid_to"))
        try:
            validate_periods([{"validFrom": valid_from, "validTo": valid_to}], f"Relationship row {row_number}")
        except ValueError as exc:
            report.issues.append(RelationshipValidationIssue(row_number, "invalid_period", str(exc), "valid_to"))
        ownership = None
        if canonical.get("ownership_percentage") not in (None, ""):
            try:
                ownership = float(canonical["ownership_percentage"])
                if not 0 <= ownership <= 100:
                    raise ValueError("must be between 0 and 100")
            except (TypeError, ValueError) as exc:
                report.issues.append(RelationshipValidationIssue(row_number, "invalid_ownership_percentage", f"ownership_percentage {exc}.", "ownership_percentage"))
                ownership = None
        if relationship_type == RelationshipType.MINORITY_OWNED_BY and ownership is not None and ownership >= 50:
            report.issues.append(RelationshipValidationIssue(row_number, "minority_control_conflict", "minority_owned_by must remain below 50% and is never classified as a subsidiary.", "ownership_percentage"))
        try:
            row_trust = TrustLevel(str(canonical.get("trust_level") or configured_trust.value).strip().lower())
        except ValueError:
            row_trust = configured_trust
            report.issues.append(RelationshipValidationIssue(row_number, "invalid_trust_level", f"Unsupported trust level {canonical.get('trust_level')!r}.", "trust_level"))
        try:
            is_direct = _boolean(canonical.get("is_direct"), default=relationship_type != RelationshipType.ULTIMATE_PARENT_OF)
        except ValueError as exc:
            is_direct = True
            report.issues.append(RelationshipValidationIssue(row_number, "invalid_is_direct", str(exc), "is_direct"))
        if relationship_id and child_id and parent_id and relationship_type:
            parsed.append((row_number, EntityRelationship(
                relationship_id, child_id, parent_id, relationship_type,
                valid_from, valid_to, ownership,
                _clean(canonical.get("source")), _clean(canonical.get("source_record_id")),
                row_trust, is_direct,
            )))

    by_relationship_id: dict[str, list[tuple[int, EntityRelationship]]] = {}
    for item in parsed:
        by_relationship_id.setdefault(item[1].relationship_id, []).append(item)
    for relationship_id, values in by_relationship_id.items():
        if len(values) > 1:
            for row_number, _ in values:
                report.issues.append(RelationshipValidationIssue(row_number, "duplicate_relationship_id", f"relationship_id {relationship_id!r} appears on rows {', '.join(str(item[0]) for item in values)}.", "relationship_id"))

    for index, (left_row, left) in enumerate(parsed):
        left_period = {"valid_from": left.valid_from, "valid_to": left.valid_to}
        for right_row, right in parsed[index + 1:]:
            right_period = {"valid_from": right.valid_from, "valid_to": right.valid_to}
            if not _periods_overlap(left_period, right_period):
                continue
            if (left.child_entity_id, left.parent_entity_id, left.relationship_type) == (right.child_entity_id, right.parent_entity_id, right.relationship_type):
                for row_number in (left_row, right_row):
                    report.issues.append(RelationshipValidationIssue(row_number, "duplicate_active_relationship", f"Rows {left_row} and {right_row} duplicate the same active relationship period."))
            elif (
                left.child_entity_id == right.child_entity_id
                and left.parent_entity_id != right.parent_entity_id
                and left.relationship_type in MUTUALLY_EXCLUSIVE_PARENT_TYPES
                and right.relationship_type in MUTUALLY_EXCLUSIVE_PARENT_TYPES
            ):
                for row_number in (left_row, right_row):
                    report.issues.append(RelationshipValidationIssue(row_number, "overlapping_parent_periods", f"Rows {left_row} and {right_row} assign overlapping mutually-exclusive parents to {left.child_entity_id!r}."))

    adjacency: dict[str, list[tuple[str, int]]] = {}
    for row_number, relationship in parsed:
        if relationship.relationship_type in CYCLE_RELATIONSHIP_TYPES:
            adjacency.setdefault(relationship.child_entity_id, []).append((relationship.parent_entity_id, row_number))
    state: dict[str, int] = {}
    stack_nodes: list[str] = []
    stack_rows: list[int] = []
    cycle_rows: set[int] = set()

    def visit(node: str) -> None:
        state[node] = 1
        stack_nodes.append(node)
        for parent_id, row_number in adjacency.get(node, []):
            if state.get(parent_id, 0) == 0:
                stack_rows.append(row_number)
                visit(parent_id)
                stack_rows.pop()
            elif state.get(parent_id) == 1:
                start = stack_nodes.index(parent_id)
                cycle_rows.update(stack_rows[start:])
                cycle_rows.add(row_number)
        stack_nodes.pop()
        state[node] = 2

    for entity_id in set(adjacency):
        if state.get(entity_id, 0) == 0:
            visit(entity_id)
    for row_number in sorted(cycle_rows):
        report.issues.append(RelationshipValidationIssue(row_number, "parent_cycle", "This relationship participates in a parent cycle."))

    if entity_ids is not None:
        for row_number, relationship in parsed:
            for field_name, value in (("child_entity_id", relationship.child_entity_id), ("parent_entity_id", relationship.parent_entity_id)):
                if value not in entity_ids:
                    report.issues.append(RelationshipValidationIssue(row_number, "unknown_entity_reference", f"{value!r} is absent from the configured entity master.", field_name, "warning"))

    report.relationships = [relationship for _, relationship in parsed]
    return report


class CustomerRelationshipMasterProvider(MatchProvider):
    name = "customer_relationship_master"
    trust_level = TrustLevel.AUTHORITATIVE
    capabilities = ProviderCapabilities(current_relationships=True, relationship_effective_dates=True)

    def __init__(
        self,
        path: str | Path,
        mapping_path: str | Path | None = None,
        *,
        mapping: dict[str, str] | None = None,
        trust_level: str | TrustLevel = TrustLevel.AUTHORITATIVE,
        entity_ids: set[str] | None = None,
    ):
        self.path = Path(path)
        self.trust_level = trust_level if isinstance(trust_level, TrustLevel) else TrustLevel(str(trust_level))
        self.mapping = dict(mapping or load_relationship_mapping(mapping_path))
        self.validation_report = validate_relationship_file(
            self.path, mapping=mapping or self.mapping,
            default_trust_level=self.trust_level, entity_ids=entity_ids,
        )
        if not self.validation_report.valid:
            raise RelationshipValidationError(self.validation_report)
        self.relationships = list(self.validation_report.relationships)

    def metadata(self) -> dict[str, Any]:
        warnings = [
            issue.to_dict() for issue in self.validation_report.issues
            if issue.severity == "warning"
        ]
        return {
            **super().metadata(),
            "relationship_count": len(self.relationships),
            "source_file": self.path.name,
            "validation_warnings": warnings,
        }

    def health_check(self) -> dict[str, Any]:
        return {**self.metadata(), "status": "available"}

    @classmethod
    def from_config(cls, config_path: str | Path, *, entity_ids: set[str] | None = None) -> CustomerRelationshipMasterProvider:
        block = load_relationship_config(config_path)
        if not block.get("path"):
            raise IngestionError("relationship_master.path is required.")
        return cls(
            block["path"], mapping=block.get("columns") or block.get("mapping"),
            trust_level=block.get("trust_level", TrustLevel.AUTHORITATIVE.value), entity_ids=entity_ids,
        )

    def search(self, input_record: EntityMatchInput, limit: int = 20) -> list[ProviderCandidate]:
        return []

    @staticmethod
    def _applies(relationship: EntityRelationship, observation_date: str | None) -> bool | None:
        if not observation_date:
            return None
        observed = date.fromisoformat(observation_date)
        start = date.fromisoformat(relationship.valid_from) if relationship.valid_from else date.min
        end = date.fromisoformat(relationship.valid_to) if relationship.valid_to else date.max
        return start <= observed <= end

    def resolve_relationships(self, candidate: ProviderCandidate, observation_date: str | None = None, max_depth: int = 8) -> dict[str, Any] | None:
        outgoing: dict[str, list[EntityRelationship]] = {}
        for relationship in self.relationships:
            outgoing.setdefault(relationship.child_entity_id, []).append(relationship)
        if candidate.entity_id not in outgoing:
            return None
        nodes: dict[str, dict[str, Any]] = {
            candidate.entity_id: {
                "entityId": candidate.entity_id, "canonicalName": candidate.canonical_name,
                "entityType": candidate.entity_type, "identifiers": dict(candidate.identifiers),
                "providers": list(candidate.sources or [candidate.provider]),
            }
        }
        edges: list[dict[str, Any]] = []
        frontier = [(candidate.entity_id, 0)]
        expanded: set[tuple[str, int]] = set()
        complete = True
        errors: list[str] = []
        while frontier:
            child_id, depth = frontier.pop(0)
            if (child_id, depth) in expanded:
                continue
            expanded.add((child_id, depth))
            relationships = outgoing.get(child_id, [])
            if depth >= max_depth:
                if relationships:
                    complete = False
                    errors.append(f"Customer relationship traversal stopped at max depth {max_depth}.")
                continue
            for relationship in relationships:
                nodes.setdefault(relationship.parent_entity_id, {
                    "entityId": relationship.parent_entity_id,
                    "canonicalName": relationship.parent_entity_id,
                    "entityType": "legal_entity",
                    "identifiers": {}, "providers": [self.name],
                })
                valid = self._applies(relationship, observation_date)
                relationship_period = period(
                    relationship.relationship_type.value, relationship.valid_from, relationship.valid_to,
                    "ACTIVE", self.name, relationshipId=relationship.relationship_id,
                    sourceRecordId=relationship.source_record_id,
                )
                edges.append({
                    "fromEntityId": relationship.child_entity_id,
                    "toEntityId": relationship.parent_entity_id,
                    "relationshipType": relationship.relationship_type.value,
                    "level": "direct" if relationship.is_direct else "ultimate",
                    "status": "ACTIVE", "validFrom": relationship.valid_from, "validTo": relationship.valid_to,
                    "periods": [relationship_period], "validOnObservationDate": valid,
                    "providers": [self.name], "provider": self.name, "source": self.name,
                    "trustLevel": relationship.trust_level.value,
                    "ownershipPercentage": relationship.ownership_percentage,
                    "relationshipId": relationship.relationship_id,
                    "provenance": [{
                        "provider": self.name, "source": relationship.source,
                        "sourceRecordId": relationship.source_record_id,
                        "relationshipId": relationship.relationship_id,
                        "sourceFile": self.path.name,
                    }],
                })
                frontier.append((relationship.parent_entity_id, depth + 1))
        return {
            "provider": self.name,
            "subject": nodes[candidate.entity_id],
            "nodes": list(nodes.values()), "edges": edges,
            "reportingExceptions": [], "complete": complete, "errors": list(dict.fromkeys(errors)),
        }

    def inspect(self, entity_id: str, observation_date: str | None = None, max_depth: int = 8) -> dict[str, Any]:
        graph = self.resolve_relationships(
            ProviderCandidate(entity_id, entity_id, "legal_entity", self.name), observation_date, max_depth,
        ) or {"provider": self.name, "subject": {"entityId": entity_id}, "nodes": [], "edges": [], "complete": True, "errors": []}
        active = [
            edge for edge in graph.get("edges") or []
            if edge.get("fromEntityId") == entity_id and edge.get("validOnObservationDate") is not False
        ]
        return {
            "entityId": entity_id,
            "observationDate": observation_date,
            "parents": [{
                "parentEntityId": edge["toEntityId"], "relationshipType": edge["relationshipType"],
                "relationshipId": edge["relationshipId"], "validFrom": edge.get("validFrom"),
                "validTo": edge.get("validTo"), "ownershipPercentage": edge.get("ownershipPercentage"),
                "trustLevel": edge.get("trustLevel"), "source": edge.get("source"),
            } for edge in active],
            "graph": graph,
        }
