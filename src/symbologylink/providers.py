from __future__ import annotations

import json
import hashlib
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .cache import SQLiteCache
from .ingest import read_records
from .models import EntityMatchInput
from .normalize import (
    normalize_address,
    normalize_country,
    normalize_domain,
    normalize_identifier,
    normalize_locality,
    normalize_name,
    normalize_postal_code,
    normalize_subdivision,
)
from .validity import period, validate_periods


class TrustLevel(str, Enum):
    EXPERIMENTAL = "experimental"
    SUPPORTING = "supporting"
    AUTHORITATIVE = "authoritative"


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Claims a provider is allowed to make during resolution."""

    entity_lookup: bool = False
    security_lookup: bool = False
    identifier_mapping: tuple[str, ...] = ()
    lei: bool = False
    current_entity_data: bool = False
    current_security_data: bool = False
    current_relationships: bool = False
    entity_effective_dates: bool = False
    security_effective_dates: bool = False
    relationship_effective_dates: bool = False
    share_class_data: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_lookup": self.entity_lookup,
            "security_lookup": self.security_lookup,
            "identifier_mapping": list(self.identifier_mapping),
            "lei": self.lei,
            "current_entity_data": self.current_entity_data,
            "current_security_data": self.current_security_data,
            "current_relationships": self.current_relationships,
            "entity_effective_dates": self.entity_effective_dates,
            "security_effective_dates": self.security_effective_dates,
            "relationship_effective_dates": self.relationship_effective_dates,
            "share_class_data": self.share_class_data,
        }

    def enabled(self) -> list[str]:
        values = self.to_dict()
        return [name for name, enabled in values.items() if enabled]

    @classmethod
    def from_customer_master_columns(cls, columns: set[str]) -> ProviderCapabilities:
        identifiers = tuple(sorted(columns & {"cik", "lei", "ticker", "exchange", "figi", "isin", "cusip"}))
        return cls(
            entity_lookup=bool(columns & {"internal_entity_id", "canonical_name", "cik", "lei"}),
            security_lookup=bool(columns & {"internal_security_id", "ticker", "figi", "isin", "cusip"}),
            identifier_mapping=identifiers,
            lei="lei" in columns,
            current_entity_data=bool(columns & {"canonical_name", "entity_type", "domain", "country"}),
            current_security_data=bool(columns & {"internal_security_id", "ticker", "exchange", "figi", "isin", "cusip"}),
            current_relationships=bool(columns & {"parent_entity_id", "parent_name", "relationship_type"}),
            entity_effective_dates=bool(columns & {"entity_valid_from", "entity_valid_to", "entity_periods", "valid_from", "valid_to"}),
            security_effective_dates=bool(columns & {"security_valid_from", "security_valid_to", "security_periods", "listing_valid_from", "listing_valid_to"}),
            relationship_effective_dates=bool(columns & {"relationship_valid_from", "relationship_valid_to", "relationship_periods"}),
            share_class_data=bool(columns & {"share_class", "share_class_figi", "shareClassFIGI"}),
        )


def provider_metadata_map(providers: list[MatchProvider]) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    for provider in providers:
        if hasattr(provider, "metadata"):
            values[provider.name] = provider.metadata()
            continue
        capabilities = getattr(provider, "capabilities", ProviderCapabilities())
        trust = getattr(provider, "trust_level", TrustLevel.EXPERIMENTAL)
        trust_value = trust.value if isinstance(trust, TrustLevel) else str(trust)
        values[provider.name] = {
            "provider": provider.name,
            "trust_level": trust_value,
            "capabilities": capabilities.to_dict(),
            "enabled_capabilities": capabilities.enabled(),
        }
    return values


def provider_configuration_fingerprint(mapping_version: str, mapping_content_sha256: str | None, metadata: dict[str, dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps({
        "mappingVersion": mapping_version,
        "mappingContentSha256": mapping_content_sha256,
        "providers": metadata,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def source_trust_level(source: str | None, metadata: dict[str, dict[str, Any]] | None = None) -> str:
    value = source or "unknown"
    if value == "human_override" or value.startswith("override:") or value.startswith("rule:"):
        return TrustLevel.AUTHORITATIVE.value
    entry = (metadata or {}).get(value) or {}
    trust = entry.get("trust_level") or entry.get("trustLevel") or TrustLevel.EXPERIMENTAL.value
    return trust.value if isinstance(trust, TrustLevel) else str(trust)


def source_rank(source: str | None, metadata: dict[str, dict[str, Any]] | None = None) -> int:
    value = source or "unknown"
    if value == "human_override" or value.startswith("override:"):
        return 0
    if value.startswith("rule:"):
        return 1
    return {
        TrustLevel.AUTHORITATIVE.value: 2,
        TrustLevel.SUPPORTING.value: 3,
        TrustLevel.EXPERIMENTAL.value: 4,
    }.get(source_trust_level(value, metadata), 5)


def _period_values(value: Any, provider: str, source_record: int, source_file: str) -> list[dict[str, Any]]:
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid validity-period JSON on source row {source_record}: {exc.msg}") from exc
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"Validity periods on source row {source_record} must be a JSON array of objects.")
    return [{**item, "provider": item.get("provider") or provider, "sourceRecord": item.get("sourceRecord") or source_record, "sourceFile": item.get("sourceFile") or source_file} for item in value]


@dataclass(slots=True)
class ProviderCandidate:
    entity_id: str
    canonical_name: str
    entity_type: str = "issuer"
    provider: str = "unknown"
    domain: str | None = None
    country: str | None = None
    address_line1: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    identifiers: dict[str, str] = field(default_factory=dict)
    security: dict[str, Any] | None = None
    public_parent: dict[str, Any] | None = None
    relationships: list[dict[str, Any]] = field(default_factory=list)
    valid_from: str | None = None
    valid_to: str | None = None
    entity_periods: list[dict[str, Any]] = field(default_factory=list)
    security_periods: list[dict[str, Any]] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProviderSecurityCandidate:
    """A provider instrument result, independent from its issuer entity result."""

    security_id: str
    issuer_entity_id: str
    canonical_name: str
    provider: str = "unknown"
    identifiers: dict[str, str] = field(default_factory=dict)
    security: dict[str, Any] = field(default_factory=dict)
    issuer_identifiers: dict[str, str] = field(default_factory=dict)
    issuer_name: str | None = None
    validity_periods: list[dict[str, Any]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProviderSearchResult:
    entities: list[ProviderCandidate] = field(default_factory=list)
    securities: list[ProviderSecurityCandidate] = field(default_factory=list)


class MatchProvider(ABC):
    name: str
    capabilities = ProviderCapabilities()
    trust_level = TrustLevel.EXPERIMENTAL

    @abstractmethod
    def search(self, input_record: EntityMatchInput, limit: int = 20) -> list[ProviderCandidate]: ...

    def search_batch(self, records: list[EntityMatchInput], limit: int = 20) -> dict[str, list[ProviderCandidate]]:
        return {record.recordId: self.search(record, limit) for record in records}

    def search_bundle_batch(self, records: list[EntityMatchInput], limit: int = 20) -> dict[str, ProviderSearchResult]:
        """Return entity and instrument candidates without making duplicate provider calls.

        Existing third-party providers remain compatible: their legacy security payloads
        are normalized into first-class security candidates here.
        """
        entity_results = self.search_batch(records, limit)
        return {
            record.recordId: ProviderSearchResult(
                entities=entity_results.get(record.recordId, []),
                securities=[
                    security
                    for entity in entity_results.get(record.recordId, [])
                    for security in self._security_candidates(entity)
                ],
            )
            for record in records
        }

    @staticmethod
    def _security_candidates(entity: ProviderCandidate) -> list[ProviderSecurityCandidate]:
        payload = entity.security or {}
        if not payload:
            return []
        listings = payload.get("listings") if isinstance(payload.get("listings"), list) else []
        instruments = [{**payload, **listing} for listing in listings if isinstance(listing, dict)] or [payload]
        results: list[ProviderSecurityCandidate] = []
        for instrument in instruments:
            identifiers = {
                key: normalize_identifier(instrument.get(key) or entity.identifiers.get(key), key)
                for key in ("figi", "isin", "cusip", "ticker", "exchange")
                if instrument.get(key) or entity.identifiers.get(key)
            }
            explicit_id = instrument.get("internal_security_id") or instrument.get("securityId") or identifiers.get("figi")
            fingerprint = "|".join(str(identifiers.get(key) or "") for key in ("figi", "isin", "cusip", "ticker", "exchange"))
            security_id = str(explicit_id or f"{entity.provider}:security:{hashlib.sha256((entity.entity_id + '|' + fingerprint).encode()).hexdigest()[:16]}")
            name = str(instrument.get("securityDescription") or instrument.get("name") or f"{entity.canonical_name} {identifiers.get('ticker') or 'security'}")
            security_payload = {key: value for key, value in instrument.items() if key != "listings"}
            results.append(ProviderSecurityCandidate(
                security_id=security_id,
                issuer_entity_id=entity.entity_id,
                canonical_name=name,
                provider=entity.provider,
                identifiers=identifiers,
                security=security_payload,
                issuer_identifiers={key: value for key, value in entity.identifiers.items() if key in {"cik", "lei"}},
                issuer_name=entity.canonical_name,
                validity_periods=list(entity.security_periods),
                sources=list(entity.sources or [entity.provider]),
            ))
        return results

    def metadata(self) -> dict[str, Any]:
        trust = self.trust_level.value if isinstance(self.trust_level, TrustLevel) else str(self.trust_level)
        return {
            "provider": self.name,
            "trust_level": trust,
            "capabilities": self.capabilities.to_dict(),
            "enabled_capabilities": self.capabilities.enabled(),
        }

    def has_capability(self, capability: str) -> bool:
        return bool(getattr(self.capabilities, capability, False))

    def health_check(self) -> dict[str, Any]:
        return {**self.metadata(), "status": "available"}

    def resolve_relationships(self, candidate: ProviderCandidate, observation_date: str | None = None, max_depth: int = 8) -> dict[str, Any] | None:
        """Return a provider-specific child-to-parent graph for a resolved candidate."""
        return None


class ProviderError(RuntimeError):
    pass


class _RateLimiter:
    def __init__(self, minimum_interval: float):
        self.minimum_interval = minimum_interval
        self._last_request = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            remaining = self.minimum_interval - (time.monotonic() - self._last_request)
            if remaining > 0:
                time.sleep(remaining)
            self._last_request = time.monotonic()


class LocalSecurityMasterProvider(MatchProvider):
    """Customer CSV or Parquet security master."""

    name = "customer_security_master"
    trust_level = TrustLevel.AUTHORITATIVE

    def __init__(self, path: str | Path, name: str | None = None):
        self.path = Path(path)
        if name:
            self.name = name
        source_rows = list(read_records(self.path))
        columns = {str(column) for row in source_rows for column in row}
        self.capabilities = ProviderCapabilities.from_customer_master_columns(columns)
        self.candidates = self._load(source_rows)

    def _load(self, source_rows: list[dict[str, Any]]) -> list[ProviderCandidate]:
        rows: list[ProviderCandidate] = []
        for index, row in enumerate(source_rows, 1):
            identifiers = {k: normalize_identifier(row.get(k), k) for k in ("ticker", "exchange", "cik", "lei", "figi", "isin", "cusip") if row.get(k)}
            entity_valid_from = str(row.get("entity_valid_from") or row.get("valid_from") or "") or None
            entity_valid_to = str(row.get("entity_valid_to") or row.get("valid_to") or "") or None
            entity_periods = _period_values(row.get("entity_periods"), self.name, index, self.path.name) or ([period("entity_existence", entity_valid_from, entity_valid_to, row.get("entity_status"), self.name, sourceRecord=index, sourceFile=self.path.name)] if entity_valid_from or entity_valid_to or row.get("entity_status") else [])
            parent_id = str(row.get("parent_entity_id") or "") or None
            parent_name = str(row.get("parent_name") or "") or None
            relationship_type = row.get("relationship_type") or "subsidiary_of"
            relationship_valid_from = str(row.get("relationship_valid_from") or "") or None
            relationship_valid_to = str(row.get("relationship_valid_to") or "") or None
            security = {k: row[k] for k in ("internal_security_id", "ticker", "exchange", "figi", "isin", "cusip", "share_class") if row.get(k)} or None
            security_valid_from = str(row.get("security_valid_from") or row.get("listing_valid_from") or "") or None
            security_valid_to = str(row.get("security_valid_to") or row.get("listing_valid_to") or "") or None
            security_periods = _period_values(row.get("security_periods"), self.name, index, self.path.name) or ([period("security_listing", security_valid_from, security_valid_to, row.get("security_status") or row.get("listing_status"), self.name, sourceRecord=index, sourceFile=self.path.name, securityId=(security or {}).get("internal_security_id"))] if security and (security_valid_from or security_valid_to or row.get("security_status") or row.get("listing_status")) else [])
            relationship_periods = _period_values(row.get("relationship_periods"), self.name, index, self.path.name) or ([period(relationship_type, relationship_valid_from, relationship_valid_to, row.get("relationship_status") or "ACTIVE", self.name, sourceRecord=index, sourceFile=self.path.name)] if relationship_valid_from or relationship_valid_to or row.get("relationship_status") else [])
            if security_periods and not security:
                raise ValueError(f"Source row {index} supplies security periods without a security.")
            if relationship_periods and not (parent_id or parent_name):
                raise ValueError(f"Source row {index} supplies relationship periods without a parent.")
            relationship = {
                "fromEntityId": str(row.get("internal_entity_id") or f"{self.name}:{index}"),
                "toEntityId": parent_id,
                "toEntity": {"entityId": parent_id, "canonicalName": parent_name, "entityType": row.get("parent_entity_type") or "legal_entity"},
                "relationshipType": relationship_type,
                "validFrom": relationship_valid_from,
                "validTo": relationship_valid_to,
                "periods": relationship_periods,
                "status": row.get("relationship_status") or "ACTIVE",
                "provider": self.name,
                "provenance": [{"provider": self.name, "sourceRecord": index, "sourceFile": self.path.name}],
            } if parent_id or parent_name else None
            rows.append(ProviderCandidate(
                entity_id=str(row.get("internal_entity_id") or f"{self.name}:{index}"),
                canonical_name=str(row.get("canonical_name") or ""),
                entity_type=row.get("entity_type") or "issuer",
                provider=self.name,
                domain=normalize_domain(row.get("domain")), country=normalize_country(row.get("country")),
                address_line1=normalize_address(row.get("address_line1") or row.get("address")),
                city=normalize_locality(row.get("city")),
                state=normalize_subdivision(row.get("state") or row.get("region")),
                postal_code=normalize_postal_code(row.get("postal_code")), identifiers=identifiers,
                security=security,
                public_parent={"entityId": parent_id, "canonicalName": parent_name, "entityType": row.get("parent_entity_type") or "legal_entity", "relationshipType": relationship_type, "validFrom": relationship_valid_from, "validTo": relationship_valid_to} if relationship else None,
                relationships=[relationship] if relationship else [],
                valid_from=entity_valid_from, valid_to=entity_valid_to,
                entity_periods=entity_periods, security_periods=security_periods,
                aliases=[x.strip() for x in str(row.get("aliases") or "").split("|") if x.strip()],
            ))
        by_id = {candidate.entity_id: candidate for candidate in rows}
        for candidate in rows:
            for relationship in candidate.relationships:
                parent = by_id.get(relationship.get("toEntityId"))
                if parent:
                    relationship["toEntity"] = {
                        "entityId": parent.entity_id, "canonicalName": parent.canonical_name, "entityType": parent.entity_type,
                        "identifiers": dict(parent.identifiers), "providers": [self.name],
                    }
                    candidate.public_parent = {**relationship["toEntity"], "relationshipType": relationship["relationshipType"], "validFrom": relationship["validFrom"], "validTo": relationship["validTo"]}
        self._validate(rows)
        return rows

    @staticmethod
    def _validate(rows: list[ProviderCandidate]) -> None:
        entity_ids: dict[str, tuple[Any, ...]] = {}
        entity_only_rows: set[str] = set()
        security_ids: set[str] = set()
        identifiers: dict[tuple[str, str], str] = {}
        for row in rows:
            if not row.canonical_name:
                raise ValueError(f"Security-master entity {row.entity_id} is missing canonical_name.")
            signature = (normalize_name(row.canonical_name), row.entity_type, row.identifiers.get("cik"), row.identifiers.get("lei"))
            if row.entity_id in entity_ids and entity_ids[row.entity_id] != signature:
                raise ValueError(f"Conflicting entity data for internal_entity_id: {row.entity_id}")
            entity_ids[row.entity_id] = signature
            security_key = next((str((row.security or {}).get(field)) for field in ("internal_security_id", "figi", "isin", "cusip") if (row.security or {}).get(field)), None)
            if not security_key and row.security:
                security_key = f"{row.entity_id}|{row.identifiers.get('ticker') or ''}|{row.identifiers.get('exchange') or ''}"
            if security_key:
                if security_key in security_ids:
                    raise ValueError(f"Duplicate security identifier: {security_key}")
                security_ids.add(security_key)
            elif row.entity_id in entity_only_rows:
                raise ValueError(f"Duplicate entity-only row for internal_entity_id: {row.entity_id}")
            else:
                entity_only_rows.add(row.entity_id)
            validate_periods(row.entity_periods, f"Entity {row.entity_id}")
            validate_periods(row.security_periods, f"Security on {row.entity_id}")
            validate_periods(row.relationships, f"Relationships on {row.entity_id}")
            for relationship in row.relationships:
                validate_periods(relationship.get("periods"), f"Relationship periods on {row.entity_id}")
            for field in ("lei", "figi", "isin", "cusip"):
                value = row.identifiers.get(field)
                key = (field, value) if value else None
                duplicate_is_invalid = field in {"figi", "isin", "cusip"} or (key and identifiers.get(key) != row.entity_id)
                if key and key in identifiers and duplicate_is_invalid:
                    raise ValueError(f"Duplicate {field.upper()} {value} on {identifiers[key]} and {row.entity_id}.")
                if key:
                    identifiers[key] = row.entity_id

    def search(self, input_record: EntityMatchInput, limit: int = 20) -> list[ProviderCandidate]:
        needles = {normalize_identifier(getattr(input_record, field), field) for field in ("ticker", "cik", "lei", "figi", "isin", "cusip") if getattr(input_record, field)}
        name = normalize_name(input_record.entityName or input_record.legalName or input_record.brandName)
        domain = normalize_domain(input_record.domain)
        ranked = []
        for candidate in self.candidates:
            candidate_ids = set(candidate.identifiers.values())
            candidate_names = [normalize_name(candidate.canonical_name), *(normalize_name(a) for a in candidate.aliases)]
            priority = 0 if needles & candidate_ids else 1 if domain and domain == candidate.domain else 2 if name in candidate_names else 3
            if priority < 3 or (name and any(name.split()[0] in (n or "").split() for n in candidate_names)):
                ranked.append((priority, candidate))
        return [item[1] for item in sorted(ranked, key=lambda item: item[0])[:limit]]

    def resolve_relationships(self, candidate: ProviderCandidate, observation_date: str | None = None, max_depth: int = 8) -> dict[str, Any] | None:
        from .relationships import candidate_node, valid_on_date
        from .validity import evaluate_periods

        by_id = {item.entity_id: item for item in self.candidates}
        current = by_id.get(candidate.entity_id)
        if not current:
            for item in self.candidates:
                if any(value and value == item.identifiers.get(key) for key, value in candidate.identifiers.items() if key in {"lei", "cik"}):
                    current = item
                    break
        if not current:
            return None
        subject = candidate_node(current)
        nodes, edges, errors = [subject], [], []
        visited = {current.entity_id}
        complete = True
        for depth in range(max_depth):
            if not current.relationships:
                break
            relationship = current.relationships[0]
            parent_id = relationship.get("toEntityId")
            parent = by_id.get(parent_id)
            parent_node = candidate_node(parent) if parent else relationship.get("toEntity") or {"entityId": parent_id, "canonicalName": relationship.get("parentName") or parent_id, "entityType": "legal_entity", "identifiers": {}, "providers": [self.name]}
            if parent_node not in nodes:
                nodes.append(parent_node)
            valid_from, valid_to = relationship.get("validFrom"), relationship.get("validTo")
            relationship_periods = relationship.get("periods") or []
            evaluated = evaluate_periods("relationships", relationship_periods, observation_date) if relationship_periods else None
            edges.append({
                "fromEntityId": current.entity_id, "toEntityId": parent_node["entityId"],
                "relationshipType": relationship.get("relationshipType") or "subsidiary_of", "level": "direct",
                "status": relationship.get("status") or "ACTIVE", "validFrom": valid_from, "validTo": valid_to,
                "periods": relationship_periods,
                "validOnObservationDate": evaluated["validOnObservationDate"] if evaluated else valid_on_date(observation_date, valid_from, valid_to),
                "providers": [self.name], "provenance": relationship.get("provenance") or [{"provider": self.name}],
            })
            if parent_node["entityId"] in visited:
                errors.append(f"Customer-master relationship cycle detected at {parent_node['entityId']}.")
                complete = False
                break
            visited.add(parent_node["entityId"])
            if not parent:
                errors.append(f"Parent {parent_node['entityId']} is referenced but not defined in the customer master.")
                complete = False
                break
            current = parent
        else:
            if current.relationships:
                errors.append(f"Customer-master relationship traversal stopped at max depth {max_depth}.")
                complete = False
        return {"provider": self.name, "subject": subject, "nodes": nodes, "edges": edges, "reportingExceptions": [], "complete": complete, "errors": errors}


class GLEIFProvider(MatchProvider):
    """GLEIF JSON:API connector for LEIs, legal names, addresses, and aliases."""

    name = "gleif"
    trust_level = TrustLevel.SUPPORTING
    capabilities = ProviderCapabilities(
        entity_lookup=True,
        identifier_mapping=("lei",),
        lei=True,
        current_entity_data=True,
        current_relationships=True,
        relationship_effective_dates=True,
    )
    base_url = "https://api.gleif.org/api/v1"

    def __init__(self, cache: SQLiteCache | None = None, timeout: float = 15, retries: int = 2, offline: bool = False, user_agent: str = "SymbologyLink/0.0.0"):
        self.cache = cache
        self.timeout = timeout
        self.retries = retries
        self.offline = offline
        self.user_agent = user_agent

    def _request(self, path: str, params: dict[str, str], query_type: str) -> dict[str, Any]:
        cache_key = json.dumps({"path": path, "params": params}, sort_keys=True, separators=(",", ":"))
        if self.cache:
            cached = self.cache.get(self.name, query_type, cache_key, allow_stale=self.offline)
            if cached is not None:
                return cached
        if self.offline:
            raise ProviderError("GLEIF offline mode has no cached response for this query.")
        url = f"{self.base_url}{path}?{urlencode(params)}"
        request = Request(url, headers={"Accept": "application/vnd.api+json", "User-Agent": self.user_agent})
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if self.cache:
                    self.cache.set(self.name, query_type, cache_key, payload, ttl_seconds=7 * 86400)
                return payload
            except HTTPError as exc:
                last_error = exc
                if exc.code == 404:
                    payload = {"data": None}
                    if self.cache:
                        self.cache.set(self.name, query_type, cache_key, payload, ttl_seconds=7 * 86400)
                    return payload
                if exc.code not in {429, 500, 502, 503, 504} or attempt == self.retries:
                    break
                retry_after = exc.headers.get("Retry-After")
                time.sleep(min(float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt, 5))
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt == self.retries:
                    break
                time.sleep(min(2 ** attempt, 5))
        raise ProviderError(f"GLEIF request failed: {last_error}")

    @staticmethod
    def _candidate(record: dict[str, Any]) -> ProviderCandidate:
        attributes = record.get("attributes") or {}
        entity = attributes.get("entity") or {}
        registration = attributes.get("registration") or {}
        legal_name = entity.get("legalName") or {}
        address = entity.get("legalAddress") or {}
        other_names = entity.get("otherNames") or []
        lei = attributes.get("lei") or record.get("id") or ""
        expiration_value = entity.get("expiration") or {}
        expiration = (entity.get("expirationDate") or (expiration_value.get("date") if isinstance(expiration_value, dict) else None)) if entity.get("status") in {"INACTIVE", "ANNULLED"} else None
        return ProviderCandidate(
            entity_id=f"gleif:{lei}",
            canonical_name=legal_name.get("name") or lei,
            entity_type="legal_entity",
            provider="gleif",
            country=normalize_country(address.get("country")),
            address_line1=normalize_address((address.get("addressLines") or [None])[0]),
            city=normalize_locality(address.get("city")),
            state=normalize_subdivision(address.get("region")),
            postal_code=normalize_postal_code(address.get("postalCode")),
            identifiers={"lei": normalize_identifier(lei)} if lei else {},
            # LEI registration is not legal-entity inception; only entity dates are used for point-in-time claims.
            valid_from=(entity.get("creationDate") or "")[:10] or None,
            valid_to=(expiration or "")[:10] or None,
            entity_periods=[period("entity_existence", (entity.get("creationDate") or "")[:10] or None, (expiration or "")[:10] or None, entity.get("status"), "gleif", sourceRecord=record.get("id"))] if entity.get("creationDate") or expiration else [],
            aliases=[item.get("name") for item in other_names if item.get("name")],
        )

    def search(self, input_record: EntityMatchInput, limit: int = 20) -> list[ProviderCandidate]:
        if input_record.lei:
            payload = self._request("/lei-records", {"filter[lei]": normalize_identifier(input_record.lei) or "", "page[size]": str(min(limit, 100))}, "identifier")
        else:
            name = input_record.legalName or input_record.entityName or input_record.brandName
            if not name:
                return []
            params = {"filter[fulltext]": name, "page[size]": str(min(limit, 100))}
            if input_record.country:
                params["filter[entity.legalAddress.country]"] = normalize_country(input_record.country) or ""
            payload = self._request("/lei-records", params, "name")
            if not payload.get("data"):
                broader_name = normalize_name(name) or name
                broader_params = {"filter[fulltext]": broader_name, "page[size]": str(min(limit, 100))}
                payload = self._request("/lei-records", broader_params, "name")
        data = payload.get("data") or []
        if isinstance(data, dict):
            data = [data]
        return [self._candidate(item) for item in data[:limit]]

    @staticmethod
    def _items(payload: dict[str, Any]) -> list[dict[str, Any]]:
        data = payload.get("data")
        if not data:
            return []
        return data if isinstance(data, list) else [data]

    @staticmethod
    def _relationship_edge(record: dict[str, Any], fallback_child: str, level: str, observation_date: str | None) -> dict[str, Any]:
        from .relationships import valid_on_date

        attributes = record.get("attributes") or {}
        relationship = attributes.get("relationship") or attributes
        start = relationship.get("startNode") or relationship.get("start_node") or {}
        end = relationship.get("endNode") or relationship.get("end_node") or {}
        periods = relationship.get("relationshipPeriods") or relationship.get("periods") or []
        relationship_period = next((item for item in periods if (item.get("periodType") or item.get("type")) == "RELATIONSHIP_PERIOD"), periods[0] if periods else {})
        valid_from = (relationship_period.get("startDate") or "")[:10] or None
        valid_to = (relationship_period.get("endDate") or "")[:10] or None
        registration = attributes.get("registration") or {}
        relationship_type = relationship.get("relationshipType") or relationship.get("type") or ("IS_ULTIMATELY_CONSOLIDATED_BY" if level == "ultimate" else "IS_DIRECTLY_CONSOLIDATED_BY")
        child_id = start.get("id") or start.get("nodeId") or fallback_child
        parent_id = end.get("id") or end.get("nodeId") or ""
        return {
            "fromEntityId": f"gleif:{child_id}" if not str(child_id).startswith("gleif:") else str(child_id),
            "toEntityId": f"gleif:{parent_id}" if parent_id and not str(parent_id).startswith("gleif:") else str(parent_id),
            "relationshipType": relationship_type,
            "level": level,
            "status": relationship.get("relationshipStatus") or relationship.get("status") or registration.get("status") or "ACTIVE",
            "validFrom": valid_from,
            "validTo": valid_to,
            "periods": [period("RELATIONSHIP_PERIOD", valid_from, valid_to, relationship.get("relationshipStatus") or relationship.get("status"), "gleif", sourceRecord=record.get("id"), relationshipType=relationship_type)] if valid_from or valid_to else [],
            "validOnObservationDate": valid_on_date(observation_date, valid_from, valid_to),
            "providers": ["gleif"],
            "provenance": [{
                "provider": "gleif", "sourceRecord": record.get("id"),
                "corroborationLevel": registration.get("corroborationLevel") or registration.get("validationSources") or registration.get("validation_source"),
                "corroborationDocuments": registration.get("corroborationDocuments"),
                "lastUpdated": registration.get("lastUpdateDate"),
                "recordValidFrom": attributes.get("validFrom"), "recordValidTo": attributes.get("validTo"),
            }],
        }

    @staticmethod
    def _reporting_exceptions(payload: dict[str, Any], child_lei: str, level: str) -> list[dict[str, Any]]:
        values = []
        for record in GLEIFProvider._items(payload):
            attributes = record.get("attributes") or {}
            exception = attributes.get("exception") or attributes
            values.append({
                "entityId": f"gleif:{child_lei}", "level": level,
                "category": exception.get("category") or exception.get("exceptionCategory"),
                "reason": exception.get("reason") or exception.get("exceptionReason"),
                "validFrom": attributes.get("validFrom"), "validTo": attributes.get("validTo"),
                "status": (attributes.get("registration") or {}).get("status"),
                "provider": "gleif", "sourceRecord": record.get("id"),
            })
        return values

    def resolve_relationships(self, candidate: ProviderCandidate, observation_date: str | None = None, max_depth: int = 8) -> dict[str, Any] | None:
        from .relationships import candidate_node

        lei = normalize_identifier(candidate.identifiers.get("lei"))
        if not lei:
            return None
        subject = {
            "entityId": f"gleif:{lei}", "canonicalName": candidate.canonical_name, "entityType": "legal_entity",
            "identifiers": {"lei": lei}, "providers": ["gleif"],
        }
        nodes: list[dict[str, Any]] = [subject]
        edges: list[dict[str, Any]] = []
        exceptions: list[dict[str, Any]] = []
        errors: list[str] = []
        known_nodes = {subject["entityId"]}
        visited = {lei}
        current_lei = lei
        complete = True

        def request(path: str, query_type: str) -> dict[str, Any]:
            nonlocal complete
            try:
                return self._request(path, {}, query_type)
            except Exception as exc:
                complete = False
                errors.append(f"{path}: {exc}")
                return {"data": None}

        for _ in range(max_depth):
            payload = request(f"/lei-records/{current_lei}/direct-parent-relationship", "direct_parent_relationship")
            records = self._items(payload)
            if not records:
                exception_payload = request(f"/lei-records/{current_lei}/direct-parent-reporting-exception", "direct_parent_exception")
                exceptions.extend(self._reporting_exceptions(exception_payload, current_lei, "direct"))
                break
            candidate_edges = [self._relationship_edge(record, current_lei, "direct", observation_date) for record in records]
            for edge in candidate_edges:
                if edge["toEntityId"]:
                    edges.append(edge)
            usable = [edge for edge in candidate_edges if edge["toEntityId"] and edge.get("validOnObservationDate") is not False and (edge.get("status") != "INACTIVE" or edge.get("validOnObservationDate") is True)]
            if not usable:
                errors.append(f"No active direct-parent relationship was valid for {current_lei}.")
                complete = False
                break
            edge = usable[0]
            parent_lei = edge["toEntityId"].removeprefix("gleif:")
            parent_payload = request(f"/lei-records/{parent_lei}", "relationship_entity")
            parent_records = self._items(parent_payload)
            if parent_records:
                parent = self._candidate(parent_records[0])
                node = candidate_node(parent)
            else:
                node = {"entityId": edge["toEntityId"], "canonicalName": parent_lei, "entityType": "legal_entity", "identifiers": {"lei": parent_lei}, "providers": ["gleif"]}
                complete = False
                errors.append(f"GLEIF parent node {parent_lei} has no LEI reference record.")
            if node["entityId"] not in known_nodes:
                nodes.append(node)
                known_nodes.add(node["entityId"])
            if parent_lei in visited:
                errors.append(f"GLEIF relationship cycle detected at {parent_lei}.")
                complete = False
                break
            visited.add(parent_lei)
            current_lei = parent_lei
        else:
            errors.append(f"GLEIF relationship traversal stopped at max depth {max_depth}.")
            complete = False

        ultimate_payload = request(f"/lei-records/{lei}/ultimate-parent-relationship", "ultimate_parent_relationship")
        ultimate_records = self._items(ultimate_payload)
        if ultimate_records:
            for record in ultimate_records:
                edge = self._relationship_edge(record, lei, "ultimate", observation_date)
                if not edge["toEntityId"]:
                    continue
                edges.append(edge)
                parent_lei = edge["toEntityId"].removeprefix("gleif:")
                if edge["toEntityId"] not in known_nodes:
                    parent_payload = request(f"/lei-records/{parent_lei}", "relationship_entity")
                    parent_records = self._items(parent_payload)
                    node = candidate_node(self._candidate(parent_records[0])) if parent_records else {"entityId": edge["toEntityId"], "canonicalName": parent_lei, "entityType": "legal_entity", "identifiers": {"lei": parent_lei}, "providers": ["gleif"]}
                    nodes.append(node)
                    known_nodes.add(node["entityId"])
        else:
            exception_payload = request(f"/lei-records/{lei}/ultimate-parent-reporting-exception", "ultimate_parent_exception")
            exceptions.extend(self._reporting_exceptions(exception_payload, lei, "ultimate"))
        return {"provider": self.name, "subject": subject, "nodes": nodes, "edges": edges, "reportingExceptions": exceptions, "complete": complete, "errors": errors}

    def health_check(self) -> dict[str, Any]:
        try:
            self._request("/lei-records", {"page[size]": "1"}, "health")
            return {**self.metadata(), "status": "available", "offline": self.offline}
        except ProviderError as exc:
            return {**self.metadata(), "status": "unavailable", "offline": self.offline, "error": str(exc)}


class SECProvider(MatchProvider):
    """SEC company index and submissions connector for US filers and issuers."""

    name = "sec"
    trust_level = TrustLevel.SUPPORTING
    capabilities = ProviderCapabilities(
        entity_lookup=True,
        identifier_mapping=("cik", "ticker"),
        current_entity_data=True,
    )
    index_url = "https://www.sec.gov/files/company_tickers_exchange.json"
    submissions_url = "https://data.sec.gov/submissions/CIK{cik}.json"

    def __init__(self, user_agent: str, cache: SQLiteCache | None = None, timeout: float = 20, retries: int = 2, offline: bool = False):
        if not user_agent or "@" not in user_agent:
            raise ValueError("SEC provider requires an identifying user agent such as 'Organization contact@example.com'.")
        self.user_agent = user_agent
        self.cache = cache
        self.timeout = timeout
        self.retries = retries
        self.offline = offline
        self._rate = _RateLimiter(.11)  # stay below the SEC's 10 requests/second ceiling

    @staticmethod
    def _cik(value: Any) -> str:
        normalized = normalize_identifier(str(value) if value is not None else None) or ""
        return normalized.zfill(10)

    def _get(self, url: str, query_type: str, cache_key: str, ttl_seconds: int) -> dict[str, Any]:
        if self.cache:
            cached = self.cache.get(self.name, query_type, cache_key, allow_stale=self.offline)
            if cached is not None:
                return cached
        if self.offline:
            raise ProviderError(f"SEC offline mode has no cached {query_type} response.")
        request = Request(url, headers={"Accept": "application/json", "User-Agent": self.user_agent})
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                self._rate.wait()
                with urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if self.cache:
                    self.cache.set(self.name, query_type, cache_key, payload, ttl_seconds)
                return payload
            except HTTPError as exc:
                last_error = exc
                if exc.code not in {429, 500, 502, 503, 504} or attempt == self.retries:
                    break
                time.sleep(min(2 ** attempt, 5))
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt == self.retries:
                    break
                time.sleep(min(2 ** attempt, 5))
        raise ProviderError(f"SEC {query_type} request failed: {last_error}")

    def _index(self) -> list[dict[str, Any]]:
        payload = self._get(self.index_url, "company_index", "company_tickers_exchange", 24 * 3600)
        fields = payload.get("fields") or ["cik", "name", "ticker", "exchange"]
        return [dict(zip(fields, row)) for row in payload.get("data", [])]

    def _submission(self, cik: str) -> dict[str, Any]:
        return self._get(self.submissions_url.format(cik=cik), "submissions", cik, 6 * 3600)

    @staticmethod
    def _candidate_from_index(row: dict[str, Any]) -> ProviderCandidate:
        cik = SECProvider._cik(row.get("cik"))
        ticker = normalize_identifier(row.get("ticker"))
        exchange = normalize_identifier(row.get("exchange"), "exchange")
        return ProviderCandidate(
            entity_id=f"sec:{cik}", canonical_name=str(row.get("name") or cik), entity_type="issuer", provider="sec",
            country="US", identifiers={key: value for key, value in {"cik": cik, "ticker": ticker, "exchange": exchange}.items() if value},
            security={key: value for key, value in {"ticker": row.get("ticker"), "exchange": row.get("exchange")}.items() if value}, sources=["sec"],
        )

    def _candidate_from_submission(self, payload: dict[str, Any]) -> ProviderCandidate:
        cik = self._cik(payload.get("cik"))
        tickers, exchanges = payload.get("tickers") or [], payload.get("exchanges") or []
        pairs = list(zip(tickers, exchanges))
        aliases = []
        for item in payload.get("formerNames") or []:
            aliases.append(item.get("name") if isinstance(item, dict) else str(item))
        ticker, exchange = pairs[0] if pairs else (None, None)
        return ProviderCandidate(
            entity_id=f"sec:{cik}", canonical_name=payload.get("name") or cik, entity_type="issuer", provider="sec", country="US",
            identifiers={key: value for key, value in {"cik": cik, "ticker": normalize_identifier(ticker), "exchange": normalize_identifier(exchange, "exchange")}.items() if value},
            security={"ticker": ticker, "exchange": exchange, "listings": [{"ticker": item[0], "exchange": item[1]} for item in pairs]} if pairs else None,
            aliases=[item for item in aliases if item], sources=["sec"],
        )

    def search(self, input_record: EntityMatchInput, limit: int = 20) -> list[ProviderCandidate]:
        if input_record.cik:
            return [self._candidate_from_submission(self._submission(self._cik(input_record.cik)))]
        ticker = normalize_identifier(input_record.ticker)
        name = normalize_name(input_record.legalName or input_record.entityName or input_record.brandName)
        if not ticker and not name:
            return []
        ranked: list[tuple[int, float, dict[str, Any]]] = []
        for row in self._index():
            row_ticker = normalize_identifier(row.get("ticker"))
            row_name = normalize_name(row.get("name"))
            if ticker and ticker == row_ticker:
                ranked.append((0, 1.0, row))
            elif name and name == row_name:
                ranked.append((1, 1.0, row))
            elif name and row_name and (name in row_name or row_name in name):
                similarity = len(set(name.split()) & set(row_name.split())) / max(len(set(name.split()) | set(row_name.split())), 1)
                ranked.append((2, similarity, row))
        selected = [item[2] for item in sorted(ranked, key=lambda item: (item[0], -item[1]))[:limit]]
        candidates = [self._candidate_from_index(row) for row in selected]
        if selected and (ticker or (name and normalize_name(selected[0].get("name")) == name)):
            try:
                candidates[0] = self._candidate_from_submission(self._submission(candidates[0].identifiers["cik"]))
            except ProviderError:
                pass  # the cached index candidate remains usable and auditable
        return candidates

    def health_check(self) -> dict[str, Any]:
        try:
            count = len(self._index())
            return {**self.metadata(), "status": "available", "offline": self.offline, "indexedCompanies": count}
        except ProviderError as exc:
            return {**self.metadata(), "status": "unavailable", "offline": self.offline, "error": str(exc)}


class OpenFIGIProvider(MatchProvider):
    """OpenFIGI v3 mapping/search connector with per-record caching and request batching."""

    name = "openfigi"
    trust_level = TrustLevel.SUPPORTING
    capabilities = ProviderCapabilities(
        security_lookup=True,
        identifier_mapping=("figi", "isin", "cusip", "ticker", "exchange"),
        current_security_data=True,
        share_class_data=True,
    )
    base_url = "https://api.openfigi.com/v3"
    exchange_to_mic = {
        "XNAS": "XNAS", "XNYS": "XNYS", "ARCX": "ARCX", "XASE": "XASE",
        "LSE": "XLON", "LONDON": "XLON", "TSX": "XTSE", "TSXV": "XTSX",
        "EURONEXTPARIS": "XPAR", "EURONEXTAMSTERDAM": "XAMS", "XETRA": "XETR",
        "TOKYO": "XTKS", "ASX": "XASX", "SINGAPORE": "XSES", "HONGKONG": "XHKG",
    }
    exchange_to_figi_code = {
        "XNAS": "US", "XNYS": "US", "ARCX": "US", "XASE": "US",
    }

    def __init__(self, api_key: str | None = None, cache: SQLiteCache | None = None, timeout: float = 20, retries: int = 2, offline: bool = False, enable_name_search: bool = False):
        self.api_key = api_key
        self.cache = cache
        self.timeout = timeout
        self.retries = retries
        self.offline = offline
        self.enable_name_search = enable_name_search
        self.max_mapping_jobs = 100 if api_key else 5
        self._mapping_rate = _RateLimiter(.25 if api_key else 2.5)
        self._search_rate = _RateLimiter(3.1 if api_key else 12.1)

    def _post(self, path: str, body: Any, rate: _RateLimiter) -> Any:
        if self.offline:
            raise ProviderError("OpenFIGI offline mode has no cached response for this query.")
        headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "SymbologyLink/0.0.0"}
        if self.api_key:
            headers["X-OPENFIGI-APIKEY"] = self.api_key
        request = Request(f"{self.base_url}{path}", data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                rate.wait()
                with urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                last_error = exc
                if exc.code not in {429, 500, 502, 503} or attempt == self.retries:
                    break
                retry_after = exc.headers.get("ratelimit-reset") or exc.headers.get("Retry-After")
                time.sleep(min(float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else 2 ** attempt, 10))
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt == self.retries:
                    break
                time.sleep(min(2 ** attempt, 5))
        raise ProviderError(f"OpenFIGI request failed: {last_error}")

    @staticmethod
    def _mapping_job(record: EntityMatchInput) -> dict[str, Any] | None:
        for field, id_type in (("figi", "ID_BB_GLOBAL"), ("isin", "ID_ISIN"), ("cusip", "ID_CUSIP")):
            value = getattr(record, field)
            if value:
                return {"idType": id_type, "idValue": normalize_identifier(value)}
        if record.ticker:
            job: dict[str, Any] = {"idType": "TICKER", "idValue": normalize_identifier(record.ticker)}
            if record.exchange:
                exchange = normalize_identifier(record.exchange, "exchange") or ""
                mic = OpenFIGIProvider.exchange_to_mic.get(exchange)
                figi_code = OpenFIGIProvider.exchange_to_figi_code.get(exchange)
                if figi_code:
                    job["exchCode"] = figi_code
                elif mic:
                    job["micCode"] = mic
                else:
                    job["micCode" if len(exchange) == 4 and exchange.startswith(("X", "A")) else "exchCode"] = exchange
            return job
        return None

    @staticmethod
    def _candidate(item: dict[str, Any], job: dict[str, Any] | None = None) -> ProviderCandidate:
        figi = item.get("figi")
        ticker = normalize_identifier(item.get("ticker"))
        exchange = normalize_identifier(item.get("exchCode"))
        identifiers = {key: value for key, value in {"figi": normalize_identifier(figi), "ticker": ticker, "exchange": exchange}.items() if value}
        if job:
            reverse = {"ID_ISIN": "isin", "ID_CUSIP": "cusip", "ID_BB_GLOBAL": "figi"}
            field = reverse.get(job.get("idType"))
            if field:
                identifiers[field] = normalize_identifier(job.get("idValue")) or ""
        security = {key: value for key, value in {"securityId": figi, "figi": figi, "ticker": item.get("ticker"), "exchange": item.get("exchCode"), "shareClassFIGI": item.get("shareClassFIGI"), "compositeFIGI": item.get("compositeFIGI"), "securityType": item.get("securityType2") or item.get("securityType"), "marketSector": item.get("marketSector")}.items() if value}
        entity_key = item.get("shareClassFIGI") or item.get("compositeFIGI") or figi or hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()[:16]
        return ProviderCandidate(entity_id=f"openfigi:{entity_key}", canonical_name=item.get("name") or item.get("securityDescription") or str(entity_key), entity_type="issuer", provider="openfigi", identifiers=identifiers, security=security, sources=["openfigi"])

    @staticmethod
    def _cache_key(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    def search_batch(self, records: list[EntityMatchInput], limit: int = 20) -> dict[str, list[ProviderCandidate]]:
        results: dict[str, list[ProviderCandidate]] = {record.recordId: [] for record in records}
        pending: list[tuple[EntityMatchInput, dict[str, Any], str]] = []
        name_records: list[EntityMatchInput] = []
        for record in records:
            job = self._mapping_job(record)
            if not job and self.enable_name_search:
                name_records.append(record)
                continue
            if not job:
                continue
            key = self._cache_key(job)
            cached = self.cache.get(self.name, "mapping", key, allow_stale=self.offline) if self.cache else None
            if cached is not None:
                results[record.recordId] = [self._candidate(item, job) for item in cached.get("data", [])[:limit]]
            else:
                pending.append((record, job, key))
        if pending and self.offline:
            raise ProviderError("OpenFIGI offline mode is missing one or more mapping responses.")
        for start in range(0, len(pending), self.max_mapping_jobs):
            chunk = pending[start:start + self.max_mapping_jobs]
            responses = self._post("/mapping", [item[1] for item in chunk], self._mapping_rate)
            for (record, job, key), response in zip(chunk, responses):
                if self.cache:
                    self.cache.set(self.name, "mapping", key, response, 30 * 86400)
                results[record.recordId] = [self._candidate(item, job) for item in response.get("data", [])[:limit]]
        for record in name_records:
            name = record.legalName or record.entityName or record.brandName
            if not name:
                continue
            body = {"query": name}
            key = self._cache_key(body)
            payload = self.cache.get(self.name, "search", key, allow_stale=self.offline) if self.cache else None
            if payload is None:
                payload = self._post("/search", body, self._search_rate)
                if self.cache:
                    self.cache.set(self.name, "search", key, payload, 7 * 86400)
            results[record.recordId] = [self._candidate(item) for item in payload.get("data", [])[:limit]]
        return results

    def search(self, input_record: EntityMatchInput, limit: int = 20) -> list[ProviderCandidate]:
        return self.search_batch([input_record], limit)[input_record.recordId]

    def health_check(self) -> dict[str, Any]:
        try:
            payload = self._post("/mapping", [{"idType": "TICKER", "idValue": "IBM", "exchCode": "US"}], self._mapping_rate)
            return {**self.metadata(), "status": "available", "authenticated": bool(self.api_key), "sampleResults": len(payload[0].get("data", [])) if payload else 0}
        except ProviderError as exc:
            return {**self.metadata(), "status": "unavailable", "authenticated": bool(self.api_key), "error": str(exc)}
