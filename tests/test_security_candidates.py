from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from symbologylink.engine import MatchEngine
from symbologylink.models import EntityMatchInput
from symbologylink.providers import LocalSecurityMasterProvider, MatchProvider, ProviderCandidate


class StaticProvider(MatchProvider):
    def __init__(self, name: str, candidates: list[ProviderCandidate]):
        self.name = name
        self.candidates = candidates

    def search(self, input_record, limit=20):
        return self.candidates[:limit]


class SecurityCandidatePipelineTests(unittest.TestCase):
    def test_customer_master_allows_multiple_securities_for_one_consistent_entity(self):
        content = """internal_entity_id,internal_security_id,canonical_name,entity_type,domain,ticker,exchange,figi,cik
issuer:dual,security:a,Dual Class Corp,issuer,dual.test,DUA,NYSE,BBGCLASSA,0000000003
issuer:dual,security:b,Dual Class Corp,issuer,dual.test,DUB,NYSE,BBGCLASSB,0000000003
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "master.csv"
            path.write_text(content, encoding="utf-8")
            provider = LocalSecurityMasterProvider(path)
            result = MatchEngine([provider]).match(EntityMatchInput("r", entityName="Dual Class Corp", domain="dual.test", figi="BBGCLASSB"))
        self.assertEqual(result.matchedEntity["entityId"], "issuer:dual")
        self.assertEqual(result.matchedSecurity["securityId"], "security:b")
        self.assertEqual(result.securityAlternatives[0].securityId, "security:a")

    def test_provider_bundle_expands_sec_listings_into_security_candidates(self):
        entity = ProviderCandidate(
            "sec:1", "Example Corp", provider="sec", identifiers={"cik": "0000000001"},
            security={"listings": [{"ticker": "EXA", "exchange": "NYSE"}, {"ticker": "EXB", "exchange": "NASDAQ"}]},
        )
        provider = StaticProvider("sec", [entity])
        bundle = provider.search_bundle_batch([EntityMatchInput("r", ticker="EXA")])["r"]
        self.assertEqual(len(bundle.entities), 1)
        self.assertEqual(len(bundle.securities), 2)
        self.assertEqual({item.identifiers["ticker"] for item in bundle.securities}, {"EXA", "EXB"})

    def test_exact_figi_selects_security_independently_and_resolves_issuer(self):
        wanted = ProviderCandidate(
            "issuer:wanted", "Wanted Holdings", provider="openfigi", identifiers={"cik": "0000000001", "figi": "BBG000WANTED", "ticker": "WNT", "exchange": "US"},
            security={"securityId": "BBG000WANTED", "figi": "BBG000WANTED", "ticker": "WNT", "exchange": "US"},
        )
        other = ProviderCandidate(
            "issuer:other", "Other Holdings", provider="openfigi", identifiers={"cik": "0000000002", "figi": "BBG000OTHER", "ticker": "OTH", "exchange": "US"},
            security={"securityId": "BBG000OTHER", "figi": "BBG000OTHER", "ticker": "OTH", "exchange": "US"},
        )
        result = MatchEngine([StaticProvider("openfigi", [other, wanted])]).match(EntityMatchInput("r", figi="BBG000WANTED"))
        self.assertEqual(result.matchedEntity["entityId"], "issuer:wanted")
        self.assertEqual(result.matchedSecurity["securityId"], "BBG000WANTED")
        self.assertEqual(result.securityDecisionStatus, "matched")
        self.assertTrue(any(item.type == "security_issuer_link" for item in result.evidence))
        self.assertTrue(all(item.security is None for item in result.alternatives))

    def test_two_securities_for_one_issuer_are_ranked_as_securities(self):
        common = {"entity_id": "issuer:dual", "canonical_name": "Dual Class Corp", "provider": "customer_security_master", "domain": "dual.test", "identifiers": {"cik": "0000000003"}}
        class_a = ProviderCandidate(**common, security={"internal_security_id": "security:a", "figi": "BBGCLASSA", "ticker": "DUA", "exchange": "NYSE"})
        class_b = ProviderCandidate(**common, security={"internal_security_id": "security:b", "figi": "BBGCLASSB", "ticker": "DUB", "exchange": "NYSE"})
        result = MatchEngine([StaticProvider("customer_security_master", [class_a, class_b])]).match(EntityMatchInput("r", entityName="Dual Class Corp", domain="dual.test", figi="BBGCLASSB"))
        self.assertEqual(result.matchedEntity["entityId"], "issuer:dual")
        self.assertEqual(result.matchedSecurity["securityId"], "security:b")
        self.assertEqual(result.securityAlternatives[0].securityId, "security:a")
        self.assertGreater(result.securityConfidence, result.securityAlternatives[0].confidence)

    def test_ticker_only_ambiguity_does_not_auto_select_a_security(self):
        base = {"entity_id": "issuer:ambiguous", "canonical_name": "Ambiguous Corp", "provider": "customer_security_master", "domain": "ambiguous.test", "identifiers": {"cik": "0000000004"}}
        first = ProviderCandidate(**base, security={"internal_security_id": "security:first", "figi": "BBGFIRST", "ticker": "AMB"})
        second = ProviderCandidate(**base, security={"internal_security_id": "security:second", "figi": "BBGSECOND", "ticker": "AMB"})
        result = MatchEngine([StaticProvider("customer_security_master", [first, second])]).match(EntityMatchInput("r", entityName="Ambiguous Corp", domain="ambiguous.test", ticker="AMB"))
        self.assertEqual(result.matchedEntity["entityId"], "issuer:ambiguous")
        self.assertIsNone(result.matchedSecurity)
        self.assertIn(result.securityDecisionStatus, {"review_required", "unmatched"})
        self.assertEqual(len(result.securityAlternatives), 2)
        self.assertEqual(result.status, "review_required")

    def test_entity_match_without_security_lookup_leaves_security_unselected(self):
        candidate = ProviderCandidate(
            "issuer:plain", "Plain Corp", provider="customer_security_master", domain="plain.test",
            security={"internal_security_id": "security:plain", "ticker": "PLN", "exchange": "NYSE"},
        )
        result = MatchEngine([StaticProvider("customer_security_master", [candidate])]).match(EntityMatchInput("r", entityName="Plain Corp", domain="plain.test"))
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.securityDecisionStatus, "not_applicable")
        self.assertIsNone(result.matchedSecurity)
        self.assertEqual(len(result.securityAlternatives), 1)


if __name__ == "__main__":
    unittest.main()
