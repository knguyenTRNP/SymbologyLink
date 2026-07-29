import json
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from symbologylink.cache import SQLiteCache
from symbologylink.decisions import OverrideStore, RuleSet
from symbologylink.engine import MatchEngine
from symbologylink.ingest import profile_file, read_records
from symbologylink.jobs import JobStore
from symbologylink.models import EntityMatchInput
from symbologylink.providers import GLEIFProvider, LocalSecurityMasterProvider

ROOT = Path(__file__).parents[1]


class CacheTests(unittest.TestCase):
    def test_cache_round_trip_stats_and_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteCache(Path(directory) / "cache.sqlite3")
            cache.set("gleif", "name", "microsoft", {"data": [1]})
            self.assertEqual(cache.get("gleif", "name", "microsoft"), {"data": [1]})
            self.assertEqual(cache.stats()["providers"]["gleif"]["entries"], 1)
            self.assertEqual(cache.clear("gleif"), 1)


class ParquetTests(unittest.TestCase):
    @unittest.skipUnless(__import__("importlib").util.find_spec("pyarrow"), "pyarrow is optional")
    def test_parquet_ingestion_and_security_master(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        with tempfile.TemporaryDirectory() as directory:
            records = Path(directory) / "records.parquet"
            pq.write_table(pa.Table.from_pylist([{"company_name": "Microsoft", "country": "US"}]), records)
            self.assertEqual(profile_file(records).row_count, 1)
            master = Path(directory) / "master.parquet"
            pq.write_table(pa.Table.from_pylist([{"internal_entity_id": "e1", "canonical_name": "Example Corp", "entity_type": "issuer", "ticker": "EX", "exchange": "NYSE"}]), master)
            provider = LocalSecurityMasterProvider(master)
            self.assertEqual(provider.candidates[0].entity_id, "e1")


class DecisionTests(unittest.TestCase):
    def test_rule_match_and_override_precedence(self):
        rules = RuleSet([{
            "id": "example_rule", "priority": 10,
            "conditions": {"entity_name": {"normalized_exact": "Example Company"}},
            "result": {"entity": {"entity_id": "entity:example", "canonical_name": "Example Company Inc", "entity_type": "issuer"}},
        }])
        record = EntityMatchInput("1", entityName="EXAMPLE COMPANY")
        rule_result = MatchEngine([], rules=rules).match(record)
        self.assertEqual(rule_result.status, "matched")
        self.assertEqual(rule_result.matchedEntity["entityId"], "entity:example")
        self.assertEqual(rule_result.evidence[0].type, "reusable_rule_match")
        with tempfile.TemporaryDirectory() as directory:
            store = OverrideStore(Path(directory) / "overrides.jsonl")
            store.append({"pattern": {"entity_name": {"normalized_exact": "Example Company"}}, "action": "unmatched", "reviewer": "reviewer-1", "reason": "Known private namesake", "mapping_version": "review-v2"})
            overridden = MatchEngine([], rules=rules, overrides=store).match(record)
            self.assertEqual(overridden.status, "unmatched")
            self.assertEqual(overridden.mappingVersion, "v1")
            self.assertEqual(overridden.decisionVersion, "review-v2")
            self.assertEqual(overridden.evidence[0].type, "human_override")


class GLEIFTests(unittest.TestCase):
    PAYLOAD = {"data": [{"type": "lei-records", "id": "7LTWFZYICNSX8D621K86", "attributes": {"lei": "7LTWFZYICNSX8D621K86", "entity": {"legalName": {"name": "MICROSOFT CORPORATION"}, "otherNames": [{"name": "Microsoft Corp"}], "legalAddress": {"country": "US", "postalCode": "98052"}, "status": "ACTIVE"}, "registration": {"initialRegistrationDate": "2012-06-06T15:53:00Z"}}}]}

    def test_parse_and_cache_reuse(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self): return json.dumps(GLEIFTests.PAYLOAD).encode()
        with tempfile.TemporaryDirectory() as directory:
            provider = GLEIFProvider(SQLiteCache(Path(directory) / "cache.sqlite3"), retries=0)
            with patch("symbologylink.providers.urlopen", return_value=Response()) as request:
                first = provider.search(EntityMatchInput("1", lei="7LTWFZYICNSX8D621K86"))
                second = provider.search(EntityMatchInput("2", lei="7LTWFZYICNSX8D621K86"))
            self.assertEqual(request.call_count, 1)
            self.assertEqual(first[0].canonical_name, "MICROSOFT CORPORATION")
            self.assertEqual(first[0].identifiers["lei"], "7LTWFZYICNSX8D621K86")
            self.assertEqual(second[0].provider, "gleif")


class JobStoreTests(unittest.TestCase):
    def test_persistence_idempotency_results_and_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            first, created = store.create([{"entityName": "Microsoft"}], "same-request")
            second, created_again = store.create([{"entityName": "Ignored duplicate"}], "same-request")
            self.assertTrue(created)
            self.assertFalse(created_again)
            self.assertEqual(first["jobId"], second["jobId"])
            store.update(first["jobId"], status="completed", stage="completed", progress=100, results_json="[]", metrics_json='{"totalRecords":1}')
            self.assertEqual(store.metrics(first["jobId"])["totalRecords"], 1)
            self.assertTrue(store.cancel(first["jobId"]))


@unittest.skipUnless(__import__("importlib").util.find_spec("fastapi") and __import__("importlib").util.find_spec("httpx"), "API dependencies are optional")
class APITests(unittest.TestCase):
    def test_single_batch_idempotency_pagination_and_rules(self):
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                "SYMBOLOGYLINK_REFERENCE": str(ROOT / "examples" / "reference.csv"),
                "SYMBOLOGYLINK_ENABLE_GLEIF": "false",
                "SYMBOLOGYLINK_ENABLE_OPENFIGI": "false",
                "SYMBOLOGYLINK_JOBS": str(Path(directory) / "jobs.sqlite3"),
                "SYMBOLOGYLINK_DATASETS": str(Path(directory) / "datasets.sqlite3"),
                "SYMBOLOGYLINK_UPLOADS": str(Path(directory) / "uploads"),
                "SYMBOLOGYLINK_CACHE": str(Path(directory) / "cache.sqlite3"),
                "SYMBOLOGYLINK_RULES": str(Path(directory) / "rules.json"),
                "SYMBOLOGYLINK_OVERRIDES": str(Path(directory) / "overrides.jsonl"),
            }
            with patch.dict(os.environ, environment):
                import symbologylink.api as api
                api = importlib.reload(api)
                client = TestClient(api.app)
                health = client.get("/health")
                self.assertEqual(health.status_code, 200)
                self.assertEqual(health.json()["version"], "0.0.0")
                single = client.post("/v1/match", json={"recordId": "one", "entityName": "Microsoft Corp", "domain": "microsoft.com", "country": "US"})
                self.assertEqual(single.status_code, 200)
                self.assertEqual(single.json()["schema_version"], "2.0")
                self.assertEqual(single.json()["entity"]["canonical_id"], "entity:microsoft")
                batch = client.post("/v1/match/batch", headers={"Idempotency-Key": "api-test"}, json={"records": [{"recordId": "two", "entityName": "GitHub", "domain": "github.com", "country": "US"}]})
                self.assertEqual(batch.status_code, 202)
                job_id = batch.json()["jobId"]
                duplicate = client.post("/v1/match/batch", headers={"Idempotency-Key": "api-test"}, json={"records": [{"recordId": "ignored"}]})
                self.assertEqual(duplicate.json()["jobId"], job_id)
                self.assertEqual(client.get(f"/v1/jobs/{job_id}").json()["status"], "completed")
                job_results = client.get(f"/v1/jobs/{job_id}/results?limit=1").json()
                self.assertEqual(job_results["total"], 1)
                self.assertEqual(job_results["results"][0]["relationship_graph"]["directParent"]["entityId"], "entity:microsoft")
                self.assertEqual(job_results["results"][0]["public_parent"]["status"], "candidate")
                metrics = client.get(f"/v1/jobs/{job_id}/metrics").json()
                self.assertEqual(metrics["relationshipStatusCounts"]["resolved"], 1)
                self.assertEqual(metrics["parentStatusCounts"]["candidate"], 1)
                self.assertEqual(metrics["parentReviewRecords"], 1)
                self.assertEqual(metrics["schemaVersion"], "2.0")
                self.assertIn("securityStatusCounts", metrics)
                self.assertIn("averageSecurityMatchScore", metrics)
                self.assertIn("identifierConflictRecords", metrics)
                self.assertIn("identifierConflictCounts", metrics)
                self.assertEqual(metrics["temporalScopeStatusCounts"]["entity"]["not_requested"], 1)
                self.assertEqual(metrics["temporalScopeStatusCounts"]["public_parent"]["not_requested"], 1)
                rule = {"id": "api-rule", "conditions": {"entity_name": {"normalized_exact": "Test Entity"}}, "result": {"entity": {"entity_id": "test", "canonical_name": "Test Entity", "entity_type": "legal_entity"}}}
                self.assertEqual(client.post("/v1/rules", json=rule).status_code, 201)
                self.assertEqual(len(client.get("/v1/rules").json()), 1)

    def test_multipart_dataset_lifecycle_and_resolution(self):
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                "SYMBOLOGYLINK_REFERENCE": str(ROOT / "examples" / "reference.csv"),
                "SYMBOLOGYLINK_ENABLE_GLEIF": "false",
                "SYMBOLOGYLINK_ENABLE_OPENFIGI": "false",
                "SYMBOLOGYLINK_JOBS": str(Path(directory) / "jobs.sqlite3"),
                "SYMBOLOGYLINK_DATASETS": str(Path(directory) / "datasets.sqlite3"),
                "SYMBOLOGYLINK_UPLOADS": str(Path(directory) / "uploads"),
                "SYMBOLOGYLINK_CACHE": str(Path(directory) / "cache.sqlite3"),
                "SYMBOLOGYLINK_RULES": str(Path(directory) / "rules.json"),
                "SYMBOLOGYLINK_OVERRIDES": str(Path(directory) / "overrides.jsonl"),
            }
            with patch.dict(os.environ, environment):
                import symbologylink.api as api
                api = importlib.reload(api)
                client = TestClient(api.app)
                upload = client.post("/v1/datasets", files={"file": ("records.csv", (ROOT / "examples" / "records.csv").read_bytes(), "text/csv")})
                self.assertEqual(upload.status_code, 201)
                dataset = upload.json()
                dataset_id = dataset["datasetId"]
                self.assertEqual(dataset["status"], "ready")
                self.assertEqual(dataset["rowCount"], 4)
                self.assertEqual(len(dataset["sha256"]), 64)
                self.assertNotIn("path", dataset["profile"])
                preview = client.get(f"/v1/datasets/{dataset_id}/preview")
                self.assertEqual(preview.status_code, 200)
                self.assertEqual(preview.json()["suggested_mapping"]["merchant_name"], "entityName")
                mapping = json.loads((ROOT / "examples" / "mapping.json").read_text())["mapping"]
                resolution = client.post(f"/v1/datasets/{dataset_id}/resolve", json={"mapping": mapping})
                self.assertEqual(resolution.status_code, 202)
                job_id = resolution.json()["jobId"]
                self.assertEqual(client.get(f"/v1/jobs/{job_id}").json()["status"], "completed")
                self.assertEqual(client.get(f"/v1/jobs/{job_id}/results").json()["total"], 4)
                rejected = client.put(f"/v1/datasets/{dataset_id}", files={"file": ("broken.json", b"{not-json", "application/json")})
                self.assertEqual(rejected.status_code, 422)
                preserved = client.get(f"/v1/datasets/{dataset_id}").json()
                self.assertEqual(preserved["status"], "ready")
                self.assertEqual(preserved["version"], 1)
                replaced = client.put(f"/v1/datasets/{dataset_id}", files={"file": ("records.json", (ROOT / "examples" / "records.json").read_bytes(), "application/json")})
                self.assertEqual(replaced.status_code, 200)
                self.assertEqual(replaced.json()["version"], 2)
                self.assertEqual(replaced.json()["rowCount"], 2)
                dataset_directory = Path(directory) / "uploads" / dataset_id
                self.assertEqual([path.name for path in dataset_directory.iterdir()], ["v2.json"])
                deleted = client.delete(f"/v1/datasets/{dataset_id}")
                self.assertEqual(deleted.status_code, 200)
                self.assertEqual(deleted.json()["status"], "deleted")
                self.assertFalse(dataset_directory.exists())
                self.assertEqual(client.get("/v1/datasets").json()["total"], 0)
                self.assertEqual(client.get("/v1/datasets?include_deleted=true").json()["total"], 1)


if __name__ == "__main__":
    unittest.main()
