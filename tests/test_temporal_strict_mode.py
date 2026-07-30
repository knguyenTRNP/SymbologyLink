from __future__ import annotations

import importlib
import io
import json
import os
import csv
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from symbologylink.cli import _read_result_rows, _write_result_rows, main
from symbologylink.decision_policies import DecisionPolicySet, TemporalPolicy
from symbologylink.engine import MatchEngine
from symbologylink.models import EntityMatchInput
from symbologylink.providers import LocalSecurityMasterProvider
from symbologylink.relationship_master import CustomerRelationshipMasterProvider


def _engine(master: Path, temporal: TemporalPolicy | None = None, *extra_providers):
    policies = DecisionPolicySet(temporal=temporal) if temporal else DecisionPolicySet()
    return MatchEngine([LocalSecurityMasterProvider(master), *extra_providers], decision_policies=policies)


class TemporalPolicyConfigurationTests(unittest.TestCase):
    def test_temporal_config_block_loads_and_rejects_unknown_values(self):
        with tempfile.TemporaryDirectory() as directory:
            valid = Path(directory) / "valid.json"
            valid.write_text(json.dumps({"temporal": {
                "require_verified_for_auto_match": True,
                "unknown_behavior": "review",
            }}), encoding="utf-8")
            loaded = DecisionPolicySet.load(valid)
            invalid = Path(directory) / "invalid.json"
            invalid.write_text(json.dumps({"temporal": {"unknown_behavior": "guess"}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "allow_with_warning, reject, review"):
                DecisionPolicySet.load(invalid)

        self.assertTrue(loaded.temporal.require_verified_for_auto_match)
        self.assertEqual(loaded.temporal.unknown_behavior, "review")
        self.assertTrue(loaded.policy_for("exact_cik").allow_auto_match)


class TemporalStrictModeTests(unittest.TestCase):
    @staticmethod
    def _entity_master(directory: str, *, dated: bool = False) -> Path:
        path = Path(directory) / "entities.csv"
        header = "internal_entity_id,canonical_name,entity_type,cik,entity_valid_from\n"
        path.write_text(header + f"entity:one,Example Issuer,issuer,1,{'2000-01-01' if dated else ''}\n", encoding="utf-8")
        return path

    def test_default_allows_temporal_unknown_with_explicit_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            result = _engine(self._entity_master(directory)).match(EntityMatchInput(
                "record", cik="1", observationDate="2025-01-01",
            ))
            v2 = result.to_dict()

        self.assertEqual(result.status, "matched")
        self.assertEqual(v2["temporal"]["entity"]["status"], "unknown")
        self.assertEqual(v2["temporal"]["overall"]["policy_action"], "allow_with_warning")
        self.assertEqual(v2["final_decision"], "entity_matched_security_unknown")
        self.assertTrue(any(item["type"] == "temporal_verification_warning" for item in v2["entity"]["evidence"]))

    def test_strict_mode_turns_unknown_into_temporal_verification_required(self):
        strict = TemporalPolicy(require_verified_for_auto_match=True)
        with tempfile.TemporaryDirectory() as directory:
            result = _engine(self._entity_master(directory), strict).match(EntityMatchInput(
                "record", cik="1", observationDate="2025-01-01",
            ))
            v2 = result.to_dict()

        self.assertEqual(result.status, "review_required")
        self.assertEqual(v2["entity"]["status"], "verified")
        self.assertEqual(v2["temporal"]["overall"]["status"], "unknown")
        self.assertEqual(v2["temporal"]["overall"]["policy_action"], "review")
        self.assertEqual(v2["final_decision"], "temporal_verification_required")

    def test_reject_policy_preserves_components_but_rejects_final_resolution(self):
        rejecting = TemporalPolicy(unknown_behavior="reject")
        with tempfile.TemporaryDirectory() as directory:
            result = _engine(self._entity_master(directory), rejecting).match(EntityMatchInput(
                "record", cik="1", observationDate="2025-01-01",
            ))
            v2 = result.to_dict()

        self.assertEqual(result.status, "unmatched")
        self.assertEqual(v2["entity"]["status"], "verified")
        self.assertEqual(v2["entity"]["canonical_id"], "entity:one")
        self.assertEqual(v2["temporal"]["overall"]["policy_action"], "reject")
        self.assertEqual(v2["final_decision"], "temporal_verification_required")

    def test_strict_mode_allows_conclusively_dated_entity(self):
        strict = TemporalPolicy(require_verified_for_auto_match=True)
        with tempfile.TemporaryDirectory() as directory:
            result = _engine(self._entity_master(directory, dated=True), strict).match(EntityMatchInput(
                "record", cik="1", observationDate="2025-01-01",
            ))
            v2 = result.to_dict()

        self.assertEqual(result.status, "matched")
        self.assertEqual(v2["temporal"]["overall"]["status"], "verified")
        self.assertEqual(v2["temporal"]["overall"]["policy_action"], "allow")

    def test_missing_observation_date_is_not_requested_and_strictly_reviewed(self):
        strict = TemporalPolicy(require_verified_for_auto_match=True)
        with tempfile.TemporaryDirectory() as directory:
            result = _engine(self._entity_master(directory, dated=True), strict).match(EntityMatchInput("record", cik="1"))
            v2 = result.to_dict()

        self.assertIsNone(v2["observation_date"])
        self.assertEqual(v2["temporal"]["overall"]["status"], "not_requested")
        self.assertEqual(v2["temporal"]["overall"]["internal_status"], "not_requested")
        self.assertEqual(v2["final_decision"], "temporal_verification_required")

    def test_undated_listing_and_relationship_are_never_temporally_verified(self):
        strict = TemporalPolicy(require_verified_for_auto_match=True)
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            master = directory / "master.csv"
            master.write_text(
                "internal_entity_id,internal_security_id,canonical_name,entity_type,cik,figi,entity_valid_from\n"
                "entity:child,security:child,Child Issuer,issuer,1,FIGI-CHILD,2000-01-01\n"
                "entity:parent,,Parent Issuer,issuer,2,,1990-01-01\n",
                encoding="utf-8",
            )
            relationships = directory / "relationships.csv"
            relationships.write_text(
                "relationship_id,child_entity_id,parent_entity_id,relationship_type,trust_level\n"
                "relationship:one,entity:child,entity:parent,subsidiary_of,authoritative\n",
                encoding="utf-8",
            )
            relationship_provider = CustomerRelationshipMasterProvider(relationships)
            result = _engine(master, strict, relationship_provider).match(EntityMatchInput(
                "record", cik="1", figi="FIGI-CHILD", observationDate="2025-01-01",
            ))
            v2 = result.to_dict()

        self.assertEqual(v2["temporal"]["entity"]["status"], "verified")
        self.assertEqual(v2["temporal"]["security"]["status"], "unknown")
        self.assertEqual(v2["temporal"]["public_parent"]["status"], "unknown")
        self.assertEqual(v2["temporal"]["overall"]["status"], "unknown")
        self.assertEqual(v2["public_parent"]["status"], "verified")
        self.assertEqual(v2["final_decision"], "temporal_verification_required")

    def test_out_of_period_ticker_cannot_auto_match_in_any_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            master = Path(directory) / "master.csv"
            master.write_text(
                "internal_entity_id,internal_security_id,canonical_name,entity_type,cik,ticker,exchange,entity_valid_from,security_valid_from,security_valid_to\n"
                "entity:one,security:one,Example Issuer,issuer,1,EXM,XNAS,2000-01-01,2010-01-01,2020-12-31\n",
                encoding="utf-8",
            )
            record = EntityMatchInput(
                "record", cik="1", ticker="EXM", exchange="XNAS", observationDate="2025-01-01",
            )
            permissive = _engine(master).match(record).to_dict()
            strict = _engine(master, TemporalPolicy(require_verified_for_auto_match=True)).match(record).to_dict()

        for result in (permissive, strict):
            self.assertEqual(result["temporal"]["security"]["status"], "contradicted")
            self.assertEqual(result["temporal"]["overall"]["status"], "contradicted")
            self.assertEqual(result["final_decision"], "temporal_verification_required")


class TemporalStrictModeSurfaceTests(unittest.TestCase):
    @staticmethod
    def _temporal_row() -> dict:
        return {
            "record_id": "record",
            "entity": {"status": "verified"},
            "public_parent": {"status": "unknown"},
            "security": {"status": "unknown"},
            "final_decision": "temporal_verification_required",
            "temporal": {
                scope: {"status": "unknown", "reason": f"{scope} has no dated evidence."}
                for scope in ("entity", "public_parent", "security", "overall")
            },
            "observation_date": "2025-01-01",
            "mapping_version": "v1",
            "schema_version": "2.0",
        }

    def test_json_output_preserves_temporal_statuses_and_reasons(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results.json"
            _write_result_rows(output, [self._temporal_row()])
            restored = _read_result_rows(output)[0]
        for scope in ("entity", "public_parent", "security", "overall"):
            self.assertEqual(restored["temporal"][scope]["status"], "unknown")
            self.assertTrue(restored["temporal"][scope]["reason"])

    @unittest.skipUnless(importlib.util.find_spec("pyarrow"), "Parquet support is optional")
    def test_parquet_output_preserves_temporal_statuses_and_reasons(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results.parquet"
            _write_result_rows(output, [self._temporal_row()])
            restored = _read_result_rows(output)[0]
        for scope in ("entity", "public_parent", "security", "overall"):
            self.assertEqual(restored["temporal"][scope]["status"], "unknown")
            self.assertTrue(restored["temporal"][scope]["reason"])

    def test_cli_flag_and_flat_export_surface_component_temporal_reasons(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            records = directory / "records.csv"
            reference = directory / "reference.csv"
            mapping = directory / "mapping.json"
            output = directory / "results.jsonl"
            export = directory / "results.csv"
            records.write_text("record_id,cik,observation_date\n1,1,2025-01-01\n", encoding="utf-8")
            reference.write_text("internal_entity_id,canonical_name,entity_type,cik\nentity:one,Example Issuer,issuer,1\n", encoding="utf-8")
            mapping.write_text(json.dumps({"mapping": {
                "record_id": "recordId", "cik": "cik", "observation_date": "observationDate",
            }}), encoding="utf-8")
            resolve_output = io.StringIO()
            with patch("sys.argv", [
                "symbologylink", "resolve", "--input", str(records), "--mapping", str(mapping),
                "--reference", str(reference), "--cache", str(directory / "cache.sqlite3"),
                "--require-temporal-verification", "--output", str(output),
            ]), redirect_stdout(resolve_output):
                self.assertEqual(main(), 0)
            with patch("sys.argv", [
                "symbologylink", "export", "--input", str(output), "--output", str(export),
            ]), redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 0)
            row = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
            with export.open(encoding="utf-8-sig", newline="") as handle:
                exported = next(csv.DictReader(handle))
            summary = json.loads(resolve_output.getvalue())

        self.assertTrue(summary["temporalPolicy"]["require_verified_for_auto_match"])
        self.assertEqual(row["final_decision"], "temporal_verification_required")
        self.assertEqual(exported["entity_temporal_status"], "unknown")
        self.assertTrue(exported["entity_temporal_reason"])
        self.assertEqual(exported["relationship_temporal_status"], "not_applicable")
        self.assertTrue(exported["relationship_temporal_reason"])
        self.assertEqual(exported["security_temporal_status"], "not_applicable")
        self.assertTrue(exported["security_temporal_reason"])
        self.assertEqual(exported["temporal_status"], "unknown")
        self.assertTrue(exported["temporal_reason"])

    @unittest.skipUnless(
        importlib.util.find_spec("fastapi") and importlib.util.find_spec("httpx"),
        "API dependencies are optional",
    )
    def test_api_environment_enables_strict_mode(self):
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            reference = directory / "reference.csv"
            reference.write_text("internal_entity_id,canonical_name,entity_type,cik\nentity:one,Example Issuer,issuer,1\n", encoding="utf-8")
            environment = {
                "SYMBOLOGYLINK_REFERENCE": str(reference),
                "SYMBOLOGYLINK_ENABLE_GLEIF": "false",
                "SYMBOLOGYLINK_ENABLE_OPENFIGI": "false",
                "SYMBOLOGYLINK_REQUIRE_TEMPORAL_VERIFICATION": "true",
                "SYMBOLOGYLINK_JOBS": str(directory / "jobs.sqlite3"),
                "SYMBOLOGYLINK_DATASETS": str(directory / "datasets.sqlite3"),
                "SYMBOLOGYLINK_UPLOADS": str(directory / "uploads"),
                "SYMBOLOGYLINK_CACHE": str(directory / "cache.sqlite3"),
                "SYMBOLOGYLINK_RULES": str(directory / "rules.json"),
                "SYMBOLOGYLINK_OVERRIDES": str(directory / "overrides.jsonl"),
            }
            with patch.dict(os.environ, environment):
                import symbologylink.api as api
                api = importlib.reload(api)
                response = TestClient(api.app).post("/v1/match", json={
                    "recordId": "record", "cik": "1", "observationDate": "2025-01-01",
                })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["final_decision"], "temporal_verification_required")
        self.assertEqual(response.json()["temporal"]["overall"]["policy_action"], "review")


if __name__ == "__main__":
    unittest.main()
