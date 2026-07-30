# Blind evaluation — results

## Protocol

- 38 transactions written from real card-feed conventions: acquirer prefixes (`SQ *`, `TST*`, `APL*`, `IN *`, `AMZ*`), 20–25 character truncation, store numbers, invoice tokens, support phone numbers, city/state tails.
- `merchant_url` populated on **4 of 38 rows (10.5%)**, matching what a real feed actually carries. The tuned dataset had it on 70% of rows.
- Includes 4 municipal/internal charges with no corporate counterparty, 4 small merchants absent from the master, and 2 lookalike traps (`DELTA DENTAL OF WA`, `APPLE ORCHARD BISTRO`).
- Restores the two rows deleted from the tuned dataset: `ADOBE INC ENTERPRISE` and `APPLE BANK FOR SAVINGS WIRE FEE`.
- `truth-blind.csv` was written and saved **before** the first resolve. Inputs were not edited afterward.
- One run. Customer security master only, offline, no rules, no overrides, default thresholds.

**Residual bias, stated plainly:** I had already read `providers.py` and `decisions.py` before writing this set. I did not consult the scoring or normalization logic while composing descriptors, and I did not revise any input after seeing results — but this is not a true blind test, and it isn't independent labeling. It is a harder, more honest test than the tuned set, not a clean one.

## Headline

| | Tuned set | Blind set |
|---|---|---|
| Resolvable vendors found | 28 / 30 (93%) | **7 / 28 (25%)** |
| Wrong answers | 0 | **0** |
| False positives on non-vendors | n/a | **0 / 10** |

Breakdown of the 28 rows that should have resolved:

- **1** auto-matched correctly (`SALESFORCE.COM`)
- **6** correct but routed to review
- **21** returned `unmatched`

All 10 rows that should not resolve returned `unmatched`, including both lookalike traps.

## The finding that matters

**Every one of the 7 successes came from an exact domain match or an exact alias already sitting in the master.** Not one came from fuzzy matching a realistic descriptor.

| Record | Descriptor | Why it worked |
|---|---|---|
| TXN-2003 | `GITHUB.COM` | `domain_match` |
| TXN-2010 | `SALESFORCE.COM` | `domain_match` |
| TXN-2015 | `ZOOM.US 888-799-9666` | `domain_match` — no `name_similarity` evidence at all |
| TXN-2023 | `APPLE.COM/BILL` | `domain_match` — no `name_similarity` evidence at all |
| TXN-2012 | `ADOBE *CREATIVE CLOUD` | alias `Adobe Creative Cloud` is in the master |
| TXN-2016 | `TWILIO SENDGRID` | alias `Twilio SendGrid` is in the master |
| TXN-2017 | `VMWARE INC` | alias `VMware Inc` is in the master |

Strip the domain column and pre-seeded aliases and the resolver finds essentially nothing. On the tuned set this was invisible, because I had supplied a clean domain on 21 of 30 rows and written descriptors that happened to sit close to alias strings.

### What defeats it

| Pattern | Example | Confidence |
|---|---|---|
| Acquirer prefix | `MSFT * E0800ABC12` | 0.25 |
| Acquirer prefix | `SQ *NORTHWIND CATERIN` | 0.00 |
| Truncation | `COURTYARD BY MARRIOT` (missing final T) | 0.59 |
| Truncation | `AMAZON WEB SERVICES AW` | 0.57 |
| Store number | `SBUX STORE 04417` | 0.00 |
| Store number | `WHOLEFDS SEA 10245` | 0.00 |
| Invoice token | `LINKEDIN-1234567890` | 0.25 |
| Trailing product word | `ADOBE INC ENTERPRISE` | 0.25 |

`ADOBE INC ENTERPRISE` at 0.25 confirms the earlier criticism concretely. When I shortened that row to `ADOBE INC` in the tuned set, I converted a total failure into a showcase of conflict detection.

`APPLE BANK FOR SAVINGS WIRE FEE` scores 0.54 — the same row scored 0.82 and matched once I removed `WIRE FEE`.

### What works well

Abstention is genuinely strong, and this is not an artifact of the data. Ten non-vendors, including two deliberate lookalikes, all correctly unmatched. `DELTA DENTAL OF WA` scored 0.25 against Delta Air Lines and `APPLE ORCHARD BISTRO` scored 0.25 against Apple Inc. Zero wrong answers across the entire run.

The engine is **precise and under-powered**, not sloppy. For a finance close that is the correct failure direction — but a 25% resolution rate means an analyst still hand-codes three quarters of the feed.

## Ranking is not the problem — deciding is

`benchmark evaluate` against the frozen truth file:

```
top_1_accuracy           0.7857
positive_resolution_rate 0.2500
false_rejection_rate     0.7500   (21 of 28)
negative_recall          1.0000
false_positive_rate      0.0000
review_queue_precision   1.0000
```

Read the first two together. **The correct entity was already ranked first for 78.6% of records, but only 25% survived the decision thresholds.** Candidate generation and ranking are working roughly three times better than the end-to-end numbers suggest. What's failing is the conversion of a correctly-ranked candidate into a decision.

That reframes the whole result. The fix is not primarily better matching — it's scoring and thresholds that are too harsh on evidence-poor records, plus the ingest normalization below.

## Threshold sensitivity — hypothesis, not result

Missed true vendors cluster at 0.52–0.59 while every true negative sits at or below 0.25. There is a real gap. Re-running with `--review-threshold 0.45`:

```
found 12/28 (up from 7), false positives 0/10 (unchanged)
```

Five more vendors recovered, no new errors. **Treat this as a hypothesis only.** I chose 0.45 after seeing this dataset's scores, which is the same overfitting error the tuned dataset made. It needs confirming on records I haven't looked at.

## What I would do next

0. **Start with the threshold and scoring, not the matcher.** `top_1_accuracy` of 0.786 against a resolution rate of 0.25 says the candidate is usually already there and correct. That is the cheapest available win.
1. **Normalize acquirer prefixes in ingest.** Stripping a known prefix table (`SQ *`, `TST*`, `PAYPAL *`, `APL*`, `AMZ*`, `IN *`, `MSFT *`, `GOOGLE *`) before matching is mechanical and would address roughly a third of the misses.
2. **Strip trailing numeric tokens** — store numbers, invoice ids, phone numbers.
3. **Handle truncation.** Prefix/edit-distance scoring against the master would catch `COURTYARD BY MARRIOT` and `AMAZON WEB SERVICES AW`. Both are unambiguous to a human.
4. **Reconsider what a review queue is for.** Twenty-one records went to `unmatched` at 0.00–0.59 when several were confidently identifiable. Records with any real evidence probably belong in review, not discarded.
5. **Get independently labeled data.** Neither of these datasets is that, and no accuracy claim should leave this repo until one exists.

## Reproducing

```powershell
symbologylink resolve `
  --input demo\expense-enrichment\blind\transactions-blind.csv `
  --mapping demo\expense-enrichment\blind\mapping-blind.json `
  --reference demo\expense-enrichment\security-master.csv `
  --providers customer_security_master `
  --offline `
  --output demo\expense-enrichment\blind\results-blind.jsonl `
  --overwrite
```

Score against frozen truth:

```powershell
symbologylink benchmark evaluate `
  --results demo\expense-enrichment\blind\results-blind.jsonl `
  --truth demo\expense-enrichment\blind\truth-blind.csv
```
