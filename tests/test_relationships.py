from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from symbologylink.decisions import RuleSet
from symbologylink.engine import MatchEngine
from symbologylink.models import EntityMatchInput
from symbologylink.providers import GLEIFProvider, LocalSecurityMasterProvider, ProviderCandidate
from symbologylink.relationships import RelationshipResolver


class CustomerRelationshipTests(unittest.TestCase):
    def test_brand_subsidiary_issuer_chain_and_point_in_time(self):
        content = """internal_entity_id,canonical_name,entity_type,domain,lei,parent_entity_id,parent_name,parent_entity_type,relationship_type,relationship_valid_from,relationship_valid_to
brand:acme,Acme Product,brand,acme.test,LEIBRAND000000000001,entity:sub,Acme Operations,subsidiary,brand_of,2020-01-01,
entity:sub,Acme Operations,subsidiary,,LEISUB00000000000002,entity:issuer,Acme Holdings,issuer,subsidiary_of,2018-01-01,
entity:issuer,Acme Holdings,issuer,,LEIISSUER00000000003,,,,,
"""
        with tempfile.TemporaryDirectory() as directory:
            master = Path(directory) / "master.csv"
            master.write_text(content, encoding="utf-8")
            provider = LocalSecurityMasterProvider(master)
            engine = MatchEngine([provider])
            result = engine.match(EntityMatchInput("r1", brandName="Acme Product", domain="acme.test", observationDate="2024-06-30"))
            historical = engine.match(EntityMatchInput("r1-old", brandName="Acme Product", domain="acme.test", observationDate="2019-06-30"))

        self.assertEqual(result.status, "matched")
        self.assertEqual(result.relationshipStatus, "resolved")
        self.assertEqual([item["entityId"] for item in result.relationshipGraph["chain"]], ["brand:acme", "entity:sub", "entity:issuer"])
        self.assertEqual(result.relationshipGraph["directParent"]["entityId"], "entity:sub")
        self.assertEqual(result.relationshipGraph["ultimateParent"]["entityId"], "entity:issuer")
        self.assertEqual(result.relationshipGraph["issuer"]["entityId"], "entity:issuer")
        self.assertEqual(result.publicParent["entityId"], "entity:issuer")
        self.assertEqual(result.relationshipGraph["pointInTimeStatus"], "verified")
        self.assertTrue(result.relationshipGraph["validOnObservationDate"])
        self.assertTrue(result.relationshipGraph["complete"])
        self.assertTrue(any(item.type == "relationship_resolution" for item in result.evidence))
        self.assertEqual(historical.relationshipGraph["pointInTimeStatus"], "invalid")
        self.assertFalse(historical.relationshipGraph["validOnObservationDate"])
        self.assertIsNone(historical.relationshipGraph["directParent"])
        self.assertEqual(historical.status, "review_required")
        self.assertEqual(historical.validity["relationships"]["status"], "invalid")

    def test_rule_supplied_relationship_is_preserved(self):
        rules = RuleSet([{
            "id": "brand-parent", "priority": 100, "conditions": {"brand_name": {"normalized_exact": "Widget"}},
            "result": {
                "entity": {"entityId": "brand:widget", "canonicalName": "Widget", "entityType": "brand"},
                "relationships": [{
                    "fromEntityId": "brand:widget", "toEntityId": "entity:widget-issuer", "relationshipType": "brand_of",
                    "toEntity": {"entityId": "entity:widget-issuer", "canonicalName": "Widget Holdings", "entityType": "issuer"},
                    "status": "INACTIVE", "periods": [
                        {"periodType": "brand_of", "validFrom": "2010-01-01", "validTo": "2011-12-31"},
                        {"periodType": "brand_of", "validFrom": "2021-01-01", "validTo": "2024-12-31"}
                    ],
                }],
            },
        }])
        result = MatchEngine([], rules=rules).match(EntityMatchInput("r2", brandName="Widget", observationDate="2023-01-01"))
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.relationshipGraph["issuer"]["entityId"], "entity:widget-issuer")
        self.assertEqual(result.relationshipGraph["pointInTimeStatus"], "verified")
        self.assertTrue(result.relationshipGraph["complete"])
        self.assertEqual(result.relationshipGraph["edges"][0]["providers"], ["rule:brand-parent"])
        self.assertEqual(len(result.validity["relationships"]["periods"]), 2)
        self.assertEqual(result.validity["relationships"]["status"], "verified")


class StubGLEIFProvider(GLEIFProvider):
    def __init__(self, responses):
        super().__init__(retries=0)
        self.responses = responses
        self.requested = []

    def _request(self, path, params, query_type):
        self.requested.append(path)
        return self.responses.get(path, {"data": None})


class GLEIFRelationshipTests(unittest.TestCase):
    def test_direct_and_ultimate_parent_traversal_with_periods_and_exception(self):
        child, parent = "CHILDLEI000000000001", "PARENTLEI00000000001"
        relationship = {
            "id": "rr-direct", "type": "relationship-records",
            "attributes": {"relationship": {
                "startNode": {"id": child}, "endNode": {"id": parent},
                "type": "IS_DIRECTLY_CONSOLIDATED_BY", "status": "ACTIVE",
                "periods": [{"type": "RELATIONSHIP_PERIOD", "startDate": "2020-01-01T00:00:00Z"}],
            }, "registration": {"validationSources": "FULLY_CORROBORATED", "lastUpdateDate": "2025-01-01T00:00:00Z"}},
        }
        ultimate = {
            "id": "rr-ultimate", "type": "relationship-records",
            "attributes": {"relationship": {
                "startNode": {"id": child}, "endNode": {"id": parent},
                "relationshipType": "IS_ULTIMATELY_CONSOLIDATED_BY", "relationshipStatus": "ACTIVE",
                "relationshipPeriods": [{"periodType": "RELATIONSHIP_PERIOD", "startDate": "2020-01-01T00:00:00Z"}],
            }},
        }
        parent_record = {"id": parent, "type": "lei-records", "attributes": {"lei": parent, "entity": {"legalName": {"name": "Parent Holdings"}, "legalAddress": {"country": "US"}, "status": "ACTIVE"}}}
        exception = {"id": "exception-1", "attributes": {"category": "DIRECT_ACCOUNTING_CONSOLIDATION_PARENT", "reason": "NO_KNOWN_PERSON", "validFrom": "2020-01-01"}}
        responses = {
            f"/lei-records/{child}/direct-parent-relationship": {"data": relationship},
            f"/lei-records/{parent}": {"data": parent_record},
            f"/lei-records/{parent}/direct-parent-relationship": {"data": None},
            f"/lei-records/{parent}/direct-parent-reporting-exception": {"data": exception},
            f"/lei-records/{child}/ultimate-parent-relationship": {"data": ultimate},
        }
        provider = StubGLEIFProvider(responses)
        candidate = ProviderCandidate("customer:child", "Child LLC", "legal_entity", "customer_security_master", identifiers={"lei": child}, sources=["customer_security_master", "gleif"])
        graph = RelationshipResolver([provider]).resolve(candidate, "2024-01-01", 8)

        self.assertEqual(graph["status"], "resolved")
        self.assertEqual(graph["subject"]["entityId"], "customer:child")
        self.assertEqual(graph["directParent"]["canonicalName"], "Parent Holdings")
        self.assertEqual(graph["ultimateParent"]["canonicalName"], "Parent Holdings")
        self.assertEqual(graph["accountingDirectParent"]["canonicalName"], "Parent Holdings")
        self.assertEqual(graph["accountingUltimateParent"]["canonicalName"], "Parent Holdings")
        self.assertEqual(graph["pointInTimeStatus"], "verified")
        self.assertTrue(graph["validOnObservationDate"])
        self.assertTrue(graph["complete"])
        self.assertEqual(graph["reportingExceptions"][0]["reason"], "NO_KNOWN_PERSON")
        self.assertIn(f"/lei-records/{parent}/direct-parent-relationship", provider.requested)
        self.assertTrue(all(edge["provenance"] for edge in graph["edges"]))


if __name__ == "__main__":
    unittest.main()
