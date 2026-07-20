from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

from .models import EntityMatchInput
from .normalize import normalize_country, normalize_null

CANONICAL_FIELDS = set(EntityMatchInput.__dataclass_fields__)
MATCHABLE_FIELDS = {
    "entityName", "legalName", "brandName", "domain", "ticker", "cik",
    "lei", "figi", "isin", "cusip",
}
MAX_FIELD_CHARACTERS = 32_768


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
    sha256: str = ""


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
            if not reader.fieldnames:
                raise IngestionError("CSV headers are missing.")
            positions: dict[str, list[int]] = {}
            for position, header in enumerate(reader.fieldnames, 1):
                positions.setdefault(header, []).append(position)
            duplicates = {header: found for header, found in positions.items() if len(found) > 1}
            if duplicates:
                detail = "; ".join(
                    f"{header!r} at positions {', '.join(map(str, found))}"
                    for header, found in duplicates.items()
                )
                raise IngestionError(f"Duplicate CSV header(s): {detail}.")
            empty_positions = [str(index) for index, header in enumerate(reader.fieldnames, 1) if not header.strip()]
            if empty_positions:
                raise IngestionError(f"Empty CSV header at position(s) {', '.join(empty_positions)}.")
            expected = len(reader.fieldnames)
            for row in reader:
                missing = [header for header in reader.fieldnames if row.get(header) is None]
                extras = row.get(None) or []
                if missing or extras:
                    actual = expected - len(missing) + len(extras)
                    problems = []
                    if missing:
                        problems.append(f"missing value(s) for {', '.join(repr(value) for value in missing)}")
                    if extras:
                        problems.append(f"{len(extras)} extra value(s)")
                    raise IngestionError(
                        f"Malformed CSV row at file line {reader.line_num}: expected {expected} fields, "
                        f"found {actual} ({'; '.join(problems)})."
                    )
                yield row
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
    for row_number, row in enumerate(rows, 1):
        for field_name, value in row.items():
            if isinstance(value, str) and len(value) > MAX_FIELD_CHARACTERS:
                raise IngestionError(
                    f"Field {field_name!r} at data row {row_number} exceeds the "
                    f"{MAX_FIELD_CHARACTERS:,}-character limit."
                )
    # JSON and JSON Lines records may legitimately be sparse. Build a stable,
    # first-seen union so optional fields need not appear in the first record.
    columns = list(dict.fromkeys(key for row in rows for key in row))
    if len(columns) > 250:
        raise IngestionError("File exceeds the configured 250-column limit.")
    null_rates = {col: sum(row.get(col) in (None, "") for row in rows) / len(rows) for col in columns}
    suffix = path.suffix.lower()
    encoding, delimiter = _sniff_csv(path) if suffix in {".csv", ".tsv"} else ("utf-8", None)
    return FileProfile(
        str(path), suffix.lstrip("."), encoding, delimiter, columns, len(rows),
        rows[:sample_size], null_rates, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


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
        canonical[target_field] = normalize_null(row.get(source_field)) or None
        mapped_sources.add(source_field)
    original_id = canonical.get("recordId")
    canonical["recordId"] = str(original_id or hashlib.sha256(f"{source}:{row_number}".encode()).hexdigest()[:20])
    canonical["source"] = canonical.get("source") or source
    canonical["country"] = normalize_country(canonical.get("country"))
    canonical["metadata"] = {key: value for key, value in row.items() if key not in mapped_sources}
    canonical["sourceRecord"] = {key: value for key, value in row.items() if key is not None}
    return EntityMatchInput(**canonical)


def validate_mapping(mapping: dict[str, str], columns: list[str]) -> None:
    if not isinstance(mapping, dict) or not mapping:
        raise IngestionError("Mapping must be a non-empty object of source columns to canonical fields.")
    missing_sources = [
        str(source_field) for source_field, target_field in mapping.items()
        if target_field not in {"", "ignore"} and source_field not in columns
    ]
    if missing_sources:
        raise IngestionError(
            "Mapping references missing source column(s): " + ", ".join(repr(value) for value in missing_sources)
            + ". Update the mapping or restore the expected input columns."
        )
    mapped_targets: dict[str, str] = {}
    for source_field, target_field in mapping.items():
        if not isinstance(target_field, str):
            raise IngestionError(f"Mapping target for {source_field!r} must be a string.")
        if target_field in {"", "ignore", "metadata"}:
            continue
        if target_field not in CANONICAL_FIELDS:
            raise IngestionError(f"Unknown canonical field: {target_field}")
        if target_field in mapped_targets:
            raise IngestionError(
                f"Duplicate mapping to canonical field: {target_field} "
                f"from {mapped_targets[target_field]!r} and {source_field!r}."
            )
        mapped_targets[target_field] = str(source_field)
    if not set(mapped_targets) & MATCHABLE_FIELDS:
        fields = ", ".join(sorted(MATCHABLE_FIELDS))
        raise IngestionError(f"Mapping has no useful entity or security fields. Map at least one of: {fields}.")


def _normalize_observation_date(value: Any, row_number: int, date_format: str | None = None) -> str | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if date_format:
            return datetime.strptime(str(value), date_format).date().isoformat()
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError) as exc:
        expected = date_format or "YYYY-MM-DD"
        raise IngestionError(
            f"Invalid observation date {value!r} at data row {row_number}; expected format {expected}."
        ) from exc


def prepare_records(
    path: str | Path,
    mapping: dict[str, str],
    profile: FileProfile | None = None,
    date_format: str | None = None,
) -> tuple[FileProfile, list[EntityMatchInput]]:
    """Validate a complete dataset and return canonical records for processing."""
    path = Path(path)
    profile = profile or profile_file(path)
    validate_mapping(mapping, profile.columns)
    records: list[EntityMatchInput] = []
    first_position: dict[str, int] = {}
    for row_number, row in enumerate(read_records(path), 1):
        record = map_row(row, mapping, row_number, path.name)
        record.observationDate = _normalize_observation_date(record.observationDate, row_number, date_format)
        previous = first_position.get(record.recordId)
        if previous is not None:
            raise IngestionError(
                f"Duplicate record identifier {record.recordId!r} at data rows {previous} and {row_number}."
            )
        first_position[record.recordId] = row_number
        records.append(record)
    return profile, records


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
