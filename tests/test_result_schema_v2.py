from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from symbologylink.cli import cmd_export, cmd_migrate_results, main
from symbologylink.engine import MatchEngine
from symbologylink.models import EntityMatchInput, MatchEvidence, MatchResultV2, ResolutionComponent
from symbologylink.providers import MatchProvider, ProviderCandidate
from symbologylink.result_schema import legacy_to_v2, v2_to_legacy


class StaticProvider(MatchProvider):
    def __init__(self, candidates):
        self.name = "customer_security_master"
        self.candidates = candidates

    def search(self, input_record, limit=20):
        return self.candidates[:limit]


class ComponentResultTests(unittest.TestCase):
    def test_entity_success_with_unknown_parent_and_security_is_valid(self):
        candidate = ProviderCandidate("entity:one", "Example Company", "legal_entity", "customer_security_master", domain="example.test")
        payload = MatchEngine([StaticProvider([candidate])]).match(
            EntityMatchInput("one", entityName="Example Company", domain="example.test", ticker="ZZZZ")
        ).to_dict()

        self.assertEqual(payload["schema_version"], "2.0")
        self.assertEqual(payload["entity"]["status"], "verified")
        self.assertEqual(payload["public_parent"]["status"], "unknown")
        self.assertEqual(payload["security"]["status"], "unknown")
        self.assertEqual(payload["final_decision"], "entity_matched_security_unknown")
        self.assertNotIn("confidence", payload)

    def test_security_ambiguity_does_not_downgrade_verified_entity(self):
        common = {
            "entity_id": "issuer:alphabet", "canonical_name": "Alphabet Inc", "entity_type": "issuer",
            "provider": "customer_security_master", "domain": "abc.xyz",
        }
        goog = ProviderCandidate(**common, security={"internal_security_id": "security:goog", "ticker": "GOOG", "figi": "BBGGOOG"})
        googl = ProviderCandidate(**common, security={"internal_security_id": "security:googl", "ticker": "GOOG", "figi": "BBGGOOGL"})
        result = MatchEngine([StaticProvider([goog, googl])]).match(
            EntityMatchInput("alphabet", entityName="Alphabet Inc", domain="abc.xyz", ticker="GOOG")
        )
        payload = result.to_dict()

        self.assertEqual(payload["entity"]["status"], "verified")
        self.assertEqual(payload["security"]["status"], "ambiguous")
        self.assertEqual(len(payload["security"]["alternatives"]), 2)
        self.assertEqual(payload["final_decision"], "ambiguous")
        self.assertTrue(any(item["type"] == "security_candidate_ambiguity" for item in payload["security"]["evidence"]))
        self.assertFalse(any(item["type"] == "security_candidate_ambiguity" for item in payload["entity"]["evidence"]))

    def test_private_entity_has_an_explicit_final_decision(self):
        candidate = ProviderCandidate("entity:private", "Private LLC", "legal_entity", "customer_security_master", domain="private.test")
        payload = MatchEngine([StaticProvider([candidate])]).match(
            EntityMatchInput("private", entityName="Private LLC", domain="private.test")
        ).to_dict()
        self.assertEqual(payload["security"]["status"], "not_applicable")
        self.assertEqual(payload["final_decision"], "private_entity")


class MigrationTests(unittest.TestCase):
    @staticmethod
    def legacy_row(strong_security: bool = False) -> dict:
        security_evidence = MatchEvidence(
            "security_identifier_match", "BBG000TEST", "BBG000TEST", 100, "openfigi",
            detail="figi" if strong_security else "ticker only",
        )
        return {
            "recordId": "legacy-1", "status": "matched", "confidence": .99, "mappingVersion": "v1",
            "matchedEntity": {"entityId": "entity:child", "canonicalName": "Child", "entityType": "subsidiary"},
            "publicParent": {"entityId": "entity:parent", "canonicalName": "Parent", "entityType": "issuer"},
            "parentStatus": "verified",
            "matchedSecurity": {"securityId": "security:test", "ticker": "TST", "figi": "BBG000TEST"},
            "securityDecisionStatus": "matched", "securityConfidence": .99,
            "evidence": [security_evidence.__dict__ if hasattr(security_evidence, "__dict__") else {
                "type": security_evidence.type, "input": security_evidence.input, "candidate": security_evidence.candidate,
                "scoreContribution": security_evidence.scoreContribution, "provider": security_evidence.provider,
                "similarity": security_evidence.similarity, "detail": security_evidence.detail,
            }],
        }

    def test_migration_is_conservative_for_parent_and_security(self):
        weak = legacy_to_v2(self.legacy_row(False), conservative_migration=True).to_dict()
        strong = legacy_to_v2(self.legacy_row(True), conservative_migration=True).to_dict()

        self.assertEqual(weak["entity"]["status"], "verified")
        self.assertEqual(weak["public_parent"]["status"], "candidate")
        self.assertEqual(weak["security"]["status"], "candidate")
        self.assertEqual(strong["public_parent"]["status"], "candidate")
        self.assertEqual(strong["security"]["status"], "verified")
        self.assertFalse(strong["security"]["score_is_calibrated"])

    def test_v2_legacy_v2_round_trip_preserves_component_statuses(self):
        original = MatchResultV2(
            record_id="round-trip",
            entity=ResolutionComponent("verified", "entity:one", "Entity One", .99, match_pathway="exact_lei"),
            public_parent=ResolutionComponent("verified", "entity:parent", "Parent"),
            security=ResolutionComponent("ambiguous", alternatives=[{"securityId": "a"}, {"securityId": "b"}]),
            final_decision="ambiguous", temporal={}, observation_date=None, mapping_version="v1",
        )
        restored = legacy_to_v2(v2_to_legacy(original), conservative_migration=True)
        self.assertEqual(
            (restored.entity.status, restored.public_parent.status, restored.security.status, restored.final_decision),
            ("verified", "verified", "ambiguous", "ambiguous"),
        )

    def test_v2_legacy_v2_round_trip_preserves_verified_security_without_legacy_evidence(self):
        original = MatchResultV2(
            record_id="round-trip-security",
            entity=ResolutionComponent("verified", "entity:issuer", "Issuer"),
            public_parent=ResolutionComponent("unknown"),
            security=ResolutionComponent("verified", "security:one", "Common Stock"),
            final_decision="entity_and_security_matched", temporal={}, observation_date=None, mapping_version="v1",
        )
        restored = legacy_to_v2(v2_to_legacy(original), conservative_migration=True)
        self.assertEqual(restored.security.status, "verified")
        self.assertEqual(restored.final_decision, "entity_and_security_matched")

    def test_migrate_command_writes_v2_without_touching_job_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source, output = directory / "v1.jsonl", directory / "v2.jsonl"
            source.write_text(json.dumps(self.legacy_row(False)) + "\n", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                cmd_migrate_results(SimpleNamespace(input=str(source), output=str(output), overwrite=False))
            migrated = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(migrated["schema_version"], "2.0")
        self.assertEqual(migrated["public_parent"]["status"], "candidate")


class ExportTests(unittest.TestCase):
    def test_legacy_output_requires_the_explicit_deprecated_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            records, reference, mapping = directory / "records.csv", directory / "reference.csv", directory / "mapping.json"
            v2_output, legacy_output = directory / "v2.jsonl", directory / "legacy.jsonl"
            records.write_text("record_id,company_name\n1,Example Issuer\n", encoding="utf-8")
            reference.write_text("internal_entity_id,canonical_name,entity_type\nentity:one,Example Issuer,issuer\n", encoding="utf-8")
            mapping.write_text(json.dumps({"mapping": {"record_id": "recordId", "company_name": "entityName"}}), encoding="utf-8")
            base = ["symbologylink", "resolve", "--input", str(records), "--mapping", str(mapping), "--reference", str(reference), "--cache", str(directory / "cache.sqlite3")]
            with patch("sys.argv", [*base, "--output", str(v2_output)]), redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 0)
            warnings = io.StringIO()
            with patch("sys.argv", [*base, "--output", str(legacy_output), "--legacy-output"]), redirect_stdout(io.StringIO()), redirect_stderr(warnings):
                self.assertEqual(main(), 0)
            v2 = json.loads(v2_output.read_text(encoding="utf-8").splitlines()[0])
            legacy = json.loads(legacy_output.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(v2["schema_version"], "2.0")
        self.assertNotIn("matchedEntity", v2)
        self.assertEqual(legacy["matchedEntity"]["entityId"], "entity:one")
        self.assertNotIn("schema_version", legacy)
        self.assertIn("deprecated", warnings.getvalue())

    def test_flat_export_uses_component_columns(self):
        row = MatchResultV2(
            record_id="export",
            entity=ResolutionComponent("verified", "entity:one", "Entity One", .99, match_pathway="exact_cik"),
            public_parent=ResolutionComponent("candidate", "entity:parent", "Parent", attributes={"relationship_type": "subsidiary_of", "relationship_source": "gleif"}),
            security=ResolutionComponent("verified", "security:one", "Class A", .99, attributes={"ticker": "ONE", "exchange": "XNAS", "figi": "BBGONE", "share_class": "A", "security_type": "Common Stock"}),
            final_decision="entity_matched_parent_candidate",
            temporal={"overall": {"status": "unknown", "reason": "No dated evidence."}},
            observation_date="2025-01-01", mapping_version="v1", source_record={"input_name": "Entity One"},
        ).to_dict()
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source, output = directory / "results.jsonl", directory / "results.csv"
            source.write_text(json.dumps(row) + "\n", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                cmd_export(SimpleNamespace(input=str(source), output=str(output), status=["review_required"], overwrite=False))
            with output.open(encoding="utf-8-sig", newline="") as handle:
                exported = next(csv.DictReader(handle))
        self.assertEqual(exported["entity_status"], "verified")
        self.assertEqual(exported["parent_status"], "candidate")
        self.assertEqual(exported["relationship_source"], "gleif")
        self.assertEqual(exported["security_type"], "Common Stock")
        self.assertEqual(exported["final_decision"], "entity_matched_parent_candidate")
        self.assertEqual(exported["schema_version"], "2.0")


if __name__ == "__main__":
    unittest.main()
