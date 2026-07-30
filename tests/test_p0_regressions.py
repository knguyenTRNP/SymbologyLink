from __future__ import annotations

import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from symbologylink.benchmark import evaluate_results, generate_benchmark
from symbologylink.cli import (
    SPREADSHEET_FORMULA_PREFIXES,
    _csv_safe_cell,
    _parquet_safe_rows,
    build_parser,
    cmd_export,
)
from symbologylink.decisions import RuleSet
from symbologylink.engine import MatchEngine
from symbologylink.ingest import map_row, prepare_records, profile_file
from symbologylink.models import EntityMatchInput, MatchConfig
from symbologylink.providers import LocalSecurityMasterProvider

ROOT = Path(__file__).parents[1]


class NormalizationRegressionTests(unittest.TestCase):
    def test_cik_padding_and_placeholder_nulls(self):
        record = EntityMatchInput("1", cik="789019", ticker="N/A", domain="-", entityName="UNKNOWN")
        self.assertEqual(record.cik, "0000789019")
        self.assertIsNone(record.ticker)
        self.assertIsNone(record.domain)
        self.assertIsNone(record.entityName)
        mapped = map_row(
            {"record_id": "1", "company_name": "N/A", "ticker": "UNKNOWN"},
            {"record_id": "recordId", "company_name": "entityName", "ticker": "ticker"},
            1,
            "records.csv",
        )
        self.assertIsNone(mapped.entityName)
        self.assertIsNone(mapped.ticker)

    def test_sparse_json_and_jsonl_use_union_of_record_keys(self):
        mapping = {
            "record_id": "recordId",
            "company_name": "entityName",
            "observation_date": "observationDate",
        }
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            jsonl_path = directory / "sparse.jsonl"
            jsonl_path.write_text(
                '{"record_id":"1","company_name":"Alpha"}\n'
                '{"record_id":"2","company_name":"Beta","observation_date":"2025-01-01"}\n',
                encoding="utf-8",
            )
            json_path = directory / "sparse.json"
            json_path.write_text(json.dumps([
                {"record_id": "1", "company_name": "Alpha"},
                {"record_id": "2", "company_name": "Beta", "observation_date": "2025-01-01"},
            ]), encoding="utf-8")
            for path in (jsonl_path, json_path):
                profile, records = prepare_records(path, mapping)
                self.assertEqual(profile.columns, ["record_id", "company_name", "observation_date"])
                self.assertEqual(len(records), 2)
                self.assertIsNone(records[0].observationDate)
                self.assertEqual(records[1].observationDate, "2025-01-01")


class MatchingRegressionTests(unittest.TestCase):
    @staticmethod
    def _provider(directory: str) -> LocalSecurityMasterProvider:
        path = Path(directory) / "master.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "internal_entity_id", "internal_security_id", "canonical_name", "entity_type",
                "ticker", "exchange", "cik", "domain",
            ])
            writer.writeheader()
            writer.writerows([
                {"internal_entity_id": "entity:microsoft", "internal_security_id": "security:msft", "canonical_name": "Microsoft Corporation", "entity_type": "issuer", "ticker": "MSFT", "exchange": "NASDAQ", "cik": "0000789019", "domain": "microsoft.com"},
                {"internal_entity_id": "entity:conflict", "internal_security_id": "security:cflt", "canonical_name": "Conflict Company", "entity_type": "issuer", "ticker": "CFLT", "exchange": "NYSE", "cik": "0000789019", "domain": "conflict.example"},
            ])
        return LocalSecurityMasterProvider(path)

    def test_identifier_collision_ticker_exchange_and_domain_decisions(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = MatchEngine([self._provider(directory)])
            conflict = engine.match(EntityMatchInput("c", cik="789019"))
            ticker = engine.match(EntityMatchInput("t", ticker="MSFT", exchange="NASDAQ"))
            domain = engine.match(EntityMatchInput("d", entityName="MSFT", domain="microsoft.com"))
        self.assertEqual(conflict.status, "review_required")
        self.assertTrue(any(item.type == "identifier_conflict" for item in conflict.evidence))
        self.assertGreaterEqual(len({conflict.matchedEntity["entityId"], *(item.entityId for item in conflict.alternatives)}), 2)
        self.assertEqual(ticker.status, "review_required")
        self.assertEqual(ticker.securityDecisionStatus, "review_required")
        self.assertEqual(ticker.matchedEntity["entityId"], "entity:microsoft")
        self.assertEqual(domain.status, "review_required")
        self.assertEqual(domain.matchedEntity["entityId"], "entity:microsoft")

    def test_source_record_is_preserved_on_result(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = MatchEngine([self._provider(directory)])
            result = engine.match(EntityMatchInput("s", entityName="Microsoft Corporation", sourceRecord={"company_name": "Microsoft Corporation", "cost_center": "CC-42"}, metadata={"cost_center": "CC-42"}))
        self.assertEqual(result.sourceRecord["cost_center"], "CC-42")
        self.assertEqual(result.sourceMetadata["cost_center"], "CC-42")


class RuleRegressionTests(unittest.TestCase):
    def test_conflicting_rules_are_rejected_and_versions_are_separate(self):
        common = {"priority": 50, "conditions": {"entity_name": {"normalized_exact": "Torn Co"}}}
        with self.assertRaisesRegex(ValueError, "Conflicting equal-priority rules"):
            RuleSet([
                {**common, "id": "one", "result": {"entity": {"entity_id": "entity:one", "canonical_name": "One"}}},
                {**common, "id": "two", "result": {"entity": {"entity_id": "entity:two", "canonical_name": "Two"}}},
            ])
        rules = RuleSet([{
            "id": "versioned", "version": "rule-v3", "priority": 10,
            "conditions": {"entity_name": {"normalized_exact": "Versioned Co"}},
            "result": {"entity": {"entity_id": "entity:versioned", "canonical_name": "Versioned Co"}},
        }])
        result = MatchEngine([], MatchConfig(mapping_version="mapping-v2"), rules=rules).match(EntityMatchInput("r", entityName="Versioned Co"))
        self.assertEqual(result.mappingVersion, "mapping-v2")
        self.assertEqual(result.decisionVersion, "rule-v3")
        self.assertEqual(result.decisionSource, "rule:versioned")


class OutputAndBenchmarkRegressionTests(unittest.TestCase):
    def test_spreadsheet_formula_vectors_are_neutralized(self):
        vectors = [
            '=HYPERLINK("http://evil","click")',
            "+CMD|calc",
            "-2+3",
            "@SUM(A1)",
            "\t=1+1",
            "\r=1+1",
            "\n=1+1",
        ]
        self.assertEqual([_csv_safe_cell(value) for value in vectors], ["'" + value for value in vectors])
        self.assertEqual(_csv_safe_cell("ordinary text"), "ordinary text")
        self.assertEqual(_csv_safe_cell(42), 42)
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            results = directory / "results.jsonl"
            results.write_text("\n".join(json.dumps({
                "recordId": str(index),
                "status": "unmatched",
                "confidence": 0.0,
                "mappingVersion": "v1",
                "sourceRecord": {"company_name": value},
            }) for index, value in enumerate(vectors, 1)) + "\n", encoding="utf-8")
            exported = directory / "enriched.csv"
            with contextlib.redirect_stdout(io.StringIO()):
                cmd_export(SimpleNamespace(input=str(results), output=str(exported)))
            with exported.open(encoding="utf-8-sig", newline="") as handle:
                cells = [cell for row in csv.reader(handle) for cell in row]
        self.assertFalse(any(cell.startswith(SPREADSHEET_FORMULA_PREFIXES) for cell in cells))

    def test_cli_version_and_profile_fingerprint(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as raised:
                build_parser().parse_args(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("0.0.0", output.getvalue())
        profile = profile_file(ROOT / "examples" / "records.csv")
        self.assertEqual(len(profile.sha256), 64)

    @unittest.skipUnless(__import__("importlib").util.find_spec("pyarrow"), "pyarrow is optional")
    def test_parquet_result_rows_have_a_stable_schema(self):
        import pyarrow as pa

        rows = _parquet_safe_rows([
            {"record_id": "1", "schema_version": "2.0", "final_decision": "entity_matched_security_unknown", "entity": {"status": "verified", "evidence": [{"input": ["a"]}]}, "security": {"status": "unknown"}, "public_parent": {"status": "unknown"}, "temporal": {}},
            {"record_id": "2", "schema_version": "2.0", "final_decision": "unmatched", "entity": {"status": "unknown", "evidence": []}, "security": {"status": "not_applicable"}, "public_parent": {"status": "not_applicable"}, "temporal": {}},
        ])
        table = pa.Table.from_pylist(rows)
        self.assertEqual(table.num_rows, 2)
        self.assertEqual(table.schema.field("schema_version").type, pa.string())
        self.assertEqual(table.schema.field("entity").type, pa.string())
        self.assertEqual(table.column("record_id").to_pylist(), ["1", "2"])

    def test_legacy_truth_and_statistical_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            truth = directory / "truth.csv"
            truth.write_text("record_id,expected_entity_id\n1,entity:microsoft\n2,\n", encoding="utf-8")
            results = directory / "results.jsonl"
            rows = [
                {"recordId": "1", "status": "matched", "confidence": 1.0, "matchedEntity": {"entityId": "entity:microsoft"}, "alternatives": [], "securityAlternatives": [], "evidence": [], "validity": {}},
                {"recordId": "2", "status": "unmatched", "confidence": 0.0, "matchedEntity": None, "alternatives": [], "securityAlternatives": [], "evidence": [], "validity": {}},
            ]
            results.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            metrics = evaluate_results(results, truth)
            generated = directory / "generated"
            manifest = generate_benchmark(ROOT / "examples" / "reference.csv", generated, count=100, seed=7)
            with (generated / "truth.csv").open(encoding="utf-8") as handle:
                generated_truth = list(csv.DictReader(handle))
        self.assertEqual(metrics["automatic_match_correct"], 1)
        self.assertEqual(metrics["automatic_match_total"], 1)
        self.assertIsNotNone(metrics["automatic_match_confidence_interval"])
        self.assertTrue(metrics["confidence_calibration"])
        self.assertIn("by_pathway", metrics)
        self.assertGreaterEqual(manifest["negative_records"] / manifest["record_count"], 0.2)
        self.assertTrue(any(row["category"] == "private_company" for row in generated_truth))


if __name__ == "__main__":
    unittest.main()
