# Symbology Link

Symbology Link is a local-first entity and security resolution engine. It links uploaded company records to legal entities, issuers, parent organizations, and securities while preserving evidence, alternatives, provider provenance, and point-in-time awareness.

## Features

- CSV, TSV, JSON, JSON Lines, and Parquet input
- Customer security masters in CSV or Parquet
- Customer relationship masters with mapped CSV or Parquet columns
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

Result formats are selected by extension: `.json`, `.jsonl`, or `.parquet`. Schema `2.0` is emitted by default in every format and by the API. CSV exports are produced with the `export` command. Parquet keeps scalar decision fields typed and stores nested components, evidence, candidates, relationships, temporal results, and source records as JSON strings.

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

`dateFormat` is optional and uses Python `strptime` syntax. It can also be supplied as `--date-format`. Unknown top-level mapping configuration keys are rejected so misspelled settings cannot fail open. Each result records `mapping_content_sha256` alongside the human-readable mapping version. `mapping_fingerprint_sha256` additionally covers the mapping version and the complete provider capability/trust matrix, so a trust-policy change produces a different reproducibility fingerprint.

Every original source row is retained in `source_record`; unknown source columns are also retained in `source_metadata`. CSV export writes the original columns beside the enrichment fields. To prevent spreadsheet-formula injection, exported cells beginning with `=`, `+`, `-`, `@`, tab, carriage return, or line feed are prefixed with an apostrophe. JSON and Parquet results retain the original values.

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

Relationship columns embedded in a security master remain backward-compatible candidate evidence. Use the dedicated customer relationship master when a parent relationship should be eligible for verification.

### Customer-master blocking and memory

The customer security master is indexed once when it is loaded. Identifier, domain, exact-name, alias, and name-token lookups then generate candidates from small blocks instead of scanning every master row for every input record. Blocking does not change scoring or decision policy; it only limits which already-eligible rows reach the existing scorer. Candidate ordering remains deterministic, and non-first name tokens broaden recall when no exact identifier, domain, or name block succeeds.

Indexes keep references to the loaded candidate objects plus normalized string keys. This trades memory for predictable batch speed: a million-row master can require low hundreds of megabytes beyond the source data, depending on alias and identifier density. Run large masters in a process with sufficient available memory. Symbology Link fails with a clear provider error if the index cannot be allocated; disk-backed indexes are not part of v1.

## Customer relationship master

The dedicated `customer_relationship_master` provider is the trusted source for subsidiary, ownership, brand, division, operating, ultimate-parent, minority, and former-ownership relationships. Its canonical fields are:

- `relationship_id`
- `child_entity_id`
- `parent_entity_id`
- `relationship_type`
- `valid_from`, `valid_to`
- `ownership_percentage`
- `source`, `source_record_id`
- `trust_level`
- `is_direct`

Supported relationship types are `subsidiary_of`, `owned_by`, `brand_of`, `division_of`, `operated_by`, `ultimate_parent_of`, `minority_owned_by`, and `formerly_owned_by`.

Customer column names can be mapped without code changes:

```json
{
  "relationship_master": {
    "path": "data/relationships.parquet",
    "columns": {
      "relationship_id": "relationship_key",
      "child_entity_id": "subsidiary_id",
      "parent_entity_id": "owner_id",
      "relationship_type": "relation_type",
      "valid_from": "start_date",
      "valid_to": "end_date",
      "ownership_percentage": "ownership_pct"
    },
    "trust_level": "authoritative"
  }
}
```

Validate the entire file before resolution. Errors are aggregated with row numbers instead of stopping at the first bad row:

```console
symbologylink relationships validate \
  --input data/relationships.parquet \
  --mapping relationship-config.json \
  --entity-master security-master.csv
```

Validation detects missing and self-referential IDs, duplicate IDs and active records, parent cycles, overlapping mutually-exclusive parent periods, invalid date ranges, ownership outside 0–100%, and invalid minority-control claims. References absent from the configured entity master are returned as warnings and included in provider provenance.

Inspect an entity at a specific observation date:

```console
symbologylink relationships inspect \
  --input examples/relationships.csv \
  --entity-id entity:wholefoods \
  --date 2018-01-01
```

## Providers

| Provider | Trust | Declared capabilities |
|---|---|---|
| Customer security master | Authoritative for entity/security data | Derived from its columns; embedded relationships remain supporting candidates |
| Customer relationship master | Authoritative | Customer-approved relationships and relationship effective dates |
| SEC | Supporting | Entity lookup, CIK/ticker mapping, and current entity data |
| OpenFIGI | Supporting | Security lookup, identifier mapping, current security data, and share-class data; no historical dates |
| GLEIF | Supporting | Entity/LEI lookup, current self-reported relationships, and relationship effective dates |

Providers declare a `ProviderCapabilities` object and `trust_level`. Undeclared capabilities default to empty with `experimental` trust, which prevents a third-party connector from gaining verification authority merely by returning a field. Human overrides remain highest precedence, followed by rules, authoritative providers, supporting providers, and experimental providers.

Capability checks are enforced during decisioning. Temporal verification requires the matching `entity_effective_dates`, `security_effective_dates`, or `relationship_effective_dates` capability. Share-class fields require `share_class_data`. Parent verification requires the dedicated customer relationship master, a rule, or an override. Unsupported claims are preserved as `provider_capability_rejected` evidence but cannot verify the affected field or period. Supporting data can corroborate authoritative values but cannot overwrite them; conflicting authoritative sources produce a `contradicted` component and require review.

Example provider configuration:

```console
symbologylink resolve \
  --input records.csv \
  --mapping mapping.json \
  --reference security-master.csv \
  --providers customer_security_master,customer_relationship_master,openfigi,sec,gleif \
  --relationship-config relationship-config.json \
  --sec-user-agent "Example Organization contact@example.com" \
  --output results.jsonl
```

Use `--offline` to prohibit network requests and replay cached responses.

Inspect configured health plus the capability/trust matrix:

```console
symbologylink providers test --reference security-master.csv
```

Schema 2.0 results include `provider_metadata` as reproducibility provenance. The same metadata is exposed by `GET /v1/providers` and provider health responses.

Provider responses are cached transactionally. If the SQLite cache is corrupt, Symbology Link quarantines it with a `.corrupt-<timestamp>` suffix and creates a clean cache. Cache failures on unsuitable network or synced filesystems return a structured error; use `--cache` to select a local writable path.

## Component result schema 2.0

Every result contains independent `entity`, `public_parent`, and `security` components. Each component has its own `status`, canonical identity, `match_score`, `match_pathway`, evidence, and alternatives. Component statuses are `verified`, `candidate`, `ambiguous`, `unknown`, `contradicted`, or `not_applicable`.

`final_decision` summarizes the workflow outcome without erasing partial success:

- `entity_and_security_matched`
- `entity_matched_security_unknown`
- `entity_matched_parent_candidate`
- `private_entity`
- `review_required`
- `ambiguous`
- `unmatched`
- `temporal_verification_required`
- `provider_error`
- `license_blocked`

For example, an entity can be verified while its parent and security remain unknown. Security ambiguity does not downgrade the entity component, and provider-only parent evidence remains on the parent component.

Every component includes its own scored evidence and provider provenance. Strong identifier conflicts, exact-name disagreements, close security candidates, and invalid observation-date periods route the affected component to review. Security and parent evidence are not merged into entity evidence.

Decisions are made through pathway-specific policies rather than one global threshold. Components record `match_pathway`, `match_score`, and `score_is_calibrated`. Scores are currently uncalibrated, so `score_is_calibrated` is `false` and must not be interpreted as a statistical probability.

Default policies allow conflict-free exact identifiers and exact name plus domain to auto-match. Ticker plus exchange can auto-match only when dated evidence verifies that the listing is active; missing or unknown listing validity routes the security to review. Ticker-only, fuzzy-name-only, brand-inference, and relationship-traversal pathways cannot auto-match. Brand-origin records remain on the `brand_inference` pathway even when their normalized name and domain agree. Configure policies with JSON or YAML:

```console
symbologylink resolve \
  --input records.csv \
  --mapping mapping.json \
  --reference security-master.csv \
  --decision-policies examples/decision_policies.json \
  --output results.jsonl
```

The same policy file can configure temporal uncertainty:

```json
{
  "temporal": {
    "require_verified_for_auto_match": false,
    "unknown_behavior": "allow_with_warning"
  }
}
```

`unknown_behavior` accepts `allow_with_warning`, `review`, or `reject`. The default preserves automatic matches but adds explicit temporal-warning evidence when applicable evidence is inconclusive. `review` routes the record to `temporal_verification_required`; `reject` rejects the automatic resolution while preserving the independently resolved components and evidence.

Enable strict mode for one run without changing the configuration file:

```console
symbologylink resolve ... --require-temporal-verification --output results.jsonl
```

Strict mode requires every applicable temporal scope to be verified before auto-match. Missing observation dates remain `not_requested` and are never replaced with today’s date. Contradicted periods cannot auto-match in any mode.

The legacy `--auto-threshold` and `--review-threshold` options remain available during migration, but emit a deprecation warning and cannot be combined with `--decision-policies`.

To temporarily emit the pre-2.0 monolithic result shape, pass deprecated `--legacy-output`. Legacy output is never selected implicitly:

```console
symbologylink resolve ... --output legacy.jsonl --legacy-output
```

Migrate stored result files conservatively:

```console
symbologylink migrate results --input v1.jsonl --output v2.jsonl
```

Migration treats every legacy parent as a candidate. A legacy security becomes verified only when stored evidence contains an exact FIGI, ISIN, CUSIP, or ticker-plus-exchange match. Historical completed jobs in `jobs.sqlite3` are not rewritten.

Flat CSV export includes `record_id`, entity status/identity/score/pathway, parent status/identity/relationship source, security status/identity/listing fields, observation date, per-component temporal statuses and reasons, overall temporal status and reason, `final_decision`, `review_reason`, `mapping_version`, and `schema_version`, alongside the original source columns.

`resolve` and `export` protect existing output files by default; pass `--overwrite` to replace one intentionally. Resolve results are written to a sibling `.partial` file and published under the requested output name only after the write completes. If a run is interrupted, the previous completed output is preserved and the retry is not blocked by a partial result. A review queue can be exported directly with `symbologylink export --input results.jsonl --output review.csv --status review_required`.

Conflict evidence types are:

- `identifier_conflict`
- `exact_name_identifier_conflict`
- `security_identifier_conflict`
- `authoritative_provider_conflict`
- `authoritative_security_provider_conflict`
- `authoritative_parent_conflict`
- `provider_capability_rejected`
- `conflict_probe_error`

## Relationships and point-in-time awareness

Relationship graphs distinguish operational chains from GLEIF accounting-consolidation relationships. Results may include direct parent, ultimate parent, accounting direct parent, accounting ultimate parent, and issuer nodes.

Parent selection is candidate-first. `public_parent.status` records the outcome, `public_parent.alternatives` retains ranked competing chains, and `public_parent.evidence` explains the decision. The customer relationship master, rules, and overrides can verify a parent. Security-master relationships, GLEIF, and other supporting connectors can propose and corroborate parents but cannot verify them. Every selected edge must be authoritative for the parent to be `verified`. Brand-origin records remain review-only under the default decision policy even when an authoritative relationship verifies their parent.

Every relationship edge records `source`, `trustLevel`, `selfReported`, and whether effective dates are within provider capability. GLEIF Level 2 edges are marked self-reported. Conflicting parents are retained rather than collapsed; conflicting authoritative sources set `parentStatus` to `contradicted` and force review.

Entity, security, and relationship periods are evaluated independently against `observationDate`. Schema 2.0 reports `verified`, `unknown`, `contradicted`, `not_requested`, or `not_applicable` with a reason for every scope and for the overall result. Internally, missing dates remain `not_verified`; current provider records are not assumed to be historically valid. A provider cannot produce temporal `verified` status unless it declares the corresponding effective-date capability.

For API deployment, configure `SYMBOLOGYLINK_RELATIONSHIP_MASTER` plus optional `SYMBOLOGYLINK_RELATIONSHIP_MAPPING`, or set `SYMBOLOGYLINK_RELATIONSHIP_CONFIG` to a JSON/YAML configuration block. `SYMBOLOGYLINK_RELATIONSHIP_TRUST_LEVEL` defaults to `authoritative`.

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

The API supports single matches, persistent batch jobs, multipart dataset uploads, previews, replacement, deletion, pagination, cancellation, rules, overrides, cache inspection, and provider health checks. New single and batch results use schema `2.0`. Completed historical job payloads remain stored and returned unchanged.

Important environment variables:

| Variable | Purpose |
|---|---|
| `SYMBOLOGYLINK_REFERENCE` | Customer security master |
| `SYMBOLOGYLINK_DECISION_POLICIES` | JSON or YAML pathway and temporal policy configuration |
| `SYMBOLOGYLINK_REQUIRE_TEMPORAL_VERIFICATION` | Require verified applicable temporal scopes before auto-match |
| `SYMBOLOGYLINK_TEMPORAL_UNKNOWN_BEHAVIOR` | `allow_with_warning`, `review`, or `reject` |
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

Benchmark local provider candidate generation separately:

```console
python scripts/bench_provider_search.py \
  --master-rows 100000 \
  --records 10000 \
  --assert-targets
```

The script measures the complete indexed batch and a small legacy linear-scan sample, then reports the projected legacy duration, measured indexed duration, records per second, and estimated speedup. Loading and index construction are reported separately from candidate-generation time.

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
