from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from symbologylink.cli import cmd_provider_test, mapping_fingerprint
from symbologylink.engine import MatchEngine
from symbologylink.models import EntityMatchInput
from symbologylink.providers import (
    GLEIFProvider,
    LocalSecurityMasterProvider,
    MatchProvider,
    OpenFIGIProvider,
    ProviderCandidate,
    ProviderCapabilities,
    SECProvider,
    TrustLevel,
)


class CandidateProvider(MatchProvider):
    capabilities = ProviderCapabilities(entity_lookup=True)

    def __init__(self, name: str, trust_level: TrustLevel, *, entity_id: str, cik: str = "0000000001"):
        self.name = name
        self.trust_level = trust_level
        self.entity_id = entity_id
        self.cik = cik

    def search(self, input_record, limit=20):
        return [ProviderCandidate(
            self.entity_id, "Example Corporation", provider=self.name,
            domain="example.test", identifiers={"cik": self.cik},
        )]


class RelationshipProvider(CandidateProvider):
    capabilities = ProviderCapabilities(entity_lookup=True, current_relationships=True)

    def search(self, input_record, limit=20):
        candidate = super().search(input_record, limit)[0]
        candidate.entity_type = "subsidiary"
        candidate.relationships = [{
            "fromEntityId": candidate.entity_id,
            "toEntityId": "entity:parent",
            "toEntity": {"entityId": "entity:parent", "canonicalName": "Parent Corporation", "entityType": "issuer"},
            "relationshipType": "subsidiary_of",
            "provider": self.name,
        }]
        return [candidate]


class TemporalProvider(CandidateProvider):
    def __init__(self, *, dates: bool):
        super().__init__("temporal", TrustLevel.SUPPORTING, entity_id="entity:temporal")
        self.capabilities = ProviderCapabilities(entity_lookup=True, entity_effective_dates=dates)

    def search(self, input_record, limit=20):
        candidate = super().search(input_record, limit)[0]
        candidate.entity_periods = [{"periodType": "entity_existence", "validFrom": "2020-01-01", "provider": self.name}]
        return [candidate]


class UnsupportedShareClassProvider(CandidateProvider):
    capabilities = ProviderCapabilities(entity_lookup=True, security_lookup=True, security_effective_dates=True)

    def __init__(self):
        super().__init__("unsupported_share_class", TrustLevel.SUPPORTING, entity_id="entity:issuer")

    def search(self, input_record, limit=20):
        candidate = super().search(input_record, limit)[0]
        candidate.security = {
            "internal_security_id": "security:one", "figi": "BBG000TEST",
            "ticker": "TEST", "exchange": "XNAS", "share_class": "Class A",
        }
        candidate.security_periods = [{"periodType": "security_listing", "validFrom": "2020-01-01", "provider": self.name}]
        return [candidate]


class ProviderMetadataTests(unittest.TestCase):
    def test_every_builtin_connector_declares_honest_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            master = Path(directory) / "master.csv"
            master.write_text(
                "internal_entity_id,internal_security_id,canonical_name,ticker,share_class,parent_entity_id,relationship_valid_from,security_valid_from\n"
                "entity:one,security:one,Example Corporation,EXM,A,entity:parent,2020-01-01,2020-01-01\n",
                encoding="utf-8",
            )
            customer = LocalSecurityMasterProvider(master)

        self.assertEqual(customer.metadata()["trust_level"], "authoritative")
        self.assertTrue(customer.capabilities.security_lookup)
        self.assertTrue(customer.capabilities.share_class_data)
        self.assertTrue(customer.capabilities.relationship_effective_dates)
        self.assertEqual(GLEIFProvider.capabilities.relationship_effective_dates, True)
        self.assertEqual(GLEIFProvider.trust_level, TrustLevel.SUPPORTING)
        self.assertEqual(SECProvider.capabilities.identifier_mapping, ("cik", "ticker"))
        self.assertFalse(SECProvider.capabilities.relationship_effective_dates)
        self.assertTrue(OpenFIGIProvider.capabilities.share_class_data)
        self.assertFalse(OpenFIGIProvider.capabilities.security_effective_dates)

    def test_trust_level_changes_parent_decision(self):
        supporting = MatchEngine([RelationshipProvider("customer_relationship_master", TrustLevel.SUPPORTING, entity_id="entity:child")]).match(
            EntityMatchInput("supporting", entityName="Example Corporation", domain="example.test")
        )
        authoritative = MatchEngine([RelationshipProvider("customer_relationship_master", TrustLevel.AUTHORITATIVE, entity_id="entity:child")]).match(
            EntityMatchInput("authoritative", entityName="Example Corporation", domain="example.test")
        )
        self.assertEqual(supporting.parentStatus, "candidate")
        self.assertEqual(supporting.status, "review_required")
        self.assertEqual(authoritative.parentStatus, "verified")
        self.assertEqual(authoritative.status, "matched")

    def test_authoritative_provider_wins_reconciliation_regardless_of_input_order(self):
        supporting = CandidateProvider("supporting", TrustLevel.SUPPORTING, entity_id="entity:supporting")
        authoritative = CandidateProvider("authoritative", TrustLevel.AUTHORITATIVE, entity_id="entity:authoritative")
        for providers in ([supporting, authoritative], [authoritative, supporting]):
            result = MatchEngine(list(providers)).match(EntityMatchInput("trust", cik="0000000001"))
            self.assertEqual(result.matchedEntity["entityId"], "entity:authoritative")

    def test_undeclared_temporal_claim_is_rejected(self):
        rejected = MatchEngine([TemporalProvider(dates=False)]).match(
            EntityMatchInput("rejected", entityName="Example Corporation", domain="example.test", observationDate="2025-01-01")
        )
        accepted = MatchEngine([TemporalProvider(dates=True)]).match(
            EntityMatchInput("accepted", entityName="Example Corporation", domain="example.test", observationDate="2025-01-01")
        )
        self.assertEqual(rejected.validity["entity"]["status"], "not_verified")
        self.assertTrue(any(item.type == "provider_capability_rejected" for item in rejected.evidence))
        self.assertEqual(accepted.validity["entity"]["status"], "verified")
        payload = rejected.to_dict()
        self.assertEqual(payload["provider_metadata"]["temporal"]["trust_level"], "supporting")
        self.assertEqual(len(payload["mapping_fingerprint_sha256"]), 64)

    def test_undeclared_share_class_claim_is_removed(self):
        result = MatchEngine([UnsupportedShareClassProvider()]).match(EntityMatchInput(
            "security", entityName="Example Corporation", domain="example.test",
            figi="BBG000TEST", observationDate="2025-01-01",
        ))
        self.assertEqual(result.securityDecisionStatus, "matched")
        self.assertNotIn("share_class", result.matchedSecurity)
        self.assertTrue(any(item.type == "provider_capability_rejected" and "share_class_data" in (item.detail or "") for item in result.securityEvidence))

    def test_authoritative_conflict_is_contradicted(self):
        first = CandidateProvider("first", TrustLevel.AUTHORITATIVE, entity_id="entity:first", cik="0000000001")
        second = CandidateProvider("second", TrustLevel.AUTHORITATIVE, entity_id="entity:second", cik="0000000002")
        result = MatchEngine([first, second]).match(EntityMatchInput(
            "conflict", entityName="Example Corporation", domain="example.test",
        ))
        payload = result.to_dict()
        self.assertEqual(result.status, "review_required")
        self.assertEqual(payload["entity"]["status"], "contradicted")
        self.assertTrue(any(item["type"] == "authoritative_provider_conflict" for item in payload["entity"]["evidence"]))

    def test_provider_matrix_and_fingerprint_include_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            master = Path(directory) / "master.csv"
            master.write_text("internal_entity_id,canonical_name\nentity:one,Example Corporation\n", encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                cmd_provider_test(SimpleNamespace(
                    reference=str(master), gleif=False, sec=False, openfigi=False,
                    cache=str(Path(directory) / "cache.sqlite3"), offline=True,
                    sec_user_agent=None, openfigi_api_key=None,
                ))
            matrix = json.loads(output.getvalue())
        metadata = matrix["capabilityTrustMatrix"][0]
        self.assertEqual(metadata["trust_level"], "authoritative")
        self.assertIn("entity_lookup", metadata["enabled_capabilities"])
        supporting = {"p": {**metadata, "trust_level": "supporting"}}
        authoritative = {"p": {**metadata, "trust_level": "authoritative"}}
        self.assertNotEqual(mapping_fingerprint("v1", "a" * 64, supporting), mapping_fingerprint("v1", "a" * 64, authoritative))


if __name__ == "__main__":
    unittest.main()
