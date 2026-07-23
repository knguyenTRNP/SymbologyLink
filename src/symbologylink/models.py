from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

EntityType = Literal["issuer", "legal_entity", "subsidiary", "brand", "facility"]
Status = Literal["matched", "review_required", "unmatched", "provider_error"]


@dataclass(slots=True)
class EntityMatchInput:
    recordId: str
    entityName: str | None = None
    legalName: str | None = None
    brandName: str | None = None
    domain: str | None = None
    ticker: str | None = None
    exchange: str | None = None
    cik: str | None = None
    lei: str | None = None
    figi: str | None = None
    isin: str | None = None
    cusip: str | None = None
    addressLine1: str | None = None
    city: str | None = None
    state: str | None = None
    postalCode: str | None = None
    country: str | None = None
    observationDate: str | None = None
    source: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    sourceRecord: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        from .normalize import (
            normalize_address,
            normalize_country,
            normalize_identifier,
            normalize_locality,
            normalize_null,
            normalize_postal_code,
            normalize_subdivision,
        )

        original_country = self.country

        for name in (
            "entityName", "legalName", "brandName", "domain", "ticker", "exchange",
            "cik", "lei", "figi", "isin", "cusip", "addressLine1", "city", "state",
            "postalCode", "country", "observationDate",
        ):
            setattr(self, name, normalize_null(getattr(self, name)))
        for name in ("ticker", "exchange", "cik", "lei", "figi", "isin", "cusip"):
            setattr(self, name, normalize_identifier(getattr(self, name), name))
        self.addressLine1 = normalize_address(self.addressLine1)
        self.city = normalize_locality(self.city)
        self.state = normalize_subdivision(self.state)
        self.postalCode = normalize_postal_code(self.postalCode)
        self.country = normalize_country(self.country)
        if normalize_null(original_country) is not None and self.country is None:
            self.metadata = dict(self.metadata)
            warnings = list(self.metadata.get("normalizationWarnings") or [])
            warning = f"Unsupported country value {original_country!r}; no country evidence was applied."
            if warning not in warnings:
                warnings.append(warning)
            self.metadata["normalizationWarnings"] = warnings


@dataclass(slots=True)
class MatchEvidence:
    type: str
    input: Any = None
    candidate: Any = None
    scoreContribution: float = 0
    provider: str | None = None
    similarity: float | None = None
    detail: str | None = None


@dataclass(slots=True)
class CandidateMatch:
    entityId: str
    canonicalName: str
    entityType: EntityType
    confidence: float
    provider: str
    security: dict[str, Any] | None = None
    publicParent: dict[str, Any] | None = None
    evidence: list[MatchEvidence] = field(default_factory=list)
    entityValidity: dict[str, Any] | None = None
    securityValidity: dict[str, Any] | None = None
    matchScore: float | None = None
    scoreIsCalibrated: bool = False
    primaryPathway: str = "unknown"

    def __post_init__(self) -> None:
        if self.matchScore is None:
            self.matchScore = self.confidence


@dataclass(slots=True)
class SecurityCandidateMatch:
    securityId: str
    issuerEntityId: str
    canonicalName: str
    confidence: float
    provider: str
    identifiers: dict[str, str] = field(default_factory=dict)
    security: dict[str, Any] = field(default_factory=dict)
    evidence: list[MatchEvidence] = field(default_factory=list)
    validity: dict[str, Any] | None = None
    matchScore: float | None = None
    scoreIsCalibrated: bool = False
    primaryPathway: str = "unknown"

    def __post_init__(self) -> None:
        if self.matchScore is None:
            self.matchScore = self.confidence


@dataclass(slots=True)
class ResolutionComponent:
    status: str
    canonical_id: str | None = None
    canonical_name: str | None = None
    match_score: float | None = None
    score_is_calibrated: bool = False
    match_pathway: str | None = None
    evidence: list[MatchEvidence] = field(default_factory=list)
    alternatives: list[Any] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        allowed = {"verified", "candidate", "ambiguous", "unknown", "contradicted", "not_applicable"}
        if self.status not in allowed:
            raise ValueError(f"Unsupported resolution component status: {self.status}")


@dataclass(slots=True)
class MatchResultV2:
    record_id: str
    entity: ResolutionComponent
    public_parent: ResolutionComponent
    security: ResolutionComponent
    final_decision: str
    temporal: dict[str, Any]
    observation_date: str | None
    mapping_version: str
    schema_version: str = "2.0"
    processed_at: str | None = None
    relationship_graph: dict[str, Any] | None = None
    review_reasons: list[str] = field(default_factory=list)
    decision_source: str | None = None
    decision_version: str | None = None
    source_record: dict[str, Any] = field(default_factory=dict)
    source_metadata: dict[str, Any] = field(default_factory=dict)
    processing_duration_ms: float | None = None
    input_file_sha256: str | None = None
    engine_version: str | None = None
    provider_versions: dict[str, str] = field(default_factory=dict)
    provider_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    mapping_content_sha256: str | None = None
    mapping_fingerprint_sha256: str | None = None

    def __post_init__(self) -> None:
        allowed = {
            "entity_and_security_matched", "entity_matched_security_unknown", "entity_matched_parent_candidate",
            "private_entity", "review_required", "ambiguous", "unmatched", "temporal_verification_required",
            "provider_error", "license_blocked",
        }
        if self.final_decision not in allowed:
            raise ValueError(f"Unsupported final decision: {self.final_decision}")
        if self.schema_version != "2.0":
            raise ValueError(f"Unsupported result schema version: {self.schema_version}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class EntityMatchResult:
    recordId: str
    status: Status
    confidence: float
    alternatives: list[CandidateMatch]
    evidence: list[MatchEvidence]
    mappingVersion: str
    processedAt: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    matchedEntity: dict[str, Any] | None = None
    matchedSecurity: dict[str, Any] | None = None
    securityDecisionStatus: str = "not_applicable"
    securityConfidence: float = 0
    securityAlternatives: list[SecurityCandidateMatch] = field(default_factory=list)
    securityEvidence: list[MatchEvidence] = field(default_factory=list)
    publicParent: dict[str, Any] | None = None
    parentStatus: str = "unknown"
    parentAlternatives: list[dict[str, Any]] = field(default_factory=list)
    parentEvidence: list[MatchEvidence] = field(default_factory=list)
    relationshipGraph: dict[str, Any] | None = None
    relationshipStatus: str = "not_resolved"
    validity: dict[str, Any] | None = None
    validOnObservationDate: bool | None = None
    pointInTimeStatus: str = "not_verified"
    pointInTimeReason: str | None = None
    decisionSource: str | None = None
    decisionVersion: str | None = None
    sourceRecord: dict[str, Any] = field(default_factory=dict)
    sourceMetadata: dict[str, Any] = field(default_factory=dict)
    processingDurationMs: float | None = None
    matchScore: float | None = None
    scoreIsCalibrated: bool = False
    primaryPathway: str = "unknown"
    securityMatchScore: float | None = None
    securityScoreIsCalibrated: bool = False
    securityPrimaryPathway: str = "unknown"
    entityDecisionStatus: str | None = None
    observationDate: str | None = None
    providerMetadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    mappingFingerprintSha256: str | None = None

    def __post_init__(self) -> None:
        if self.matchScore is None:
            self.matchScore = self.confidence
        if self.securityMatchScore is None:
            self.securityMatchScore = self.securityConfidence
        if self.entityDecisionStatus is None:
            self.entityDecisionStatus = self.status

    def to_dict(self) -> dict[str, Any]:
        from .result_schema import result_to_v2

        return result_to_v2(self).to_dict()

    def to_legacy_dict(self) -> dict[str, Any]:
        from .result_schema import result_to_legacy

        return result_to_legacy(self)


@dataclass(slots=True)
class MatchConfig:
    auto_match_threshold: float = 0.98
    review_threshold: float = 0.80
    max_candidates: int = 20
    mapping_version: str = "v1"
    relationship_max_depth: int = 8
    weights: dict[str, float] = field(default_factory=lambda: {
        "exact_identifier": 100,
        "ticker_exchange": 80,
        "exact_name": 50,
        "exact_domain": 45,
        "high_name_similarity": 35,
        "country_match": 10,
        "provider_agreement": 5,
        "address_match": 20,
        "city_match": 8,
        "state_match": 8,
        "postal_match": 15,
        "date_valid": 15,
        "entity_date_valid": 15,
        "security_date_valid": 10,
        "conflicting_identifier": -100,
        "date_invalid": -40,
        "entity_date_invalid": -40,
        "security_date_invalid": -60,
        "country_conflict": -15,
    })

    def __post_init__(self) -> None:
        if not 1 <= self.relationship_max_depth <= 32:
            raise ValueError("relationship_max_depth must be between 1 and 32.")
