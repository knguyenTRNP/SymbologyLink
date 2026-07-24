#!/usr/bin/env python3
"""Benchmark indexed customer-security-master candidate generation."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from symbologylink.models import EntityMatchInput  # noqa: E402
from symbologylink.providers import LocalSecurityMasterProvider  # noqa: E402


def write_synthetic_master(path: Path, rows: int) -> None:
    fields = [
        "internal_entity_id", "internal_security_id", "canonical_name", "aliases",
        "ticker", "exchange", "cik", "domain",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index in range(rows):
            key = f"Unique{index:07d}"
            writer.writerow({
                "internal_entity_id": f"entity:{index:07d}",
                "internal_security_id": f"security:{index:07d}",
                "canonical_name": f"Synthetic {key}",
                "aliases": f"Alias {key}",
                "ticker": f"T{index:07d}",
                "exchange": "XNAS",
                "cik": f"{index + 1:010d}",
                "domain": f"entity{index:07d}.example",
            })


def synthetic_records(count: int, master_rows: int) -> list[EntityMatchInput]:
    records: list[EntityMatchInput] = []
    for record_index in range(count):
        master_index = (record_index * 7919) % master_rows
        key = f"Unique{master_index:07d}"
        pathway = record_index % 4
        values = (
            {"cik": f"{master_index + 1:010d}"}
            if pathway == 0 else
            {"domain": f"entity{master_index:07d}.example"}
            if pathway == 1 else
            {"entityName": f"Synthetic {key}"}
            if pathway == 2 else
            {"entityName": f"Lookup {key}"}
        )
        records.append(EntityMatchInput(f"record:{record_index:07d}", **values))
    return records


def run_benchmark(master_rows: int, record_count: int, limit: int, scan_sample: int) -> dict[str, object]:
    if master_rows < 1 or record_count < 1 or limit < 1 or scan_sample < 1:
        raise ValueError("master rows, records, limit, and scan sample must all be positive")
    with tempfile.TemporaryDirectory() as directory:
        master = Path(directory) / "synthetic-master.csv"
        write_synthetic_master(master, master_rows)
        load_started = time.perf_counter()
        provider = LocalSecurityMasterProvider(master)
        load_seconds = time.perf_counter() - load_started
        records = synthetic_records(record_count, master_rows)

        indexed_started = time.perf_counter()
        indexed = provider.search_batch(records, limit)
        indexed_seconds = time.perf_counter() - indexed_started
        if len(indexed) != record_count or any(not values for values in indexed.values()):
            raise RuntimeError("Indexed benchmark did not return candidates for every synthetic record.")

        sample = records[:min(scan_sample, record_count)]
        scan_started = time.perf_counter()
        for record in sample:
            provider._search_scan(record, limit)
        scan_sample_seconds = time.perf_counter() - scan_started

        projected_scan_seconds = scan_sample_seconds * record_count / len(sample)
        speedup = projected_scan_seconds / indexed_seconds if indexed_seconds else float("inf")
        index_keys = {
            "identifier": len(provider._by_identifier),
            "domain": len(provider._by_domain),
            "exact_name": len(provider._by_exact_name),
            "token": len(provider._by_token),
            "entity_id": len(provider._by_entity_id),
        }

    return {
        "master_rows": master_rows,
        "records": record_count,
        "limit": limit,
        "load_and_index_seconds": round(load_seconds, 6),
        "indexed_candidate_generation_seconds": round(indexed_seconds, 6),
        "indexed_records_per_second": round(record_count / indexed_seconds, 2) if indexed_seconds else None,
        "legacy_scan_sample_records": len(sample),
        "legacy_scan_sample_seconds": round(scan_sample_seconds, 6),
        "legacy_scan_projected_seconds": round(projected_scan_seconds, 2),
        "estimated_speedup": round(speedup, 2),
        "under_60_seconds": indexed_seconds < 60,
        "at_least_100x_faster": speedup >= 100,
        "index_key_counts": index_keys,
        "notes": "Legacy duration and speedup are projected from the measured scan sample; indexed duration is measured over the full batch.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master-rows", type=int, default=100_000)
    parser.add_argument("--records", type=int, default=10_000)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument(
        "--scan-sample", type=int, default=1,
        help="Legacy full-scan records to measure before projection (default: 1; each record scans the entire master).",
    )
    parser.add_argument("--assert-targets", action="store_true")
    args = parser.parse_args()
    result = run_benchmark(args.master_rows, args.records, args.limit, args.scan_sample)
    print(json.dumps(result, indent=2))
    if args.assert_targets and (not result["under_60_seconds"] or not result["at_least_100x_faster"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
