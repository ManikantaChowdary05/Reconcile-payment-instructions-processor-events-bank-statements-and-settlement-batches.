# Reasoning, Decisions & Trade-offs

## Problem

Build an end-to-end **payment reconciliation** workflow: given payment
instructions, processor events, settlement batches/lines, and bank statement
lines, decide what matched, what didn't, why, and what to do about it —
with traceability, measurable quality, and reproducibility.

## Key assumptions

- **Synthetic data only** — no real/payment-card data; schema inspired by
  common processor concepts (Stripe/Adyen-style).
- **Matching keys**: `reference` (payment_id) + amount within **±0.01** +
  currency + value-date within **±5 days**. These are configurable
  (`--tolerance`, `--window-days`).
- **Fee model**: 2.9% + 0.30 flat, rounded per currency (JPY = 0 decimals).
- **Bank grain**: one bank payout per payment, covering N settlement lines
  (N:M) — this is what real processor payouts look like (a payout nets a
  capture plus its refunds).

## Key decisions

| Decision | Rationale | Trade-off |
|---|---|---|
| N:M subset-sum matching | Split refunds legitimately share one payout; 1:1 matching produced 21 false `AMOUNT_MISMATCH` exceptions | Slightly more compute; bounded because groups are tiny (≤4 lines) |
| Per-subset date window | A dropped refund line must not push the capture's payout out of window | Requires evaluating window inside the subset loop |
| Auto-resolve duplicates & fee variances | Deterministic, policy-backed resolutions demonstrate the resolution step without human bottlenecks | Only "safe" exception types are auto-closed; everything else stays OPEN |
| Ground truth never an input feature | Prevents leakage; detection is scored, not assumed | Requires disciplined labelling in the generator |
| Per-type P/R/F1 vs ground truth | "100% detection" on 6 anomalies is meaningless; per-type scoring exposes false positives | Only measures *injected* anomalies |
| Append-only resolutions | Audit trail; history never overwritten | Storage grows; no hard deletes by design |
| LLM triage is advisory | LLM suggests, rules decide; never overwrites resolutions | Lower autonomy, higher trust |

## Testing approach

- **`validate.py` — 33 integrity checks**: PK uniqueness, FK integrity, enum
  validity, date ordering, batch/line arithmetic, duplicate presence,
  N:M match integrity (non-grouped bank payouts used once, GROUPED may
  share), every exception has a resolution.
- **Reproducibility**: fixed seed → byte-identical CSVs; SHA-256 manifest.
- **Empirical verification**: full pipeline run on starter + full scales;
  inspected residuals (e.g. the 2 `UNMATCHED_BANK_LINE` false positives
  that revealed the per-subset window bug).

## How we know it works

- 33/33 checks PASS on both scales.
- Match rate ≈ 97.7%; **every** exception corresponds to an injected,
  labelled anomaly (155/155 at full scale) — zero unexplained exceptions.
- Per-type precision/recall/F1 = 1.0 on all 9 anomaly types.
- Deterministic: same seed → identical checksums.

## Limitations

- Synthetic distributions are tuned, not learned.
- Rule-based matching only; no ML.
- Subset-sum assumes small per-payment groups.
- Real-world messy data would lower precision; the harness measures
  *detection of known injections*, which is the honest claim.
