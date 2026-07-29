from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from symbologylink.cli import main
from symbologylink.decision_policies import DecisionPolicySet, derive_primary_pathway
from symbologylink.decisions import RuleSet
from symbologylink.engine import MatchEngine
from symbologylink.models import EntityMatchInput, MatchEvidence
from symbologylink.providers import MatchProvider, ProviderCandidate, ProviderCapabilities


class StaticProvider(MatchProvider):
    name = "static"
    capabilities = ProviderCapabilities(entity_lookup=True, security_lookup=True, security_effective_dates=True)

    def __init__(self, candidates):
        self.candidates = candidates

    def search(self, input_record, limit=20):
        return self.candidates[:limit]


class PathwayDerivationTests(unittest.TestCase):
    def test_pathway_precedence_uses_strongest_evidence(self):
        evidence = [
            MatchEvidence("name_similarity", similarity=1.0),
            MatchEvidence("domain_match"),
            MatchEvidence("identifier_match", detail="cik"),
        ]
        self.assertEqual(derive_primary_pathway(evidence), "exact_cik")
        self.assertEqual(
            derive_primary_pathway([MatchEvidence("name_similarity", similarity=1.0), MatchEvidence("domain_match")]),
            "exact_name_and_domain",
        )
        self.assertEqual(
            derive_primary_pathway([MatchEvidence("name_similarity", similarity=.91), MatchEvidence("country_match")]),
            "fuzzy_name_and_country",
        )
        self.assertEqual(
            derive_primary_pathway([MatchEvidence("security_identifier_match", detail="ticker only")]),
            "ticker_only",
        )
        self.assertEqual(derive_primary_pathway([MatchEvidence("human_override")]), "human_override")
        self.assertEqual(
            derive_primary_pathway([
                MatchEvidence("brand_inference"),
                MatchEvidence("name_similarity", similarity=1.0),
                MatchEvidence("domain_match"),
            ]),
            "brand_inference",
        )

    def test_each_supported_evidence_pathway_is_derived(self):
        cases = {
            "exact_cik": [MatchEvidence("identifier_match", detail="cik")],
            "exact_lei": [MatchEvidence("identifier_match", detail="lei")],
            "exact_figi": [MatchEvidence("security_identifier_match", detail="figi")],
            "exact_isin": [MatchEvidence("security_identifier_match", detail="isin")],
            "exact_cusip": [MatchEvidence("security_identifier_match", detail="cusip")],
            "ticker_and_exchange": [MatchEvidence("security_identifier_match", detail="ticker and exchange")],
            "exact_name_and_domain": [MatchEvidence("name_similarity", similarity=1.0), MatchEvidence("domain_match")],
            "exact_domain": [MatchEvidence("domain_match")],
            "exact_name": [MatchEvidence("name_similarity", similarity=1.0)],
            "fuzzy_name_and_country": [MatchEvidence("name_similarity", similarity=.9), MatchEvidence("country_match")],
            "fuzzy_name_only": [MatchEvidence("name_similarity", similarity=.9)],
            "customer_rule": [MatchEvidence("reusable_rule_match")],
            "human_override": [MatchEvidence("human_override")],
            "relationship_traversal": [MatchEvidence("relationship_resolution")],
            "brand_inference": [MatchEvidence("brand_inference")],
            "ticker_only": [MatchEvidence("security_identifier_match", detail="ticker only")],
            "unknown": [],
        }
        for expected, evidence in cases.items():
            with self.subTest(pathway=expected):
                self.assertEqual(derive_primary_pathway(evidence), expected)

    def test_default_fuzzy_only_policy_never_auto_matches(self):
        policies = DecisionPolicySet()
        self.assertEqual(policies.decide("fuzzy_name_only", 1.0), "review_required")
        self.assertEqual(policies.decide("fuzzy_name_and_country", 1.0), "review_required")

    def test_threshold_boundaries_are_evaluated_before_display_rounding(self):
        policies = DecisionPolicySet()
        self.assertEqual(policies.decide("exact_cik", .97999), "review_required")
        self.assertEqual(policies.decide("exact_cik", .98), "matched")


class DecisionPolicyIntegrationTests(unittest.TestCase):
    def test_result_records_pathway_and_uncalibrated_match_score(self):
        candidate = ProviderCandidate("entity:one", "Example Corporation", provider="static", domain="example.test")
        result = MatchEngine([StaticProvider([candidate])]).match(
            EntityMatchInput("record", entityName="Example Corporation", domain="example.test")
        )
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.primaryPathway, "exact_name_and_domain")
        self.assertEqual(result.matchScore, result.confidence)
        self.assertFalse(result.scoreIsCalibrated)
        payload = result.to_dict()
        self.assertEqual(payload["schema_version"], "2.0")
        self.assertIn("match_score", payload["entity"])
        self.assertIn("score_is_calibrated", payload["entity"])
        self.assertIn("match_pathway", payload["entity"])

    def test_security_result_records_its_own_pathway_and_score(self):
        candidate = ProviderCandidate(
            "entity:one", "Example Corporation", provider="static", domain="example.test",
            security={"internal_security_id": "security:one", "ticker": "EXM", "exchange": "XNAS"},
            security_periods=[{"periodType": "security_listing", "validFrom": "2020-01-01"}],
        )
        result = MatchEngine([StaticProvider([candidate])]).match(
            EntityMatchInput("record", entityName="Example Corporation", domain="example.test", ticker="EXM", exchange="NASDAQ", observationDate="2025-01-01")
        )
        self.assertEqual(result.securityDecisionStatus, "matched")
        self.assertEqual(result.securityPrimaryPathway, "ticker_and_exchange")
        self.assertEqual(result.securityMatchScore, result.securityConfidence)
        self.assertFalse(result.securityScoreIsCalibrated)

    def test_ticker_and_exchange_requires_verified_active_listing(self):
        candidate = ProviderCandidate(
            "entity:one", "Example Corporation", provider="static", domain="example.test",
            security={"internal_security_id": "security:one", "ticker": "EXM", "exchange": "XNAS"},
        )
        result = MatchEngine([StaticProvider([candidate])]).match(
            EntityMatchInput("record", entityName="Example Corporation", domain="example.test", ticker="EXM", exchange="NASDAQ")
        )
        self.assertEqual(result.securityPrimaryPathway, "ticker_and_exchange")
        self.assertEqual(result.securityDecisionStatus, "review_required")
        self.assertIsNone(result.matchedSecurity)

    def test_brand_origin_takes_precedence_over_exact_name_and_domain(self):
        candidate = ProviderCandidate("entity:one", "Example Brand", provider="static", domain="brand.test")
        result = MatchEngine([StaticProvider([candidate])]).match(
            EntityMatchInput("record", brandName="Example Brand", domain="brand.test")
        )
        self.assertEqual(result.primaryPathway, "brand_inference")
        self.assertEqual(result.status, "review_required")

    def test_json_policy_override_changes_exact_name_decision(self):
        candidate = ProviderCandidate("entity:one", "Example Corporation", provider="static")
        record = EntityMatchInput("record", entityName="Example Corporation")
        default_result = MatchEngine([StaticProvider([candidate])]).match(record)
        self.assertEqual(default_result.status, "review_required")
        self.assertEqual(default_result.primaryPathway, "exact_name")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policies.json"
            path.write_text(json.dumps({"decision_policies": {"exact_name": {
                "allow_auto_match": True,
                "minimum_match_score": .82,
            }}}), encoding="utf-8")
            configured = MatchEngine(
                [StaticProvider([candidate])],
                decision_policies=DecisionPolicySet.load(path),
            ).match(record)
        self.assertEqual(configured.status, "matched")

    def test_customer_rule_uses_its_own_configurable_pathway(self):
        rules = RuleSet([{
            "id": "example-rule",
            "conditions": {"entity_name": {"normalized_exact": "Example Corporation"}},
            "result": {"entity": {"entity_id": "entity:one", "canonical_name": "Example Corporation"}},
        }])
        default_result = MatchEngine([], rules=rules).match(EntityMatchInput("record", entityName="Example Corporation"))
        self.assertEqual(default_result.status, "matched")
        self.assertEqual(default_result.primaryPathway, "customer_rule")
        self.assertEqual(default_result.matchScore, 1.0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policies.json"
            path.write_text(json.dumps({"customer_rule": {
                "allow_auto_match": False,
                "review_minimum_score": 0,
            }}), encoding="utf-8")
            configured = MatchEngine(
                [], rules=rules, decision_policies=DecisionPolicySet.load(path),
            ).match(EntityMatchInput("record", entityName="Example Corporation"))
        self.assertEqual(configured.status, "review_required")

    def test_customer_rule_can_require_verified_active_security(self):
        rules = RuleSet([{
            "id": "security-rule",
            "conditions": {"entity_name": {"normalized_exact": "Example Corporation"}},
            "result": {
                "entity": {"entity_id": "entity:one", "canonical_name": "Example Corporation"},
                "security": {"securityId": "security:one", "ticker": "EXM", "exchange": "XNAS"},
            },
        }])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policies.json"
            path.write_text(json.dumps({"customer_rule": {
                "allow_auto_match": True,
                "minimum_match_score": 0,
                "review_minimum_score": 0,
                "require_active_security": True,
            }}), encoding="utf-8")
            result = MatchEngine(
                [], rules=rules, decision_policies=DecisionPolicySet.load(path),
            ).match(EntityMatchInput("record", entityName="Example Corporation", ticker="EXM", exchange="NASDAQ"))
        self.assertEqual(result.primaryPathway, "customer_rule")
        self.assertEqual(result.securityDecisionStatus, "review_required")
        self.assertIsNone(result.matchedSecurity)

    def test_exact_identifier_conflict_forces_review_even_if_policy_allows_conflicts(self):
        candidates = [
            ProviderCandidate("entity:cik", "CIK Target", provider="static", identifiers={"cik": "0000000001"}),
            ProviderCandidate("entity:lei", "LEI Target", provider="static", identifiers={"lei": "LEI0000000000000001"}),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policies.json"
            path.write_text(json.dumps({"decision_policies": {"exact_cik": {
                "allow_auto_match": True,
                "minimum_match_score": 0,
                "review_minimum_score": 0,
                "require_no_conflicts": False,
            }}}), encoding="utf-8")
            result = MatchEngine(
                [StaticProvider(candidates)],
                decision_policies=DecisionPolicySet.load(path),
            ).match(EntityMatchInput("record", cik="0000000001", lei="LEI0000000000000001"))
        self.assertEqual(result.status, "review_required")
        self.assertTrue(any(item.type == "identifier_conflict" for item in result.evidence))

    def test_cli_legacy_thresholds_warn_and_use_legacy_policy_set(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            records = directory / "records.csv"
            reference = directory / "reference.csv"
            mapping = directory / "mapping.json"
            output = directory / "results.jsonl"
            records.write_text("record_id,company_name\n1,Example Corporation\n", encoding="utf-8")
            reference.write_text("internal_entity_id,canonical_name\nentity:one,Example Corporation\n", encoding="utf-8")
            mapping.write_text(json.dumps({"mapping": {"record_id": "recordId", "company_name": "entityName"}}), encoding="utf-8")
            argv = [
                "symbologylink", "resolve", "--input", str(records), "--mapping", str(mapping),
                "--reference", str(reference), "--cache", str(directory / "cache.sqlite3"),
                "--output", str(output), "--auto-threshold", ".82", "--review-threshold", ".50",
            ]
            stderr = StringIO()
            with patch("sys.argv", argv), redirect_stdout(StringIO()), redirect_stderr(stderr):
                self.assertEqual(main(), 0)
            row = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["entity"]["status"], "verified")
        self.assertEqual(row["schema_version"], "2.0")
        self.assertIn("deprecated", stderr.getvalue())

    def test_invalid_legacy_thresholds_return_structured_cli_error(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            records = directory / "records.csv"
            reference = directory / "reference.csv"
            mapping = directory / "mapping.json"
            records.write_text("record_id,company_name\n1,Example Corporation\n", encoding="utf-8")
            reference.write_text("internal_entity_id,canonical_name\nentity:one,Example Corporation\n", encoding="utf-8")
            mapping.write_text(json.dumps({"mapping": {"record_id": "recordId", "company_name": "entityName"}}), encoding="utf-8")
            argv = [
                "symbologylink", "resolve", "--input", str(records), "--mapping", str(mapping),
                "--reference", str(reference), "--cache", str(directory / "cache.sqlite3"),
                "--output", str(directory / "results.jsonl"),
                "--auto-threshold", ".70", "--review-threshold", ".80",
            ]
            stderr = StringIO()
            with patch("sys.argv", argv), redirect_stdout(StringIO()), redirect_stderr(stderr):
                self.assertEqual(main(), 2)
            error = json.loads(stderr.getvalue().splitlines()[-1])
        self.assertEqual(error["error"], "IngestionError")
        self.assertIn("Invalid legacy thresholds", error["message"])

    def test_policy_configuration_rejects_unknown_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policies.json"
            path.write_text(json.dumps({"exact_cik": {"allow_aut_match": True}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unknown field"):
                DecisionPolicySet.load(path)

    def test_finance_negative_pair_baseline_is_seeded(self):
        fixture = Path(__file__).parent / "fixtures" / "finance_negative_pairs.json"
        rows = json.loads(fixture.read_text(encoding="utf-8"))
        self.assertEqual(
            {row["id"] for row in rows},
            {
                "meta-platforms-vs-meta-financial",
                "apple-bank-vs-apple-inc",
                "metabank-vs-meta",
                "ticker-reuse-requires-context",
            },
        )


if __name__ == "__main__":
    unittest.main()
