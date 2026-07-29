from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from .models import EntityMatchResult, MatchEvidence, MatchResultV2, ResolutionComponent


SCHEMA_VERSION = "2.0"
ENTITY_CONFLICT_TYPES = {"identifier_conflict", "exact_name_identifier_conflict", "authoritative_provider_conflict"}
SECURITY_CONFLICT_TYPES = {"security_identifier_conflict", "security_candidate_ambiguity", "authoritative_security_provider_conflict"}
CONTRADICTION_TYPES = {"authoritative_provider_conflict", "authoritative_security_provider_conflict", "authoritative_parent_conflict"}
PARENT_EVIDENCE_TYPES = {
    "parent_verified", "parent_candidate", "parent_conflict", "authoritative_parent_conflict",
    "relationship_resolution", "relationship_reporting_exception", "relationship_resolution_error",
    "relationship_observation_date_validity", "brand_inference",
}
STRONG_SECURITY_DETAILS = {"figi", "isin", "cusip", "ticker and exchange"}


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    return value


def _evidence_dicts(values: list[Any] | None) -> list[dict[str, Any]]:
    return [dict(_plain(value)) for value in (values or [])]


def _evidence_objects(values: list[Any] | None) -> list[MatchEvidence]:
    objects = []
    for value in values or []:
        if isinstance(value, MatchEvidence):
            objects.append(value)
        else:
            objects.append(MatchEvidence(**{key: item for key, item in dict(value).items() if key in MatchEvidence.__dataclass_fields__}))
    return objects


def _component_status(status: str | None, evidence: list[dict[str, Any]], conflict_types: set[str]) -> str:
    if any(item.get("type") in CONTRADICTION_TYPES for item in evidence):
        return "contradicted"
    if status == "matched":
        return "verified"
    if status == "review_required":
        return "ambiguous" if any(item.get("type") in conflict_types for item in evidence) else "candidate"
    if status == "not_applicable":
        return "not_applicable"
    return "unknown"


def _temporal_scope(value: dict[str, Any] | None) -> dict[str, Any]:
    scope = dict(value or {})
    internal = scope.get("status") or "not_verified"
    status = {
        "verified": "verified",
        "invalid": "contradicted",
        "not_applicable": "not_applicable",
        "not_requested": "not_requested",
        "not_verified": "unknown",
        "partial": "unknown",
    }.get(internal, "unknown")
    return {
        "scope": scope.get("scope"),
        "status": status,
        "reason": scope.get("reason") or "No temporal evaluation was available for this scope.",
        "valid_on_observation_date": scope.get("validOnObservationDate"),
        "periods": list(scope.get("periods") or []),
        "internal_status": internal,
        "policy_action": scope.get("policyAction") or scope.get("policy_action"),
        "policy_reason": scope.get("policyReason") or scope.get("policy_reason"),
        "policy": dict(scope.get("policy") or {}),
    }


def _legacy_temporal_scope(value: dict[str, Any] | None) -> dict[str, Any]:
    scope = dict(value or {})
    status = scope.get("internal_status") or {
        "contradicted": "invalid",
        "unknown": "not_verified",
    }.get(scope.get("status"), scope.get("status") or "not_verified")
    return {
        "scope": scope.get("scope"),
        "status": status,
        "validOnObservationDate": scope.get("valid_on_observation_date"),
        "reason": scope.get("reason"),
        "periods": list(scope.get("periods") or []),
        "policyAction": scope.get("policy_action"),
        "policyReason": scope.get("policy_reason"),
        "policy": dict(scope.get("policy") or {}),
    }


def _final_decision(entity: ResolutionComponent, parent: ResolutionComponent, security: ResolutionComponent, legacy_status: str | None, temporal: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    if legacy_status == "provider_error":
        return "provider_error"
    if any(item.get("type") == "license_blocked" for item in evidence):
        return "license_blocked"
    overall_temporal = temporal.get("overall") or {}
    if overall_temporal.get("status") == "contradicted" or overall_temporal.get("policy_action") in {"review", "reject"}:
        return "temporal_verification_required"
    if entity.status == "unknown":
        return "unmatched"
    if "contradicted" in {entity.status, parent.status, security.status} or "ambiguous" in {entity.status, parent.status, security.status}:
        return "ambiguous"
    if entity.status != "verified":
        return "review_required"
    if parent.status == "candidate":
        return "entity_matched_parent_candidate"
    if security.status == "candidate":
        return "review_required"
    if security.status == "verified":
        return "entity_and_security_matched"
    if security.status == "not_applicable":
        return "private_entity"
    return "entity_matched_security_unknown"


def _review_reasons(components: list[ResolutionComponent], final_decision: str) -> list[str]:
    reasons: list[str] = []
    for component in components:
        for item in _evidence_dicts(component.evidence):
            if item.get("scoreContribution", 0) < 0 or item.get("type") in PARENT_EVIDENCE_TYPES | SECURITY_CONFLICT_TYPES:
                reason = item.get("detail") or item.get("type")
                if reason and reason not in reasons:
                    reasons.append(str(reason))
    if not reasons and final_decision in {"review_required", "ambiguous", "temporal_verification_required", "entity_matched_parent_candidate"}:
        reasons.append(final_decision.replace("_", " "))
    return reasons


def result_to_v2(result: EntityMatchResult) -> MatchResultV2:
    entity_evidence = _evidence_dicts(result.evidence)
    parent_evidence = _evidence_dicts(result.parentEvidence)
    security_evidence = _evidence_dicts(result.securityEvidence)
    entity_status = _component_status(result.entityDecisionStatus, entity_evidence, ENTITY_CONFLICT_TYPES)
    entity_value = dict(result.matchedEntity or {})
    entity_alternatives = [_plain(item) for item in result.alternatives]
    if entity_status == "ambiguous" and entity_value:
        entity_alternatives = [{
            "entityId": entity_value.get("entityId"),
            "canonicalName": entity_value.get("canonicalName"),
            "entityType": entity_value.get("entityType"),
            "confidence": result.matchScore,
            "primaryPathway": result.primaryPathway,
        }, *entity_alternatives]
    entity = ResolutionComponent(
        status=entity_status,
        canonical_id=None if entity_status in {"ambiguous", "unknown", "contradicted"} else entity_value.get("entityId"),
        canonical_name=None if entity_status in {"ambiguous", "unknown", "contradicted"} else entity_value.get("canonicalName"),
        match_score=result.matchScore,
        score_is_calibrated=result.scoreIsCalibrated,
        match_pathway=result.primaryPathway,
        evidence=_evidence_objects(entity_evidence),
        alternatives=entity_alternatives,
        attributes={"entity_type": entity_value.get("entityType"), "leading_candidate": entity_value or None},
    )

    parent_status = result.parentStatus if result.parentStatus in {"verified", "candidate", "ambiguous", "unknown", "contradicted", "not_applicable"} else "unknown"
    parent_value = dict(result.publicParent or {})
    parent = ResolutionComponent(
        status=parent_status,
        canonical_id=None if parent_status in {"ambiguous", "unknown", "contradicted", "not_applicable"} else parent_value.get("entityId"),
        canonical_name=None if parent_status in {"ambiguous", "unknown", "contradicted", "not_applicable"} else parent_value.get("canonicalName"),
        evidence=_evidence_objects(parent_evidence),
        alternatives=list(result.parentAlternatives),
        attributes={
            "relationship_type": (parent_value.get("relationshipTypes") or [parent_value.get("relationshipType")])[0],
            "relationship_source": ",".join(parent_value.get("sources") or []),
            "relationship_status": result.relationshipStatus,
            "selected_candidate": parent_value or None,
        },
    )

    security_status = _component_status(result.securityDecisionStatus, security_evidence, SECURITY_CONFLICT_TYPES)
    if result.securityDecisionStatus == "not_applicable" and entity_value.get("entityType") == "issuer":
        security_status = "unknown"
    security_value = dict(result.matchedSecurity or {})
    security_alternatives = [_plain(item) for item in result.securityAlternatives]
    leading_security = security_value
    if not leading_security and security_alternatives:
        first = security_alternatives[0]
        leading_security = dict(first.get("security") or first)
    if security_status == "ambiguous":
        security_id = security_name = None
    else:
        security_id = leading_security.get("securityId") or leading_security.get("internal_security_id")
        security_name = leading_security.get("canonicalName") or leading_security.get("securityDescription") or leading_security.get("name")
    security = ResolutionComponent(
        status=security_status,
        canonical_id=security_id if security_status not in {"unknown", "not_applicable", "contradicted"} else None,
        canonical_name=security_name if security_status not in {"unknown", "not_applicable", "contradicted"} else None,
        match_score=result.securityMatchScore,
        score_is_calibrated=result.securityScoreIsCalibrated,
        match_pathway=result.securityPrimaryPathway,
        evidence=_evidence_objects(security_evidence),
        alternatives=security_alternatives,
        attributes={**leading_security, "query_requested": result.securityDecisionStatus != "not_applicable"},
    )

    validity = result.validity or {}
    temporal = {
        "entity": _temporal_scope(validity.get("entity")),
        "public_parent": _temporal_scope(validity.get("relationships")),
        "security": _temporal_scope(validity.get("security")),
        "overall": _temporal_scope(validity.get("overall") or {
            "status": result.pointInTimeStatus,
            "reason": result.pointInTimeReason,
            "validOnObservationDate": result.validOnObservationDate,
        }),
    }
    final_decision = _final_decision(entity, parent, security, result.status, temporal, [*entity_evidence, *parent_evidence, *security_evidence])
    return MatchResultV2(
        record_id=result.recordId,
        entity=entity,
        public_parent=parent,
        security=security,
        final_decision=final_decision,
        temporal=temporal,
        observation_date=result.observationDate,
        mapping_version=result.mappingVersion,
        processed_at=result.processedAt,
        relationship_graph=result.relationshipGraph,
        review_reasons=_review_reasons([entity, parent, security], final_decision),
        decision_source=result.decisionSource,
        decision_version=result.decisionVersion,
        source_record=dict(result.sourceRecord),
        source_metadata=dict(result.sourceMetadata),
        processing_duration_ms=result.processingDurationMs,
        provider_metadata=dict(result.providerMetadata),
        mapping_fingerprint_sha256=result.mappingFingerprintSha256,
    )


def result_to_legacy(result: EntityMatchResult) -> dict[str, Any]:
    row = asdict(result)
    security_evidence = row.pop("securityEvidence", [])
    parent_evidence = row.get("parentEvidence", [])
    row["evidence"] = [*row.get("evidence", []), *parent_evidence, *security_evidence]
    row.pop("entityDecisionStatus", None)
    row.pop("observationDate", None)
    v2 = result_to_v2(result).to_dict()
    row["schemaCompatibility"] = {
        "sourceSchemaVersion": SCHEMA_VERSION,
        "componentStatuses": {
            "entity": v2["entity"]["status"],
            "public_parent": v2["public_parent"]["status"],
            "security": v2["security"]["status"],
        },
        "finalDecision": v2["final_decision"],
    }
    return row


def _split_legacy_evidence(row: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    entity, parent, security = [], _evidence_dicts(row.get("parentEvidence")), _evidence_dicts(row.get("securityEvidence"))
    for item in _evidence_dicts(row.get("evidence")):
        evidence_type = item.get("type") or ""
        if evidence_type.startswith("security_"):
            if item not in security:
                security.append(item)
        elif evidence_type in PARENT_EVIDENCE_TYPES or evidence_type.startswith("relationship_") or evidence_type.startswith("parent_"):
            if item not in parent:
                parent.append(item)
        else:
            entity.append(item)
    return entity, parent, security


def legacy_to_v2(row: dict[str, Any], *, conservative_migration: bool = True) -> MatchResultV2:
    if str(row.get("schema_version") or "").startswith("2"):
        return match_result_v2_from_dict(row)
    entity_evidence, parent_evidence, security_evidence = _split_legacy_evidence(row)
    marker = row.get("schemaCompatibility") or {}
    preserved = marker.get("componentStatuses") if marker.get("sourceSchemaVersion") == SCHEMA_VERSION else {}

    entity_value = dict(row.get("matchedEntity") or {})
    entity_status = preserved.get("entity") or _component_status(row.get("entityDecisionStatus") or row.get("status"), entity_evidence, ENTITY_CONFLICT_TYPES)
    entity = ResolutionComponent(
        status=entity_status,
        canonical_id=entity_value.get("entityId") if entity_status not in {"ambiguous", "unknown", "contradicted"} else None,
        canonical_name=entity_value.get("canonicalName") if entity_status not in {"ambiguous", "unknown", "contradicted"} else None,
        match_score=row.get("matchScore", row.get("confidence")),
        score_is_calibrated=False,
        match_pathway=row.get("primaryPathway"),
        evidence=_evidence_objects(entity_evidence),
        alternatives=list(row.get("alternatives") or []),
        attributes={"entity_type": entity_value.get("entityType"), "leading_candidate": entity_value or None},
    )

    parent_value = dict(row.get("publicParent") or {})
    if preserved.get("public_parent"):
        parent_status = preserved["public_parent"]
    elif parent_value:
        parent_status = "candidate" if conservative_migration else row.get("parentStatus", "candidate")
    elif row.get("parentStatus") == "not_applicable":
        parent_status = "not_applicable"
    else:
        parent_status = "unknown"
    parent = ResolutionComponent(
        status=parent_status,
        canonical_id=parent_value.get("entityId") if parent_status in {"candidate", "verified"} else None,
        canonical_name=parent_value.get("canonicalName") if parent_status in {"candidate", "verified"} else None,
        evidence=_evidence_objects(parent_evidence),
        alternatives=list(row.get("parentAlternatives") or ([parent_value] if parent_value else [])),
        attributes={
            "relationship_type": (parent_value.get("relationshipTypes") or [parent_value.get("relationshipType")])[0],
            "relationship_source": ",".join(parent_value.get("sources") or []),
            "relationship_status": row.get("relationshipStatus"),
            "selected_candidate": parent_value or None,
        },
    )

    security_value = dict(row.get("matchedSecurity") or {})
    alternatives = list(row.get("securityAlternatives") or [])
    strong_evidence = any(item.get("type") == "security_identifier_match" and item.get("detail") in STRONG_SECURITY_DETAILS for item in security_evidence)
    if preserved.get("security"):
        security_status = preserved["security"]
    elif security_value:
        security_status = "verified" if strong_evidence or (not conservative_migration and row.get("securityDecisionStatus") == "matched") else "candidate"
    else:
        security_status = _component_status(row.get("securityDecisionStatus"), security_evidence, SECURITY_CONFLICT_TYPES)
        if row.get("securityDecisionStatus") == "not_applicable" and entity_value.get("entityType") == "issuer":
            security_status = "unknown"
    leading_security = security_value
    if not leading_security and alternatives:
        leading_security = dict(alternatives[0].get("security") or alternatives[0])
    security = ResolutionComponent(
        status=security_status,
        canonical_id=(leading_security.get("securityId") or leading_security.get("internal_security_id")) if security_status not in {"ambiguous", "unknown", "not_applicable", "contradicted"} else None,
        canonical_name=(leading_security.get("canonicalName") or leading_security.get("securityDescription")) if security_status not in {"ambiguous", "unknown", "not_applicable", "contradicted"} else None,
        match_score=row.get("securityMatchScore", row.get("securityConfidence")),
        score_is_calibrated=False,
        match_pathway=row.get("securityPrimaryPathway"),
        evidence=_evidence_objects(security_evidence),
        alternatives=alternatives,
        attributes={**leading_security, "query_requested": row.get("securityDecisionStatus") != "not_applicable"},
    )

    validity = row.get("validity") or {}
    temporal = {
        "entity": _temporal_scope(validity.get("entity")),
        "public_parent": _temporal_scope(validity.get("relationships")),
        "security": _temporal_scope(validity.get("security")),
        "overall": _temporal_scope(validity.get("overall") or {
            "status": row.get("pointInTimeStatus"),
            "reason": row.get("pointInTimeReason"),
            "validOnObservationDate": row.get("validOnObservationDate"),
        }),
    }
    final_decision = marker.get("finalDecision") if preserved else _final_decision(entity, parent, security, row.get("status"), temporal, [*entity_evidence, *parent_evidence, *security_evidence])
    result = MatchResultV2(
        record_id=str(row.get("recordId") or row.get("record_id") or ""),
        entity=entity,
        public_parent=parent,
        security=security,
        final_decision=final_decision,
        temporal=temporal,
        observation_date=row.get("observationDate") or row.get("observation_date"),
        mapping_version=str(row.get("mappingVersion") or row.get("mapping_version") or "v1"),
        processed_at=row.get("processedAt"),
        relationship_graph=row.get("relationshipGraph"),
        decision_source=row.get("decisionSource"),
        decision_version=row.get("decisionVersion"),
        source_record=dict(row.get("sourceRecord") or {}),
        source_metadata=dict(row.get("sourceMetadata") or {}),
        processing_duration_ms=row.get("processingDurationMs"),
        input_file_sha256=row.get("inputFileSha256"),
        engine_version=row.get("engineVersion"),
        provider_versions=dict(row.get("providerVersions") or {}),
        provider_metadata=dict(row.get("providerMetadata") or {}),
        mapping_content_sha256=row.get("mappingContentSha256"),
        mapping_fingerprint_sha256=row.get("mappingFingerprintSha256"),
    )
    result.review_reasons = _review_reasons([entity, parent, security], result.final_decision)
    return result


def match_result_v2_from_dict(row: dict[str, Any]) -> MatchResultV2:
    def component(name: str) -> ResolutionComponent:
        value = dict(row.get(name) or {})
        value["evidence"] = _evidence_objects(value.get("evidence"))
        return ResolutionComponent(**{key: item for key, item in value.items() if key in ResolutionComponent.__dataclass_fields__})

    values = {key: item for key, item in row.items() if key in MatchResultV2.__dataclass_fields__ and key not in {"entity", "public_parent", "security"}}
    return MatchResultV2(entity=component("entity"), public_parent=component("public_parent"), security=component("security"), **values)


def v2_to_legacy(row: MatchResultV2 | dict[str, Any]) -> dict[str, Any]:
    value = row.to_dict() if isinstance(row, MatchResultV2) else dict(row)
    entity, parent, security = value["entity"], value["public_parent"], value["security"]
    entity_selected = {
        "entityId": entity.get("canonical_id"),
        "canonicalName": entity.get("canonical_name"),
        "entityType": (entity.get("attributes") or {}).get("entity_type"),
    } if entity.get("canonical_id") else (entity.get("attributes") or {}).get("leading_candidate")
    parent_selected = {
        **((parent.get("attributes") or {}).get("selected_candidate") or {}),
        "entityId": parent.get("canonical_id") or ((parent.get("attributes") or {}).get("selected_candidate") or {}).get("entityId"),
        "canonicalName": parent.get("canonical_name") or ((parent.get("attributes") or {}).get("selected_candidate") or {}).get("canonicalName"),
    } if parent.get("canonical_id") or (parent.get("attributes") or {}).get("selected_candidate") else None
    security_selected = dict(security.get("attributes") or {})
    security_selected.pop("query_requested", None)
    if security.get("canonical_id"):
        security_selected["securityId"] = security["canonical_id"]
        if security.get("canonical_name"):
            security_selected["canonicalName"] = security["canonical_name"]

    final = value.get("final_decision")
    query_requested = (security.get("attributes") or {}).get("query_requested", False)
    if final == "provider_error":
        status = "provider_error"
    elif final == "unmatched":
        status = "unmatched"
    elif final in {"ambiguous", "review_required", "temporal_verification_required", "entity_matched_parent_candidate", "license_blocked"}:
        status = "review_required"
    elif final == "entity_matched_security_unknown" and query_requested:
        status = "review_required"
    else:
        status = "matched"
    security_status = {
        "verified": "matched", "candidate": "review_required", "ambiguous": "review_required",
        "contradicted": "review_required", "unknown": "unmatched", "not_applicable": "not_applicable",
    }.get(security.get("status"), "unmatched")
    evidence = [*_evidence_dicts(entity.get("evidence")), *_evidence_dicts(parent.get("evidence")), *_evidence_dicts(security.get("evidence"))]
    temporal = value.get("temporal") or {}
    validity = {
        "entity": _legacy_temporal_scope(temporal.get("entity")),
        "security": _legacy_temporal_scope(temporal.get("security")),
        "relationships": _legacy_temporal_scope(temporal.get("public_parent")),
        "overall": _legacy_temporal_scope(temporal.get("overall")),
    }
    return {
        "recordId": value.get("record_id"),
        "status": status,
        "confidence": entity.get("match_score") or 0,
        "alternatives": list(entity.get("alternatives") or []),
        "evidence": evidence,
        "mappingVersion": value.get("mapping_version"),
        "processedAt": value.get("processed_at"),
        "matchedEntity": entity_selected if entity.get("status") != "unknown" else None,
        "matchedSecurity": security_selected if security.get("status") == "verified" else None,
        "securityDecisionStatus": security_status,
        "securityConfidence": security.get("match_score") or 0,
        "securityAlternatives": list(security.get("alternatives") or []),
        "publicParent": parent_selected,
        "parentStatus": parent.get("status"),
        "parentAlternatives": list(parent.get("alternatives") or []),
        "parentEvidence": _evidence_dicts(parent.get("evidence")),
        "relationshipGraph": value.get("relationship_graph"),
        "relationshipStatus": (parent.get("attributes") or {}).get("relationship_status") or "not_resolved",
        "validity": validity,
        "validOnObservationDate": (temporal.get("overall") or {}).get("valid_on_observation_date"),
        "pointInTimeStatus": validity["overall"]["status"],
        "pointInTimeReason": validity["overall"]["reason"],
        "decisionSource": value.get("decision_source"),
        "decisionVersion": value.get("decision_version"),
        "sourceRecord": dict(value.get("source_record") or {}),
        "sourceMetadata": dict(value.get("source_metadata") or {}),
        "processingDurationMs": value.get("processing_duration_ms"),
        "inputFileSha256": value.get("input_file_sha256"),
        "engineVersion": value.get("engine_version"),
        "providerVersions": dict(value.get("provider_versions") or {}),
        "providerMetadata": dict(value.get("provider_metadata") or {}),
        "mappingContentSha256": value.get("mapping_content_sha256"),
        "mappingFingerprintSha256": value.get("mapping_fingerprint_sha256"),
        "matchScore": entity.get("match_score"),
        "scoreIsCalibrated": entity.get("score_is_calibrated", False),
        "primaryPathway": entity.get("match_pathway") or "unknown",
        "securityMatchScore": security.get("match_score"),
        "securityScoreIsCalibrated": security.get("score_is_calibrated", False),
        "securityPrimaryPathway": security.get("match_pathway") or "unknown",
        "schemaCompatibility": {
            "sourceSchemaVersion": SCHEMA_VERSION,
            "componentStatuses": {
                "entity": entity.get("status"), "public_parent": parent.get("status"), "security": security.get("status"),
            },
            "finalDecision": final,
        },
    }
