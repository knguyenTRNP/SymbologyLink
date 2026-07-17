from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .models import EntityMatchInput
from .normalize import normalize_country

CANONICAL_FIELDS = set(EntityMatchInput.__dataclass_fields__)


class IngestionError(ValueError):
    pass


@dataclass
class FileProfile:
    path: str
    file_type: str
    encoding: str
    delimiter: str | None
    columns: list[str]
    row_count: int
    sample_rows: list[dict[str, Any]]
    null_rates: dict[str, float]
    malformed_rows: int = 0
    warnings: list[str] = field(default_factory=list)


def _sniff_csv(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()[:65536]
    if not raw:
        raise IngestionError("The file is empty.")
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise IngestionError("Unsupported encoding. Use UTF-8 or Windows-1252.")
    try:
        delimiter = csv.Sniffer().sniff(text, delimiters=",\t;|").delimiter
    except csv.Error:
        delimiter = ","
    return encoding, delimiter


def read_records(path: str | Path) -> Iterator[dict[str, Any]]:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in {".csv", ".tsv"}:
        encoding, delimiter = _sniff_csv(path)
        with path.open(encoding=encoding, newline="") as handle:
            reader = csv.DictReader(handle, delimiter=delimiter)
            if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
                raise IngestionError("Headers are missing or duplicated.")
            yield from reader
    elif suffix in {".jsonl", ".ndjson"}:
        with path.open(encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, 1):
                if line.strip():
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise IngestionError(f"Invalid JSON on line {line_number}: {exc.msg}") from exc
                    if not isinstance(value, dict):
                        raise IngestionError(f"Line {line_number} must contain a JSON object.")
                    yield value
    elif suffix == ".json":
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise IngestionError(f"Invalid JSON: {exc.msg}") from exc
        rows = value if isinstance(value, list) else value.get("records", []) if isinstance(value, dict) else []
        if not isinstance(rows, list):
            raise IngestionError("JSON must be an array or an object with a records array.")
        for row in rows:
            if not isinstance(row, dict):
                raise IngestionError("Every JSON record must be an object.")
            yield row
    elif suffix in {".parquet", ".pq"}:
        try:
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise IngestionError("Parquet support requires: pip install 'symbologylink[parquet]'") from exc
        try:
            table = pq.read_table(path)
        except Exception as exc:
            raise IngestionError(f"Corrupted or unreadable Parquet file: {exc}") from exc
        for row in table.to_pylist():
            yield row
    else:
        raise IngestionError(f"Unsupported file type: {suffix}. Use CSV, TSV, JSON, JSON Lines, or Parquet.")


def profile_file(path: str | Path, sample_size: int = 5) -> FileProfile:
    path = Path(path)
    if path.stat().st_size > 100 * 1024 * 1024:
        raise IngestionError("File exceeds the configured 100 MB limit.")
    rows = list(read_records(path))
    if not rows:
        raise IngestionError("The file contains no records.")
    if len(rows) > 100_000:
        raise IngestionError("File exceeds the configured 100,000-row limit.")
    columns = list(rows[0])
    if len(columns) > 250:
        raise IngestionError("File exceeds the configured 250-column limit.")
    null_rates = {col: sum(row.get(col) in (None, "") for row in rows) / len(rows) for col in columns}
    suffix = path.suffix.lower()
    encoding, delimiter = _sniff_csv(path) if suffix in {".csv", ".tsv"} else ("utf-8", None)
    return FileProfile(str(path), suffix.lstrip("."), encoding, delimiter, columns, len(rows), rows[:sample_size], null_rates)


def map_row(row: dict[str, Any], mapping: dict[str, str], row_number: int, source: str) -> EntityMatchInput:
    canonical: dict[str, Any] = {}
    mapped_sources = set()
    for source_field, target_field in mapping.items():
        if target_field in {"", "ignore", "metadata"}:
            continue
        if target_field not in CANONICAL_FIELDS:
            raise IngestionError(f"Unknown canonical field: {target_field}")
        if target_field in canonical:
            raise IngestionError(f"Duplicate mapping to canonical field: {target_field}")
        canonical[target_field] = row.get(source_field) or None
        mapped_sources.add(source_field)
    original_id = canonical.get("recordId")
    canonical["recordId"] = str(original_id or hashlib.sha256(f"{source}:{row_number}".encode()).hexdigest()[:20])
    canonical["source"] = canonical.get("source") or source
    canonical["country"] = normalize_country(canonical.get("country"))
    canonical["metadata"] = {key: value for key, value in row.items() if key not in mapped_sources}
    return EntityMatchInput(**canonical)


def suggest_mapping(columns: list[str]) -> dict[str, str]:
    aliases = {
        "record_id": "recordId", "source_id": "recordId", "id": "recordId", "company": "entityName",
        "company_name": "entityName", "merchant_name": "entityName", "legal_name": "legalName",
        "brand": "brandName", "ticker_symbol": "ticker", "symbol": "ticker", "ticker": "ticker",
        "url": "domain", "website": "domain", "company_website": "domain", "domain": "domain",
        "event_date": "observationDate", "event_dt": "observationDate", "transaction_date": "observationDate", "observation_date": "observationDate",
        "country_code": "country", "country": "country", "cik": "cik", "lei": "lei",
        "figi": "figi", "isin": "isin", "cusip": "cusip", "exchange": "exchange",
    }
    return {col: aliases.get(col.casefold().strip().replace(" ", "_"), "metadata") for col in columns}
