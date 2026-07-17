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

Result formats are selected by extension: `.json`, `.jsonl`, or `.parquet`. CSV exports are produced with the `export` command.

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
  "mapping": {
    "merchant_name": "entityName",
    "company_website": "domain",
    "symbol": "ticker",
    "event_date": "observationDate"
  }
}
```

Unknown source columns are retained in `metadata`.

## Customer security master

The customer provider accepts one row per entity or security. Multiple securities may share an `internal_entity_id` when their entity attributes agree.

Frequently used columns:

- Identity: `internal_entity_id`, `canonical_name`, `entity_type`, `aliases`
- Security: `internal_security_id`, `ticker`, `exchange`, `figi`, `isin`, `cusip`
- Entity reference: `cik`, `lei`, `domain`, `country`, `postal_code`
- Relationships: `parent_entity_id`, `parent_name`, `parent_entity_type`, `relationship_type`
- Validity: `entity_valid_from`, `entity_valid_to`, `security_valid_from`, `security_valid_to`, `relationship_valid_from`, `relationship_valid_to`
- Multiple periods: `entity_periods`, `security_periods`, `relationship_periods`

Aliases are pipe-delimited in CSV. Multiple validity intervals use native arrays in JSON or Parquet and JSON arrays in CSV cells.

Invalid periods, conflicting entity attributes, duplicate security identifiers, and missing canonical names fail validation before matching.

## Providers

| Provider | Coverage | Notes |
|---|---|---|
| Customer security master | Private entity, security, and relationship data | Preferred identifiers and relationships |
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

## Decisions and evidence

Entity decisions use four statuses:

- `matched`
- `review_required`
- `unmatched`
- `provider_error`

Security decisions are reported independently through `securityDecisionStatus`, `securityConfidence`, `matchedSecurity`, and `securityAlternatives`.

Every selected result includes scored evidence and provider provenance. Strong identifier conflicts, exact-name disagreements, close candidates, and invalid observation-date periods route records to review.

Conflict evidence types are:

- `identifier_conflict`
- `exact_name_identifier_conflict`
- `security_identifier_conflict`
- `conflict_probe_error`

## Relationships and point-in-time validity

Relationship graphs distinguish operational chains from GLEIF accounting-consolidation relationships. Results may include direct parent, ultimate parent, accounting direct parent, accounting ultimate parent, and issuer nodes.

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
| `SYMBOLOGYLINK_ENABLE_GLEIF` | Enable GLEIF |
| `SYMBOLOGYLINK_ENABLE_SEC` | Enable SEC |
| `SYMBOLOGYLINK_SEC_USER_AGENT` | SEC organization and contact |
| `SYMBOLOGYLINK_ENABLE_OPENFIGI` | Enable OpenFIGI |
| `SYMBOLOGYLINK_OPENFIGI_API_KEY` | Optional OpenFIGI API key |
| `SYMBOLOGYLINK_OFFLINE` | Disable provider network requests |
| `SYMBOLOGYLINK_API_KEY` | Optional `X-API-Key` authentication |

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

The benchmark is deterministic synthetic regression data. It does not represent production accuracy. Public accuracy claims require independently labeled records and a frozen holdout set.

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
