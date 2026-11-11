# Data Dictionary — Payment Reconciliation Dataset

All IDs are immutable, zero-padded, and consistent across files. Timestamps are
UTC ISO-8601 (`YYYY-MM-DDTHH:MM:SSZ`); dates are `YYYY-MM-DD`.

## merchants.csv — Merchant (master)

| Field | Type | Req | Allowed / Rule |
|---|---|---|---|
| merchant_id | string | Y | `MER-000001`… unique PK |
| legal_name | string | Y | non-empty |
| country | string | Y | ISO-3166 alpha-2 |
| settlement_currency | string | Y | USD/EUR/GBP/JPY |
| status | enum | Y | ACTIVE, SUSPENDED |
| created_at | datetime | Y | 2024-01-01..2025-12-31 |

Cardinality: 1 merchant → N payment_orders, N settlement_lines (via payment).

## payment_orders.csv — PaymentOrder

| Field | Type | Req | Allowed / Rule |
|---|---|---|---|
| payment_id | string | Y | `PAY-2026-00000001`… unique PK |
| merchant_id | string | Y | FK → merchants |
| amount | decimal | Y | > 0; JPY integer, others 2dp |
| currency | string | Y | USD/EUR/GBP/JPY |
| status | enum | Y | CREATED, AUTHORIZED, CAPTURED, FAILED, EXPIRED, REFUNDED, PARTIALLY_REFUNDED — **derived** from last attempt |
| created_at | datetime | Y | 2026-01-01..2026-07-01 |

1 order → 1..N attempts (retry after failure). Status is derived, never random.

## payment_attempts.csv — PaymentAttempt

| Field | Type | Req | Allowed / Rule |
|---|---|---|---|
| attempt_id | string | Y | `ATT-<payment_id>-NN` unique PK |
| payment_id | string | Y | FK → payment_orders |
| merchant_id | string | Y | FK → merchants (denormalized) |
| processor | enum | Y | stripe, adyen, checkout, worldpay |
| amount | decimal | Y | equals parent order amount |
| currency | string | Y | equals parent order currency |
| status | enum | Y | PENDING, AUTHORIZED, CAPTURED, FAILED, EXPIRED |
| created_at | datetime | Y | ≥ order.created_at |

## processor_events.csv — ProcessorEvent (append-only event history)

| Field | Type | Req | Allowed / Rule |
|---|---|---|---|
| event_id | string | Y | `PE-2026-0000001`… unique PK |
| provider_event_id | string | Y | idempotency key; **duplicates allowed by design** |
| attempt_id | string | Y | FK → payment_attempts |
| payment_id | string | Y | denormalized for traceability |
| event_type | enum | Y | AUTHORIZED, CAPTURED, REFUNDED, FAILED, EXPIRED |
| amount | decimal | Y | event amount (REFUNDED may be partial) |
| currency | string | Y | ISO currency |
| occurred_at | datetime | Y | processor-side timestamp |
| ingested_at | datetime | Y | ≥ occurred_at |

Lifecycle rule: CAPTURED must precede REFUNDED (one injected violation exists —
see ground_truth.csv). Duplicate webhooks share `provider_event_id`.

## settlement_batches.csv — SettlementBatch

| Field | Type | Req | Allowed / Rule |
|---|---|---|---|
| batch_id | string | Y | `SET-2026-000001`… unique PK |
| currency | string | Y | batch currency |
| period_start / period_end | date | Y | min/max line value_date |
| value_date | date | Y | max line value_date |
| status | enum | Y | SETTLED |
| line_count | int | Y | **derived** = count of lines |
| gross_total / fee_total / net_total | decimal | Y | **derived** = Σ lines |

1 batch → N settlement_lines. Batches group lines by (currency, ISO week);
count allocated ∝ group size to hit the scale target.

## settlement_lines.csv — SettlementLine

| Field | Type | Req | Allowed / Rule |
|---|---|---|---|
| line_id | string | Y | `STL-<batch_id>-NNN` unique PK |
| batch_id | string | Y | FK → settlement_batches |
| payment_id | string | Y | FK → payment_orders |
| merchant_id | string | Y | FK → merchants (denormalized) |
| line_type | enum | Y | CAPTURE (positive), REFUND (negative) |
| gross_amount | decimal | Y | signed; REFUND < 0 |
| fee_amount | decimal | Y | CAPTURE: 2.9% + 0.30 rounded; REFUND: 0 |
| net_amount | decimal | Y | **derived** = gross − fee |
| currency | string | Y | ISO currency |
| value_date | date | Y | capture/refund date + 1..3d; 8% delayed +5..9d |

N lines → 1 payment (a payment may have 1 CAPTURE + N REFUND lines).

## bank_statement_lines.csv — BankStatementLine

| Field | Type | Req | Allowed / Rule |
|---|---|---|---|
| bank_id | string | Y | `BANK-2026-000001`… unique PK |
| account_id | string | Y | settlement account |
| reference | string | Y | = settlement line's payment_id |
| amount | decimal | Y | = Σ net_amount of covered settlement lines (sign preserved) |
| currency | string | Y | may mismatch (injected anomaly) |
| value_date / booking_date | date | Y | value ≥ booking |
| description | string | Y | `STTL <batch> PAYOUT lines=<n>` |

**Grain**: one bank payout per `reference` (payment_id); a payout covers N
settlement lines of that payment (N:M). Matching key: (reference, amount ±
tolerance vs line subset sum, currency, |Δvalue_date| ≤ 5d).

## Reconciliation outputs (src/reconcile.py)

- **reconciliation_matches.csv** — `REC-2026-000001…`; FKs to settlement_lines
  and bank_statement_lines; match_type EXACT|TOLERANCE|GROUPED (N:M);
  amount_variance = payout amount − matched subset sum.
- **exceptions.csv** — `EXC-2026-000001…`; types: DUPLICATE_SETTLEMENT_LINE,
  UNMATCHED_SETTLEMENT_LINE, UNMATCHED_BANK_LINE, FEE_VARIANCE,
  CURRENCY_MISMATCH, AMOUNT_MISMATCH, DATE_OUT_OF_WINDOW,
  LIFECYCLE_ORDER_VIOLATION; severity LOW/MEDIUM/HIGH; status OPEN/RESOLVED.
- **resolutions.csv** — `RES-2026-000001…`; append-only; action +
  resolved_by (rule-engine / analyst-pending / llm-assistant).
- **llm_recommendations.csv** — advisory LLM/heuristic suggestions (never
  overwrites resolutions).
- **reconciliation_report.json** — counts, match/exception rates,
  exceptions-by-type, ground-truth detection rate.

## ground_truth.csv — labelled anomalies (NEVER an input feature)

| anomaly_id | issue_type | expected_exception_type |
|---|---|---|
| DUPLICATE_SETTLEMENT_LINE ×25 | DUPLICATE_SETTLEMENT_LINE | DUPLICATE_SETTLEMENT_LINE |
| UNMATCHED_SILENTLY_DROPPED ×25 | UNMATCHED_SILENTLY_DROPPED | UNMATCHED_SETTLEMENT_LINE |
| REFUND_BEFORE_CAPTURE ×15 | REFUND_BEFORE_CAPTURE | LIFECYCLE_ORDER_VIOLATION |
| BANK_AMOUNT_DIFFERS_BY_FEE ×25 | BANK_AMOUNT_DIFFERS_BY_FEE | FEE_VARIANCE |
| CURRENCY_MISMATCH ×25 | CURRENCY_MISMATCH | CURRENCY_MISMATCH |
| BANK_AMOUNT_PARTIAL_MISMATCH ×20 | BANK_AMOUNT_PARTIAL_MISMATCH | AMOUNT_MISMATCH |
| BANK_DATE_OUT_OF_WINDOW ×20 | BANK_DATE_OUT_OF_WINDOW | DATE_OUT_OF_WINDOW |
| CURRENCY_ROUNDING ×5 | CURRENCY_ROUNDING | NONE_VALID_ROUNDING (no exception) |
| DUPLICATE_PROCESSOR_EVENT ×1 | DUPLICATE_PROCESSOR_EVENT | NONE_IDEMPOTENT_DEDUP |

Counts shown are full scale; starter uses ~1/10. `entity_id` is the
`payment_id` for per-record anomalies, enabling per-type P/R/F1 scoring.

## manifest.json

Seed, scale, row counts, SHA-256 per file, nomenclature, and matching
parameters (tolerance 0.01, window 5 days, fee model 2.9% + 0.30).
