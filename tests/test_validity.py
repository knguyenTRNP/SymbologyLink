from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path

from symbologylink.engine import MatchEngine
from symbologylink.models import EntityMatchInput
from symbologylink.providers import LocalSecurityMasterProvider
from symbologylink.validity import evaluate_periods, validate_periods


class PeriodEvaluationTests(unittest.TestCase):
    def test_alternative_periods_use_any_and_relationship_chain_uses_all(self):
        periods = [
            {"periodType": "listing", "validFrom": "2010-01-01", "validTo": "2015-12-31"},
            {"periodType": "listing", "validFrom": "2020-01-01"},
        ]
        self.assertEqual(evaluate_periods("security", periods, "2022-01-01")["status"], "verified")
        self.assertEqual(evaluate_periods("relationships", periods, "2022-01-01", require_all=True)["status"], "invalid")
        self.assertEqual(evaluate_periods("security", [{"validFrom": "2010-01-01", "status": "DELISTED"}], "2012-01-01")["status"], "not_verified")
        with self.assertRaisesRegex(ValueError, "validFrom after validTo"):
            validate_periods([{"validFrom": "2022-01-01", "validTo": "2020-01-01"}], "test period")


class IndependentPipelineValidityTests(unittest.TestCase):
    MASTER = """internal_entity_id,internal_security_id,canonical_name,entity_type,domain,ticker,exchange,parent_entity_id,parent_name,parent_entity_type,relationship_type,entity_valid_from,entity_valid_to,security_valid_from,security_valid_to,relationship_valid_from,relationship_valid_to
entity:operating,security:operating,Example Operating,subsidiary,example.test,EXM,NASDAQ,entity:parent,Example Parent,issuer,subsidiary_of,2000-01-01,,2010-01-01,2020-12-31,2015-01-01,
entity:parent,,Example Parent,issuer,,,,,,,,1990-01-01,,,,,
"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        master = Path(self.directory.name) / "master.csv"
        master.write_text(self.MASTER, encoding="utf-8")
        self.provider = LocalSecurityMasterProvider(master)
        self.engine = MatchEngine([self.provider])

    def tearDown(self):
        self.directory.cleanup()

    def match(self, observation_date=None):
        return self.engine.match(EntityMatchInput("v", entityName="Example Operating", domain="example.test", ticker="EXM", exchange="NASDAQ", observationDate=observation_date))

    def test_each_scope_can_be_verified_independently(self):
        result = self.match("2018-06-30")
        self.assertEqual(result.status, "review_required")
        self.assertEqual(result.parentStatus, "candidate")
        self.assertEqual(result.validity["entity"]["status"], "verified")
        self.assertEqual(result.validity["security"]["status"], "verified")
        self.assertEqual(result.validity["relationships"]["status"], "verified")
        self.assertEqual(result.validity["overall"]["status"], "verified")
        self.assertTrue(result.validOnObservationDate)

    def test_expired_security_does_not_invalidate_entity_or_relationship(self):
        result = self.match("2022-06-30")
        self.assertEqual(result.status, "review_required")
        self.assertEqual(result.validity["entity"]["status"], "verified")
        self.assertEqual(result.validity["security"]["status"], "invalid")
        self.assertEqual(result.validity["relationships"]["status"], "verified")
        self.assertEqual(result.validity["overall"]["status"], "invalid")
        self.assertFalse(result.validOnObservationDate)
        self.assertTrue(any(item.type == "security_observation_date_validity" and item.scoreContribution < 0 for item in result.securityEvidence))

    def test_future_relationship_is_invalid_without_rewriting_other_scopes(self):
        result = self.match("2012-06-30")
        self.assertEqual(result.status, "review_required")
        self.assertEqual(result.validity["entity"]["status"], "verified")
        self.assertEqual(result.validity["security"]["status"], "verified")
        self.assertEqual(result.validity["relationships"]["status"], "invalid")
        self.assertIsNone(result.publicParent)

    def test_no_observation_date_is_not_requested_for_every_applicable_scope(self):
        result = self.match()
        self.assertEqual(result.status, "review_required")
        self.assertEqual(result.securityDecisionStatus, "review_required")
        self.assertEqual(result.validity["entity"]["status"], "not_requested")
        self.assertEqual(result.validity["security"]["status"], "not_requested")
        self.assertEqual(result.validity["relationships"]["status"], "not_requested")
        self.assertEqual(result.validity["overall"]["status"], "not_requested")
        self.assertIsNone(result.validOnObservationDate)

    def test_legacy_validity_applies_only_to_entity_scope(self):
        content = "internal_entity_id,canonical_name,entity_type,parent_entity_id,parent_name,relationship_type,valid_from,valid_to\nlegacy,Legacy Sub,subsidiary,parent,Parent,subsidiary_of,2010-01-01,\n"
        path = Path(self.directory.name) / "legacy.csv"
        path.write_text(content, encoding="utf-8")
        candidate = LocalSecurityMasterProvider(path).candidates[0]
        self.assertEqual(candidate.entity_periods[0]["validFrom"], "2010-01-01")
        self.assertEqual(candidate.relationships[0]["validFrom"], None)
        self.assertEqual(candidate.security_periods, [])

    def test_customer_json_accepts_multiple_typed_periods(self):
        payload = [{
            "internal_entity_id": "multi", "internal_security_id": "multi-sec", "canonical_name": "Multi Period", "entity_type": "issuer", "ticker": "MUL",
            "entity_periods": [{"periodType": "entity_existence", "validFrom": "2000-01-01"}],
            "security_periods": [
                {"periodType": "security_listing", "validFrom": "2010-01-01", "validTo": "2015-12-31"},
                {"periodType": "security_listing", "validFrom": "2020-01-01"},
            ],
        }]
        path = Path(self.directory.name) / "periods.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        provider = LocalSecurityMasterProvider(path)
        self.assertEqual(len(provider.candidates[0].security_periods), 2)
        result = MatchEngine([provider]).match(EntityMatchInput("multi", entityName="Multi Period", ticker="MUL", observationDate="2022-01-01"))
        self.assertEqual(result.validity["security"]["status"], "verified")
        self.assertEqual(len(result.validity["security"]["periods"]), 2)


if __name__ == "__main__":
    unittest.main()
