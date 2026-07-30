from __future__ import annotations

import csv
import importlib
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from symbologylink.benchmark import evaluate_results
from symbologylink.cache import CacheError, SQLiteCache
from symbologylink.cli import _atomic_output_path, cmd_export, load_mapping_config, main
from symbologylink.decisions import OverrideStore
from symbologylink.engine import MatchEngine, _confidence_from_raw
from symbologylink.ingest import IngestionError, prepare_records, profile_file
from symbologylink.models import EntityMatchInput
from symbologylink.normalize import normalize_address, normalize_country, normalize_identifier
from symbologylink.providers import LocalSecurityMasterProvider


class GeographicNormalizationTests(unittest.TestCase):
    def test_country_aliases_use_iso_codes_and_unknowns_are_null(self):
        expected = {
            "United States": "US", "USA": "US", "us": "US", "U.S.": "US",
            "United Kingdom": "GB", "uk": "GB", "Great Britain": "GB",
            "Germany": "DE", "de": "DE",
        }
        self.assertEqual({value: normalize_country(value) for value in expected}, expected)
        self.assertIsNone(normalize_country("Ruritania"))
        self.assertIsNone(normalize_country("ZZ"))
        record = EntityMatchInput("unknown-country", country="Ruritania")
        self.assertIsNone(record.country)
        self.assertIn("Unsupported country", record.metadata["normalizationWarnings"][0])

    def test_exchange_aliases_are_canonical_mics(self):
        aliases = [normalize_identifier(value, "exchange") for value in ("NASDAQ", "NasdaqGS", "XNAS")]
        self.assertEqual(aliases, ["XNAS", "XNAS", "XNAS"])

    def test_normalized_address_components_disambiguate_entities(self):
        self.assertEqual(normalize_address("One Microsoft Way"), normalize_address("1 Microsoft Wy"))
        with tempfile.TemporaryDirectory() as directory:
            master = Path(directory) / "master.csv"
            fields = [
                "internal_entity_id", "internal_security_id", "canonical_name", "entity_type",
                "ticker", "exchange", "country", "address_line1", "city", "state", "postal_code",
            ]
            with master.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows([
                    {"internal_entity_id": "entity:wa", "internal_security_id": "security:wa", "canonical_name": "ACME LLC", "entity_type": "issuer", "ticker": "ACME", "exchange": "NASDAQ", "country": "US", "address_line1": "1 Microsoft Wy", "city": "Redmond", "state": "WA", "postal_code": "98052"},
                    {"internal_entity_id": "entity:ny", "internal_security_id": "security:ny", "canonical_name": "ACME LLC", "entity_type": "issuer", "ticker": "ACM2", "exchange": "NYSE", "country": "US", "address_line1": "10 Broadway", "city": "New York", "state": "NY", "postal_code": "10001"},
                ])
            engine = MatchEngine([LocalSecurityMasterProvider(master)])
            result = engine.match(EntityMatchInput("address", entityName="ACME LLC", country="US", addressLine1="One Microsoft Way", city="Redmond", state="Washington"))
            exchange_result = engine.match(EntityMatchInput("exchange", entityName="ACME LLC", ticker="ACME", exchange="NasdaqGS"))
        self.assertEqual(result.matchedEntity["entityId"], "entity:wa")
        self.assertTrue(any(item.type == "address_match" and item.scoreContribution > 0 for item in result.evidence))
        self.assertTrue(any(item.type == "city_match" and item.scoreContribution > 0 for item in result.evidence))
        self.assertEqual(exchange_result.securityPrimaryPathway, "ticker_and_exchange")
        self.assertEqual(exchange_result.securityDecisionStatus, "review_required")


class ConfidenceAndBenchmarkTests(unittest.TestCase):
    def test_confidence_is_monotonic_when_evidence_exceeds_100(self):
        values = [_confidence_from_raw(score) for score in (0, 50, 99, 100, 105, 150)]
        self.assertEqual(values, sorted(values))
        self.assertEqual(_confidence_from_raw(100), 1.0)
        self.assertEqual(_confidence_from_raw(105), 1.0)

    def test_abstention_metrics_use_the_abstained_set_as_denominator(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            truth = directory / "truth.csv"
            truth.write_text(
                "record_id,expected_match,expected_entity_id\n"
                "p1,true,entity:one\n"
                "p2,true,entity:two\n"
                "n1,false,\n"
                "n2,false,\n",
                encoding="utf-8",
            )
            rows = [
                {"recordId": "p1", "status": "matched", "confidence": 1.0, "matchedEntity": {"entityId": "entity:one"}, "processingDurationMs": 1.0},
                {"recordId": "p2", "status": "unmatched", "confidence": 0.3, "alternatives": [{"entityId": "entity:two"}], "processingDurationMs": 2.0},
                {"recordId": "n1", "status": "unmatched", "confidence": 0.0, "processingDurationMs": 3.0},
                {"recordId": "n2", "status": "unmatched", "confidence": 0.0, "processingDurationMs": 4.0},
            ]
            results = directory / "results.jsonl"
            results.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            metrics = evaluate_results(results, truth)
        self.assertEqual(metrics["unmatched_correct"], 2)
        self.assertEqual(metrics["unmatched_total"], 3)
        self.assertEqual(metrics["unmatched_precision"], 0.6667)
        self.assertEqual(metrics["unmatched_accuracy"], 0.6667)
        self.assertEqual(metrics["negative_recall"], 1.0)
        self.assertEqual(metrics["false_rejection_rate"], 0.5)
        self.assertEqual(metrics["false_rejection_examples"][0]["recordId"], "p2")
        self.assertEqual(metrics["latency_ms"], {"records": 4, "median": 2.5, "p95": 4.0, "max": 4.0})
        self.assertTrue(all(values["latencyMs"] for values in metrics["by_pathway"].values()))


class ReliabilityAndWorkflowTests(unittest.TestCase):
    @staticmethod
    def _master(path: Path) -> None:
        path.write_text(
            "internal_entity_id,canonical_name,entity_type,domain,country,aliases\n"
            "entity:block,Block Inc,issuer,block.xyz,US,Square|Block\n"
            "entity:squarespace,Squarespace Inc,issuer,squarespace.com,US,Square Space|Square\n",
            encoding="utf-8",
        )

    def test_corrupt_cache_is_quarantined_and_updates_are_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.sqlite3"
            path.write_bytes(b"not a sqlite database")
            cache = SQLiteCache(path)
            self.assertTrue(list(Path(directory).glob("cache.sqlite3.corrupt-*")))
            cache.set("test", "query", "key", {"value": 1})
            with self.assertRaises(CacheError):
                cache.set("test", "query", "key", {"bad": object()})
            self.assertEqual(cache.get("test", "query", "key"), {"value": 1})

    def test_result_output_is_published_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            output = directory / "results.jsonl"
            output.write_text("completed-before\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                with _atomic_output_path(output) as partial:
                    partial.write_text('{"recordId":"partial"}\n', encoding="utf-8")
                    self.assertEqual(output.read_text(encoding="utf-8"), "completed-before\n")
                    raise RuntimeError("interrupted")
            self.assertEqual(output.read_text(encoding="utf-8"), "completed-before\n")
            self.assertFalse(list(directory.glob("*.partial")))
            with _atomic_output_path(output) as partial:
                partial.write_text("completed-after\n", encoding="utf-8")
                self.assertEqual(output.read_text(encoding="utf-8"), "completed-before\n")
            self.assertEqual(output.read_text(encoding="utf-8"), "completed-after\n")

    def test_date_format_mapping_validation_and_field_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            records = directory / "records.csv"
            records.write_text("record_id,company_name,observation_date\n1,ACME,03-04-2025\n", encoding="utf-8")
            mapping = {"record_id": "recordId", "company_name": "entityName", "observation_date": "observationDate"}
            _, prepared = prepare_records(records, mapping, date_format="%d-%m-%Y")
            self.assertEqual(prepared[0].observationDate, "2025-04-03")
            config = directory / "mapping.json"
            config.write_text(json.dumps({"mapping": mapping, "dateFormat": "%d-%m-%Y", "typo": True}), encoding="utf-8")
            with self.assertRaisesRegex(IngestionError, "Unknown mapping configuration"):
                load_mapping_config(str(config), ["record_id", "company_name", "observation_date"])
            oversized = directory / "oversized.csv"
            oversized.write_text("record_id,company_name\n1," + "A" * 32_769 + "\n", encoding="utf-8")
            with self.assertRaisesRegex(IngestionError, "32,768-character limit"):
                profile_file(oversized)

    def test_resolve_protects_existing_output_and_emits_mapping_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            records, master, mapping = directory / "records.csv", directory / "master.csv", directory / "mapping.json"
            records.write_text("record_id,company_name\n1,Block Inc\n", encoding="utf-8")
            self._master(master)
            mapping.write_text(json.dumps({"mapping": {"record_id": "recordId", "company_name": "entityName"}}), encoding="utf-8")
            output = directory / "results.jsonl"
            output.write_text("preserve me", encoding="utf-8")
            argv = ["symbologylink", "resolve", "--input", str(records), "--mapping", str(mapping), "--reference", str(master), "--cache", str(directory / "cache.sqlite3"), "--output", str(output)]
            with patch("sys.argv", argv), redirect_stderr(StringIO()):
                self.assertEqual(main(), 2)
            self.assertEqual(output.read_text(encoding="utf-8"), "preserve me")
            with patch("sys.argv", [*argv, "--overwrite"]), redirect_stdout(StringIO()):
                self.assertEqual(main(), 0)
            row = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(len(row["mapping_content_sha256"]), 64)
            self.assertEqual(len(row["mapping_fingerprint_sha256"]), 64)
            self.assertEqual(row["provider_metadata"]["customer_security_master"]["trust_level"], "authoritative")
            self.assertEqual(row["schema_version"], "2.0")

    def test_filtered_export_builds_review_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source, output = directory / "results.jsonl", directory / "review.csv"
            source.write_text("\n".join(json.dumps({
                "recordId": str(index), "status": status, "confidence": .8, "mappingVersion": "v1",
                "sourceRecord": {"company": f"Company {index}"},
            }) for index, status in enumerate(("matched", "review_required", "unmatched"), 1)) + "\n", encoding="utf-8")
            with redirect_stdout(StringIO()):
                cmd_export(SimpleNamespace(input=str(source), output=str(output), status=["review_required"], overwrite=False))
            with output.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["final_decision"] for row in rows], ["review_required"])

    def test_ambiguous_override_preserves_ranked_candidates_and_latency(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            master = directory / "master.csv"
            self._master(master)
            overrides = OverrideStore(directory / "overrides.jsonl")
            overrides.append({
                "pattern": {"entity_name": {"normalized_exact": "Square"}},
                "action": "ambiguous", "reviewer": "reviewer", "reason": "Cannot determine",
            })
            engine = MatchEngine([LocalSecurityMasterProvider(master)], overrides=overrides)
            result = engine.match(EntityMatchInput("ambiguous", entityName="Square"))
        self.assertEqual(result.status, "review_required")
        self.assertGreater(len(result.alternatives), 0)
        self.assertEqual(result.decisionSource, "human_override")
        self.assertIsNotNone(result.processingDurationMs)

    def test_per_key_rate_limiter_returns_retry_delay(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "SYMBOLOGYLINK_CACHE": str(Path(directory) / "cache.sqlite3"),
            "SYMBOLOGYLINK_JOBS": str(Path(directory) / "jobs.sqlite3"),
            "SYMBOLOGYLINK_DATASETS": str(Path(directory) / "datasets.sqlite3"),
            "SYMBOLOGYLINK_UPLOADS": str(Path(directory) / "uploads"),
            "SYMBOLOGYLINK_OVERRIDES": str(Path(directory) / "overrides.jsonl"),
            "SYMBOLOGYLINK_ENABLE_GLEIF": "false",
            "SYMBOLOGYLINK_ENABLE_OPENFIGI": "false",
            "SYMBOLOGYLINK_API_KEY": "key-a",
            "SYMBOLOGYLINK_RATE_LIMIT_PER_MINUTE": "2",
        }):
            sys.modules.pop("symbologylink.api", None)
            api = importlib.import_module("symbologylink.api")
            from fastapi.testclient import TestClient

            with TestClient(api.app) as client:
                headers = {"X-API-Key": "key-a"}
                responses = [client.post("/v1/match", headers=headers, json={"recordId": str(index), "entityName": "Unknown"}) for index in range(3)]
            self.assertEqual([response.status_code for response in responses], [200, 200, 429])
            self.assertGreaterEqual(int(responses[-1].headers["Retry-After"]), 1)


if __name__ == "__main__":
    unittest.main()
