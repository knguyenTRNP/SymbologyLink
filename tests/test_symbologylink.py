import csv
import tempfile
import unittest
from pathlib import Path

from symbologylink.engine import MatchEngine
from symbologylink.ingest import map_row, profile_file, suggest_mapping
from symbologylink.models import EntityMatchInput
from symbologylink.normalize import normalize_domain, normalize_name
from symbologylink.providers import LocalSecurityMasterProvider

ROOT = Path(__file__).parents[1]


class NormalizationTests(unittest.TestCase):
    def test_company_and_domain_normalization(self):
        self.assertEqual(normalize_name("THE PROCTER & GAMBLE COMPANY"), "the procter and gamble")
        self.assertEqual(normalize_domain("https://www.microsoft.com/en-us/?x=1"), "microsoft.com")


class IngestionTests(unittest.TestCase):
    def test_profile_and_metadata_preservation(self):
        profile = profile_file(ROOT / "examples" / "records.csv")
        self.assertEqual(profile.row_count, 4)
        mapping = suggest_mapping(profile.columns)
        self.assertEqual(mapping["merchant_name"], "entityName")
        row = profile.sample_rows[0]
        record = map_row(row, mapping, 1, "records.csv")
        self.assertIn("amount", record.metadata)


class RegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = MatchEngine([LocalSecurityMasterProvider(ROOT / "examples" / "reference.csv")])

    def test_microsoft(self):
        result = self.engine.match(EntityMatchInput("1", entityName="Microsoft Corp", domain="microsoft.com", ticker="MSFT", exchange="NASDAQ", country="US", observationDate="2025-01-01"))
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.matchedEntity["entityId"], "entity:microsoft")
        self.assertTrue(result.evidence)

    def test_github_keeps_operating_entity_and_parent(self):
        result = self.engine.match(EntityMatchInput("2", entityName="GitHub", domain="github.com", country="US", observationDate="2025-01-01"))
        self.assertEqual(result.matchedEntity["entityId"], "entity:github")
        self.assertEqual(result.publicParent["entityId"], "entity:microsoft")
        self.assertEqual(result.relationshipGraph["directParent"]["entityId"], "entity:microsoft")
        self.assertEqual(result.relationshipGraph["issuer"]["entityId"], "entity:microsoft")

    def test_apple_bank_does_not_match_apple(self):
        result = self.engine.match(EntityMatchInput("3", entityName="Apple Bank", country="US"))
        self.assertEqual(result.matchedEntity["entityId"], "entity:apple-bank")
        self.assertNotEqual((result.matchedSecurity or {}).get("ticker"), "AAPL")

    def test_unknown_is_unmatched(self):
        result = self.engine.match(EntityMatchInput("4", entityName="Local Family Pharmacy", country="US"))
        self.assertEqual(result.status, "unmatched")


class BenchmarkTests(unittest.TestCase):
    def test_generation_is_reproducible_and_evaluable(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            reference = ROOT / "examples" / "reference.csv"
            manifest = generate_benchmark(reference, first, count=100, seed=42)
            generate_benchmark(reference, second, count=100, seed=42)
            self.assertEqual((Path(first) / "records.csv").read_bytes(), (Path(second) / "records.csv").read_bytes())
            self.assertEqual(manifest["record_count"], 100)
            self.assertGreater(manifest["positive_records"], manifest["negative_records"])
            provider = LocalSecurityMasterProvider(reference)
            engine = MatchEngine([provider])
            mapping = __import__("json").loads((Path(first) / "mapping.json").read_text())["mapping"]
            results = Path(first) / "results.jsonl"
            with results.open("w", encoding="utf-8") as handle:
                for index, row in enumerate(__import__("symbologylink.ingest", fromlist=["read_records"]).read_records(Path(first) / "records.csv"), 1):
                    record = map_row(row, mapping, index, "benchmark")
                    handle.write(__import__("json").dumps(engine.match(record).to_dict()) + "\n")
            metrics = evaluate_results(results, Path(first) / "truth.csv")
            self.assertEqual(metrics["records"], 100)
            self.assertIn("automatic_match_precision", metrics)
            self.assertIn("direct_parent_accuracy", metrics)
            self.assertIn("relationship_resolution_coverage", metrics)
            self.assertIn("security_top_1_accuracy", metrics)
            self.assertIn("security_decision_status_counts", metrics)
            self.assertIn("identifier_conflict_records", metrics)
            self.assertIn("identifier_conflict_review_rate", metrics)
            self.assertIn("temporal_scope_status_counts", metrics)
            self.assertIn("overall_temporal_verified_rate", metrics)
            self.assertIn("by_category", metrics)


if __name__ == "__main__":
    unittest.main()
from symbologylink.benchmark import evaluate_results, generate_benchmark
