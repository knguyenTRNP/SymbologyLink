from __future__ import annotations

import csv
import hashlib
import html
import json
import random
import re
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .providers import LocalSecurityMasterProvider, ProviderCandidate

GENERATOR_VERSION = "1.0"
FIELDS = [
    "record_id", "entity_name", "domain", "ticker", "exchange", "cik", "lei",
    "figi", "isin", "cusip", "country", "observation_date", "benchmark_category",
]
TRUTH_FIELDS = [
    "record_id", "expected_entity_id", "expected_parent_entity_id",
    "expected_security_id", "expected_match", "category", "source_entity_id",
]
POSITIVE_CATEGORIES = (
    "exact_name", "case_and_punctuation", "legal_suffix_removed", "alias",
    "domain_only", "ticker_exchange", "strong_identifier", "single_typo",
    "cik_only", "figi_only", "ticker_only", "conflicting_identifier",
    "swapped_words", "added_division", "conflicting_country", "historical_date",
)
NEGATIVE_CATEGORIES = (
    "fictional_company", "insufficient_information", "invalid_ticker",
    "generic_name", "lookalike_name", "private_company",
)


def _reference_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _wilson_interval(successes: int, total: int, z: float = 1.96) -> dict[str, float | int] | None:
    if not total:
        return None
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((proportion * (1 - proportion) + z * z / (4 * total)) / total) / denominator
    return {
        "lower": round(max(0.0, center - margin), 4),
        "upper": round(min(1.0, center + margin), 4),
        "confidenceLevel": 0.95,
        "method": "Wilson score",
        "sampleSize": total,
    }


def _remove_suffix(name: str) -> str:
    suffix = r"\b(?:incorporated|corporation|company|limited|holdings|group|inc|corp|co|llc|ltd|plc)\.?$"
    return re.sub(suffix, "", name, flags=re.IGNORECASE).strip(" ,.") or name


def _punctuate(name: str) -> str:
    return re.sub(r"\s+", "  ", name.upper().replace("AND", "&")) + "."


def _single_typo(name: str) -> str:
    words = name.split()
    target = len(words) - 1
    word = words[target]
    if len(word) < 4 and len(words) > 1:
        target, word = 0, words[0]
    if len(word) >= 2:
        index = max(0, len(word) // 2 - 1)
        chars = list(word)
        chars[index], chars[index + 1] = chars[index + 1], chars[index]
        words[target] = "".join(chars)
    return " ".join(words)


def _base_row(record_id: str, candidate: ProviderCandidate, category: str) -> dict[str, str]:
    security = candidate.security or {}
    return {
        "record_id": record_id,
        "entity_name": candidate.canonical_name,
        "domain": candidate.domain or "",
        "ticker": candidate.identifiers.get("ticker", ""),
        "exchange": candidate.identifiers.get("exchange", ""),
        "cik": candidate.identifiers.get("cik", ""),
        "lei": candidate.identifiers.get("lei", ""),
        "figi": candidate.identifiers.get("figi", ""),
        "isin": candidate.identifiers.get("isin", ""),
        "cusip": candidate.identifiers.get("cusip", ""),
        "country": candidate.country or "",
        "observation_date": "2025-01-01",
        "benchmark_category": category,
    }


def _positive_case(record_id: str, candidate: ProviderCandidate, category: str, rng: random.Random) -> dict[str, str]:
    row = _base_row(record_id, candidate, category)
    if category == "exact_name":
        for field in ("domain", "ticker", "exchange", "cik", "lei", "figi", "isin", "cusip"):
            row[field] = ""
    elif category == "case_and_punctuation":
        row["entity_name"] = _punctuate(candidate.canonical_name)
        row["domain"] = ""
    elif category == "legal_suffix_removed":
        row["entity_name"] = _remove_suffix(candidate.canonical_name)
        row["domain"] = ""
    elif category == "alias":
        row["entity_name"] = rng.choice(candidate.aliases) if candidate.aliases else _remove_suffix(candidate.canonical_name)
        row["domain"] = ""
    elif category == "domain_only":
        row["entity_name"] = ""
        for field in ("ticker", "exchange", "cik", "lei", "figi", "isin", "cusip"):
            row[field] = ""
    elif category == "ticker_exchange":
        row["entity_name"] = row["domain"] = ""
        for field in ("cik", "lei", "figi", "isin", "cusip"):
            row[field] = ""
    elif category == "ticker_only":
        row["entity_name"] = row["domain"] = row["exchange"] = ""
        for field in ("cik", "lei", "figi", "isin", "cusip"):
            row[field] = ""
    elif category in {"cik_only", "figi_only"}:
        row["entity_name"] = row["domain"] = row["ticker"] = row["exchange"] = ""
        selected_field = category.removesuffix("_only")
        for field in ("cik", "lei", "figi", "isin", "cusip"):
            if field != selected_field:
                row[field] = ""
    elif category == "strong_identifier":
        row["entity_name"] = row["domain"] = row["ticker"] = row["exchange"] = ""
        available = [field for field in ("cik", "lei", "figi", "isin", "cusip") if row[field]]
        chosen = rng.choice(available) if available else None
        for field in ("cik", "lei", "figi", "isin", "cusip"):
            if field != chosen:
                row[field] = ""
        if not chosen:
            row["entity_name"] = candidate.canonical_name
    elif category == "single_typo":
        row["entity_name"] = _single_typo(candidate.canonical_name)
        row["domain"] = ""
    elif category == "swapped_words":
        words = candidate.canonical_name.split()
        row["entity_name"] = " ".join(reversed(words)) if len(words) > 1 else candidate.canonical_name
        row["domain"] = ""
    elif category == "added_division":
        row["entity_name"] = f"{candidate.canonical_name} {rng.choice(('Cloud Division', 'Payments Unit', 'North America'))}"
        row["domain"] = ""
    elif category == "conflicting_country":
        row["country"] = "GB" if candidate.country != "GB" else "US"
        row["domain"] = ""
    elif category == "historical_date":
        row["observation_date"] = "1900-01-01"
        row["domain"] = ""
    return row


def _negative_case(record_id: str, category: str, index: int) -> dict[str, str]:
    row = {field: "" for field in FIELDS}
    row.update({"record_id": record_id, "country": "US", "observation_date": "2025-01-01", "benchmark_category": category})
    if category == "fictional_company":
        row["entity_name"] = f"Northstar Fictional Industries {index} LLC"
        row["domain"] = f"northstar-fictional-{index}.example"
    elif category == "insufficient_information":
        row["entity_name"] = "Holdings" if index % 2 else "Company"
    elif category == "invalid_ticker":
        row["ticker"] = f"ZZ{index % 1000:03d}"
        row["exchange"] = "NASDAQ"
    elif category == "generic_name":
        row["entity_name"] = ("National Bank" if index % 2 else "Global Services Group")
    elif category == "lookalike_name":
        row["entity_name"] = f"Microsoft Plumbing {index}" if index % 2 else f"Amazon River Tours {index}"
    elif category == "private_company":
        row["entity_name"] = f"Harborview Family Office {index}"
        row["domain"] = f"harborview-private-{index}.example"
    return row


def generate_benchmark(reference: str | Path, output_dir: str | Path, count: int = 1000, seed: int = 20260716) -> dict[str, Any]:
    if count < 50:
        raise ValueError("Benchmark count must be at least 50 so categories are represented.")
    reference_path = Path(reference)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    candidates = LocalSecurityMasterProvider(reference_path).candidates
    if not candidates:
        raise ValueError("The reference master contains no candidates.")
    categories = [*POSITIVE_CATEGORIES, *NEGATIVE_CATEGORIES]
    records: list[dict[str, str]] = []
    truth: list[dict[str, str]] = []
    for index in range(count):
        category = categories[index % len(categories)]
        record_id = f"bench-{index + 1:06d}"
        if category in POSITIVE_CATEGORIES:
            eligible = candidates
            if category == "domain_only":
                eligible = [item for item in candidates if item.domain] or candidates
            elif category == "ticker_exchange":
                eligible = [item for item in candidates if item.identifiers.get("ticker") and item.identifiers.get("exchange")] or candidates
            elif category == "strong_identifier":
                eligible = [item for item in candidates if any(item.identifiers.get(f) for f in ("cik", "lei", "figi", "isin", "cusip"))] or candidates
            elif category in {"cik_only", "figi_only"}:
                field = category.removesuffix("_only")
                eligible = [item for item in candidates if item.identifiers.get(field)] or candidates
            elif category == "ticker_only":
                eligible = [item for item in candidates if item.identifiers.get("ticker")] or candidates
            candidate = rng.choice(eligible)
            row = _positive_case(record_id, candidate, category, rng)
            if category == "conflicting_identifier":
                conflicts = [item for item in candidates if item.entity_id != candidate.entity_id and item.identifiers.get("figi")]
                if conflicts:
                    row["figi"] = rng.choice(conflicts).identifiers["figi"]
                    row["domain"] = row["ticker"] = row["exchange"] = ""
            parent = candidate.public_parent or {}
            security = candidate.security or {}
            expected = {
                "record_id": record_id,
                "expected_entity_id": candidate.entity_id,
                "expected_parent_entity_id": parent.get("entityId", ""),
                "expected_security_id": security.get("internal_security_id", "") if any(row.get(field) for field in ("ticker", "figi", "isin", "cusip")) else "",
                "expected_match": "true",
                "category": category,
                "source_entity_id": candidate.entity_id,
            }
        else:
            row = _negative_case(record_id, category, index)
            expected = {"record_id": record_id, "expected_entity_id": "", "expected_parent_entity_id": "", "expected_security_id": "", "expected_match": "false", "category": category, "source_entity_id": ""}
        records.append(row)
        truth.append(expected)
    with (output_path / "records.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS); writer.writeheader(); writer.writerows(records)
    with (output_path / "truth.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRUTH_FIELDS); writer.writeheader(); writer.writerows(truth)
    mapping = {"mapping": {field: target for field, target in {
        "record_id": "recordId", "entity_name": "entityName", "domain": "domain", "ticker": "ticker",
        "exchange": "exchange", "cik": "cik", "lei": "lei", "figi": "figi", "isin": "isin",
        "cusip": "cusip", "country": "country", "observation_date": "observationDate",
        "benchmark_category": "metadata",
    }.items()}}
    (output_path / "mapping.json").write_text(json.dumps(mapping, indent=2) + "\n", encoding="utf-8")
    category_counts = dict(sorted(Counter(row["benchmark_category"] for row in records).items()))
    manifest = {"generator_version": GENERATOR_VERSION, "seed": seed, "record_count": count, "reference_sha256": _reference_hash(reference_path), "positive_records": sum(row["expected_match"] == "true" for row in truth), "negative_records": sum(row["expected_match"] == "false" for row in truth), "category_counts": category_counts}
    (output_path / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def evaluate_results(results: str | Path, truth: str | Path) -> dict[str, Any]:
    with Path(truth).open(encoding="utf-8-sig", newline="") as handle:
        truth_rows = {}
        for row in csv.DictReader(handle):
            if not row.get("record_id"):
                raise ValueError("Every truth row requires record_id.")
            row["expected_entity_id"] = row.get("expected_entity_id") or ""
            row["expected_parent_entity_id"] = row.get("expected_parent_entity_id") or ""
            row["expected_security_id"] = row.get("expected_security_id") or ""
            row["expected_match"] = (row.get("expected_match") or ("true" if row["expected_entity_id"] else "false")).lower()
            row["category"] = row.get("category") or "legacy"
            row["source_entity_id"] = row.get("source_entity_id") or row["expected_entity_id"]
            truth_rows[row["record_id"]] = row
    result_rows = [json.loads(line) for line in Path(results).read_text(encoding="utf-8").splitlines() if line.strip()]
    if set(row["recordId"] for row in result_rows) != set(truth_rows):
        raise ValueError("Result and truth record IDs differ; evaluate matching datasets only.")

    def ranked_ids(row: dict[str, Any]) -> list[str]:
        values = []
        entity_id = (row.get("matchedEntity") or {}).get("entityId")
        if entity_id:
            values.append(entity_id)
        values.extend(item.get("entityId") for item in row.get("alternatives", []) if item.get("entityId"))
        return list(dict.fromkeys(values))

    def ranked_security_ids(row: dict[str, Any]) -> list[str]:
        values = []
        selected = row.get("matchedSecurity") or {}
        selected_id = selected.get("securityId") or selected.get("internal_security_id")
        if selected_id:
            values.append(selected_id)
        values.extend(item.get("securityId") for item in row.get("securityAlternatives", []) if item.get("securityId"))
        return list(dict.fromkeys(values))

    positives = [row for row in result_rows if truth_rows[row["recordId"]]["expected_match"] == "true"]
    negatives = [row for row in result_rows if truth_rows[row["recordId"]]["expected_match"] == "false"]
    auto = [row for row in result_rows if row["status"] == "matched"]
    review = [row for row in result_rows if row["status"] == "review_required"]
    correct_top1 = lambda row: bool(ranked_ids(row)) and ranked_ids(row)[0] == truth_rows[row["recordId"]]["expected_entity_id"]
    correct_top3 = lambda row: truth_rows[row["recordId"]]["expected_entity_id"] in ranked_ids(row)[:3]
    correct_decision = lambda row: (row["status"] != "unmatched" and correct_top1(row)) if truth_rows[row["recordId"]]["expected_match"] == "true" else row["status"] == "unmatched"
    parent_rows = [row for row in positives if truth_rows[row["recordId"]]["expected_parent_entity_id"]]
    security_rows = [row for row in positives if truth_rows[row["recordId"]]["expected_security_id"]]
    security_auto = [row for row in security_rows if row.get("securityDecisionStatus") == "matched"]
    correct_security_top1 = lambda row: bool(ranked_security_ids(row)) and ranked_security_ids(row)[0] == truth_rows[row["recordId"]]["expected_security_id"]

    def resolved_parent_id(row: dict[str, Any]) -> str | None:
        graph = row.get("relationshipGraph") or {}
        return (graph.get("directParent") or row.get("publicParent") or {}).get("entityId")

    correct_parent = lambda row: resolved_parent_id(row) == truth_rows[row["recordId"]]["expected_parent_entity_id"]
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in result_rows:
        by_category[truth_rows[row["recordId"]]["category"]].append(row)
    category_metrics = {}
    for category, rows in sorted(by_category.items()):
        category_parent_rows = [row for row in rows if truth_rows[row["recordId"]]["expected_parent_entity_id"]]
        category_metrics[category] = {
            "count": len(rows),
            "records": len(rows),
            "small_sample_warning": len(rows) < 30,
            "candidate_top_1_accuracy": round(sum(correct_top1(row) for row in rows) / len(rows), 4) if truth_rows[rows[0]["recordId"]]["expected_match"] == "true" else None,
            "decision_accuracy": round(sum(correct_decision(row) for row in rows) / len(rows), 4),
            "auto_match_rate": round(sum(row["status"] == "matched" for row in rows) / len(rows), 4),
            "review_rate": round(sum(row["status"] == "review_required" for row in rows) / len(rows), 4),
            "direct_parent_accuracy": round(sum(correct_parent(row) for row in category_parent_rows) / len(category_parent_rows), 4) if category_parent_rows else None,
            "temporal_invalid_rate": round(sum(((row.get("validity") or {}).get("overall") or {}).get("status") == "invalid" for row in rows) / len(rows), 4),
        }
    temporal_status_counts = {scope: dict(Counter(((row.get("validity") or {}).get(scope) or {}).get("status", "not_available") for row in result_rows)) for scope in ("entity", "security", "relationships", "overall")}
    conflict_types = {"identifier_conflict", "exact_name_identifier_conflict", "security_identifier_conflict"}
    conflict_rows = [row for row in result_rows if any(item.get("type") in conflict_types for item in row.get("evidence", []))]
    automatic_match_correct = sum(correct_top1(row) for row in auto)
    unmatched_rows = [row for row in result_rows if row["status"] == "unmatched"]
    correctly_unmatched = [row for row in unmatched_rows if truth_rows[row["recordId"]]["expected_match"] == "false"]
    false_rejections = [row for row in positives if row["status"] == "unmatched"]

    def pathway(row: dict[str, Any]) -> str:
        if row.get("primaryPathway"):
            return str(row["primaryPathway"])
        types = {item.get("type") for item in row.get("evidence", [])}
        details = {item.get("detail") for item in row.get("evidence", [])}
        if "human_override" in types:
            return "human_override"
        if "reusable_rule_match" in types:
            return "rule"
        if "identifier_match" in types or "security_identifier_match" in types:
            return "strong_identifier"
        if "ticker and exchange" in details:
            return "ticker_exchange"
        if "domain_match" in types:
            return "domain"
        if any(item.get("type") == "name_similarity" and item.get("similarity") == 1 for item in row.get("evidence", [])):
            return "exact_name"
        if "name_similarity" in types:
            return "fuzzy_name"
        return row.get("status", "unknown")

    def latency_summary(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        values = sorted(float(row["processingDurationMs"]) for row in rows if row.get("processingDurationMs") is not None)
        if not values:
            return None
        middle = len(values) // 2
        median = values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2
        p95 = values[max(0, math.ceil(len(values) * .95) - 1)]
        return {"records": len(values), "median": round(median, 4), "p95": round(p95, 4), "max": round(values[-1], 4)}

    pathway_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in result_rows:
        pathway_rows[pathway(row)].append(row)
    pathway_metrics = {}
    for name, rows in sorted(pathway_rows.items()):
        pathway_auto = [row for row in rows if row["status"] == "matched"]
        pathway_correct = sum(correct_top1(row) for row in pathway_auto)
        pathway_metrics[name] = {
            "count": len(rows),
            "records": len(rows),
            "automaticMatches": len(pathway_auto),
            "automaticMatchPrecision": round(pathway_correct / len(pathway_auto), 4) if pathway_auto else None,
            "coverage": round(len(pathway_auto) / len(rows), 4),
            "decisionAccuracy": round(sum(correct_decision(row) for row in rows) / len(rows), 4),
            "smallSampleWarning": len(rows) < 30,
            "latencyMs": latency_summary(rows),
        }

    calibration_bins: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in result_rows:
        calibration_bins[min(int(float(row.get("confidence", 0)) * 5), 4)].append(row)
    confidence_calibration = []
    weighted_gap = 0.0
    for index in range(5):
        rows = calibration_bins.get(index, [])
        if not rows:
            continue
        mean_confidence = sum(float(row.get("confidence", 0)) for row in rows) / len(rows)
        observed_accuracy = sum(correct_decision(row) for row in rows) / len(rows)
        gap = abs(mean_confidence - observed_accuracy)
        weighted_gap += gap * len(rows) / len(result_rows)
        confidence_calibration.append({
            "bin": f"{index / 5:.1f}-{(index + 1) / 5:.1f}",
            "count": len(rows),
            "meanConfidence": round(mean_confidence, 4),
            "observedAccuracy": round(observed_accuracy, 4),
            "absoluteGap": round(gap, 4),
            "smallSampleWarning": len(rows) < 30,
        })
    return {
        "records": len(result_rows),
        "positive_records": len(positives),
        "negative_records": len(negatives),
        "top_1_accuracy": round(sum(correct_top1(row) for row in positives) / len(positives), 4) if positives else None,
        "top_3_recall": round(sum(correct_top3(row) for row in positives) / len(positives), 4) if positives else None,
        "decision_accuracy": round(sum(correct_decision(row) for row in result_rows) / len(result_rows), 4),
        "positive_resolution_rate": round(sum(row["status"] != "unmatched" for row in positives) / len(positives), 4) if positives else None,
        "automatic_match_precision": round(sum(correct_top1(row) for row in auto) / len(auto), 4) if auto else None,
        "automatic_match_correct": automatic_match_correct,
        "automatic_match_total": len(auto),
        "automatic_match_confidence_interval": _wilson_interval(automatic_match_correct, len(auto)),
        "automatic_match_coverage": round(len(auto) / len(result_rows), 4) if result_rows else 0,
        "review_queue_precision": round(sum(correct_top1(row) for row in review) / len(review), 4) if review else None,
        "review_queue_size": len(review),
        "false_positive_rate": round(sum(row["status"] == "matched" for row in negatives) / len(negatives), 4) if negatives else None,
        "unmatched_accuracy": round(len(correctly_unmatched) / len(unmatched_rows), 4) if unmatched_rows else None,
        "unmatched_precision": round(len(correctly_unmatched) / len(unmatched_rows), 4) if unmatched_rows else None,
        "unmatched_correct": len(correctly_unmatched),
        "unmatched_total": len(unmatched_rows),
        "negative_recall": round(len(correctly_unmatched) / len(negatives), 4) if negatives else None,
        "false_rejection_rate": round(len(false_rejections) / len(positives), 4) if positives else None,
        "false_rejection_count": len(false_rejections),
        "false_rejection_total": len(positives),
        "false_rejection_examples": [
            {
                "recordId": row["recordId"],
                "expectedEntityId": truth_rows[row["recordId"]]["expected_entity_id"],
                "topCandidateEntityIds": ranked_ids(row)[:3],
                "confidence": row.get("confidence"),
            }
            for row in false_rejections[:25]
        ],
        "direct_parent_accuracy": round(sum(correct_parent(row) for row in parent_rows) / len(parent_rows), 4) if parent_rows else None,
        "relationship_resolution_coverage": round(sum(bool((row.get("relationshipGraph") or {}).get("edges")) for row in positives) / len(positives), 4) if positives else None,
        "security_top_1_accuracy": round(sum(correct_security_top1(row) for row in security_rows) / len(security_rows), 4) if security_rows else None,
        "security_automatic_match_precision": round(sum(correct_security_top1(row) for row in security_auto) / len(security_auto), 4) if security_auto else None,
        "security_automatic_match_coverage": round(len(security_auto) / len(security_rows), 4) if security_rows else None,
        "security_decision_status_counts": dict(Counter(row.get("securityDecisionStatus", "not_available") for row in result_rows)),
        "identifier_conflict_records": len(conflict_rows),
        "identifier_conflict_counts": dict(Counter(item.get("type") for row in result_rows for item in row.get("evidence", []) if item.get("type") in conflict_types)),
        "identifier_conflict_review_rate": round(sum(row.get("status") == "review_required" for row in conflict_rows) / len(conflict_rows), 4) if conflict_rows else None,
        "temporal_scope_status_counts": temporal_status_counts,
        "overall_temporal_verified_rate": round(sum(((row.get("validity") or {}).get("overall") or {}).get("status") == "verified" for row in result_rows) / len(result_rows), 4) if result_rows else None,
        "overall_temporal_invalid_rate": round(sum(((row.get("validity") or {}).get("overall") or {}).get("status") == "invalid" for row in result_rows) / len(result_rows), 4) if result_rows else None,
        "by_category": category_metrics,
        "by_pathway": pathway_metrics,
        "confidence_calibration": confidence_calibration,
        "expected_calibration_error": round(weighted_gap, 4) if result_rows else None,
        "latency_ms": latency_summary(result_rows),
        "limitations": ["Synthetic perturbations are not evidence of real-world accuracy.", "Metrics are valid only for the supplied reference master and generated cases.", "unmatched_accuracy is retained as a compatibility alias for unmatched_precision; negative_recall reports coverage of the negative set."],
    }


def render_html_report(metrics: dict[str, Any], output: str | Path) -> None:
    def pct(value: float | None) -> str:
        return "—" if value is None else f"{value:.1%}"

    headline = [
        ("Top-1 accuracy", metrics.get("top_1_accuracy")),
        ("Top-3 recall", metrics.get("top_3_recall")),
        ("Auto precision", metrics.get("automatic_match_precision")),
        ("Auto coverage", metrics.get("automatic_match_coverage")),
        ("Decision accuracy", metrics.get("decision_accuracy")),
        ("Direct-parent accuracy", metrics.get("direct_parent_accuracy")),
        ("Security top-1", metrics.get("security_top_1_accuracy")),
        ("Security auto precision", metrics.get("security_automatic_match_precision")),
        ("Conflict review capture", metrics.get("identifier_conflict_review_rate")),
        ("Temporal verified", metrics.get("overall_temporal_verified_rate")),
        ("False-positive rate", metrics.get("false_positive_rate")),
        ("Abstention precision", metrics.get("unmatched_precision")),
        ("False-rejection rate", metrics.get("false_rejection_rate")),
    ]
    cards = "".join(f'<div class="card"><span>{html.escape(label)}</span><strong>{pct(value)}</strong></div>' for label, value in headline)
    rows = "".join(
        f"<tr><td>{html.escape(category)}</td><td>{values['records']}</td><td>{pct(values['candidate_top_1_accuracy'])}</td><td>{pct(values['direct_parent_accuracy'])}</td><td>{pct(values['decision_accuracy'])}</td><td>{pct(values['temporal_invalid_rate'])}</td><td>{pct(values['auto_match_rate'])}</td><td>{pct(values['review_rate'])}</td></tr>"
        for category, values in metrics.get("by_category", {}).items()
    )
    limitations = "".join(f"<li>{html.escape(item)}</li>" for item in metrics.get("limitations", []))
    document = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Symbology Link benchmark report</title><style>
body{{font:15px system-ui;margin:0;background:#f5f7fb;color:#152033}}main{{max-width:1100px;margin:auto;padding:40px 24px}}h1{{margin-bottom:4px}}.muted{{color:#607087}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:28px 0}}.card{{background:white;border:1px solid #dfe5ee;border-radius:12px;padding:18px;display:grid;gap:8px}}.card span{{color:#607087}}.card strong{{font-size:24px}}table{{width:100%;border-collapse:collapse;background:white;border-radius:12px;overflow:hidden}}th,td{{padding:12px;text-align:left;border-bottom:1px solid #e8edf4}}th{{background:#eef2f8}}.warning{{margin-top:24px;padding:16px;background:#fff7db;border:1px solid #efd87a;border-radius:10px}}
</style></head><body><main><h1>Symbology Link benchmark</h1><p class="muted">{metrics.get('records', 0)} records · {metrics.get('positive_records', 0)} positive · {metrics.get('negative_records', 0)} negative</p><section class="cards">{cards}</section><h2>Category performance</h2><table><thead><tr><th>Category</th><th>Records</th><th>Candidate top-1</th><th>Direct parent</th><th>Decision accuracy</th><th>Temporal invalid</th><th>Auto-match</th><th>Review</th></tr></thead><tbody>{rows}</tbody></table><aside class="warning"><strong>Interpretation limits</strong><ul>{limitations}</ul></aside></main></body></html>"""
    Path(output).write_text(document, encoding="utf-8")
