from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .benchmark import evaluate_results, generate_benchmark, render_html_report
from .cache import CacheError, SQLiteCache
from .decision_policies import DecisionPolicySet
from .decisions import OverrideStore, RuleSet
from .engine import MatchEngine
from .ingest import IngestionError, prepare_records, profile_file, read_records, suggest_mapping
from .models import MatchConfig
from .providers import GLEIFProvider, LocalSecurityMasterProvider, OpenFIGIProvider, SECProvider


STRUCTURED_RESULT_FIELDS = {
    "alternatives", "evidence", "matchedEntity", "matchedSecurity",
    "securityAlternatives", "publicParent", "parentAlternatives", "parentEvidence", "relationshipGraph", "validity",
    "sourceRecord", "sourceMetadata", "providerVersions",
}

SPREADSHEET_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


@contextmanager
def _atomic_output_path(output: Path):
    """Publish a completed file atomically without exposing partial output."""
    partial = output.with_name(f"{output.name}.{uuid.uuid4().hex}.partial")
    try:
        yield partial
        os.replace(partial, output)
    except BaseException:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def package_version() -> str:
    try:
        return version("SymbologyLink")
    except PackageNotFoundError:
        return "0.0.0"


def _json_value(value) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _parquet_safe_rows(rows: list[dict]) -> list[dict]:
    return [
        {key: _json_value(value) if key in STRUCTURED_RESULT_FIELDS else value for key, value in row.items()}
        for row in rows
    ]


def _decode_structured_result(row: dict) -> dict:
    decoded = dict(row)
    for key in STRUCTURED_RESULT_FIELDS:
        value = decoded.get(key)
        if isinstance(value, str):
            try:
                decoded[key] = json.loads(value)
            except json.JSONDecodeError:
                pass
    return decoded


def _csv_safe_cell(value):
    """Prevent exported text from being interpreted as a spreadsheet formula."""
    if isinstance(value, str) and value.startswith(SPREADSHEET_FORMULA_PREFIXES):
        return "'" + value
    return value


def load_mapping(path: str | None, columns: list[str]) -> dict[str, str]:
    return load_mapping_config(path, columns)[0]


def load_mapping_config(path: str | None, columns: list[str]) -> tuple[dict[str, str], str | None, str]:
    if not path:
        mapping = suggest_mapping(columns)
        digest = hashlib.sha256(json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return mapping, None, digest
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(value, dict) and "mapping" in value:
        unknown = sorted(set(value) - {"mapping", "dateFormat", "mappingVersion"})
        if unknown:
            raise IngestionError(f"Unknown mapping configuration key(s): {', '.join(unknown)}.")
        mapping = value["mapping"]
        date_format = value.get("dateFormat")
    else:
        mapping, date_format = value, None
    digest = hashlib.sha256(json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return mapping, date_format, digest


def cmd_preview(args: argparse.Namespace) -> int:
    profile = profile_file(args.input, args.samples)
    output = asdict(profile)
    output["suggested_mapping"] = suggest_mapping(profile.columns)
    print(json.dumps(output, indent=2, default=str))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    profile = profile_file(args.input)
    mapping, configured_date_format, mapping_hash = load_mapping_config(args.mapping, profile.columns)
    date_format = getattr(args, "date_format", None) or configured_date_format
    profile, _ = prepare_records(args.input, mapping, profile, date_format)
    print(json.dumps({"valid": True, "rows": profile.row_count, "columns": len(profile.columns), "mapping": mapping, "dateFormat": date_format, "mappingContentSha256": mapping_hash}, indent=2))
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    output = Path(args.output)
    if output.exists() and not getattr(args, "overwrite", False):
        raise IngestionError(f"Output already exists: {output}. Use --overwrite to replace it.")
    profile = profile_file(args.input)
    mapping, configured_date_format, mapping_hash = load_mapping_config(args.mapping, profile.columns)
    date_format = getattr(args, "date_format", None) or configured_date_format
    profile, input_records = prepare_records(args.input, mapping, profile, date_format)
    cache = SQLiteCache(args.cache)
    providers = []
    provider_names = {item.strip() for item in args.providers.split(",") if item.strip()}
    if "customer_security_master" in provider_names or "local" in provider_names:
        if not args.reference:
            raise IngestionError("--reference is required when using the customer security master provider.")
        providers.append(LocalSecurityMasterProvider(args.reference))
    if "gleif" in provider_names:
        providers.append(GLEIFProvider(cache=cache, offline=args.offline))
    if "sec" in provider_names:
        user_agent = args.sec_user_agent or os.getenv("SEC_USER_AGENT")
        if not user_agent:
            raise IngestionError("SEC requires --sec-user-agent 'Organization contact@example.com'.")
        providers.append(SECProvider(user_agent, cache=cache, offline=args.offline))
    if "openfigi" in provider_names:
        providers.append(OpenFIGIProvider(args.openfigi_api_key or os.getenv("OPENFIGI_API_KEY"), cache=cache, offline=args.offline, enable_name_search=args.openfigi_name_search))
    if not providers:
        raise IngestionError("Enable at least one provider.")
    legacy_auto = getattr(args, "auto_threshold", None)
    legacy_review = getattr(args, "review_threshold", None)
    if args.decision_policies and (legacy_auto is not None or legacy_review is not None):
        raise IngestionError("Use --decision-policies or the deprecated threshold flags, not both.")
    config = MatchConfig(
        auto_match_threshold=legacy_auto if legacy_auto is not None else .98,
        review_threshold=legacy_review if legacy_review is not None else .80,
        max_candidates=args.max_candidates,
        mapping_version=args.mapping_version,
        relationship_max_depth=args.relationship_max_depth,
    )
    if legacy_auto is not None or legacy_review is not None:
        print("warning: --auto-threshold and --review-threshold are deprecated; use --decision-policies instead.", file=sys.stderr)
        try:
            decision_policies = DecisionPolicySet.legacy(config.auto_match_threshold, config.review_threshold)
        except ValueError as exc:
            raise IngestionError(f"Invalid legacy thresholds: {exc}") from exc
    else:
        decision_policies = DecisionPolicySet.load(args.decision_policies)
    engine = MatchEngine(providers, config, RuleSet.load(args.rules), OverrideStore(args.overrides) if args.overrides else None, decision_policies)
    results = [result.to_dict() for result in engine.match_batch(input_records)]
    engine_version = package_version()
    provider_versions = {provider.name: str(getattr(provider, "version", engine_version)) for provider in providers}
    for result in results:
        result["inputFileSha256"] = profile.sha256
        result["engineVersion"] = engine_version
        result["providerVersions"] = provider_versions
        result["mappingContentSha256"] = mapping_hash
    with _atomic_output_path(output) as partial:
        if output.suffix.lower() in {".parquet", ".pq"}:
            try:
                import pyarrow as pa
                import pyarrow.parquet as pq
            except ImportError as exc:
                raise IngestionError("Parquet output requires: pip install 'symbologylink[parquet]'") from exc
            pq.write_table(pa.Table.from_pylist(_parquet_safe_rows(results)), partial, compression="zstd")
        elif output.suffix.lower() == ".json":
            partial.write_text(json.dumps(results, indent=2, default=str) + "\n", encoding="utf-8")
        else:
            with partial.open("w", encoding="utf-8", newline="") as handle:
                for result in results:
                    handle.write(json.dumps(result, separators=(",", ":"), default=str) + "\n")
    print(json.dumps({"status": "completed", "records": profile.row_count, "output": str(output.resolve()), "mappingVersion": args.mapping_version, "mappingContentSha256": mapping_hash, "dateFormat": date_format, "inputFileSha256": profile.sha256, "engineVersion": engine_version, "providerVersions": provider_versions}, indent=2))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    output_path = Path(args.output)
    if output_path.exists() and not getattr(args, "overwrite", False):
        raise IngestionError(f"Output already exists: {output_path}. Use --overwrite to replace it.")
    input_path = Path(args.input)
    if input_path.suffix.lower() in {".parquet", ".pq"}:
        rows = [_decode_structured_result(row) for row in read_records(input_path)]
    elif input_path.suffix.lower() == ".json":
        rows = [_decode_structured_result(row) for row in json.loads(input_path.read_text(encoding="utf-8"))]
    else:
        rows = [_decode_structured_result(json.loads(line)) for line in input_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    status_filter = getattr(args, "status", None)
    if status_filter:
        allowed_statuses = set(status_filter)
        rows = [row for row in rows if row.get("status") in allowed_statuses]
    source_fields = list(dict.fromkeys(key for row in rows for key in (row.get("sourceRecord") or {})))
    enrichment_fields = ["record_id", "match_status", "matched_entity_id", "matched_entity_name", "match_score", "score_is_calibrated", "primary_pathway", "security_decision_status", "security_confidence", "security_match_score", "security_score_is_calibrated", "security_primary_pathway", "security_id", "security_alternative_count", "identifier_conflict_count", "identifier_conflict_types", "ticker", "exchange", "figi", "confidence", "parent_status", "parent_alternative_count", "selected_parent_id", "selected_parent_sources", "direct_parent_id", "ultimate_parent_id", "accounting_direct_parent_id", "accounting_ultimate_parent_id", "issuer_id", "entity_validity_status", "security_validity_status", "relationship_validity_status", "overall_validity_status", "valid_on_observation_date", "relationship_status", "mapping_version", "decision_source", "decision_version", "input_file_sha256", "engine_version", "source_metadata_json"]
    fields = list(dict.fromkeys([*source_fields, *enrichment_fields]))
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        csv.writer(handle).writerow([_csv_safe_cell(field) for field in fields])
        for row in rows:
            entity, security = row.get("matchedEntity") or {}, row.get("matchedSecurity") or {}
            graph = row.get("relationshipGraph") or {}
            parent = row.get("publicParent") or {}
            validity = row.get("validity") or {}
            conflict_types = [item.get("type") for item in row.get("evidence", []) if item.get("type") in {"identifier_conflict", "exact_name_identifier_conflict", "security_identifier_conflict"}]
            enriched = {"record_id": row["recordId"], "match_status": row["status"], "matched_entity_id": entity.get("entityId"), "matched_entity_name": entity.get("canonicalName"), "match_score": row.get("matchScore"), "score_is_calibrated": row.get("scoreIsCalibrated"), "primary_pathway": row.get("primaryPathway"), "security_decision_status": row.get("securityDecisionStatus"), "security_confidence": row.get("securityConfidence"), "security_match_score": row.get("securityMatchScore"), "security_score_is_calibrated": row.get("securityScoreIsCalibrated"), "security_primary_pathway": row.get("securityPrimaryPathway"), "security_id": security.get("securityId"), "security_alternative_count": len(row.get("securityAlternatives") or []), "identifier_conflict_count": len(conflict_types), "identifier_conflict_types": "|".join(dict.fromkeys(conflict_types)), "ticker": security.get("ticker"), "exchange": security.get("exchange"), "figi": security.get("figi"), "confidence": row["confidence"], "parent_status": row.get("parentStatus"), "parent_alternative_count": len(row.get("parentAlternatives") or []), "selected_parent_id": parent.get("entityId"), "selected_parent_sources": "|".join(parent.get("sources") or []), "direct_parent_id": (graph.get("directParent") or {}).get("entityId"), "ultimate_parent_id": (graph.get("ultimateParent") or {}).get("entityId"), "accounting_direct_parent_id": (graph.get("accountingDirectParent") or {}).get("entityId"), "accounting_ultimate_parent_id": (graph.get("accountingUltimateParent") or {}).get("entityId"), "issuer_id": (graph.get("issuer") or {}).get("entityId"), "entity_validity_status": (validity.get("entity") or {}).get("status"), "security_validity_status": (validity.get("security") or {}).get("status"), "relationship_validity_status": (validity.get("relationships") or {}).get("status"), "overall_validity_status": (validity.get("overall") or {}).get("status"), "valid_on_observation_date": (validity.get("overall") or {}).get("validOnObservationDate"), "relationship_status": row.get("relationshipStatus"), "mapping_version": row["mappingVersion"], "decision_source": row.get("decisionSource"), "decision_version": row.get("decisionVersion"), "input_file_sha256": row.get("inputFileSha256"), "engine_version": row.get("engineVersion"), "source_metadata_json": _json_value(row.get("sourceMetadata") or {})}
            writer.writerow({
                key: _csv_safe_cell(value)
                for key, value in {**(row.get("sourceRecord") or {}), **enriched}.items()
            })
    print(json.dumps({"status": "completed", "records": len(rows), "statuses": status_filter or "all", "output": str(output_path.resolve())}, indent=2))
    return 0


def cmd_provider_test(args: argparse.Namespace) -> int:
    values = []
    if args.reference:
        values.append(LocalSecurityMasterProvider(args.reference).health_check())
    if args.gleif:
        values.append(GLEIFProvider(SQLiteCache(args.cache), offline=args.offline).health_check())
    if args.sec:
        user_agent = args.sec_user_agent or os.getenv("SEC_USER_AGENT")
        if not user_agent:
            raise IngestionError("SEC requires --sec-user-agent 'Organization contact@example.com'.")
        values.append(SECProvider(user_agent, SQLiteCache(args.cache), offline=args.offline).health_check())
    if args.openfigi:
        values.append(OpenFIGIProvider(args.openfigi_api_key or os.getenv("OPENFIGI_API_KEY"), SQLiteCache(args.cache), offline=args.offline).health_check())
    if not values:
        raise IngestionError("Choose --reference and/or --gleif.")
    print(json.dumps(values, indent=2))
    return 0


def cmd_rules_validate(args: argparse.Namespace) -> int:
    rules = RuleSet.load(args.file)
    print(json.dumps({"valid": True, "rules": len(rules.rules), "enabled": sum(item.get("enabled", True) for item in rules.rules)}, indent=2))
    return 0


def cmd_override_add(args: argparse.Namespace) -> int:
    decision = json.loads(Path(args.decision).read_text(encoding="utf-8"))
    saved = OverrideStore(args.store).append(decision)
    print(json.dumps(saved, indent=2))
    return 0


def cmd_cache(args: argparse.Namespace) -> int:
    cache = SQLiteCache(args.path)
    if args.cache_command == "clear":
        print(json.dumps({"cleared": cache.clear(args.provider)}, indent=2))
    else:
        print(json.dumps(cache.stats(), indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("The API requires: pip install 'symbologylink[api]'") from exc
    uvicorn.run("symbologylink.api:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_benchmark(args: argparse.Namespace) -> int:
    if args.benchmark_command == "generate":
        metrics = generate_benchmark(args.reference, args.output_dir, args.count, args.seed)
        metrics["output_dir"] = str(Path(args.output_dir).resolve())
    else:
        metrics = evaluate_results(args.results, args.truth)
        if args.output:
            Path(args.output).write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
        if args.html:
            render_html_report(metrics, args.html)
    print(json.dumps(metrics, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="symbologylink", description="Auditable company and security resolution")
    parser.add_argument("--version", action="version", version=f"%(prog)s {package_version()}")
    sub = parser.add_subparsers(dest="command", required=True)
    preview = sub.add_parser("preview"); preview.add_argument("--input", required=True); preview.add_argument("--samples", type=int, default=5); preview.set_defaults(func=cmd_preview)
    validate = sub.add_parser("validate"); validate.add_argument("--input", required=True); validate.add_argument("--mapping"); validate.add_argument("--date-format"); validate.set_defaults(func=cmd_validate)
    resolve = sub.add_parser("resolve"); resolve.add_argument("--input", required=True); resolve.add_argument("--mapping"); resolve.add_argument("--date-format"); resolve.add_argument("--reference"); resolve.add_argument("--providers", default="customer_security_master"); resolve.add_argument("--cache", default=".symbologylink/cache.sqlite3"); resolve.add_argument("--offline", action="store_true"); resolve.add_argument("--sec-user-agent"); resolve.add_argument("--openfigi-api-key"); resolve.add_argument("--openfigi-name-search", action="store_true"); resolve.add_argument("--rules"); resolve.add_argument("--overrides"); resolve.add_argument("--decision-policies", help="JSON or YAML pathway decision policy configuration"); resolve.add_argument("--output", required=True); resolve.add_argument("--overwrite", action="store_true"); resolve.add_argument("--auto-threshold", type=float, default=None, help="Deprecated: use --decision-policies"); resolve.add_argument("--review-threshold", type=float, default=None, help="Deprecated: use --decision-policies"); resolve.add_argument("--max-candidates", type=int, default=20); resolve.add_argument("--relationship-max-depth", type=int, default=8); resolve.add_argument("--mapping-version", default="v1"); resolve.set_defaults(func=cmd_resolve)
    export = sub.add_parser("export"); export.add_argument("--input", required=True); export.add_argument("--output", required=True); export.add_argument("--status", action="append", choices=["matched", "review_required", "unmatched", "provider_error"]); export.add_argument("--overwrite", action="store_true"); export.set_defaults(func=cmd_export)
    providers = sub.add_parser("providers"); provider_sub = providers.add_subparsers(required=True); provider_test = provider_sub.add_parser("test"); provider_test.add_argument("--reference"); provider_test.add_argument("--gleif", action="store_true"); provider_test.add_argument("--sec", action="store_true"); provider_test.add_argument("--sec-user-agent"); provider_test.add_argument("--openfigi", action="store_true"); provider_test.add_argument("--openfigi-api-key"); provider_test.add_argument("--cache", default=".symbologylink/cache.sqlite3"); provider_test.add_argument("--offline", action="store_true"); provider_test.set_defaults(func=cmd_provider_test)
    benchmark = sub.add_parser("benchmark"); benchmark_sub = benchmark.add_subparsers(dest="benchmark_command", required=True)
    benchmark_generate = benchmark_sub.add_parser("generate"); benchmark_generate.add_argument("--reference", required=True); benchmark_generate.add_argument("--output-dir", required=True); benchmark_generate.add_argument("--count", type=int, default=1000); benchmark_generate.add_argument("--seed", type=int, default=20260716); benchmark_generate.set_defaults(func=cmd_benchmark)
    benchmark_evaluate = benchmark_sub.add_parser("evaluate"); benchmark_evaluate.add_argument("--results", required=True); benchmark_evaluate.add_argument("--truth", required=True); benchmark_evaluate.add_argument("--output"); benchmark_evaluate.add_argument("--html"); benchmark_evaluate.set_defaults(func=cmd_benchmark)
    rules = sub.add_parser("rules"); rule_sub = rules.add_subparsers(required=True); rule_validate = rule_sub.add_parser("validate"); rule_validate.add_argument("--file", required=True); rule_validate.set_defaults(func=cmd_rules_validate)
    overrides = sub.add_parser("overrides"); override_sub = overrides.add_subparsers(required=True); override_add = override_sub.add_parser("add"); override_add.add_argument("--store", required=True); override_add.add_argument("--decision", required=True); override_add.set_defaults(func=cmd_override_add)
    cache_parser = sub.add_parser("cache"); cache_sub = cache_parser.add_subparsers(dest="cache_command", required=True); cache_stats = cache_sub.add_parser("stats"); cache_stats.add_argument("--path", default=".symbologylink/cache.sqlite3"); cache_stats.set_defaults(func=cmd_cache); cache_clear = cache_sub.add_parser("clear"); cache_clear.add_argument("--path", default=".symbologylink/cache.sqlite3"); cache_clear.add_argument("--provider"); cache_clear.set_defaults(func=cmd_cache)
    serve = sub.add_parser("serve"); serve.add_argument("--host", default="127.0.0.1"); serve.add_argument("--port", type=int, default=8000); serve.add_argument("--reload", action="store_true"); serve.set_defaults(func=cmd_serve)
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        return args.func(args)
    except (CacheError, IngestionError, ValueError, OSError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc), "suggested_action": "Review the input file, mapping, and reference paths."}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
