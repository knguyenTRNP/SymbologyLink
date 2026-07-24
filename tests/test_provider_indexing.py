from __future__ import annotations

import csv
import os
import tempfile
import time
import unittest
from pathlib import Path

from symbologylink.models import EntityMatchInput
from symbologylink.providers import LocalSecurityMasterProvider, ProviderCandidate


def _candidate_key(candidate: ProviderCandidate) -> tuple[str, str | None]:
    return candidate.entity_id, (candidate.security or {}).get("internal_security_id")


def _write_master(path: Path, count: int) -> None:
    fields = [
        "internal_entity_id", "internal_security_id", "canonical_name", "aliases",
        "ticker", "exchange", "cik", "domain", "parent_entity_id",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index in range(count):
            writer.writerow({
                "internal_entity_id": f"entity:{index:06d}",
                "internal_security_id": f"security:{index:06d}",
                "canonical_name": f"Entity{index:06d} Industries",
                "aliases": f"Alias{index:06d} Works",
                "ticker": f"T{index:06d}",
                "exchange": "XNAS",
                "cik": f"{index + 1:010d}",
                "domain": f"entity{index:06d}.example",
                "parent_entity_id": "entity:000001" if index == 0 else "",
            })


class CustomerMasterIndexTests(unittest.TestCase):
    def test_indexes_cover_identifiers_domains_names_aliases_and_collisions(self):
        content = (
            "internal_entity_id,internal_security_id,canonical_name,aliases,ticker,domain,cik\n"
            "entity:one,security:one,Alpha Corporation,Alpha Labs|First Alpha,DUP,alpha.example,1\n"
            "entity:two,security:two,Beta Limited,Beta Research,DUP,beta.example,2\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "master.csv"
            path.write_text(content, encoding="utf-8")
            provider = LocalSecurityMasterProvider(path)

        self.assertEqual(
            [_candidate_key(item) for item in provider._by_identifier["DUP"]],
            [("entity:one", "security:one"), ("entity:two", "security:two")],
        )
        self.assertEqual(provider._by_identifier["0000000001"][0].entity_id, "entity:one")
        self.assertEqual(provider._by_domain["alpha.example"][0].entity_id, "entity:one")
        self.assertEqual(provider._by_exact_name["alpha"][0].entity_id, "entity:one")
        self.assertEqual(provider._by_exact_name["alpha labs"][0].entity_id, "entity:one")
        self.assertEqual(provider._by_token["labs"][0].entity_id, "entity:one")
        self.assertEqual(provider._by_entity_id["entity:two"].canonical_name, "Beta Limited")
        self.assertEqual(provider._normalized_names["entity:one"], ["alpha", "alpha labs", "first alpha"])

    def test_non_first_token_block_expands_recall(self):
        content = (
            "internal_entity_id,canonical_name\n"
            "entity:wanted,Alpha Meridian Ventures\n"
            "entity:other,Unrelated Company\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "master.csv"
            path.write_text(content, encoding="utf-8")
            provider = LocalSecurityMasterProvider(path)
            record = EntityMatchInput("record", entityName="Different Meridian")
            legacy = provider._search_scan(record)
            indexed = provider.search(record)

        self.assertEqual(legacy, [])
        self.assertEqual([item.entity_id for item in indexed], ["entity:wanted"])

    def test_indexed_search_is_equivalent_or_a_superset_on_5000_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "master.csv"
            _write_master(path, 5000)
            provider = LocalSecurityMasterProvider(path)
            records = [
                EntityMatchInput("identifier", cik="0000000124"),
                EntityMatchInput("domain", domain="entity000456.example"),
                EntityMatchInput("exact", entityName="Entity000789 Industries"),
                EntityMatchInput("legacy-token", entityName="Entity001234 Division"),
                EntityMatchInput("no-hit", entityName="Completely Unknown Name"),
            ]
            for record in records:
                legacy = {_candidate_key(item) for item in provider._search_scan(record, limit=20)}
                indexed = {_candidate_key(item) for item in provider.search(record, limit=20)}
                self.assertTrue(legacy <= indexed, record.recordId)
                if record.recordId in {"identifier", "domain", "exact"}:
                    self.assertEqual(indexed, legacy, record.recordId)

    def test_ordering_is_deterministic_and_multiple_securities_are_preserved(self):
        content = (
            "internal_entity_id,internal_security_id,canonical_name,ticker,domain,cik\n"
            "issuer:dual,security:a,Dual Class Corp,DUA,dual.example,3\n"
            "issuer:dual,security:b,Dual Class Corp,DUB,dual.example,3\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "master.csv"
            path.write_text(content, encoding="utf-8")
            provider = LocalSecurityMasterProvider(path)
            record = EntityMatchInput("record", entityName="Dual Class Corp", domain="dual.example")
            runs = [[_candidate_key(item) for item in provider.search(record)] for _ in range(5)]

        self.assertTrue(all(run == runs[0] for run in runs[1:]))
        self.assertEqual(runs[0], [("issuer:dual", "security:a"), ("issuer:dual", "security:b")])

    def test_hot_token_bucket_keeps_legacy_top_n_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "master.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["internal_entity_id", "canonical_name"])
                writer.writeheader()
                for index in range(30):
                    writer.writerow({
                        "internal_entity_id": f"entity:{index:02d}",
                        "canonical_name": f"Common Name{index:02d} Incorporated",
                    })
            provider = LocalSecurityMasterProvider(path)
            record = EntityMatchInput("record", entityName="Common Name25 Incorporated")
            legacy = [_candidate_key(item) for item in provider._search_scan(record, limit=5)]
            indexed = [_candidate_key(item) for item in provider.search(record, limit=5)]

        self.assertEqual(indexed, legacy)
        self.assertEqual(indexed[0][0], "entity:25")

    def test_relationship_resolution_uses_prebuilt_indexes(self):
        content = (
            "internal_entity_id,canonical_name,cik,parent_entity_id\n"
            "entity:child,Child Company,1,entity:parent\n"
            "entity:parent,Parent Company,2,\n"
        )

        class NoIterationList(list):
            def __iter__(self):
                raise AssertionError("resolve_relationships rebuilt an index from candidates")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "master.csv"
            path.write_text(content, encoding="utf-8")
            provider = LocalSecurityMasterProvider(path)
            child = provider._by_entity_id["entity:child"]
            provider.candidates = NoIterationList(provider.candidates)
            graph = provider.resolve_relationships(child)
            fallback = provider.resolve_relationships(ProviderCandidate(
                "external:child", "Child Company", provider="test", identifiers={"cik": "0000000001"},
            ))

        self.assertEqual(graph["edges"][0]["toEntityId"], "entity:parent")
        self.assertEqual(fallback["subject"]["entityId"], "entity:child")


class CustomerMasterIndexPerformanceTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("SYMBOLOGYLINK_RUN_SLOW_TESTS") == "1",
        "Set SYMBOLOGYLINK_RUN_SLOW_TESTS=1 to run the provider indexing performance smoke test.",
    )
    def test_10000_by_1000_candidate_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "master.csv"
            _write_master(path, 10_000)
            provider = LocalSecurityMasterProvider(path)
            records = [EntityMatchInput(str(index), cik=f"{index + 1:010d}") for index in range(1000)]
            started = time.perf_counter()
            results = provider.search_batch(records)
            duration = time.perf_counter() - started

        self.assertEqual(len(results), 1000)
        self.assertLess(duration, 5.0)


if __name__ == "__main__":
    unittest.main()
