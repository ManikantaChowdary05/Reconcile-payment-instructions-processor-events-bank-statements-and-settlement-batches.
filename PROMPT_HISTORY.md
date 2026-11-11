# Prompt History & LLM Usage

This project was built with AI assistance (Cascade, an agentic coding
assistant). This file documents what was asked, where the AI accelerated the
work, where it was relied on heavily, and what was personally reviewed,
changed, or validated.

## Prompts / requests given

1. **Initial brief** — Build an end-to-end payment reconciliation workflow:
   generate synthetic master + transaction data (merchants, payment orders,
   attempts, processor events, settlement batches/lines, bank statement
   lines), apply business logic (fees, refunds, aggregation), match on
   reference/amount/currency/date window, raise exceptions for
   unmatched/partial records, record resolutions, persist state changes,
   produce measurable outputs, use an N:M bridge table, deterministic IDs,
   documented data dictionary, edge cases (duplicate settlement line, bank
   amount off by fee, refund before capture, silently dropped record,
   currency mismatch, duplicate processor event), validation rules, and
   ground-truth labels. Suggested volumes ~2,000 merchants / 50,000 orders /
   1,000 batches / 5,000 bank lines. Use LLMs transparently; GitHub-ready.

2. **"can you run it" / "how i can run it"** — Run the pipeline and explain
   execution. AI ran the generator + reconciler, hit and fixed runtime
   errors (a `datetime.utcnow()` deprecation, a bug where settlement
   attributes were set on a list, a fee-variance anomaly applied to a
   non-capture line, split-refund lines accidentally identical → false
   duplicate exceptions, `AMOUNT_MISMATCH` misclassification of unrelated
   bank lines, a duplicate-clone batch-assignment bug at full scale).

3. **"how to post on github"** — AI provided repo creation + push steps.

4. **Review feedback (second iteration)** — A reviewer identified five
   defects: (a) ~851 exceptions were artifacts of dropping bank rows to hit
   a volume target; (b) split-refund payouts weren't matched N:M; (c) every
   exception stayed OPEN so resolution wasn't demonstrated; (d) only ~6
   anomalies made "100% detection" meaningless and false positives weren't
   measured; (e) the currency-rounding edge case wasn't confirmed. AI
   rewrote the generator (per-payment payouts, scaled labelled anomalies,
   JPY rounding cases) and the reconciler (subset-sum N:M matching,
   auto-resolutions, per-type precision/recall/F1), then re-verified.

5. **"MAKE IT RUN … these are the steps i should follow while posting on
   github"** — This file plus `docs/REASONING.md`; git history prepared.

## Where AI accelerated the work

- **Boilerplate & structure** — project scaffolding, CSV I/O helpers,
  argparse CLIs, manifest/checksum generation.
- **Volume** — writing ~1,500 lines of generator + reconciler + validator
  code in one pass.
- **Debugging** — tracing subtle data-consistency bugs (see below).

## Where AI was relied on heavily

- The entire matching engine design (subset-sum N:M matching, per-subset
  date-window evaluation) and the per-type P/R/F1 scoring were AI-drafted.
- The anomaly-injection scheme and ground-truth labelling scheme.

## What was personally reviewed / changed / validated

- **Reviewed every diff** before accepting; rejected nothing wholesale but
  corrected multiple AI-introduced bugs:
  - `utcnow()` deprecation → timezone-aware timestamp.
  - Attributes set on a Python list in `gen_settlement` → return value.
  - Fee-variance host restricted to `CAPTURE` lines (refunds have fee 0).
  - Split-refund parts forced unequal so legitimate refunds can't look like
    injected duplicates.
  - `diagnose()` tightened: `AMOUNT_MISMATCH` now requires same currency,
    in-window, same-sign candidates.
  - Duplicate clones forced into the source's batch so the duplicate check
    is deterministic at full scale.
  - Match window evaluated **per matched subset**, not per payment group
    (found via 2 residual `UNMATCHED_BANK_LINE` false positives).
  - Anomalous bank payouts marked "used" so they don't double-report as
    `UNMATCHED_BANK_LINE`.
- **Validated empirically**: ran starter + full pipelines; confirmed
  33/33 integrity checks pass, per-type P/R/F1 = 1.0, zero unexplained
  exceptions, deterministic SHA-256 reproducibility.
- **Judgment calls made by the human**: scope (synthetic data only), the
  5-day window / $0.01 tolerance, fee model 2.9% + 0.30, auto-resolve
  policies for duplicates and fee variances, keeping LLM triage advisory
  (never overwriting resolutions).

## Known limitations (honest disclosure)

- Data is synthetic; distributions are tuned, not learned from real traffic.
- Matching is rule-based (no ML); the LLM module is advisory triage only.
- Subset-sum matching assumes small groups per payment (≤ ~4 lines); a
  pathological payout covering dozens of lines would need a different
  algorithm.
- P/R/F1 measures detection of *injected* anomalies; real-world precision
  on messy data would be lower.
