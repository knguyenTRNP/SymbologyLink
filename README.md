# Symbology Link

Symbology Link is a local-first entity and security resolution engine. It links uploaded company records to legal entities, issuers, parent organizations, and securities while preserving evidence, alternatives, provider provenance, and point-in-time validity.

## Features

- CSV, TSV, JSON, JSON Lines, and Parquet input
- Customer security masters in CSV or Parquet
- SEC, OpenFIGI, and GLEIF connectors
- Independent entity and security candidate ranking
- Brand, subsidiary, parent, and issuer relationship resolution
- CIK, LEI, FIGI, ISIN, CUSIP, ticker, name, domain, and address evidence
- Identifier-conflict detection with automatic review routing
- Observation-date validity for entities, securities, and relationships
- Deterministic rules and append-only human overrides
- SQLite caching and offline replay
- CLI, batch REST API, benchmark reports, and Docker deployment

## Status

Version `0.0.0` is an early public release. Interfaces and result schemas may change before the first stable release.

## Installation

Python 3.10 or newer is required.

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[all,test]"
.\.venv\Scripts\Activate.ps1
```

If PowerShell activation is unavailable, invoke the executable directly:

```powershell
.\.venv\Scripts\symbologylink.exe --help
```

### macOS and Linux

```console
python -m venv .venv
./.venv/bin/python -m pip install -e ".[all,test]"
source .venv/bin/activate
```

## Quick start

Inspect and validate the example dataset:

```console
symbologylink preview --input examples/records.csv
symbologylink validate \
  --input examples/records.csv \
  --mapping examples/mapping.json
```

Resolve it against the example security master:

```console
symbologylink resolve \
  --input examples/records.csv \
  --mapping examples/mapping.json \
  --reference examples/reference.csv \
  --providers customer_security_master \
  --output results.jsonl
```

Result formats are selected by extension: `.json`, `.jsonl`, or `.parquet`. CSV exports are produced with the `export` command. Parquet keeps scalar decision fields typed and stores nested evidence, candidates, relationships, validity, and source records as JSON strings for a stable mixed-result schema.

## Input model

All fields except `recordId` are optional. Missing record IDs are generated from the source filename and row number.

Common canonical fields:

| Category | Fields |
|---|---|
| Names | `entityName`, `legalName`, `brandName` |
| Entity identifiers | `cik`, `lei` |
| Security identifiers | `figi`, `isin`, `cusip`, `ticker`, `exchange` |
| Location | `addressLine1`, `city`, `state`, `postalCode`, `country` |
| Other | `domain`, `observationDate`, `source`, `metadata` |

Mappings are JSON documents:

```json
{
  "dateFormat": "%d-%m-%Y",
  "mapping": {
    "merchant_name": "entityName",
    "company_website": "domain",
    "symbol": "ticker",
    "event_date": "observationDate"
  }
}
```

`dateFormat` is optional and uses Python `strptime` syntax. It can also be supplied as `--date-format`. Unknown top-level mapping configuration keys are rejected so misspelled settings cannot fail open. Each result records `mappingContentSha256` alongside the human-readable mapping version.

Every original source row is retained in `sourceRecord`; unknown source columns are also retained in `sourceMetadata`. CSV export writes the original columns beside the enrichment fields. To prevent spreadsheet-formula injection, exported cells beginning with `=`, `+`, `-`, `@`, tab, carriage return, or line feed are prefixed with an apostrophe. JSON and Parquet results retain the original values.

JSON and JSON Lines inputs may be sparse: optional mapped fields can be absent from individual records as long as the field exists somewhere in the dataset. Schema-drift validation still rejects mappings whose source field is absent from the entire file.

CIKs are normalized to ten digits. Exchange names, market tiers, and MIC aliases are normalized to MICs—for example, `NASDAQ`, `NasdaqGS`, and `XNAS` become `XNAS`. Country aliases are normalized to valid ISO alpha-2 codes; unsupported country values become null and do not contribute evidence. Street suffixes, leading number words, cities, states, and postal codes are normalized before address comparison. Blank values and the placeholder tokens `N/A`, `NA`, `UNKNOWN`, `NULL`, `NONE`, and `-` do not contribute matching evidence.

## Customer security master

The customer provider accepts one row per entity or security. Multiple securities may share an `internal_entity_id` when their entity attributes agree.

Frequently used columns:

- Identity: `internal_entity_id`, `canonical_name`, `entity_type`, `aliases`
- Security: `internal_security_id`, `ticker`, `exchange`, `figi`, `isin`, `cusip`
- Entity reference: `cik`, `lei`, `domain`, `country`, `address_line1`, `city`, `state`, `postal_code`
- Relationships: `parent_entity_id`, `parent_name`, `parent_entity_type`, `relationship_type`
- Validity: `entity_valid_from`, `entity_valid_to`, `security_valid_from`, `security_valid_to`, `relationship_valid_from`, `relationship_valid_to`
- Multiple periods: `entity_periods`, `security_periods`, `relationship_periods`

Aliases are pipe-delimited in CSV. Multiple validity intervals use native arrays in JSON or Parquet and JSON arrays in CSV cells.

Invalid periods, conflicting entity attributes, duplicate security identifiers, and missing canonical names fail validation before matching.

## Providers

| Provider | Coverage | Notes |
|---|---|---|
| Customer security master | Private entity, security, and relationship data | Relationship values remain candidates until approved by a rule, override, or relationship master |
| SEC | EDGAR filers, CIKs, names, tickers, exchanges | Requires an organization and contact email in the user agent |
| OpenFIGI | FIGI, ISIN, CUSIP, ticker, and instrument metadata | API key optional; name search disabled by default |
| GLEIF | LEIs, legal names, addresses, and Level 2 relationships | Public API with cached relationship traversal |

Example provider configuration:

```console
symbologylink resolve \
  --input records.csv \
  --mapping mapping.json \
  --reference security-master.csv \
  --providers customer_security_master,openfigi,sec,gleif \
  --sec-user-agent "Example Organization contact@example.com" \
  --output results.jsonl
```

Use `--offline` to prohibit network requests and replay cached responses.

Provider responses are cached transactionally. If the SQLite cache is corrupt, Symbology Link quarantines it with a `.corrupt-<timestamp>` suffix and creates a clean cache. Cache failures on unsuitable network or synced filesystems return a structured error; use `--cache` to select a local writable path.

## Decisions and evidence

Entity decisions use four statuses:

- `matched`
- `review_required`
- `unmatched`
- `provider_error`

Security decisions are reported independently through `securityDecisionStatus`, `securityConfidence`, `matchedSecurity`, and `securityAlternatives`.

Every selected result includes scored evidence and provider provenance. Strong identifier conflicts, exact-name disagreements, close candidates, and invalid observation-date periods route records to review.

Decisions are made through pathway-specific policies rather than one global threshold. Each result and candidate records `primaryPathway`, `matchScore`, and `scoreIsCalibrated`. Scores are currently uncalibrated, so `scoreIsCalibrated` is `false`; the existing `confidence` field remains temporarily for compatibility and should not be interpreted as a statistical probability.

Default policies allow conflict-free exact identifiers and exact name plus domain to auto-match. Ticker plus exchange can auto-match only when dated evidence verifies that the listing is active; missing or unknown listing validity routes the security to review. Ticker-only, fuzzy-name-only, brand-inference, and relationship-traversal pathways cannot auto-match. Brand-origin records remain on the `brand_inference` pathway even when their normalized name and domain agree. Configure policies with JSON or YAML:

```console
symbologylink resolve \
  --input records.csv \
  --mapping mapping.json \
  --reference security-master.csv \
  --decision-policies examples/decision_policies.json \
  --output results.jsonl
```

The legacy `--auto-threshold` and `--review-threshold` options remain available during migration, but emit a deprecation warning and cannot be combined with `--decision-policies`.

`resolve` and `export` protect existing output files by default; pass `--overwrite` to replace one intentionally. Resolve results are written to a sibling `.partial` file and published under the requested output name only after the write completes. If a run is interrupted, the previous completed output is preserved and the retry is not blocked by a partial result. A review queue can be exported directly with `symbologylink export --input results.jsonl --output review.csv --status review_required`.

Conflict evidence types are:

- `identifier_conflict`
- `exact_name_identifier_conflict`
- `security_identifier_conflict`
- `conflict_probe_error`

## Relationships and point-in-time validity

Relationship graphs distinguish operational chains from GLEIF accounting-consolidation relationships. Results may include direct parent, ultimate parent, accounting direct parent, accounting ultimate parent, and issuer nodes.

Parent selection is candidate-first. `parentStatus` is one of `verified`, `candidate`, `ambiguous`, `unknown`, or `not_applicable`; `parentAlternatives` retains ranked competing chains and `parentEvidence` explains the decision. SEC, OpenFIGI, GLEIF, and customer security-master relationships are supporting evidence and cannot verify a parent by themselves. A parent is `verified` only when every edge in its selected chain comes from a rule, override, or `customer_relationship_master`. Provider-only and brand-derived parents route the record to review while preserving the matched operating entity.

Every relationship edge records `source`, `trustLevel`, and `selfReported`. GLEIF Level 2 edges are marked self-reported. Conflicting parents are retained rather than collapsed; conflicting authoritative sources set `parentStatus` to `ambiguous` and force review.

Entity, security, and relationship periods are evaluated independently against `observationDate`. Missing dates remain `not_verified`; current provider records are not assumed to be historically valid.

## Rules and overrides

Rules run before provider candidate generation. Higher priorities run first.

```console
symbologylink rules validate --file examples/rules.json
```

Overrides are append-only and take precedence over rules:

```console
symbologylink overrides add \
  --store .symbologylink/overrides.jsonl \
  --decision examples/override.json
```

## REST API

Start the API locally:

```console
symbologylink serve --host 127.0.0.1 --port 8000
```

OpenAPI documentation is available at `http://127.0.0.1:8000/docs`.

The API supports single matches, persistent batch jobs, multipart dataset uploads, previews, replacement, deletion, pagination, cancellation, rules, overrides, cache inspection, and provider health checks.

Important environment variables:

| Variable | Purpose |
|---|---|
| `SYMBOLOGYLINK_REFERENCE` | Customer security master |
| `SYMBOLOGYLINK_DECISION_POLICIES` | JSON or YAML pathway decision policy configuration |
| `SYMBOLOGYLINK_ENABLE_GLEIF` | Enable GLEIF |
| `SYMBOLOGYLINK_ENABLE_SEC` | Enable SEC |
| `SYMBOLOGYLINK_SEC_USER_AGENT` | SEC organization and contact |
| `SYMBOLOGYLINK_ENABLE_OPENFIGI` | Enable OpenFIGI |
| `SYMBOLOGYLINK_OPENFIGI_API_KEY` | Optional OpenFIGI API key |
| `SYMBOLOGYLINK_OFFLINE` | Disable provider network requests |
| `SYMBOLOGYLINK_API_KEY` | Optional `X-API-Key` authentication |
| `SYMBOLOGYLINK_RATE_LIMIT_PER_MINUTE` | Per-key request limit; default `30` |

## Docker

```console
mkdir data
cp examples/reference.csv data/reference.csv
docker compose up --build
```

The container runs as an unprivileged user and stores jobs, uploads, cache data, rules, and overrides under `/data`.

## Benchmarks

```console
symbologylink benchmark generate \
  --reference examples/reference.csv \
  --output-dir benchmark-data \
  --count 1000
```

The benchmark is deterministic synthetic regression data. Reports include metric numerators and denominators, Wilson confidence intervals, sample-size warnings, decision-pathway breakdowns, per-pathway median/P95 latency, confidence-calibration bins, abstention precision, negative recall, false-rejection rate, and representative false-rejection examples. It does not represent production accuracy. Public accuracy claims require independently labeled records and a frozen holdout set.

## Testing

```console
python -m unittest discover -s tests -v
```

## Operational boundaries

- SQLite is intended for local and small-team deployments.
- Background jobs run in a single application process.
- SEC coverage is limited to EDGAR filers.
- OpenFIGI resolves instruments but not corporate ownership.
- GLEIF relationships reflect reported consolidation and supported fund relationships, not complete commercial-control history.
- Customer data or rules are required for private entities, brands, and relationships not covered by public providers.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines and [SECURITY.md](SECURITY.md) for vulnerability reporting.

Licensed under the [MIT License](LICENSE).
