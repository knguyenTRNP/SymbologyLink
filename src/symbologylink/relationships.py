from __future__ import annotations

from datetime import date
from typing import Any

from .normalize import normalize_identifier, normalize_name
from .providers import MatchProvider, ProviderCandidate, provider_metadata_map, source_rank, source_trust_level
from .validity import evaluate_periods


def relationship_trust(source: str | None, provider_metadata: dict[str, dict[str, Any]] | None = None) -> str:
    """Classify relationship evidence without treating provider data as approval."""
    return source_trust_level(source, provider_metadata)


def _edge_source(edge: dict[str, Any], fallback: str = "unknown", provider_metadata: dict[str, dict[str, Any]] | None = None) -> str:
    explicit = edge.get("source") or edge.get("provider")
    if explicit:
        return str(explicit)
    providers = edge.get("providers") or []
    return str(min(providers, key=lambda source: source_rank(source, provider_metadata))) if providers else fallback


def annotate_relationship_edge(edge: dict[str, Any], fallback: str = "unknown", provider_metadata: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    source = _edge_source(edge, fallback, provider_metadata)
    providers = list(dict.fromkeys(edge.get("providers") or [source]))
    authoritative = [item for item in providers if relationship_trust(item, provider_metadata) == "authoritative"]
    if authoritative:
        source = min(authoritative, key=lambda item: source_rank(item, provider_metadata))
    date_capable = any(
        item == "human_override" or item.startswith("override:") or item.startswith("rule:")
        or bool((((provider_metadata or {}).get(item) or {}).get("capabilities") or {}).get("relationship_effective_dates"))
        for item in providers
    )
    valid_on_date_value = edge.get("validOnObservationDate") if date_capable else None
    return {
        **edge,
        "source": source,
        "trustLevel": relationship_trust(source, provider_metadata),
        "selfReported": bool(edge.get("selfReported") or edge.get("self_reported") or "gleif" in providers or source == "gleif"),
        "effectiveDatesCapable": date_capable,
        "validOnObservationDate": valid_on_date_value,
        "providers": providers,
    }


def candidate_node(candidate: ProviderCandidate) -> dict[str, Any]:
    return {
        "entityId": candidate.entity_id,
        "canonicalName": candidate.canonical_name,
        "entityType": candidate.entity_type,
        "identifiers": dict(candidate.identifiers),
        "providers": list(candidate.sources or [candidate.provider]),
    }


def valid_on_date(observation_date: str | None, valid_from: str | None, valid_to: str | None) -> bool | None:
    if not observation_date or (not valid_from and not valid_to):
        return None
    observed = date.fromisoformat(observation_date)
    return (not valid_from or observed >= date.fromisoformat(valid_from[:10])) and (not valid_to or observed <= date.fromisoformat(valid_to[:10]))


def _edge_level(relationship_type: str) -> str:
    value = relationship_type.upper()
    if "ULTIMATE" in value:
        return "ultimate"
    return "direct"


def _edge_family(relationship_type: str) -> str:
    value = relationship_type.upper()
    if "BRAND" in value:
        return "brand"
    if "CONSOLIDAT" in value:
        return "accounting_consolidation"
    if "FUND" in value or "FEEDER" in value:
        return "fund"
    return "ownership"


def embedded_relationship_graph(candidate: ProviderCandidate, observation_date: str | None, provider_metadata: dict[str, dict[str, Any]] | None = None) -> dict[str, Any] | None:
    relationships = list(candidate.relationships)
    if not relationships and candidate.public_parent:
        parent = candidate.public_parent
        relationships = [{
            "fromEntityId": candidate.entity_id,
            "toEntityId": parent.get("entityId"),
            "toEntity": parent,
            "relationshipType": parent.get("relationshipType") or "subsidiary_of",
            "validFrom": parent.get("validFrom"),
            "validTo": parent.get("validTo"),
            "status": parent.get("status") or "ACTIVE",
            "provider": candidate.provider,
        }]
    if not relationships:
        return None
    subject = candidate_node(candidate)
    nodes = [subject]
    edges = []
    for relationship in relationships:
        parent = relationship.get("toEntity") or relationship.get("parent") or {}
        parent_id = relationship.get("toEntityId") or parent.get("entityId") or parent.get("entity_id")
        if not parent_id:
            parent_name = parent.get("canonicalName") or parent.get("canonical_name") or relationship.get("parentName")
            parent_id = f"{candidate.provider}:name:{normalize_name(parent_name) or 'unknown'}"
        parent_node = {
            "entityId": str(parent_id),
            "canonicalName": parent.get("canonicalName") or parent.get("canonical_name") or relationship.get("parentName") or str(parent_id),
            "entityType": parent.get("entityType") or parent.get("entity_type") or "legal_entity",
            "identifiers": dict(parent.get("identifiers") or {}),
            "providers": [relationship.get("provider") or candidate.provider],
        }
        nodes.append(parent_node)
        valid_from = relationship.get("validFrom") or relationship.get("valid_from")
        valid_to = relationship.get("validTo") or relationship.get("valid_to")
        relationship_periods = list(relationship.get("periods") or [])
        evaluated = evaluate_periods("relationships", relationship_periods, observation_date) if relationship_periods else None
        provider = relationship.get("provider") or candidate.provider
        edges.append(annotate_relationship_edge({
            "fromEntityId": relationship.get("fromEntityId") or relationship.get("from_entity_id") or candidate.entity_id,
            "toEntityId": str(parent_id),
            "relationshipType": relationship.get("relationshipType") or relationship.get("relationship_type") or "subsidiary_of",
            "level": relationship.get("level") or _edge_level(relationship.get("relationshipType") or relationship.get("relationship_type") or "subsidiary_of"),
            "status": relationship.get("status") or "ACTIVE",
            "validFrom": valid_from,
            "validTo": valid_to,
            "periods": relationship_periods,
            "validOnObservationDate": evaluated["validOnObservationDate"] if evaluated else valid_on_date(observation_date, valid_from, valid_to),
            "providers": [provider],
            "provenance": relationship.get("provenance") or [{"provider": provider, "sourceRecord": relationship.get("sourceRecord")}],
        }, provider, provider_metadata))
    source_ids = {edge["fromEntityId"] for edge in edges}
    terminal_nodes = [node for node in nodes if node["entityId"] not in source_ids and node["entityId"] != candidate.entity_id]
    complete = any(node.get("entityType") == "issuer" for node in terminal_nodes) or any(relationship.get("terminal") is True for relationship in relationships)
    return {"provider": candidate.provider, "subject": subject, "nodes": nodes, "edges": edges, "reportingExceptions": [], "complete": complete, "errors": []}


class RelationshipResolver:
    """Merge provider hierarchies into one auditable, point-in-time relationship graph."""

    def __init__(self, providers: list[MatchProvider], metadata: dict[str, dict[str, Any]] | None = None):
        self.providers = providers
        self.provider_metadata = metadata or provider_metadata_map(providers)

    def resolve(self, candidate: ProviderCandidate, observation_date: str | None = None, max_depth: int = 8) -> dict[str, Any]:
        graphs: list[dict[str, Any]] = []
        embedded = embedded_relationship_graph(candidate, observation_date, self.provider_metadata)
        if embedded:
            graphs.append(embedded)
        provider_errors: list[str] = []
        for provider in self.providers:
            try:
                graph = provider.resolve_relationships(candidate, observation_date, max_depth)
                if graph:
                    graphs.append(graph)
            except Exception as exc:
                provider_errors.append(f"{provider.name}: {exc}")
        return self._merge(candidate_node(candidate), graphs, provider_errors, observation_date, max_depth)

    def _merge(self, subject: dict[str, Any], graphs: list[dict[str, Any]], provider_errors: list[str], observation_date: str | None, max_depth: int) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = []
        aliases: dict[str, str] = {}
        strong_index: dict[tuple[str, str], str] = {}

        def add_node(raw: dict[str, Any]) -> str:
            raw_id = str(raw.get("entityId") or raw.get("id") or "")
            identifiers = {key: normalize_identifier(str(value)) for key, value in (raw.get("identifiers") or {}).items() if value}
            existing_id = aliases.get(raw_id)
            if not existing_id:
                for key, value in identifiers.items():
                    if key in {"lei", "cik"} and (key, value) in strong_index:
                        existing_id = strong_index[(key, value)]
                        break
            existing = next((item for item in nodes if item["entityId"] == existing_id), None) if existing_id else None
            providers = list(dict.fromkeys(raw.get("providers") or ([raw.get("provider")] if raw.get("provider") else [])))
            if existing:
                current_rank = min((source_rank(item, self.provider_metadata) for item in existing.get("providers", [])), default=10)
                incoming_rank = min((source_rank(item, self.provider_metadata) for item in providers), default=10)
                if incoming_rank < current_rank:
                    existing["canonicalName"] = raw.get("canonicalName") or existing["canonicalName"]
                    existing["entityType"] = raw.get("entityType") or existing["entityType"]
                existing["identifiers"] = {**identifiers, **existing.get("identifiers", {})}
                existing["providers"] = list(dict.fromkeys([*existing.get("providers", []), *providers]))
                aliases[raw_id] = existing["entityId"]
                for key, value in identifiers.items():
                    if key in {"lei", "cik"}:
                        strong_index[(key, value)] = existing["entityId"]
                return existing["entityId"]
            node = {
                "entityId": raw_id,
                "canonicalName": raw.get("canonicalName") or raw_id,
                "entityType": raw.get("entityType") or "legal_entity",
                "identifiers": identifiers,
                "providers": providers,
            }
            nodes.append(node)
            aliases[raw_id] = raw_id
            for key, value in identifiers.items():
                if key in {"lei", "cik"}:
                    strong_index[(key, value)] = raw_id
            return raw_id

        subject_id = add_node(subject)
        for graph in graphs:
            for node in graph.get("nodes") or []:
                canonical_id = add_node(node)
                if node.get("entityId") == graph.get("subject", {}).get("entityId") and node.get("entityId") == subject.get("entityId"):
                    subject_id = canonical_id

        edges: list[dict[str, Any]] = []
        for graph in graphs:
            graph_provider = graph.get("provider") or "unknown"
            for raw in graph.get("edges") or []:
                source = aliases.get(str(raw.get("fromEntityId")), str(raw.get("fromEntityId")))
                target = aliases.get(str(raw.get("toEntityId")), str(raw.get("toEntityId")))
                relationship_type = raw.get("relationshipType") or "subsidiary_of"
                providers = list(dict.fromkeys(raw.get("providers") or [raw.get("source") or raw.get("provider") or graph_provider]))
                existing = next((item for item in edges if item["fromEntityId"] == source and item["toEntityId"] == target and item["relationshipType"] == relationship_type), None)
                if existing:
                    existing["providers"] = list(dict.fromkeys([*existing["providers"], *providers]))
                    annotated = annotate_relationship_edge(existing, graph_provider, self.provider_metadata)
                    existing.update({key: annotated[key] for key in ("source", "trustLevel", "selfReported", "effectiveDatesCapable", "validOnObservationDate", "providers")})
                    existing["provenance"].extend(item for item in (raw.get("provenance") or []) if item not in existing["provenance"])
                    for item in raw.get("periods") or []:
                        if item not in existing["periods"]:
                            existing["periods"].append(item)
                    if existing["periods"] and existing.get("effectiveDatesCapable"):
                        existing["validOnObservationDate"] = evaluate_periods("relationships", existing["periods"], observation_date)["validOnObservationDate"]
                    continue
                valid_from, valid_to = raw.get("validFrom"), raw.get("validTo")
                edges.append(annotate_relationship_edge({
                    "fromEntityId": source,
                    "toEntityId": target,
                    "relationshipType": relationship_type,
                    "level": raw.get("level") or _edge_level(relationship_type),
                    "status": raw.get("status") or "ACTIVE",
                    "validFrom": valid_from,
                    "validTo": valid_to,
                    "periods": list(raw.get("periods") or []),
                    "validOnObservationDate": raw.get("validOnObservationDate") if "validOnObservationDate" in raw else valid_on_date(observation_date, valid_from, valid_to),
                    "providers": providers,
                    "provenance": list(raw.get("provenance") or [{"provider": graph_provider}]),
                }, graph_provider, self.provider_metadata))

        conflicts: list[dict[str, Any]] = []
        chain_ids = [subject_id]
        selected_edges: list[dict[str, Any]] = []
        current = subject_id
        for _ in range(max_depth):
            outgoing = [edge for edge in edges if edge["fromEntityId"] == current and edge["level"] == "direct" and edge.get("validOnObservationDate") is not False and (edge.get("status") != "INACTIVE" or edge.get("validOnObservationDate") is True)]
            if not outgoing:
                break
            outgoing.sort(key=lambda edge: (min((source_rank(provider, self.provider_metadata) for provider in edge["providers"]), default=10), edge["relationshipType"], edge["toEntityId"]))
            chosen = outgoing[0]
            same_family = [edge for edge in outgoing[1:] if _edge_family(edge["relationshipType"]) == _edge_family(chosen["relationshipType"]) and edge["toEntityId"] != chosen["toEntityId"]]
            if same_family:
                conflicts.append({"fromEntityId": current, "relationshipFamily": _edge_family(chosen["relationshipType"]), "candidateParents": [chosen["toEntityId"], *(edge["toEntityId"] for edge in same_family)]})
            if chosen["toEntityId"] in chain_ids:
                provider_errors.append(f"Relationship cycle detected at {chosen['toEntityId']}.")
                break
            selected_edges.append(chosen)
            current = chosen["toEntityId"]
            chain_ids.append(current)
        else:
            if any(edge["fromEntityId"] == current and edge["level"] == "direct" for edge in edges):
                provider_errors.append(f"Relationship traversal stopped at max depth {max_depth}.")

        node_by_id = {node["entityId"]: node for node in nodes}
        chain = [node_by_id[item] for item in chain_ids if item in node_by_id]
        direct_parent = chain[1] if len(chain) > 1 else None
        accounting_direct_edge = next((edge for edge in edges if edge["fromEntityId"] == subject_id and "DIRECTLY_CONSOLIDATED" in edge["relationshipType"].upper() and edge.get("validOnObservationDate") is not False), None)
        accounting_direct_parent = node_by_id.get(accounting_direct_edge["toEntityId"]) if accounting_direct_edge else None
        explicit_ultimate = next((edge for edge in edges if edge["fromEntityId"] == subject_id and edge["level"] == "ultimate" and edge.get("validOnObservationDate") is not False), None)
        accounting_ultimate_parent = node_by_id.get(explicit_ultimate["toEntityId"]) if explicit_ultimate else None
        ultimate_parent = chain[-1] if len(chain) > 1 else accounting_ultimate_parent
        issuer = next((node for node in chain if node.get("entityType") == "issuer"), None)
        exceptions = [item for graph in graphs for item in (graph.get("reportingExceptions") or [])]
        graph_errors = [item for graph in graphs for item in (graph.get("errors") or [])]
        errors = list(dict.fromkeys([*provider_errors, *graph_errors]))
        if conflicts:
            status = "conflict"
        elif edges and errors:
            status = "partial"
        elif edges:
            status = "resolved"
        elif exceptions:
            status = "not_reported"
        else:
            status = "not_available"
        relevant_edges = selected_edges or [edge for edge in edges if edge["fromEntityId"] == subject_id and edge["level"] == "direct"]
        validity = [edge.get("validOnObservationDate") for edge in relevant_edges]
        if not observation_date:
            point_in_time = "not_requested"
            valid_at_observation = None
        elif any(value is False for value in validity):
            point_in_time = "invalid"
            valid_at_observation = False
        elif validity and all(value is True for value in validity):
            point_in_time = "verified"
            valid_at_observation = True
        else:
            point_in_time = "not_verified"
            valid_at_observation = None
        return {
            "subject": node_by_id.get(subject_id, subject),
            "nodes": nodes,
            "edges": edges,
            "selectedEdges": selected_edges,
            "chain": chain,
            "directParent": direct_parent,
            "ultimateParent": ultimate_parent,
            "accountingDirectParent": accounting_direct_parent,
            "accountingUltimateParent": accounting_ultimate_parent,
            "issuer": issuer,
            "status": status,
            "complete": any(graph.get("complete", False) for graph in graphs) and not errors and not conflicts,
            "pointInTimeStatus": point_in_time,
            "validOnObservationDate": valid_at_observation,
            "reportingExceptions": exceptions,
            "conflicts": conflicts,
            "errors": errors,
            "providerMetadata": self.provider_metadata,
        }


def parent_resolution(graph: dict[str, Any], brand_origin: bool = False, max_depth: int = 8) -> dict[str, Any]:
    """Rank parent proposals while keeping relationship traversal separate from verification."""
    subject = graph.get("subject") or {}
    subject_id = subject.get("entityId")
    nodes = {node.get("entityId"): node for node in graph.get("nodes") or []}
    provider_metadata = graph.get("providerMetadata") or {}
    eligible = [
        annotate_relationship_edge(edge, provider_metadata=provider_metadata)
        for edge in graph.get("edges") or []
        if edge.get("validOnObservationDate") is not False
        and (edge.get("status") != "INACTIVE" or edge.get("validOnObservationDate") is True)
    ]
    outgoing: dict[str, list[dict[str, Any]]] = {}
    for edge in eligible:
        if edge.get("level") == "direct":
            outgoing.setdefault(str(edge.get("fromEntityId")), []).append(edge)

    paths: list[list[dict[str, Any]]] = []

    def walk(current: str, path: list[dict[str, Any]], visited: set[str]) -> None:
        next_edges = outgoing.get(current, [])
        if not next_edges or len(path) >= max_depth:
            if path:
                paths.append(path)
            return
        progressed = False
        for edge in next_edges:
            target = str(edge.get("toEntityId") or "")
            if not target or target in visited:
                continue
            progressed = True
            walk(target, [*path, edge], {*visited, target})
        if path and not progressed:
            paths.append(path)

    if subject_id:
        walk(str(subject_id), [], {str(subject_id)})

    alternatives: list[dict[str, Any]] = []
    for path in paths:
        direct = nodes.get(path[0].get("toEntityId"), {"entityId": path[0].get("toEntityId")})
        terminal = nodes.get(path[-1].get("toEntityId"), {"entityId": path[-1].get("toEntityId")})
        sources = list(dict.fromkeys(edge.get("source") or "unknown" for edge in path))
        authoritative = all(edge.get("trustLevel") == "authoritative" for edge in path)
        proposal = {
            **terminal,
            "directParent": direct,
            "chain": [subject_id, *(edge.get("toEntityId") for edge in path)],
            "relationshipTypes": [edge.get("relationshipType") for edge in path],
            "sources": sources,
            "trustLevel": "authoritative" if authoritative else "supporting",
            "selfReported": any(edge.get("selfReported") is True for edge in path),
            "brandInference": brand_origin or any(_edge_family(edge.get("relationshipType") or "") == "brand" for edge in path),
            "status": "verified" if authoritative else "candidate",
        }
        existing = next((item for item in alternatives if item.get("entityId") == proposal.get("entityId")), None)
        if existing:
            combined_sources = list(dict.fromkeys([*existing["sources"], *sources]))
            if proposal["trustLevel"] == "authoritative":
                proposal["sources"] = combined_sources
                existing.update(proposal)
            else:
                existing["sources"] = combined_sources
        else:
            alternatives.append(proposal)

    alternatives.sort(key=lambda item: (
        0 if item["status"] == "verified" else 1,
        min((source_rank(source, provider_metadata) for source in item["sources"]), default=10),
        str(item.get("entityId")),
    ))
    for rank, alternative in enumerate(alternatives, 1):
        alternative["rank"] = rank

    distinct = {item.get("entityId") for item in alternatives}
    authoritative_targets = [item.get("entityId") for item in alternatives if item["status"] == "verified"]
    authoritative_conflict = len(set(authoritative_targets)) > 1
    if authoritative_conflict:
        status = "contradicted"
    elif len(distinct) > 1:
        status = "ambiguous"
    elif alternatives:
        status = alternatives[0]["status"]
    elif subject.get("entityType") == "issuer":
        status = "not_applicable"
    else:
        status = "unknown"
    return {
        "status": status,
        "selectedParent": alternatives[0] if alternatives else None,
        "alternatives": alternatives,
        "authoritativeConflict": authoritative_conflict,
    }
