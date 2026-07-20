from __future__ import annotations

from difflib import SequenceMatcher

from .decisions import Decision, OverrideStore, RuleSet
from .models import CandidateMatch, EntityMatchInput, EntityMatchResult, MatchConfig, MatchEvidence, SecurityCandidateMatch
from .normalize import normalize_domain, normalize_identifier, normalize_name
from .providers import MatchProvider, ProviderCandidate, ProviderSecurityCandidate
from .relationships import RelationshipResolver
from .validity import combine_validity, evaluate_periods, not_applicable, period, relationship_validity


def _similarity(left: str | None, right: str | None) -> float:
    if not left or not right:
        return 0
    sequence = SequenceMatcher(None, left, right).ratio()
    lt, rt = set(left.split()), set(right.split())
    token_set = len(lt & rt) / len(lt | rt) if lt | rt else 0
    token_sort = SequenceMatcher(None, " ".join(sorted(lt)), " ".join(sorted(rt))).ratio()
    return max(sequence, token_set, token_sort)


class MatchEngine:
    def __init__(self, providers: list[MatchProvider], config: MatchConfig | None = None, rules: RuleSet | None = None, overrides: OverrideStore | None = None):
        self.providers = providers
        self.config = config or MatchConfig()
        self.rules = rules or RuleSet()
        self.overrides = overrides
        self.relationship_resolver = RelationshipResolver(providers)

    def _relationship_graph(self, record: EntityMatchInput, candidate: ProviderCandidate, evidence: list[MatchEvidence]) -> dict:
        graph = self.relationship_resolver.resolve(candidate, record.observationDate, self.config.relationship_max_depth)
        if graph["edges"]:
            providers = sorted({provider for edge in graph["edges"] for provider in edge.get("providers", [])})
            evidence.append(MatchEvidence("relationship_resolution", record.observationDate, graph["status"], 0, ",".join(providers), detail=f"Resolved {len(graph['edges'])} relationship edge(s) across {len(graph['chain'])} chain node(s)."))
        elif graph["reportingExceptions"]:
            evidence.append(MatchEvidence("relationship_reporting_exception", candidate.entity_id, graph["reportingExceptions"], 0, "gleif", detail="Parent relationship was not reported; the provider exception is preserved."))
        for error in graph["errors"]:
            evidence.append(MatchEvidence("relationship_resolution_error", candidate.entity_id, None, 0, "relationship_resolver", detail=error))
        return graph

    @staticmethod
    def _candidate_validity(record: EntityMatchInput, candidate: ProviderCandidate) -> dict:
        entity_periods = list(candidate.entity_periods)
        if not entity_periods and (candidate.valid_from or candidate.valid_to):
            entity_periods = [period("entity_existence", candidate.valid_from, candidate.valid_to, provider=candidate.provider, provenance="legacy_valid_from_valid_to")]
        return evaluate_periods("entity", entity_periods, record.observationDate)

    def _add_validity_evidence(self, record: EntityMatchInput, candidate: ProviderCandidate, scope: str, evaluation: dict, evidence: list[MatchEvidence]) -> float:
        if not record.observationDate or evaluation["status"] == "not_applicable":
            return 0
        weights = self.config.weights
        if evaluation["validOnObservationDate"] is True:
            contribution = weights[f"{scope}_date_valid"]
        elif evaluation["validOnObservationDate"] is False:
            contribution = weights[f"{scope}_date_invalid"]
        else:
            contribution = 0
        bounds = [{"validFrom": item.get("validFrom"), "validTo": item.get("validTo"), "periodType": item.get("periodType")} for item in evaluation["periods"]]
        evidence.append(MatchEvidence(f"{scope}_observation_date_validity", record.observationDate, bounds, contribution, candidate.provider, detail=f"{evaluation['status']}: {evaluation['reason']}"))
        return contribution

    @staticmethod
    def _add_relationship_validity_evidence(record: EntityMatchInput, evaluation: dict, evidence: list[MatchEvidence]) -> None:
        if not record.observationDate or evaluation["status"] == "not_applicable":
            return
        bounds = [{"fromEntityId": item.get("fromEntityId"), "toEntityId": item.get("toEntityId"), "validFrom": item.get("validFrom"), "validTo": item.get("validTo")} for item in evaluation["periods"]]
        evidence.append(MatchEvidence("relationship_observation_date_validity", record.observationDate, bounds, 0, "relationship_resolver", detail=f"{evaluation['status']}: {evaluation['reason']}"))

    def _decision_result(self, record: EntityMatchInput, decision: Decision) -> EntityMatchResult:
        evidence_type = "human_override" if decision.source == "human_override" else "reusable_rule_match"
        evidence = [MatchEvidence(evidence_type, provider=decision.source, scoreContribution=100, detail=decision.reason or decision.action)]
        mapping_version = self.config.mapping_version
        if decision.action != "match" or not decision.candidate:
            status = "review_required" if decision.action == "ambiguous" else "unmatched"
            return EntityMatchResult(record.recordId, status, 0, [], evidence, mapping_version, pointInTimeReason="Decision did not select a dated entity.", decisionSource=decision.source, decisionVersion=decision.version)
        candidate = decision.candidate
        entity_validity = self._candidate_validity(record, candidate)
        security_candidates = MatchProvider._security_candidates(candidate)
        security_validity = evaluate_periods("security", security_candidates[0].validity_periods, record.observationDate) if security_candidates else not_applicable("security", "The decision has no security to evaluate.")
        self._add_validity_evidence(record, candidate, "entity", entity_validity, evidence)
        self._add_validity_evidence(record, candidate, "security", security_validity, evidence)
        entity = {"entityId": candidate.entity_id, "canonicalName": candidate.canonical_name, "entityType": candidate.entity_type}
        graph = self._relationship_graph(record, candidate, evidence)
        relationship_scope = relationship_validity(graph, record.observationDate)
        self._add_relationship_validity_evidence(record, relationship_scope, evidence)
        validity = combine_validity(record.observationDate, entity_validity, security_validity, relationship_scope)
        overall = validity["overall"]
        public_parent = (graph.get("issuer") if graph.get("issuer") and graph["issuer"].get("entityId") != candidate.entity_id else None)
        if not graph.get("edges") or relationship_scope.get("validOnObservationDate") is not False:
            public_parent = public_parent or candidate.public_parent
        status = "matched" if overall["validOnObservationDate"] is not False else "review_required"
        security_match = self._score_security(record, security_candidates[0], candidate.entity_id) if security_candidates else None
        security_status = "matched" if security_match and security_validity.get("validOnObservationDate") is not False else "review_required" if security_match else "not_applicable"
        return EntityMatchResult(record.recordId, status, 1.0 if status == "matched" else .8, [], evidence, mapping_version, matchedEntity=entity, matchedSecurity=security_match.security if security_status == "matched" else None, securityDecisionStatus=security_status, securityConfidence=security_match.confidence if security_match else 0, securityAlternatives=[] if security_status == "matched" else ([security_match] if security_match else []), publicParent=public_parent, relationshipGraph=graph, relationshipStatus=graph["status"], validity=validity, validOnObservationDate=overall["validOnObservationDate"], pointInTimeStatus=overall["status"], pointInTimeReason=overall["reason"], decisionSource=decision.source, decisionVersion=decision.version)

    def _score(self, record: EntityMatchInput, candidate: ProviderCandidate) -> CandidateMatch:
        w = self.config.weights
        evidence: list[MatchEvidence] = []
        raw = 0.0
        sources = candidate.sources or [candidate.provider]
        if len(sources) > 1:
            contribution = w.get("provider_agreement", 5) * (len(sources) - 1)
            raw += contribution
            evidence.append(MatchEvidence("provider_agreement", sources, candidate.entity_id, contribution, ",".join(sources), detail=f"{len(sources)} providers reconciled"))
        input_ids = {field: normalize_identifier(getattr(record, field), field) for field in ("cik", "lei") if getattr(record, field)}
        for field, value in input_ids.items():
            candidate_value = candidate.identifiers.get(field)
            if candidate_value == value:
                raw += w["exact_identifier"]
                evidence.append(MatchEvidence("identifier_match", value, candidate_value, w["exact_identifier"], candidate.provider, detail=field))
            elif candidate_value:
                raw += w["conflicting_identifier"]
                evidence.append(MatchEvidence("provider_disagreement", value, candidate_value, w["conflicting_identifier"], candidate.provider, detail=f"Conflicting {field}"))
        input_name = normalize_name(record.entityName or record.legalName or record.brandName)
        candidate_names = [normalize_name(candidate.canonical_name), *(normalize_name(a) for a in candidate.aliases)]
        similarity = max((_similarity(input_name, name) for name in candidate_names), default=0)
        if input_name and input_name in candidate_names:
            raw += w["exact_name"]
            evidence.append(MatchEvidence("name_similarity", input_name, normalize_name(candidate.canonical_name), w["exact_name"], candidate.provider, 1.0, "Exact normalized name"))
        elif similarity >= .75:
            contribution = w["high_name_similarity"] * similarity
            raw += contribution
            evidence.append(MatchEvidence("name_similarity", input_name, normalize_name(candidate.canonical_name), round(contribution, 2), candidate.provider, round(similarity, 4)))
        input_domain = normalize_domain(record.domain)
        if input_domain and input_domain == candidate.domain:
            raw += w["exact_domain"]
            evidence.append(MatchEvidence("domain_match", input_domain, candidate.domain, w["exact_domain"], candidate.provider))
        if record.country and candidate.country:
            contribution = w["country_match"] if record.country == candidate.country else w["country_conflict"]
            raw += contribution
            evidence.append(MatchEvidence("country_match" if contribution > 0 else "provider_disagreement", record.country, candidate.country, contribution, candidate.provider))
        if record.postalCode and candidate.postal_code and record.postalCode == candidate.postal_code:
            raw += w["postal_match"]
            evidence.append(MatchEvidence("address_match", record.postalCode, candidate.postal_code, w["postal_match"], candidate.provider, detail="postal code"))
        entity_validity = self._candidate_validity(record, candidate)
        raw += self._add_validity_evidence(record, candidate, "entity", entity_validity, evidence)
        # Saturating normalization rewards corroborating evidence without letting fuzzy-only matches auto-match.
        confidence = max(0.0, min(1.0, raw / 100 if raw <= 100 else .80 + .20 * (1 - 2 ** (-(raw - 100) / 50))))
        evidence_types = {item.type for item in evidence if item.scoreContribution > 0}
        strong_identifier_match = any(item.type == "identifier_match" and (item.detail in {"cik", "lei", "figi", "isin", "cusip"} or item.detail == "ticker and exchange") for item in evidence)
        temporal_invalid = entity_validity["validOnObservationDate"] is False
        if strong_identifier_match and not temporal_invalid and not any(item.scoreContribution < -50 for item in evidence):
            confidence = max(confidence, .99)
        elif input_name and input_name in candidate_names and "domain_match" in evidence_types and not temporal_invalid:
            confidence = max(confidence, .99)
        elif input_name and input_name in candidate_names and not temporal_invalid:
            confidence = max(confidence, .82)
        elif "domain_match" in evidence_types and not temporal_invalid:
            confidence = max(confidence, self.config.review_threshold)
        if similarity < 1 and not input_ids and not input_domain:
            confidence = min(confidence, .89)
        return CandidateMatch(candidate.entity_id, candidate.canonical_name, candidate.entity_type, round(confidence, 4), ",".join(sources), None, candidate.public_parent, evidence, entityValidity=entity_validity, securityValidity=not_applicable("security", "Security candidates are ranked independently."))

    @staticmethod
    def _provider_rank(candidate: ProviderCandidate) -> int:
        ranks = {"customer_security_master": 0, "sec": 1, "gleif": 2, "openfigi": 3}
        return min((ranks.get(source, 10) for source in (candidate.sources or [candidate.provider])), default=10)

    @staticmethod
    def _should_merge(left: ProviderCandidate, right: ProviderCandidate) -> bool:
        strong = {"cik", "lei"}
        for field in strong:
            left_value, right_value = left.identifiers.get(field), right.identifiers.get(field)
            if left_value and right_value and left_value != right_value:
                return False
        left_name, right_name = normalize_name(left.canonical_name), normalize_name(right.canonical_name)
        shared_strong = any(left.identifiers.get(field) and left.identifiers.get(field) == right.identifiers.get(field) for field in strong)
        if shared_strong:
            if left.provider == right.provider and left.entity_id != right.entity_id:
                return False
            return _similarity(left_name, right_name) >= .80
        left_strong = {field for field in strong if left.identifiers.get(field)}
        right_strong = {field for field in strong if right.identifiers.get(field)}
        if left_name and left_name == right_name and left_strong and right_strong:
            return True
        left_ticker, right_ticker = left.identifiers.get("ticker"), right.identifiers.get("ticker")
        if left_ticker and left_ticker == right_ticker and _similarity(left_name, right_name) >= .90:
            return True
        if left.domain and left.domain == right.domain and _similarity(left_name, right_name) >= .85:
            return True
        return False

    def _merge(self, left: ProviderCandidate, right: ProviderCandidate) -> ProviderCandidate:
        winner, other = (left, right) if self._provider_rank(left) <= self._provider_rank(right) else (right, left)
        sources = list(dict.fromkeys([*(winner.sources or [winner.provider]), *(other.sources or [other.provider])]))
        identifiers = {**other.identifiers, **winner.identifiers}
        relationships = []
        for relationship in [*winner.relationships, *other.relationships]:
            key = (relationship.get("fromEntityId"), relationship.get("toEntityId"), relationship.get("relationshipType"))
            existing_relationship = next((item for item in relationships if (item.get("fromEntityId"), item.get("toEntityId"), item.get("relationshipType")) == key), None)
            if existing_relationship:
                existing_relationship.setdefault("periods", [])
                for value in relationship.get("periods") or []:
                    if value not in existing_relationship["periods"]:
                        existing_relationship["periods"].append(value)
            else:
                relationships.append(relationship)
        entity_periods = []
        for value in [*winner.entity_periods, *other.entity_periods]:
            if value not in entity_periods:
                entity_periods.append(value)
        valid_from = max(value for value in (winner.valid_from, other.valid_from) if value) if winner.valid_from or other.valid_from else None
        valid_to = min(value for value in (winner.valid_to, other.valid_to) if value) if winner.valid_to or other.valid_to else None
        return ProviderCandidate(
            entity_id=winner.entity_id,
            canonical_name=winner.canonical_name,
            entity_type=winner.entity_type,
            provider=winner.provider,
            domain=winner.domain or other.domain,
            country=winner.country or other.country,
            postal_code=winner.postal_code or other.postal_code,
            identifiers=identifiers,
            security=None,
            public_parent=winner.public_parent or other.public_parent,
            relationships=relationships,
            valid_from=valid_from,
            valid_to=valid_to,
            entity_periods=entity_periods,
            security_periods=[],
            aliases=list(dict.fromkeys([*winner.aliases, other.canonical_name, *other.aliases])),
            sources=sources,
        )

    def _reconcile(self, candidates: list[ProviderCandidate]) -> list[ProviderCandidate]:
        reconciled: list[ProviderCandidate] = []
        for candidate in candidates:
            if not candidate.sources:
                candidate.sources = [candidate.provider]
            for index, existing in enumerate(reconciled):
                if self._should_merge(existing, candidate):
                    reconciled[index] = self._merge(existing, candidate)
                    break
            else:
                reconciled.append(candidate)
        return reconciled

    @staticmethod
    def _security_provider_rank(candidate: ProviderSecurityCandidate) -> int:
        ranks = {"customer_security_master": 0, "openfigi": 1, "sec": 2}
        return min((ranks.get(source, 10) for source in (candidate.sources or [candidate.provider])), default=10)

    @staticmethod
    def _security_issuer_agrees(left: ProviderSecurityCandidate, right: ProviderSecurityCandidate) -> bool:
        if left.issuer_entity_id == right.issuer_entity_id:
            return True
        for field in ("cik", "lei"):
            if left.issuer_identifiers.get(field) and left.issuer_identifiers.get(field) == right.issuer_identifiers.get(field):
                return True
        return _similarity(normalize_name(left.issuer_name), normalize_name(right.issuer_name)) >= .85

    @classmethod
    def _should_merge_security(cls, left: ProviderSecurityCandidate, right: ProviderSecurityCandidate) -> bool:
        strong = ("figi", "isin", "cusip")
        for field in strong:
            left_value, right_value = left.identifiers.get(field), right.identifiers.get(field)
            if left_value and right_value and left_value != right_value:
                return False
        if any(left.identifiers.get(field) and left.identifiers.get(field) == right.identifiers.get(field) for field in strong):
            return True
        same_ticker = left.identifiers.get("ticker") and left.identifiers.get("ticker") == right.identifiers.get("ticker")
        if not same_ticker or not cls._security_issuer_agrees(left, right):
            return False
        left_exchange, right_exchange = left.identifiers.get("exchange"), right.identifiers.get("exchange")
        return not left_exchange or not right_exchange or left_exchange == right_exchange or "US" in {left_exchange, right_exchange}

    def _merge_security(self, left: ProviderSecurityCandidate, right: ProviderSecurityCandidate) -> ProviderSecurityCandidate:
        winner, other = (left, right) if self._security_provider_rank(left) <= self._security_provider_rank(right) else (right, left)
        periods = []
        for value in [*winner.validity_periods, *other.validity_periods]:
            if value not in periods:
                periods.append(value)
        return ProviderSecurityCandidate(
            security_id=winner.security_id,
            issuer_entity_id=winner.issuer_entity_id,
            canonical_name=winner.canonical_name,
            provider=winner.provider,
            identifiers={**other.identifiers, **winner.identifiers},
            security={**other.security, **winner.security},
            issuer_identifiers={**other.issuer_identifiers, **winner.issuer_identifiers},
            issuer_name=winner.issuer_name or other.issuer_name,
            validity_periods=periods,
            sources=list(dict.fromkeys([*(winner.sources or [winner.provider]), *(other.sources or [other.provider])])),
        )

    def _reconcile_securities(self, candidates: list[ProviderSecurityCandidate]) -> list[ProviderSecurityCandidate]:
        reconciled: list[ProviderSecurityCandidate] = []
        for candidate in candidates:
            if not candidate.sources:
                candidate.sources = [candidate.provider]
            for index, existing in enumerate(reconciled):
                if self._should_merge_security(existing, candidate):
                    reconciled[index] = self._merge_security(existing, candidate)
                    break
            else:
                reconciled.append(candidate)
        return reconciled

    @staticmethod
    def _resolved_issuer_id(candidate: ProviderSecurityCandidate, entities: list[ProviderCandidate]) -> str:
        exact = next((entity for entity in entities if entity.entity_id == candidate.issuer_entity_id), None)
        if exact:
            return exact.entity_id
        for field in ("cik", "lei"):
            value = candidate.issuer_identifiers.get(field)
            if value:
                match = next((entity for entity in entities if entity.identifiers.get(field) == value), None)
                if match:
                    return match.entity_id
        named = sorted(entities, key=lambda entity: _similarity(normalize_name(candidate.issuer_name), normalize_name(entity.canonical_name)), reverse=True)
        if named and _similarity(normalize_name(candidate.issuer_name), normalize_name(named[0].canonical_name)) >= .85:
            return named[0].entity_id
        return candidate.issuer_entity_id

    def _score_security(self, record: EntityMatchInput, candidate: ProviderSecurityCandidate, selected_entity_id: str | None, entities: list[ProviderCandidate] | None = None) -> SecurityCandidateMatch:
        w = self.config.weights
        evidence: list[MatchEvidence] = []
        raw = 0.0
        sources = candidate.sources or [candidate.provider]
        if len(sources) > 1:
            contribution = w.get("provider_agreement", 5) * (len(sources) - 1)
            raw += contribution
            evidence.append(MatchEvidence("security_provider_agreement", sources, candidate.security_id, contribution, ",".join(sources), detail=f"{len(sources)} providers reconciled this security"))
        input_ids = {field: normalize_identifier(getattr(record, field), field) for field in ("figi", "isin", "cusip") if getattr(record, field)}
        for field, value in input_ids.items():
            candidate_value = candidate.identifiers.get(field)
            if candidate_value == value:
                raw += w["exact_identifier"]
                evidence.append(MatchEvidence("security_identifier_match", value, candidate_value, w["exact_identifier"], candidate.provider, detail=field))
            elif candidate_value:
                raw += w["conflicting_identifier"]
                evidence.append(MatchEvidence("security_identifier_conflict", value, candidate_value, w["conflicting_identifier"], candidate.provider, detail=field))
        ticker, exchange = normalize_identifier(record.ticker), normalize_identifier(record.exchange)
        ticker_exact = bool(ticker and ticker == candidate.identifiers.get("ticker"))
        exchange_exact = bool(exchange and exchange == candidate.identifiers.get("exchange"))
        exchange_compatible = exchange_exact or bool(exchange and candidate.identifiers.get("exchange") == "US")
        if ticker_exact:
            contribution = w["ticker_exchange"] if exchange and exchange_compatible else w["ticker_exchange"] * .5
            raw += contribution
            evidence.append(MatchEvidence("security_identifier_match", ticker, candidate.identifiers.get("ticker"), contribution, candidate.provider, detail="ticker and exchange" if contribution == w["ticker_exchange"] else "ticker only"))
        issuer_entity_id = self._resolved_issuer_id(candidate, entities or [])
        issuer_linked = bool(selected_entity_id and issuer_entity_id == selected_entity_id)
        if issuer_linked:
            raw += 20
            evidence.append(MatchEvidence("security_issuer_link", issuer_entity_id, selected_entity_id, 20, candidate.provider, detail="Security issuer matches the selected entity."))
        validity = evaluate_periods("security", candidate.validity_periods, record.observationDate)
        if record.observationDate and validity["status"] != "not_applicable":
            contribution = w["security_date_valid"] if validity["validOnObservationDate"] is True else w["security_date_invalid"] if validity["validOnObservationDate"] is False else 0
            raw += contribution
            evidence.append(MatchEvidence("security_observation_date_validity", record.observationDate, [{"validFrom": item.get("validFrom"), "validTo": item.get("validTo"), "periodType": item.get("periodType")} for item in validity["periods"]], contribution, candidate.provider, detail=f"{validity['status']}: {validity['reason']}"))
        confidence = max(0.0, min(1.0, raw / 100 if raw <= 100 else .80 + .20 * (1 - 2 ** (-(raw - 100) / 50))))
        strong_exact = any(item.type == "security_identifier_match" and item.detail in {"figi", "isin", "cusip"} for item in evidence)
        conflict = any(item.scoreContribution < -50 for item in evidence)
        invalid = validity["validOnObservationDate"] is False
        if strong_exact and not conflict and not invalid:
            confidence = max(confidence, .99)
        elif ticker_exact and exchange and exchange_compatible and issuer_linked and not conflict and not invalid:
            confidence = max(confidence, .99)
        elif ticker_exact and not exchange:
            confidence = min(confidence, .79)
        payload = {**candidate.security, **candidate.identifiers, "securityId": candidate.security_id, "issuerEntityId": issuer_entity_id, "canonicalName": candidate.canonical_name}
        return SecurityCandidateMatch(candidate.security_id, issuer_entity_id, candidate.canonical_name, round(confidence, 4), ",".join(sources), dict(candidate.identifiers), payload, evidence, validity)

    def _rank_securities(self, record: EntityMatchInput, candidates: list[ProviderSecurityCandidate], selected_entity_id: str | None, entities: list[ProviderCandidate]) -> list[SecurityCandidateMatch]:
        reconciled = self._reconcile_securities(candidates)
        return sorted((self._score_security(record, candidate, selected_entity_id, entities) for candidate in reconciled), key=lambda value: value.confidence, reverse=True)[:self.config.max_candidates]

    def _forced_decision(self, record: EntityMatchInput) -> EntityMatchResult | None:
        if self.overrides:
            override = self.overrides.resolve(record)
            if override:
                return self._decision_result(record, override)
        rule = self.rules.resolve(record)
        if rule:
            return self._decision_result(record, rule)
        return None

    @staticmethod
    def _conflict_probes(record: EntityMatchInput) -> list[EntityMatchInput]:
        """Split a mixed record into independent exact-name/identifier lookups."""
        strong_fields = ("cik", "lei", "figi", "isin", "cusip")
        supplied = [field for field in strong_fields if getattr(record, field)]
        input_name = record.entityName or record.legalName or record.brandName
        if not supplied or (len(supplied) == 1 and not input_name):
            return []
        probes = []
        if input_name:
            probes.append(EntityMatchInput(
                recordId=f"{record.recordId}::conflict::name",
                entityName=input_name,
                observationDate=record.observationDate,
                source=record.source,
                metadata={"conflictProbe": "exact_name", "sourceRecordId": record.recordId},
            ))
        for field in supplied:
            probes.append(EntityMatchInput(
                recordId=f"{record.recordId}::conflict::{field}",
                observationDate=record.observationDate,
                source=record.source,
                metadata={"conflictProbe": field, "sourceRecordId": record.recordId},
                **{field: getattr(record, field)},
            ))
        return probes

    def _identifier_conflicts(self, record: EntityMatchInput, entities: list[ProviderCandidate], security_values: list[ProviderSecurityCandidate]) -> tuple[list[MatchEvidence], list[MatchEvidence]]:
        """Return entity-level and security-level conflicts backed by exact signals."""
        entity_by_id = {candidate.entity_id: candidate for candidate in entities}

        def identity_key(entity_id: str) -> str:
            candidate = entity_by_id.get(entity_id)
            return normalize_name(candidate.canonical_name) if candidate and normalize_name(candidate.canonical_name) else entity_id

        strong_signals: list[dict] = []
        for field in ("cik", "lei"):
            value = normalize_identifier(getattr(record, field), field)
            if not value:
                continue
            for candidate in entities:
                if candidate.identifiers.get(field) == value:
                    strong_signals.append({"field": field, "value": value, "entityId": candidate.entity_id, "entityName": candidate.canonical_name, "provider": ",".join(candidate.sources or [candidate.provider])})

        securities = self._reconcile_securities(security_values)
        security_signals: list[dict] = []
        for field in ("figi", "isin", "cusip"):
            value = normalize_identifier(getattr(record, field), field)
            if not value:
                continue
            for candidate in securities:
                if candidate.identifiers.get(field) == value:
                    issuer_id = self._resolved_issuer_id(candidate, entities)
                    signal = {"field": field, "value": value, "securityId": candidate.security_id, "entityId": issuer_id, "entityName": (entity_by_id.get(issuer_id) or candidate).canonical_name, "provider": ",".join(candidate.sources or [candidate.provider])}
                    security_signals.append(signal)
                    strong_signals.append(signal)

        entity_evidence: list[MatchEvidence] = []
        strong_keys = {identity_key(signal["entityId"]) for signal in strong_signals}
        if len(strong_keys) > 1:
            entity_evidence.append(MatchEvidence(
                "identifier_conflict", strong_signals, sorted(strong_keys), -100, "conflict_discovery",
                detail="Exact strong identifiers resolve to different entities.",
            ))

        input_name = normalize_name(record.entityName or record.legalName or record.brandName)
        exact_name_signals = [
            {"entityId": candidate.entity_id, "entityName": candidate.canonical_name, "provider": ",".join(candidate.sources or [candidate.provider])}
            for candidate in entities
            if input_name and input_name in {normalize_name(candidate.canonical_name), *(normalize_name(alias) for alias in candidate.aliases)}
        ]
        exact_name_keys = {identity_key(signal["entityId"]) for signal in exact_name_signals}
        if strong_keys and exact_name_keys - strong_keys:
            entity_evidence.append(MatchEvidence(
                "exact_name_identifier_conflict",
                {"strongIdentifiers": strong_signals, "exactName": input_name, "exactNameCandidates": exact_name_signals},
                {"strongTargets": sorted(strong_keys), "exactNameTargets": sorted(exact_name_keys)},
                -100, "conflict_discovery", detail="An exact name and a strong identifier resolve to different entities.",
            ))

        security_evidence: list[MatchEvidence] = []
        security_ids = {signal["securityId"] for signal in security_signals}
        if len(security_ids) > 1:
            security_evidence.append(MatchEvidence(
                "security_identifier_conflict", security_signals, sorted(security_ids), -100, "conflict_discovery",
                detail="Exact strong security identifiers resolve to different securities.",
            ))
        return entity_evidence, security_evidence

    def _finalize(self, record: EntityMatchInput, candidate_values: list[ProviderCandidate], security_values: list[ProviderSecurityCandidate], provider_errors: list[str], probe_warnings: list[str] | None = None) -> EntityMatchResult:
        candidates = self._reconcile(candidate_values)
        conflict_evidence, security_conflict_evidence = self._identifier_conflicts(record, candidates, security_values)
        ranked = list(self._score(record, candidate) for candidate in candidates)
        preliminary_securities = self._rank_securities(record, security_values, None, candidates)
        if preliminary_securities:
            security_best = preliminary_securities[0]
            exact_security_id = any(item.type == "security_identifier_match" and item.detail in {"figi", "isin", "cusip", "ticker and exchange"} for item in security_best.evidence)
            security_ambiguous = len(preliminary_securities) > 1 and preliminary_securities[1].confidence >= security_best.confidence - .02 and preliminary_securities[1].issuerEntityId != security_best.issuerEntityId
            if exact_security_id and not security_ambiguous:
                linked = next((candidate for candidate in ranked if candidate.entityId == security_best.issuerEntityId), None)
                if linked:
                    linked.confidence = max(linked.confidence, .99)
                    linked.evidence.append(MatchEvidence("security_issuer_link", security_best.securityId, linked.entityId, 100, security_best.provider, detail="An exact security identifier resolved this issuer."))
        ranked = sorted(ranked, key=lambda candidate: candidate.confidence, reverse=True)[:self.config.max_candidates]
        best = ranked[0] if ranked else None
        if not best:
            status = "provider_error" if provider_errors and len(provider_errors) == len(self.providers) else "unmatched"
            security_status = "provider_error" if status == "provider_error" and any((record.ticker, record.figi, record.isin, record.cusip)) else "unmatched" if any((record.ticker, record.figi, record.isin, record.cusip)) else "not_applicable"
            diagnostics = [MatchEvidence("provider_disagreement", detail=e) for e in provider_errors]
            diagnostics.extend(MatchEvidence("conflict_probe_error", provider="conflict_discovery", detail=warning) for warning in (probe_warnings or []))
            return EntityMatchResult(record.recordId, status, 0, [], diagnostics, self.config.mapping_version, securityDecisionStatus=security_status, pointInTimeReason="No dated provider evidence was available.")
        temporal_conflict = (best.entityValidity or {}).get("validOnObservationDate") is False
        best.evidence.extend(conflict_evidence)
        has_conflict = any(e.scoreContribution < -50 for e in best.evidence) or temporal_conflict
        best_has_strong_identifier = any(e.type == "identifier_match" and e.detail in {"cik", "lei"} for e in best.evidence)
        best_has_exact_name = any(e.type == "name_similarity" and e.similarity == 1.0 for e in best.evidence)
        alternative_has_exact_name = any(any(e.type == "name_similarity" and e.similarity == 1.0 for e in alternative.evidence) for alternative in ranked[1:])
        if best_has_strong_identifier and not best_has_exact_name and alternative_has_exact_name:
            has_conflict = True
            best.evidence.append(MatchEvidence("provider_disagreement", detail="Strong identifier and exact name point to different candidates.", scoreContribution=-100))
        for error in provider_errors:
            best.evidence.append(MatchEvidence("provider_disagreement", provider="provider_runtime", detail=error, scoreContribution=0))
        for warning in probe_warnings or []:
            best.evidence.append(MatchEvidence("conflict_probe_error", provider="conflict_discovery", detail=warning, scoreContribution=0))
        entity_status = "matched" if best.confidence >= self.config.auto_match_threshold and not has_conflict else "review_required" if best.confidence >= self.config.review_threshold or has_conflict else "unmatched"
        graph = None
        public_parent = best.publicParent if entity_status != "unmatched" else None
        validity = None
        selected = None
        relationship_scope = not_applicable("relationships", "No entity was selected.")
        if entity_status != "unmatched":
            selected = next((candidate for candidate in candidates if candidate.entity_id == best.entityId), None)
            if selected:
                graph = self._relationship_graph(record, selected, best.evidence)
                relationship_scope = relationship_validity(graph, record.observationDate)
                self._add_relationship_validity_evidence(record, relationship_scope, best.evidence)
                public_parent = (graph.get("issuer") if graph.get("issuer") and graph["issuer"].get("entityId") != selected.entity_id else None)
                if not graph.get("edges") or relationship_scope.get("validOnObservationDate") is not False:
                    public_parent = public_parent or best.publicParent
        has_security_query = any((record.ticker, record.figi, record.isin, record.cusip))
        security_ranked = self._rank_securities(record, security_values, best.entityId if entity_status != "unmatched" else None, candidates)
        security_best = security_ranked[0] if security_ranked else None
        security_conflict = bool(security_conflict_evidence or (security_best and any(item.scoreContribution < -50 for item in security_best.evidence)))
        security_ambiguous = bool(security_best and len(security_ranked) > 1 and security_ranked[1].confidence >= security_best.confidence - .02 and security_ranked[1].securityId != security_best.securityId)
        if not has_security_query:
            security_status = "not_applicable"
        elif not security_best:
            security_status = "unmatched"
        elif security_best.confidence >= self.config.auto_match_threshold and not security_conflict and not security_ambiguous:
            security_status = "matched"
        elif security_best.confidence >= self.config.review_threshold or security_conflict or security_ambiguous or (security_best.validity or {}).get("validOnObservationDate") is False:
            security_status = "review_required"
        else:
            security_status = "unmatched"
        if security_best and has_security_query:
            best.evidence.extend(security_best.evidence)
            best.evidence.extend(security_conflict_evidence)
            if security_ambiguous:
                best.evidence.append(MatchEvidence("security_candidate_ambiguity", security_best.securityId, security_ranked[1].securityId, -100, "security_ranker", detail="Top security candidates are too close to auto-select."))
        security_scope = security_best.validity if security_best and has_security_query else not_applicable("security", "No security lookup was requested for this record.")
        if selected:
            validity = combine_validity(record.observationDate, best.entityValidity or evaluate_periods("entity", [], record.observationDate), security_scope, relationship_scope)
        status = entity_status
        if entity_status != "unmatched" and has_security_query and security_status != "matched":
            status = "review_required"
        if validity and validity["overall"]["validOnObservationDate"] is False:
            status = "review_required"
        matched_entity = {"entityId": best.entityId, "canonicalName": best.canonicalName, "entityType": best.entityType} if entity_status != "unmatched" else None
        alternatives = ranked[1:] if entity_status != "unmatched" else ranked
        overall = validity["overall"] if validity else {"status": "not_verified", "validOnObservationDate": None, "reason": "No candidate was selected for complete temporal evaluation."}
        return EntityMatchResult(record.recordId, status, best.confidence, alternatives, best.evidence, self.config.mapping_version, matchedEntity=matched_entity, matchedSecurity=security_best.security if security_status == "matched" and security_best else None, securityDecisionStatus=security_status, securityConfidence=security_best.confidence if security_best else 0, securityAlternatives=security_ranked[1:] if security_status == "matched" else security_ranked, publicParent=public_parent, relationshipGraph=graph, relationshipStatus=graph["status"] if graph else "not_resolved", validity=validity, validOnObservationDate=overall["validOnObservationDate"], pointInTimeStatus=overall["status"], pointInTimeReason=overall["reason"])

    def match_batch(self, records: list[EntityMatchInput]) -> list[EntityMatchResult]:
        results: dict[str, EntityMatchResult] = {}
        unresolved = []
        for record in records:
            forced = self._forced_decision(record)
            if forced:
                results[record.recordId] = forced
            else:
                unresolved.append(record)
        candidates: dict[str, list[ProviderCandidate]] = {record.recordId: [] for record in unresolved}
        securities: dict[str, list[ProviderSecurityCandidate]] = {record.recordId: [] for record in unresolved}
        errors: dict[str, list[str]] = {record.recordId: [] for record in unresolved}
        probe_warnings: dict[str, list[str]] = {record.recordId: [] for record in unresolved}
        probes: list[EntityMatchInput] = []
        probe_owners: dict[str, str] = {}
        for record in unresolved:
            for probe in self._conflict_probes(record):
                probes.append(probe)
                probe_owners[probe.recordId] = record.recordId
        for provider in self.providers:
            try:
                provider_results = provider.search_bundle_batch(unresolved, self.config.max_candidates)
                for record in unresolved:
                    bundle = provider_results.get(record.recordId)
                    if bundle:
                        candidates[record.recordId].extend(bundle.entities)
                        securities[record.recordId].extend(bundle.securities)
            except Exception as exc:  # provider failures must not fail the entire record
                for record in unresolved:
                    errors[record.recordId].append(f"{provider.name}: {exc}")
                continue
            if probes:
                try:
                    provider_probes = provider.search_bundle_batch(probes, self.config.max_candidates)
                    for probe in probes:
                        bundle = provider_probes.get(probe.recordId)
                        owner = probe_owners[probe.recordId]
                        if bundle:
                            candidates[owner].extend(bundle.entities)
                            securities[owner].extend(bundle.securities)
                except Exception as exc:  # base results remain usable when best-effort probing fails
                    for owner in set(probe_owners.values()):
                        probe_warnings[owner].append(f"{provider.name}: conflict discovery unavailable: {exc}")
        for record in unresolved:
            results[record.recordId] = self._finalize(record, candidates[record.recordId], securities[record.recordId], errors[record.recordId], probe_warnings[record.recordId])
        for record in records:
            result = results[record.recordId]
            result.sourceRecord = dict(record.sourceRecord)
            result.sourceMetadata = dict(record.metadata)
        return [results[record.recordId] for record in records]

    def match(self, record: EntityMatchInput) -> EntityMatchResult:
        return self.match_batch([record])[0]
