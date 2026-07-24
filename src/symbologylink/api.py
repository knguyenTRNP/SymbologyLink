from __future__ import annotations

import json
import hashlib
import os
import threading
import time
import uuid
from collections import Counter, deque
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

try:
    from fastapi import BackgroundTasks, Body, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
    from fastapi.responses import JSONResponse
except ImportError as exc:  # pragma: no cover - exercised only without API extras
    raise RuntimeError("The API requires: pip install 'symbologylink[api]'") from exc

from .cache import SQLiteCache
from .decision_policies import DecisionPolicySet
from .decisions import OverrideStore, RuleSet
from .datasets import DatasetStore
from .engine import MatchEngine
from .ingest import IngestionError, prepare_records, profile_file, suggest_mapping
from .jobs import JobStore
from .models import EntityMatchInput, MatchConfig
from .providers import GLEIFProvider, LocalSecurityMasterProvider, MatchProvider, OpenFIGIProvider, SECProvider
from .relationship_master import CustomerRelationshipMasterProvider

INPUT_FIELDS = {item.name for item in fields(EntityMatchInput)}


def _input(value: dict[str, Any]) -> EntityMatchInput:
    known = {key: item for key, item in value.items() if key in INPUT_FIELDS}
    known["recordId"] = str(known.get("recordId") or uuid.uuid4())
    known["metadata"] = {**value.get("metadata", {}), **{key: item for key, item in value.items() if key not in INPUT_FIELDS}}
    known["sourceRecord"] = dict(value.get("sourceRecord") or {key: item for key, item in value.items() if key != "sourceRecord"})
    return EntityMatchInput(**known)


class Settings:
    def __init__(self):
        self.reference = os.getenv("SYMBOLOGYLINK_REFERENCE")
        self.relationship_master = os.getenv("SYMBOLOGYLINK_RELATIONSHIP_MASTER")
        self.relationship_mapping = os.getenv("SYMBOLOGYLINK_RELATIONSHIP_MAPPING")
        self.relationship_config = os.getenv("SYMBOLOGYLINK_RELATIONSHIP_CONFIG")
        self.relationship_trust_level = os.getenv("SYMBOLOGYLINK_RELATIONSHIP_TRUST_LEVEL", "authoritative")
        self.rules = os.getenv("SYMBOLOGYLINK_RULES", ".symbologylink/rules.json")
        self.decision_policies = os.getenv("SYMBOLOGYLINK_DECISION_POLICIES")
        self.overrides = os.getenv("SYMBOLOGYLINK_OVERRIDES", ".symbologylink/overrides.jsonl")
        self.cache = os.getenv("SYMBOLOGYLINK_CACHE", ".symbologylink/cache.sqlite3")
        self.jobs = os.getenv("SYMBOLOGYLINK_JOBS", ".symbologylink/jobs.sqlite3")
        self.datasets = os.getenv("SYMBOLOGYLINK_DATASETS", ".symbologylink/datasets.sqlite3")
        self.uploads = os.getenv("SYMBOLOGYLINK_UPLOADS", ".symbologylink/uploads")
        self.enable_gleif = os.getenv("SYMBOLOGYLINK_ENABLE_GLEIF", "true").lower() == "true"
        self.enable_sec = os.getenv("SYMBOLOGYLINK_ENABLE_SEC", "false").lower() == "true"
        self.sec_user_agent = os.getenv("SYMBOLOGYLINK_SEC_USER_AGENT")
        self.enable_openfigi = os.getenv("SYMBOLOGYLINK_ENABLE_OPENFIGI", "true").lower() == "true"
        self.openfigi_api_key = os.getenv("SYMBOLOGYLINK_OPENFIGI_API_KEY")
        self.openfigi_name_search = os.getenv("SYMBOLOGYLINK_OPENFIGI_NAME_SEARCH", "false").lower() == "true"
        self.relationship_max_depth = int(os.getenv("SYMBOLOGYLINK_RELATIONSHIP_MAX_DEPTH", "8"))
        self.offline = os.getenv("SYMBOLOGYLINK_OFFLINE", "false").lower() == "true"
        self.api_key = os.getenv("SYMBOLOGYLINK_API_KEY")
        self.rate_limit_per_minute = int(os.getenv("SYMBOLOGYLINK_RATE_LIMIT_PER_MINUTE", "30"))


class _PerKeyRateLimiter:
    def __init__(self, limit: int, window_seconds: float = 60):
        self.limit = max(1, limit)
        self.window_seconds = window_seconds
        self.requests: dict[str, deque[float]] = {}
        self.lock = threading.Lock()

    def retry_after(self, key: str) -> int | None:
        now = time.monotonic()
        with self.lock:
            values = self.requests.setdefault(key, deque())
            while values and now - values[0] >= self.window_seconds:
                values.popleft()
            if len(values) >= self.limit:
                return max(1, int(self.window_seconds - (now - values[0]) + .999))
            values.append(now)
            return None


settings = Settings()
cache = SQLiteCache(settings.cache)
job_store = JobStore(settings.jobs)
dataset_store = DatasetStore(settings.datasets, settings.uploads)
override_store = OverrideStore(settings.overrides)
rate_limiter = _PerKeyRateLimiter(settings.rate_limit_per_minute)


def providers() -> list[MatchProvider]:
    values: list[MatchProvider] = []
    if settings.reference:
        values.append(LocalSecurityMasterProvider(settings.reference))
    if settings.relationship_config or settings.relationship_master:
        entity_ids = {
            candidate.entity_id
            for provider in values if isinstance(provider, LocalSecurityMasterProvider)
            for candidate in provider.candidates
        }
        if settings.relationship_config:
            values.append(CustomerRelationshipMasterProvider.from_config(settings.relationship_config, entity_ids=entity_ids or None))
        else:
            values.append(CustomerRelationshipMasterProvider(
                settings.relationship_master, settings.relationship_mapping,
                trust_level=settings.relationship_trust_level, entity_ids=entity_ids or None,
            ))
    if settings.enable_openfigi:
        values.append(OpenFIGIProvider(settings.openfigi_api_key, cache=cache, offline=settings.offline, enable_name_search=settings.openfigi_name_search))
    if settings.enable_sec:
        if not settings.sec_user_agent:
            raise RuntimeError("SYMBOLOGYLINK_SEC_USER_AGENT is required when SEC is enabled.")
        values.append(SECProvider(settings.sec_user_agent, cache=cache, offline=settings.offline))
    if settings.enable_gleif:
        values.append(GLEIFProvider(cache=cache, offline=settings.offline))
    return values


def engine() -> MatchEngine:
    return MatchEngine(providers(), MatchConfig(relationship_max_depth=settings.relationship_max_depth), RuleSet.load(settings.rules), override_store, DecisionPolicySet.load(settings.decision_policies))


app = FastAPI(title="Symbology Link API", version="0.0.0")


@app.middleware("http")
async def authentication_middleware(request: Request, call_next):
    public_paths = {"/health", "/docs", "/openapi.json", "/redoc"}
    if settings.api_key and request.url.path not in public_paths and request.headers.get("X-API-Key") != settings.api_key:
        return JSONResponse(status_code=401, content={"error": {"code": "invalid_api_key", "message": "Supply a valid X-API-Key header."}})
    if settings.api_key and request.url.path not in public_paths:
        retry_after = rate_limiter.retry_after(request.headers.get("X-API-Key") or "anonymous")
        if retry_after is not None:
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(retry_after)},
                content={"error": {"code": "rate_limit_exceeded", "message": "API key request limit exceeded. Retry later."}},
            )
    return await call_next(request)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(Exception)
async def unhandled_error(_: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"error": {"code": "internal_error", "message": str(exc), "suggestedAction": "Check the server logs and provider configuration."}})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.0.0"}


@app.post("/v1/match")
def match_one(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return engine().match(_input(payload)).to_dict()


def _run_job(job_id: str) -> None:
    try:
        records = job_store.request(job_id)
        matcher = engine()
        results = []
        job_store.update(job_id, status="validating", stage="validating", progress=2)
        total = len(records)
        job_store.update(job_id, status="normalizing", stage="normalizing", progress=5)
        for start in range(0, total, 100):
            if job_store.cancellation_requested(job_id):
                job_store.update(job_id, status="cancelled", stage="cancelled", progress=len(results) / max(total, 1) * 100, rows_processed=len(results), results_json=json.dumps(results))
                return
            job_store.update(job_id, status="generating_candidates", stage="generating_candidates", progress=5 + 90 * start / max(total, 1), rows_processed=len(results))
            chunk = [_input(value) for value in records[start:start + 100]]
            job_store.update(job_id, status="scoring", stage="scoring", progress=5 + 90 * start / max(total, 1), rows_processed=len(results))
            results.extend(item.to_dict() for item in matcher.match_batch(chunk))
        counts = Counter(result["final_decision"] for result in results)
        entity_counts = Counter((item.get("entity") or {}).get("status", "unknown") for item in results)
        parent_counts = Counter((item.get("public_parent") or {}).get("status", "unknown") for item in results)
        security_counts = Counter((item.get("security") or {}).get("status", "unknown") for item in results)
        relationship_counts = Counter(((item.get("public_parent") or {}).get("attributes") or {}).get("relationship_status", "not_resolved") for item in results)
        temporal_counts = {scope: dict(Counter(((item.get("temporal") or {}).get(scope) or {}).get("status", "unknown") for item in results)) for scope in ("entity", "public_parent", "security", "overall")}
        conflict_types = {"identifier_conflict", "exact_name_identifier_conflict", "security_identifier_conflict"}
        component_evidence = lambda item: [evidence for name in ("entity", "public_parent", "security") for evidence in (item.get(name) or {}).get("evidence", [])]
        conflict_counts = Counter(evidence.get("type") for item in results for evidence in component_evidence(item) if evidence.get("type") in conflict_types)
        metrics = {
            "totalRecords": total,
            "schemaVersion": "2.0",
            "finalDecisionCounts": dict(counts),
            "entityStatusCounts": dict(entity_counts),
            "parentStatusCounts": dict(parent_counts),
            "securityStatusCounts": dict(security_counts),
            "averageEntityMatchScore": round(sum((item.get("entity") or {}).get("match_score") or 0 for item in results) / total, 4) if total else 0,
            "averageSecurityMatchScore": round(sum((item.get("security") or {}).get("match_score") or 0 for item in results) / total, 4) if total else 0,
            "identifierConflictRecords": sum(any(evidence.get("type") in conflict_types for evidence in component_evidence(item)) for item in results),
            "identifierConflictCounts": dict(conflict_counts),
            "temporalScopeStatusCounts": temporal_counts,
            "relationshipStatusCounts": dict(relationship_counts),
            "parentReviewRecords": sum((item.get("public_parent") or {}).get("status") in {"candidate", "ambiguous"} for item in results),
        }
        job_store.update(job_id, status="completed", stage="completed", progress=100, rows_processed=total, results_json=json.dumps(results, separators=(",", ":")), metrics_json=json.dumps(metrics, separators=(",", ":")))
    except Exception as exc:
        job_store.update(job_id, status="failed", stage="failed", error=str(exc))


SUPPORTED_UPLOAD_SUFFIXES = {".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".parquet", ".pq"}
MAX_UPLOAD_BYTES = 100 * 1024 * 1024


async def _stream_upload(upload: UploadFile, destination: Path) -> tuple[int, str]:
    size, digest = 0, hashlib.sha256()
    try:
        with destination.open("wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise IngestionError("File exceeds the 100 MB upload limit.")
                digest.update(chunk)
                handle.write(chunk)
    finally:
        await upload.close()
    if size == 0:
        raise IngestionError("The uploaded file is empty.")
    return size, digest.hexdigest()


def _profile_payload(path: Path) -> tuple[dict[str, Any], Any]:
    profile = profile_file(path)
    payload = asdict(profile)
    payload.pop("path", None)
    payload["suggested_mapping"] = suggest_mapping(profile.columns)
    return payload, profile


def _dataset_or_404(dataset_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    public, internal = dataset_store.get(dataset_id), dataset_store.internal(dataset_id)
    if not public or not internal:
        raise HTTPException(status_code=404, detail={"code": "dataset_not_found", "message": "No dataset has that ID."})
    return public, internal


@app.post("/v1/datasets", status_code=201)
async def upload_dataset(file: UploadFile = File(...)) -> dict[str, Any]:
    filename = Path(file.filename or "").name
    suffix = Path(filename).suffix.lower()
    if not filename or suffix not in SUPPORTED_UPLOAD_SUFFIXES:
        raise HTTPException(status_code=415, detail={"code": "unsupported_file_type", "message": "Upload CSV, TSV, JSON, JSON Lines, or Parquet."})
    dataset = dataset_store.create(filename, file.content_type, suffix)
    internal = dataset_store.internal(dataset["datasetId"])
    destination = Path(internal["stored_path"])
    try:
        size, sha256 = await _stream_upload(file, destination)
        dataset_store.update(dataset["datasetId"], status="uploaded", file_size=size, sha256=sha256, file_type=suffix.lstrip("."), error=None)
        dataset_store.update(dataset["datasetId"], status="validating")
        profile, raw_profile = _profile_payload(destination)
        return dataset_store.update(dataset["datasetId"], status="ready", row_count=raw_profile.row_count, column_count=len(raw_profile.columns), profile_json=json.dumps(profile, default=str), error=None)
    except IngestionError as exc:
        if destination.exists():
            destination.unlink()
        dataset_store.update(dataset["datasetId"], status="invalid", error=str(exc))
        status = 413 if "100 MB" in str(exc) else 422
        raise HTTPException(status_code=status, detail={"code": "invalid_dataset", "message": str(exc), "datasetId": dataset["datasetId"]})
    except Exception as exc:
        if destination.exists():
            destination.unlink()
        dataset_store.update(dataset["datasetId"], status="failed", error=str(exc))
        raise


@app.get("/v1/datasets")
def list_datasets(offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000), include_deleted: bool = False) -> dict[str, Any]:
    return dataset_store.list(offset, limit, include_deleted)


@app.get("/v1/datasets/{dataset_id}")
def get_dataset(dataset_id: str) -> dict[str, Any]:
    return _dataset_or_404(dataset_id)[0]


@app.get("/v1/datasets/{dataset_id}/preview")
def preview_dataset(dataset_id: str) -> dict[str, Any]:
    dataset, _ = _dataset_or_404(dataset_id)
    if dataset["status"] != "ready":
        raise HTTPException(status_code=409, detail={"code": "dataset_not_ready", "message": f"Dataset status is {dataset['status']}."})
    return {"datasetId": dataset_id, "version": dataset["version"], **(dataset["profile"] or {})}


@app.put("/v1/datasets/{dataset_id}")
async def replace_dataset(dataset_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    current, internal = _dataset_or_404(dataset_id)
    if current["status"] == "deleted":
        raise HTTPException(status_code=410, detail={"code": "dataset_deleted", "message": "Deleted datasets cannot be replaced."})
    filename = Path(file.filename or "").name
    suffix = Path(filename).suffix.lower()
    if not filename or suffix not in SUPPORTED_UPLOAD_SUFFIXES:
        raise HTTPException(status_code=415, detail={"code": "unsupported_file_type", "message": "Upload CSV, TSV, JSON, JSON Lines, or Parquet."})
    version = current["version"] + 1
    destination = dataset_store.file_path(dataset_id, version, suffix)
    dataset_store.update(dataset_id, status="replacing", error=None)
    try:
        size, sha256 = await _stream_upload(file, destination)
        profile, raw_profile = _profile_payload(destination)
    except IngestionError as exc:
        if destination.exists():
            destination.unlink()
        dataset_store.update(dataset_id, status=current["status"], error=f"Replacement rejected: {exc}")
        status = 413 if "100 MB" in str(exc) else 422
        raise HTTPException(status_code=status, detail={"code": "replacement_invalid", "message": str(exc), "datasetId": dataset_id})
    except Exception as exc:
        if destination.exists():
            destination.unlink()
        dataset_store.update(dataset_id, status=current["status"], error=f"Replacement failed: {exc}")
        raise
    old_path = Path(internal["stored_path"]) if internal["stored_path"] else None
    updated = dataset_store.update(dataset_id, original_filename=filename, stored_path=str(destination), content_type=file.content_type, status="ready", file_size=size, sha256=sha256, file_type=suffix.lstrip("."), row_count=raw_profile.row_count, column_count=len(raw_profile.columns), profile_json=json.dumps(profile, default=str), error=None, version=version)
    if old_path and old_path != destination and old_path.exists():
        old_path.unlink()
    return updated


@app.delete("/v1/datasets/{dataset_id}")
def delete_dataset(dataset_id: str) -> dict[str, Any]:
    try:
        return dataset_store.delete(dataset_id)
    except KeyError:
        raise HTTPException(status_code=404, detail={"code": "dataset_not_found", "message": "No dataset has that ID."})


@app.post("/v1/datasets/{dataset_id}/resolve", status_code=202)
def resolve_dataset(dataset_id: str, background_tasks: BackgroundTasks, payload: dict[str, Any] = Body(default={}), idempotency_key: str | None = Header(None, alias="Idempotency-Key")) -> dict[str, Any]:
    dataset, internal = _dataset_or_404(dataset_id)
    if dataset["status"] != "ready":
        raise HTTPException(status_code=409, detail={"code": "dataset_not_ready", "message": f"Dataset status is {dataset['status']}."})
    mapping = payload.get("mapping") or (dataset.get("profile") or {}).get("suggested_mapping")
    if not mapping:
        raise HTTPException(status_code=422, detail={"code": "mapping_required", "message": "Supply a mapping or upload a dataset with recognizable columns."})
    try:
        _, prepared = prepare_records(internal["stored_path"], mapping, date_format=payload.get("dateFormat"))
    except IngestionError as exc:
        raise HTTPException(status_code=422, detail={"code": "dataset_preflight_failed", "message": str(exc)}) from exc
    records = [{field.name: getattr(record, field.name) for field in fields(EntityMatchInput)} for record in prepared]
    job, created = job_store.create(records, idempotency_key or f"dataset:{dataset_id}:v{dataset['version']}:{json.dumps(mapping, sort_keys=True)}")
    if created:
        background_tasks.add_task(_run_job, job["jobId"])
    return {**job, "datasetId": dataset_id, "datasetVersion": dataset["version"]}


@app.post("/v1/match/batch", status_code=202)
def match_batch(background_tasks: BackgroundTasks, payload: dict[str, Any] = Body(...), idempotency_key: str | None = Header(None, alias="Idempotency-Key")) -> dict[str, Any]:
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        raise HTTPException(status_code=422, detail={"code": "invalid_records", "message": "records must be a non-empty array."})
    if len(records) > 100_000:
        raise HTTPException(status_code=413, detail={"code": "row_limit", "message": "Batch exceeds 100,000 records."})
    job, created = job_store.create(records, idempotency_key)
    if created:
        background_tasks.add_task(_run_job, job["jobId"])
    return job


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    value = job_store.get(job_id)
    if not value:
        raise HTTPException(status_code=404, detail={"code": "job_not_found", "message": "No job has that ID."})
    return value


@app.get("/v1/jobs/{job_id}/results")
def get_results(job_id: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
    try:
        return job_store.results(job_id, offset, limit)
    except KeyError:
        raise HTTPException(status_code=404, detail={"code": "job_not_found", "message": "No job has that ID."})


@app.get("/v1/jobs/{job_id}/metrics")
def get_metrics(job_id: str) -> dict[str, Any] | None:
    try:
        return job_store.metrics(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail={"code": "job_not_found", "message": "No job has that ID."})


@app.post("/v1/jobs/{job_id}/cancel", status_code=202)
def cancel_job(job_id: str) -> dict[str, Any]:
    if not job_store.cancel(job_id):
        raise HTTPException(status_code=404, detail={"code": "job_not_found", "message": "No job has that ID."})
    return {"jobId": job_id, "cancelRequested": True}


@app.get("/v1/providers")
def list_providers() -> list[dict[str, Any]]:
    return [{**item.metadata(), "name": item.name, "configured": True} for item in providers()]


@app.post("/v1/providers/test")
def test_providers() -> list[dict[str, Any]]:
    return [item.health_check() for item in providers()]


@app.get("/v1/rules")
def list_rules() -> list[dict[str, Any]]:
    return RuleSet.load(settings.rules).rules


@app.post("/v1/rules", status_code=201)
def create_rule(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    current = RuleSet.load(settings.rules).rules
    RuleSet([*current, payload])  # validate before writing
    path = Path(settings.rules)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:
            raise HTTPException(status_code=500, detail={"code": "yaml_unavailable", "message": "Install symbologylink[rules] to write YAML rules."}) from exc
        path.write_text(yaml.safe_dump({"rules": [*current, payload]}, sort_keys=False), encoding="utf-8")
    else:
        path.write_text(json.dumps({"rules": [*current, payload]}, indent=2) + "\n", encoding="utf-8")
    return payload


@app.post("/v1/overrides", status_code=201)
def create_override(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return override_store.append(payload)


@app.get("/v1/cache")
def cache_stats() -> dict[str, Any]:
    return cache.stats()
