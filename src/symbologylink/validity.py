from __future__ import annotations

from datetime import date
from typing import Any


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value[:10]) if value else None


def period(
    period_type: str,
    valid_from: str | None = None,
    valid_to: str | None = None,
    status: str | None = None,
    provider: str | None = None,
    **provenance: Any,
) -> dict[str, Any]:
    return {
        "periodType": period_type,
        "validFrom": valid_from,
        "validTo": valid_to,
        "status": status,
        "provider": provider,
        **{key: value for key, value in provenance.items() if value is not None},
    }


def _normalize(raw: dict[str, Any], default_type: str) -> dict[str, Any]:
    return {
        **raw,
        "periodType": raw.get("periodType") or raw.get("period_type") or raw.get("relationshipType") or raw.get("type") or default_type,
        "validFrom": raw.get("validFrom") or raw.get("valid_from") or raw.get("startDate"),
        "validTo": raw.get("validTo") or raw.get("valid_to") or raw.get("endDate"),
    }


def validate_periods(periods: list[dict[str, Any]] | None, context: str) -> None:
    for raw in periods or []:
        item = _normalize(raw, "validity")
        try:
            start, end = _date(item.get("validFrom")), _date(item.get("validTo"))
        except ValueError as exc:
            raise ValueError(f"{context} contains an invalid ISO date: {exc}") from exc
        if start and end and start > end:
            raise ValueError(f"{context} has validFrom after validTo: {item['validFrom']} > {item['validTo']}")


def evaluate_periods(scope: str, periods: list[dict[str, Any]] | None, observation_date: str | None, require_all: bool = False) -> dict[str, Any]:
    normalized = [_normalize(item, f"{scope}_validity") for item in (periods or [])]
    if not observation_date:
        return {"scope": scope, "status": "not_requested", "validOnObservationDate": None, "periods": normalized, "reason": "No observation date was supplied."}
    if not normalized:
        return {"scope": scope, "status": "not_verified", "validOnObservationDate": None, "periods": [], "reason": f"No {scope} validity periods were supplied."}

    observed = _date(observation_date)
    evaluated = []
    for item in normalized:
        start, end = _date(item.get("validFrom")), _date(item.get("validTo"))
        status = str(item.get("status") or "").upper()
        valid = None if (not start and not end) or (status in {"INACTIVE", "ANNULLED", "RETIRED", "DELISTED"} and not end) else (not start or observed >= start) and (not end or observed <= end)
        evaluated.append({**item, "validOnObservationDate": valid})
    dated = [item["validOnObservationDate"] for item in evaluated if item["validOnObservationDate"] is not None]
    undated = len(evaluated) - len(dated)
    if require_all:
        if any(value is False for value in dated):
            status, valid, reason = "invalid", False, f"At least one required {scope} period excludes the observation date."
        elif dated and not undated:
            status, valid, reason = "verified", True, f"All required {scope} periods include the observation date."
        elif dated:
            status, valid, reason = "partial", None, f"Dated {scope} periods are valid, but at least one required period is undated."
        else:
            status, valid, reason = "not_verified", None, f"The {scope} periods have no usable date bounds."
    else:
        if any(value is True for value in dated):
            status, valid, reason = "verified", True, f"At least one {scope} period includes the observation date."
        elif dated and not undated:
            status, valid, reason = "invalid", False, f"All dated {scope} periods exclude the observation date."
        elif dated:
            status, valid, reason = "partial", None, f"Dated {scope} periods exclude the observation date, but an undated period remains unresolved."
        else:
            status, valid, reason = "not_verified", None, f"The {scope} periods have no usable date bounds."
    return {"scope": scope, "status": status, "validOnObservationDate": valid, "periods": evaluated, "reason": reason}


def relationship_validity(graph: dict[str, Any] | None, observation_date: str | None) -> dict[str, Any]:
    if not graph:
        return {"scope": "relationships", "status": "not_applicable", "validOnObservationDate": None, "periods": [], "reason": "No relationship graph was selected."}
    selected = graph.get("selectedEdges") or []
    if not selected and graph.get("edges"):
        selected = [edge for edge in graph["edges"] if edge.get("fromEntityId") == (graph.get("subject") or {}).get("entityId") and edge.get("level") == "direct"]
    if not selected:
        status = "not_requested" if not observation_date else "not_applicable"
        return {"scope": "relationships", "status": status, "validOnObservationDate": None, "periods": [], "reason": "No selected relationship chain requires temporal evaluation."}
    if not observation_date:
        periods = [{**item, "fromEntityId": edge.get("fromEntityId"), "toEntityId": edge.get("toEntityId")} for edge in selected for item in (edge.get("periods") or [edge])]
        return {"scope": "relationships", "status": "not_requested", "validOnObservationDate": None, "periods": periods, "edgeEvaluations": [], "reason": "No observation date was supplied."}
    edge_evaluations = []
    flattened = []
    for edge in selected:
        values = edge.get("periods") or [edge]
        if edge.get("effectiveDatesCapable"):
            evaluation = evaluate_periods("relationship_edge", values, observation_date)
        else:
            evaluation = evaluate_periods("relationship_edge", [], observation_date)
            evaluation["capabilityRejectedProviders"] = list(edge.get("providers") or [])
            evaluation["rejectedPeriods"] = values
            evaluation["reason"] = "Relationship dates were rejected because their providers do not declare relationship_effective_dates."
        edge_evaluations.append({"fromEntityId": edge.get("fromEntityId"), "toEntityId": edge.get("toEntityId"), "relationshipType": edge.get("relationshipType"), **evaluation})
        flattened.extend({**item, "fromEntityId": edge.get("fromEntityId"), "toEntityId": edge.get("toEntityId"), "relationshipType": edge.get("relationshipType")} for item in evaluation["periods"])
    values = [item["validOnObservationDate"] for item in edge_evaluations]
    if any(value is False for value in values):
        status, valid, reason = "invalid", False, "At least one selected relationship edge excludes the observation date."
    elif values and all(value is True for value in values):
        status, valid, reason = "verified", True, "Every selected relationship edge has an applicable period."
    elif any(value is True for value in values):
        status, valid, reason = "partial", None, "Some selected relationship edges are valid while others lack conclusive periods."
    else:
        status, valid, reason = "not_verified", None, "Selected relationship edges lack conclusive periods."
    return {"scope": "relationships", "status": status, "validOnObservationDate": valid, "periods": flattened, "edgeEvaluations": edge_evaluations, "reason": reason}


def combine_validity(
    observation_date: str | None,
    entity: dict[str, Any],
    security: dict[str, Any],
    relationships: dict[str, Any],
) -> dict[str, Any]:
    scopes = [entity]
    if security.get("status") != "not_applicable":
        scopes.append(security)
    if relationships.get("status") != "not_applicable":
        scopes.append(relationships)
    if not observation_date:
        overall_status, valid, reason = "not_requested", None, "No observation date was supplied."
    elif any(item.get("validOnObservationDate") is False for item in scopes):
        overall_status, valid, reason = "invalid", False, "At least one applicable temporal scope excludes the observation date."
    elif scopes and all(item.get("validOnObservationDate") is True for item in scopes):
        overall_status, valid, reason = "verified", True, "Every applicable temporal scope includes the observation date."
    elif any(item.get("validOnObservationDate") is True for item in scopes):
        overall_status, valid, reason = "partial", None, "Some temporal scopes are verified while others lack conclusive periods."
    else:
        overall_status, valid, reason = "not_verified", None, "No applicable temporal scope could be conclusively verified."
    return {
        "observationDate": observation_date,
        "entity": entity,
        "security": security,
        "relationships": relationships,
        "overall": {"status": overall_status, "validOnObservationDate": valid, "reason": reason},
    }


def not_applicable(scope: str, reason: str) -> dict[str, Any]:
    return {"scope": scope, "status": "not_applicable", "validOnObservationDate": None, "periods": [], "reason": reason}
