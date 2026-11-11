# YottaFlex — Payment Reconciliation Platform

End-to-end reconciliation of **payment instructions → processor events →
settlement batches → bank statements**, with explicit exceptions, an
append-only audit trail, LLM-assisted triage, and measurable outputs.

Fully synthetic, deterministic (fixed seed), stdlib-only core — ready to run,
validate, and submit.

## Pipeline

```
generate_data.py  →  reconcile.py  →  llm_assistant.py (optional)  →  validate.py
   master data         matching          LLM triage                    integrity
   + edge cases        + exceptions      (advisory)                   checks
   + ground_truth      + resolutions
```

## Quickstart

```powershell
# 1. Generate data (starter ~seconds, full ~1 min)
python src/generate_data.py --scale starter --out data/starter --seed 20261022
python src/generate_data.py --scale full   --out data/full   --seed 20261022

# 2. Reconcile settlement lines vs bank statement
python src/reconcile.py --data data/full --out output/full

# 3. LLM triage of open exceptions (optional; works offline via fallback)
python src/llm_assistant.py --recon output/full --summary

# 4. Validate everything
python src/validate.py --data data/full --recon output/full
```

Optional LLM config: set `OPENAI_API_KEY` (and optionally `OPENAI_BASE_URL`,
`OPENAI_MODEL`). Without it, a deterministic heuristic fallback is used.

## Volumes (full scale, seed 20261022)

| Entity | Rows |
|---|---|
| merchants | 2,000 |
| payment_orders | 50,000 |
| payment_attempts | ~55,600 |
| processor_events | ~102,600 (incl. 25 duplicate webhooks) |
| settlement_batches | 1,000 |
| settlement_lines | ~5,720 (captures + full/partial/split refunds + fees) |
| bank_statement_lines | ~5,008 (one payout per payment; N:M vs settlement lines) |

`--scale starter` runs the same generator at ~1/10 volume for fast iteration.

## Business rules implemented

- **Derived, not random**: order status from last attempt; batch totals from
  lines; `net = gross − fee`; fee = 2.9% + 0.30 per currency rounding.
- **Matching**: reference + amount (±0.01) + currency + value-date window
  (±5d), with **N:M grouped payouts** — one bank line can settle several
  settlement lines of a payment (capture + split refunds) via subset-sum
  matching (`match_type=GROUPED`).
- **Every unmatched/partial record → exception**; every exception → resolution
  (append-only; history never overwritten). Duplicates and fee variances are
  **auto-resolved** by rule; the rest stay OPEN for the analyst/LLM.
- **Idempotency**: duplicate processor events deduped by `provider_event_id`.
- **Scaled anomalies**: dozens of each injected type (25 duplicates, 25 fee
  variances, 25 currency mismatches, 25 dropped lines, 20 amount mismatches,
  20 date-window, 15 lifecycle, 40 duplicate webhooks, 5 JPY rounding cases)
  — all labelled in `ground_truth.csv`, never leaked into features.
- **Scored detection**: `reconciliation_report.json` reports per-type
  precision / recall / F1 against ground truth.

## Outputs & metrics (`reconciliation_report.json`)

- match_rate, exception_rate, exceptions-by-type, open/auto-resolved counts,
  lifecycle violations, **per-type precision/recall/F1** vs ground truth
  (all 1.0 on both scales), grouped-match count.

## Reproducibility

Same `--scale` + `--seed` → byte-identical CSVs (SHA-256 in `manifest.json`).
Verify:

```powershell
python src/generate_data.py --scale starter --out data/check --seed 20261022
python -c "import json;a=json.load(open('data/starter/manifest.json'));b=json.load(open('data/check/manifest.json'));print(a['sha256']==b['sha256'])"
```

## Actors

- **Operations analyst** — works the exception queue (`exceptions.csv`).
- **Risk/compliance reviewer** — receives HIGH-severity escalations
  (currency mismatch, amount mismatch, lifecycle violations).
- **System administrator** — runs the pipeline; LLM module is config-driven.
- **Customer/account holder** — represented by payment orders/attempts.

## Project layout

```
src/generate_data.py   deterministic synthetic data generator
src/reconcile.py       matching engine → matches/exceptions/resolutions/report
src/llm_assistant.py   LLM exception triage (OpenAI-compatible + fallback)
src/validate.py        33 integrity checks → validation_summary.json
data_dictionary.md     full field-level spec
data/, output/         generated artifacts (git-ignored; regenerate locally)
```

## Validation status

`validate.py` performs 33 checks: PK uniqueness, FK integrity, enum validity,
date ordering, batch/line arithmetic, duplicate checks, N:M match integrity,
and reconciliation referential integrity — **33/33 PASS** on starter and full.

## Data provenance

100% synthetic, generated in-repo by `src/generate_data.py`. No external or
personal data is used; schema is inspired by common payment-processor
concepts (Stripe/Adyen-style events and payouts). Seed `20261022` is fixed for
reproducibility; all randomness flows through `random.Random(seed)`.
