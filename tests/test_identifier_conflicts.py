from __future__ import annotations

import unittest

from symbologylink.engine import MatchEngine
from symbologylink.models import EntityMatchInput
from symbologylink.providers import MatchProvider, ProviderCandidate


class PrecedenceProvider(MatchProvider):
    """Mimics a provider that returns only its first matching lookup branch."""

    name = "precedence_provider"

    def __init__(self, by_field):
        self.by_field = by_field

    def search(self, input_record, limit=20):
        for field in ("cik", "lei", "figi", "isin", "cusip"):
            value = getattr(input_record, field)
            if value and (field, value) in self.by_field:
                return [self.by_field[(field, value)]]
        name = input_record.entityName or input_record.legalName or input_record.brandName
        return [self.by_field[("name", name)]] if name and ("name", name) in self.by_field else []


class ProbeFailureProvider(MatchProvider):
    name = "probe_failure_provider"

    def __init__(self, candidate):
        self.candidate = candidate

    def search(self, input_record, limit=20):
        if "::conflict::" in input_record.recordId:
            raise RuntimeError("probe endpoint unavailable")
        return [self.candidate]


class IdentifierConflictDiscoveryTests(unittest.TestCase):
    def test_name_probe_discovers_candidate_hidden_by_identifier_precedence(self):
        by_cik = ProviderCandidate("entity:identifier", "Different Holdings", provider="precedence_provider", identifiers={"cik": "0000000001"})
        by_name = ProviderCandidate("entity:name", "Right Company", provider="precedence_provider", identifiers={"cik": "0000000002"})
        engine = MatchEngine([PrecedenceProvider({("cik", "0000000001"): by_cik, ("name", "Right Company"): by_name})])

        result = engine.match(EntityMatchInput("r", entityName="Right Company", cik="0000000001"))

        self.assertEqual(result.status, "review_required")
        self.assertEqual(result.matchedEntity["entityId"], "entity:identifier")
        self.assertIn("entity:name", {item.entityId for item in result.alternatives})
        conflict = next(item for item in result.evidence if item.type == "exact_name_identifier_conflict")
        self.assertEqual(conflict.provider, "conflict_discovery")
        self.assertLess(conflict.scoreContribution, 0)

    def test_separate_identifier_probes_discover_two_entity_targets(self):
        by_cik = ProviderCandidate("entity:cik", "CIK Target", provider="precedence_provider", identifiers={"cik": "0000000001"})
        by_lei = ProviderCandidate("entity:lei", "LEI Target", provider="precedence_provider", identifiers={"lei": "LEI0000000000000001"})
        engine = MatchEngine([PrecedenceProvider({("cik", "0000000001"): by_cik, ("lei", "LEI0000000000000001"): by_lei})])

        result = engine.match(EntityMatchInput("r", cik="0000000001", lei="LEI0000000000000001"))

        self.assertEqual(result.status, "review_required")
        conflict = next(item for item in result.evidence if item.type == "identifier_conflict")
        self.assertEqual({signal["field"] for signal in conflict.input}, {"cik", "lei"})
        self.assertEqual({signal["entityId"] for signal in conflict.input}, {"entity:cik", "entity:lei"})

    def test_complementary_identifiers_with_the_same_exact_name_reconcile(self):
        by_cik = ProviderCandidate("entity:cik", "Acme Corporation", provider="precedence_provider", identifiers={"cik": "0000000001"})
        by_lei = ProviderCandidate("entity:lei", "Acme Corp", provider="precedence_provider", identifiers={"lei": "LEI0000000000000001"})
        engine = MatchEngine([PrecedenceProvider({("cik", "0000000001"): by_cik, ("lei", "LEI0000000000000001"): by_lei})])

        result = engine.match(EntityMatchInput("r", cik="0000000001", lei="LEI0000000000000001"))

        self.assertEqual(result.status, "matched")
        self.assertFalse(any(item.type == "identifier_conflict" for item in result.evidence))

    def test_probe_failure_preserves_base_candidate_and_reports_diagnostic(self):
        candidate = ProviderCandidate("entity:base", "Base Company", provider="probe_failure_provider", identifiers={"cik": "0000000001"})
        result = MatchEngine([ProbeFailureProvider(candidate)]).match(EntityMatchInput("r", entityName="Base Company", cik="0000000001"))

        self.assertEqual(result.status, "matched")
        self.assertEqual(result.matchedEntity["entityId"], "entity:base")
        warning = next(item for item in result.evidence if item.type == "conflict_probe_error")
        self.assertIn("probe endpoint unavailable", warning.detail)

    def test_entity_and_security_identifiers_disagree_on_issuer(self):
        by_cik = ProviderCandidate("entity:cik", "CIK Target", provider="precedence_provider", identifiers={"cik": "0000000001"})
        by_figi = ProviderCandidate(
            "entity:figi", "FIGI Issuer", provider="precedence_provider", identifiers={"figi": "BBG000CONFLICT"},
            security={"securityId": "BBG000CONFLICT", "figi": "BBG000CONFLICT"},
        )
        engine = MatchEngine([PrecedenceProvider({("cik", "0000000001"): by_cik, ("figi", "BBG000CONFLICT"): by_figi})])

        result = engine.match(EntityMatchInput("r", cik="0000000001", figi="BBG000CONFLICT"))

        self.assertEqual(result.status, "review_required")
        self.assertEqual(result.securityDecisionStatus, "matched")
        self.assertTrue(any(item.type == "identifier_conflict" for item in result.evidence))

    def test_security_identifiers_resolving_to_different_instruments_force_review(self):
        by_figi = ProviderCandidate(
            "entity:issuer", "Shared Issuer", provider="precedence_provider", identifiers={"cik": "0000000003", "figi": "BBG000FIRST"},
            security={"securityId": "security:first", "figi": "BBG000FIRST"},
        )
        by_isin = ProviderCandidate(
            "entity:issuer", "Shared Issuer", provider="precedence_provider", identifiers={"cik": "0000000003", "isin": "US0000000002"},
            security={"securityId": "security:second", "isin": "US0000000002"},
        )
        engine = MatchEngine([PrecedenceProvider({("figi", "BBG000FIRST"): by_figi, ("isin", "US0000000002"): by_isin})])

        result = engine.match(EntityMatchInput("r", figi="BBG000FIRST", isin="US0000000002"))

        self.assertEqual(result.status, "review_required")
        self.assertEqual(result.securityDecisionStatus, "review_required")
        self.assertIsNone(result.matchedSecurity)
        conflict = next(item for item in result.securityEvidence if item.type == "security_identifier_conflict")
        self.assertEqual({signal["field"] for signal in conflict.input}, {"figi", "isin"})


if __name__ == "__main__":
    unittest.main()
