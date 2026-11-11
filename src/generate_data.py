#!/usr/bin/env python3
"""
Deterministic synthetic data generator for the payment-reconciliation dataset.

Entities produced (CSV):
    merchants, payment_orders, payment_attempts, processor_events,
    settlement_batches, settlement_lines, bank_statement_lines,
    ground_truth (labelled anomalies — NEVER used as input features).

Reconciliation outputs (matches / exceptions / resolutions) are produced by
src/reconcile.py, not here.

Usage:
    python src/generate_data.py --scale starter --out data/starter
    python src/generate_data.py --scale full   --out data/full --seed 20261022

Reproducibility: same --scale and --seed always produce byte-identical CSVs.
A manifest.json with SHA-256 checksums and row counts is written alongside.
"""

import argparse
import csv
import hashlib
import json
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

SEED_DEFAULT = 20261022
PERIOD_START = datetime(2026, 1, 1)
PERIOD_END = datetime(2026, 7, 1)  # orders created in [START, END)

# anomaly counts per scale — dozens of each injected type so per-type
# precision/recall/F1 is meaningful
SCALES = {
    "starter": {
        "merchants": 200, "orders": 5_000, "batches": 100,
        "anomalies": {"dup_lines": 3, "dropped": 3, "lifecycle": 2,
                      "fee_var": 3, "ccy": 3, "amount_mm": 2, "date_win": 2,
                      "dup_events": 5, "rounding": 2},
    },
    "full": {
        "merchants": 2_000, "orders": 50_000, "batches": 1_000,
        "anomalies": {"dup_lines": 25, "dropped": 25, "lifecycle": 15,
                      "fee_var": 25, "ccy": 25, "amount_mm": 20, "date_win": 20,
                      "dup_events": 40, "rounding": 5},
    },
}

CURRENCIES = ["USD", "EUR", "GBP", "JPY"]
CURRENCY_WEIGHTS = [0.45, 0.25, 0.15, 0.15]
MERCHANT_CCY_WEIGHTS = [0.55, 0.20, 0.15, 0.10]

COUNTRIES = [
    ("US", 0.30), ("GB", 0.15), ("DE", 0.12), ("FR", 0.10), ("NL", 0.07),
    ("ES", 0.06), ("IT", 0.05), ("JP", 0.05), ("CA", 0.04), ("AU", 0.03),
    ("BR", 0.02), ("IN", 0.01),
]

NAME_PREFIX = ["North", "Blue", "Vertex", "Atlas", "Prime", "Nova", "Orbit",
               "Silver", "Redwood", "Quantum", "Harbor", "Pioneer", "Lumen", "Cobalt"]
NAME_SUFFIX = ["Pay", "Commerce", "Markets", "Goods", "Retail", "Trading",
               "Store", "Ventures", "Supply", "Exchange", "Hub", "Works"]

# Order outcome weights. Capture rate tuned so settlement lines ~5-6k and
# bank payouts (one per payment with settled lines) ~5k at full scale.
OUTCOME_WEIGHTS = [("captured", 0.065), ("authorized", 0.10),
                   ("failed", 0.23), ("expired", 0.605)]
RETRY_PROB = 0.50
RETRY_CAPTURE_PROB = 0.32
REFUND_PROB = 0.10          # of captured orders
REFUND_SPLIT_PROB = 0.30    # of refunds -> two partial REFUNDED events/lines
DELAYED_SETTLE_PROB = 0.08  # settlement value_date pushed +5..9 days

MATCH_WINDOW_DAYS = 5       # enforced by reconcile.py
TOLERANCE = 0.01


def money(value, currency):
    """Round to currency minor units (JPY has 0 decimals — currency rounding)."""
    decimals = {"JPY": "1"}.get(currency, "0.01")
    return float(Decimal(str(value)).quantize(Decimal(decimals), rounding=ROUND_HALF_UP))


def fee_for(amount, currency):
    """Processor fee: 2.9% + 0.30 flat, rounded per currency."""
    return money(Decimal(str(amount)) * Decimal("0.029") + Decimal("0.30"), currency)


def iso_week(d):
    y, w, _ = d.isocalendar()
    return y, w


def weighted_choice(rng, items_weights):
    total = sum(w for _, w in items_weights)
    r = rng.random() * total
    acc = 0.0
    for item, w in items_weights:
        acc += w
        if r <= acc:
            return item
    return items_weights[-1][0]


def ts(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def write_csv(path, fieldnames, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# Entity generators
# --------------------------------------------------------------------------

def gen_merchants(rng, n):
    rows = []
    for i in range(1, n + 1):
        name = f"{rng.choice(NAME_PREFIX)} {rng.choice(NAME_SUFFIX)}"
        country = weighted_choice(rng, COUNTRIES)
        ccy = weighted_choice(rng, list(zip(CURRENCIES, MERCHANT_CCY_WEIGHTS)))
        status = "SUSPENDED" if rng.random() < 0.03 else "ACTIVE"
        created = datetime(2024, 1, 1) + timedelta(days=rng.randrange(0, 730))
        rows.append({
            "merchant_id": f"MER-{i:06d}",
            "legal_name": f"{name} Ltd.",
            "country": country,
            "settlement_currency": ccy,
            "status": status,
            "created_at": ts(created),
        })
    return rows


def gen_orders_attempts_events(rng, merchants, n_orders, n_dup_events):
    """Returns (orders, attempts, events, captures, refunds)."""
    orders, attempts, events = [], [], []
    captures = []
    ev_seq = 0

    def next_event(**kw):
        nonlocal ev_seq
        ev_seq += 1
        kw["event_id"] = f"PE-2026-{ev_seq:07d}"
        kw.setdefault("ingested_at",
                      ts(kw["occurred_at"] + timedelta(seconds=rng.randrange(1, 30))))
        events.append(kw)
        return kw["event_id"]

    for i in range(1, n_orders + 1):
        pid = f"PAY-2026-{i:08d}"
        m = merchants[rng.randrange(len(merchants))]
        currency = m["settlement_currency"] if rng.random() < 0.90 else rng.choice(CURRENCIES)
        if currency == "JPY":
            amount = float(rng.randrange(500, 500_000))
        else:
            amount = round(rng.uniform(5.0, 2000.0), 2)
        created = PERIOD_START + timedelta(
            seconds=rng.randrange(0, int((PERIOD_END - PERIOD_START).total_seconds())))

        outcome = weighted_choice(rng, OUTCOME_WEIGHTS)
        order_attempts = []

        att_id = f"ATT-{pid}-01"
        att_created = created + timedelta(minutes=rng.randrange(1, 2880))
        fail_at_auth = outcome == "failed" and rng.random() < 0.40
        a_outcome = ("failed_auth" if fail_at_auth
                     else outcome if outcome != "failed" else "failed")

        order_attempts.append({
            "attempt_id": att_id, "payment_id": pid, "merchant_id": m["merchant_id"],
            "processor": rng.choice(["stripe", "adyen", "checkout", "worldpay"]),
            "amount": amount, "currency": currency,
            "status": {"captured": "CAPTURED", "authorized": "AUTHORIZED",
                       "failed": "FAILED", "failed_auth": "FAILED",
                       "expired": "EXPIRED"}[a_outcome],
            "created_at": ts(att_created),
        })

        pev = f"PEVD-2026-{i:08d}-A1"
        if a_outcome == "failed_auth":
            next_event(provider_event_id=pev, attempt_id=att_id, payment_id=pid,
                       event_type="FAILED", amount=amount, currency=currency,
                       occurred_at=att_created + timedelta(seconds=rng.randrange(2, 20)))
        else:
            auth_t = att_created + timedelta(seconds=rng.randrange(2, 30))
            next_event(provider_event_id=f"{pev}-AUTH", attempt_id=att_id, payment_id=pid,
                       event_type="AUTHORIZED", amount=amount, currency=currency,
                       occurred_at=auth_t)
            if a_outcome == "captured":
                cap_t = auth_t + timedelta(minutes=rng.randrange(5, 4320))
                next_event(provider_event_id=f"{pev}-CAP", attempt_id=att_id, payment_id=pid,
                           event_type="CAPTURED", amount=amount, currency=currency,
                           occurred_at=cap_t)
            elif a_outcome == "failed":
                next_event(provider_event_id=f"{pev}-FAIL", attempt_id=att_id, payment_id=pid,
                           event_type="FAILED", amount=amount, currency=currency,
                           occurred_at=auth_t + timedelta(minutes=rng.randrange(5, 720)))
            elif a_outcome == "expired":
                next_event(provider_event_id=f"{pev}-EXP", attempt_id=att_id, payment_id=pid,
                           event_type="EXPIRED", amount=amount, currency=currency,
                           occurred_at=auth_t + timedelta(days=rng.randrange(5, 15)))

        # --- attempt 2 (retry after failure) ---
        final_status = order_attempts[-1]["status"]
        if outcome == "failed" and rng.random() < RETRY_PROB:
            att2_id = f"ATT-{pid}-02"
            att2_created = att_created + timedelta(hours=rng.randrange(2, 72))
            a2 = "captured" if rng.random() < RETRY_CAPTURE_PROB else "failed"
            order_attempts.append({
                "attempt_id": att2_id, "payment_id": pid, "merchant_id": m["merchant_id"],
                "processor": rng.choice(["stripe", "adyen", "checkout", "worldpay"]),
                "amount": amount, "currency": currency,
                "status": "CAPTURED" if a2 == "captured" else "FAILED",
                "created_at": ts(att2_created),
            })
            pev2 = f"PEVD-2026-{i:08d}-A2"
            auth_t = att2_created + timedelta(seconds=rng.randrange(2, 30))
            next_event(provider_event_id=f"{pev2}-AUTH", attempt_id=att2_id, payment_id=pid,
                       event_type="AUTHORIZED", amount=amount, currency=currency,
                       occurred_at=auth_t)
            if a2 == "captured":
                cap_t = auth_t + timedelta(minutes=rng.randrange(5, 4320))
                next_event(provider_event_id=f"{pev2}-CAP", attempt_id=att2_id, payment_id=pid,
                           event_type="CAPTURED", amount=amount, currency=currency,
                           occurred_at=cap_t)
            else:
                next_event(provider_event_id=f"{pev2}-FAIL", attempt_id=att2_id, payment_id=pid,
                           event_type="FAILED", amount=amount, currency=currency,
                           occurred_at=auth_t + timedelta(minutes=rng.randrange(5, 720)))
            final_status = order_attempts[-1]["status"]

        orders.append({
            "payment_id": pid, "merchant_id": m["merchant_id"],
            "amount": amount, "currency": currency,
            "status": final_status, "created_at": ts(created),
        })
        attempts.extend(order_attempts)

        if final_status == "CAPTURED":
            cap_att = order_attempts[-1]
            cap_event = next((e for e in reversed(events)
                              if e["attempt_id"] == cap_att["attempt_id"]
                              and e["event_type"] == "CAPTURED"), None)
            cap_time = cap_event["occurred_at"] if cap_event else att_created
            captures.append({
                "payment_id": pid, "merchant_id": m["merchant_id"],
                "amount": amount, "currency": currency,
                "captured_at": cap_time, "attempt_id": cap_att["attempt_id"],
            })

    # --- refunds on a subset of captured orders ---
    refunds = []
    for c in captures:
        if rng.random() < REFUND_PROB:
            if rng.random() < 0.55:
                r_amt = c["amount"]
            else:
                r_amt = money(c["amount"] * rng.uniform(0.2, 0.8), c["currency"])
            r_time = c["captured_at"] + timedelta(days=rng.randrange(1, 20))
            if rng.random() < REFUND_SPLIT_PROB and r_amt > 2:
                # unequal parts so two REFUND lines are never coincidentally identical
                p1 = money(r_amt * 0.4, c["currency"])
                p2 = money(r_amt - p1, c["currency"])
                if p1 == p2:
                    p1 = money(p1 - 0.01 if c["currency"] != "JPY" else p1 - 1,
                               c["currency"])
                    p2 = money(r_amt - p1, c["currency"])
                parts = [p1, p2]
            else:
                parts = [r_amt]
            refunds.append({"payment_id": c["payment_id"], "amount": r_amt,
                            "refunded_at": r_time, "parts": parts,
                            "currency": c["currency"], "attempt_id": c["attempt_id"]})
            for k, p in enumerate(parts):
                pev = f"PEVD-R{len(refunds):07d}-{k}"
                next_event(provider_event_id=pev,
                           attempt_id=c["attempt_id"], payment_id=c["payment_id"],
                           event_type="REFUNDED", amount=p, currency=c["currency"],
                           occurred_at=r_time + timedelta(hours=k * rng.randrange(1, 48)))

    refunded_pids = {r["payment_id"] for r in refunds}
    for o in orders:
        if o["payment_id"] in refunded_pids:
            o["status"] = "REFUNDED" if any(
                r["payment_id"] == o["payment_id"] and r["amount"] == o["amount"]
                for r in refunds) else "PARTIALLY_REFUNDED"

    # --- duplicate webhook injection (idempotency noise) ---
    cap_events = [e for e in events if e["event_type"] == "CAPTURED"]
    for e in rng.sample(cap_events, min(n_dup_events, len(cap_events))):
        dup = dict(e)
        dup["event_id"] = f"PE-2026-{ev_seq + 1:07d}"
        ev_seq += 1
        dup["occurred_at"] = e["occurred_at"] + timedelta(seconds=rng.randrange(1, 5))
        dup["ingested_at"] = dup["occurred_at"] + timedelta(seconds=rng.randrange(1, 10))
        events.append(dup)  # same provider_event_id -> must dedupe downstream

    return orders, attempts, events, captures, refunds


def gen_settlement(rng, captures, refunds, target_batches, an):
    """Build settlement lines + batches; inject line-level anomalies.

    Returns (batches, lines, gt, skip_indices, swap_pids).
    skip_indices: line indices excluded from bank payout sums
    (duplicate clones + deliberately dropped lines).
    """
    gt = []
    lines = []
    swap_pids = []

    refund_by_pid = {}
    for r in refunds:
        refund_by_pid.setdefault(r["payment_id"], []).append(r)

    for c in captures:
        cap_vdate = c["captured_at"].date() + timedelta(days=1 + rng.randrange(0, 3))
        if rng.random() < DELAYED_SETTLE_PROB:
            cap_vdate += timedelta(days=rng.randrange(5, 10))  # delayed settlement
        gross = c["amount"]
        fee = fee_for(gross, c["currency"])
        net = money(gross - fee, c["currency"])
        lines.append({
            "payment_id": c["payment_id"], "merchant_id": c["merchant_id"],
            "line_type": "CAPTURE", "gross_amount": gross, "fee_amount": fee,
            "net_amount": net, "currency": c["currency"], "value_date": cap_vdate,
        })
        for r in refund_by_pid.get(c["payment_id"], []):
            r_vdate = r["refunded_at"].date() + timedelta(days=1 + rng.randrange(0, 3))
            for p in r["parts"]:
                lines.append({
                    "payment_id": c["payment_id"], "merchant_id": c["merchant_id"],
                    "line_type": "REFUND", "gross_amount": -p, "fee_amount": 0.0,
                    "net_amount": -p, "currency": c["currency"], "value_date": r_vdate,
                })

    skip_indices = set()
    clone_pairs = []  # (clone_idx, src_idx)

    # ---- anomaly: duplicate settlement lines ----
    capture_idx = [i for i, l in enumerate(lines) if l["line_type"] == "CAPTURE"]
    for src_idx in rng.sample(capture_idx, min(an["dup_lines"], len(capture_idx))):
        src = lines[src_idx]
        clone_pairs.append((len(lines), src_idx))
        lines.append(dict(src))
        skip_indices.add(len(lines) - 1)
        gt.append({
            "entity": "settlement_lines", "entity_id": src["payment_id"],
            "issue_type": "DUPLICATE_SETTLEMENT_LINE",
            "expected_exception_type": "DUPLICATE_SETTLEMENT_LINE",
            "expected_resolution": "VOID_DUPLICATE_LINE", "severity": "HIGH",
            "description": f"Settlement line duplicated for {src['payment_id']} "
                           f"(same batch/payment/type/amount).",
        })

    # ---- anomaly: settlement lines silently dropped from bank feed ----
    drop_candidates = [i for i in range(len(lines)) if i not in skip_indices]
    for idx in rng.sample(drop_candidates, min(an["dropped"], len(drop_candidates))):
        skip_indices.add(idx)
        l = lines[idx]
        gt.append({
            "entity": "settlement_lines", "entity_id": l["payment_id"],
            "issue_type": "UNMATCHED_SILENTLY_DROPPED",
            "expected_exception_type": "UNMATCHED_SETTLEMENT_LINE",
            "expected_resolution": "INVESTIGATE", "severity": "MEDIUM",
            "description": f"Settlement line for {l['payment_id']} "
                           f"({l['line_type']}) excluded from bank payout.",
        })

    # ---- anomaly: refund event before capture (lifecycle violation) ----
    refund_pids = sorted({r["payment_id"] for r in refunds})
    for pid in rng.sample(refund_pids, min(an["lifecycle"], len(refund_pids))):
        swap_pids.append(pid)
        gt.append({
            "entity": "processor_events", "entity_id": pid,
            "issue_type": "REFUND_BEFORE_CAPTURE",
            "expected_exception_type": "LIFECYCLE_ORDER_VIOLATION",
            "expected_resolution": "INVESTIGATE", "severity": "HIGH",
            "description": f"REFUNDED event timestamped before CAPTURED for {pid}.",
        })

    # ---- edge case (not an exception): currency rounding on JPY lines ----
    jpy_caps = [l["payment_id"] for l in lines
                if l["line_type"] == "CAPTURE" and l["currency"] == "JPY"]
    for pid in rng.sample(jpy_caps, min(an["rounding"], len(jpy_caps))):
        gt.append({
            "entity": "settlement_lines", "entity_id": pid,
            "issue_type": "CURRENCY_ROUNDING",
            "expected_exception_type": "NONE_VALID_ROUNDING",
            "expected_resolution": "NONE", "severity": "INFO",
            "description": f"JPY capture for {pid}: fee/net rounded to 0 decimals; "
                           f"must reconcile exactly, no exception expected.",
        })

    # ---- group lines into batches by (currency, ISO week of value_date) ----
    groups = {}
    for idx, l in enumerate(lines):
        y, w = iso_week(datetime.combine(l["value_date"], datetime.min.time()))
        groups.setdefault((l["currency"], y, w), []).append(idx)

    total = len(lines)
    keys = sorted(groups)
    raw = {k: len(groups[k]) / total * target_batches for k in keys}
    counts = {k: max(1, int(raw[k])) for k in keys}
    remainder = target_batches - sum(counts.values())
    fracs = sorted(keys, key=lambda k: raw[k] - int(raw[k]), reverse=True)
    i = 0
    while remainder > 0 and fracs:
        counts[fracs[i % len(fracs)]] += 1
        remainder -= 1
        i += 1
    while remainder < 0:
        k = max((k for k in keys if counts[k] > 1), key=lambda k: counts[k], default=None)
        if k is None:
            break
        counts[k] -= 1
        remainder += 1

    batches, batch_of_line = [], {}
    bseq = 0
    for k in keys:
        idxs = sorted(groups[k], key=lambda i: (lines[i]["value_date"], lines[i]["payment_id"]))
        n_b = counts[k]
        base, extra = divmod(len(idxs), n_b)
        pos = 0
        for b in range(n_b):
            size = base + (1 if b < extra else 0)
            chunk = idxs[pos:pos + size]
            pos += size
            if not chunk:
                continue
            bseq += 1
            bid = f"SET-2026-{bseq:06d}"
            vdates = [lines[i]["value_date"] for i in chunk]
            batches.append({
                "batch_id": bid, "currency": k[0],
                "period_start": min(vdates).isoformat(),
                "period_end": max(vdates).isoformat(),
                "value_date": max(vdates).isoformat(),
                "status": "SETTLED",
            })
            for i in chunk:
                batch_of_line[i] = bid

    # force duplicate clones into the SAME batch as their source
    for clone_idx, src_idx in clone_pairs:
        batch_of_line[clone_idx] = batch_of_line[src_idx]

    totals = {b["batch_id"]: {"gross": 0.0, "fee": 0.0, "net": 0.0, "n": 0}
              for b in batches}
    per_batch_seq = {}
    for idx, l in enumerate(lines):
        bid = batch_of_line[idx]
        per_batch_seq[bid] = per_batch_seq.get(bid, 0) + 1
        l["batch_id"] = bid
        l["line_id"] = f"STL-{bid}-{per_batch_seq[bid]:03d}"
        t = totals[bid]
        t["gross"] += l["gross_amount"]; t["fee"] += l["fee_amount"]
        t["net"] += l["net_amount"]; t["n"] += 1

    for b in batches:
        t = totals[b["batch_id"]]
        b["line_count"] = t["n"]
        b["gross_total"] = round(t["gross"], 2)
        b["fee_total"] = round(t["fee"], 2)
        b["net_total"] = round(t["net"], 2)

    return batches, lines, gt, skip_indices, swap_pids


def gen_bank_lines(rng, lines, skip_indices, an):
    """One bank payout per payment (N:M: a payout can cover several settlement
    lines — e.g. capture + split refunds). Injects payout-level anomalies.

    Returns (bank_rows, gt).
    """
    gt = []
    # group covered lines by payment -> one payout per payment
    pay_groups = {}
    for i, l in enumerate(lines):
        if i in skip_indices:
            continue
        pay_groups.setdefault(l["payment_id"], []).append(l)

    group_list = []
    for pid, glines in pay_groups.items():
        glines.sort(key=lambda l: l["value_date"])
        group_list.append({
            "payment_id": pid,
            "currency": glines[0]["currency"],
            "net_sum": money(sum(l["net_amount"] for l in glines), glines[0]["currency"]),
            "fee_sum": sum(l["fee_amount"] for l in glines),
            "max_vdate": max(l["value_date"] for l in glines),
            "batch_id": glines[0]["batch_id"],
            "line_count": len(glines),
        })
    rng.shuffle(group_list)

    n = len(group_list)
    hosts = {}
    pool = list(range(n))
    rng.shuffle(pool)
    pos = 0

    def take(k, key):
        nonlocal pos
        out = []
        while len(out) < k and pos < len(pool):
            gi = pool[pos]; pos += 1
            if gi not in hosts:
                hosts[gi] = key
                out.append(gi)
        return out

    take(an["fee_var"], "fee_var")
    take(an["ccy"], "ccy")
    take(an["amount_mm"], "amount_mm")
    take(an["date_win"], "date_win")

    bank_rows = []
    for gi, g in enumerate(group_list):
        bid = f"BANK-2026-{gi + 1:06d}"
        amount = g["net_sum"]
        currency = g["currency"]
        vdate = g["max_vdate"] + timedelta(days=rng.choice([0, 0, 1, 1, 2]))
        kind = hosts.get(gi)
        pid = g["payment_id"]

        if kind == "fee_var":
            amount = money(amount + g["fee_sum"], currency)  # off by exactly the fee
            gt.append({
                "entity": "bank_statement_lines", "entity_id": pid,
                "issue_type": "BANK_AMOUNT_DIFFERS_BY_FEE",
                "expected_exception_type": "FEE_VARIANCE",
                "expected_resolution": "ESCALATE_TO_RISK", "severity": "MEDIUM",
                "description": f"Payout {bid} for {pid} differs from settlement net "
                               f"by fee {g['fee_sum']} {currency}.",
            })
        elif kind == "ccy":
            currency = rng.choice([c for c in CURRENCIES if c != currency])
            gt.append({
                "entity": "bank_statement_lines", "entity_id": pid,
                "issue_type": "CURRENCY_MISMATCH",
                "expected_exception_type": "CURRENCY_MISMATCH",
                "expected_resolution": "ESCALATE_TO_RISK", "severity": "HIGH",
                "description": f"Payout {bid} currency {currency} != settlement "
                               f"{g['currency']} for {pid}.",
            })
        elif kind == "amount_mm":
            # partial amount mismatch: differs, but NOT by the fee
            delta = 1.37 if currency != "JPY" else 137.0
            amount = money(amount + delta, currency)
            gt.append({
                "entity": "bank_statement_lines", "entity_id": pid,
                "issue_type": "BANK_AMOUNT_PARTIAL_MISMATCH",
                "expected_exception_type": "AMOUNT_MISMATCH",
                "expected_resolution": "ESCALATE_TO_RISK", "severity": "HIGH",
                "description": f"Payout {bid} for {pid} differs from settlement net "
                               f"by {delta} {currency} (not the fee).",
            })
        elif kind == "date_win":
            vdate = vdate + timedelta(days=10)  # outside the 5-day match window
            gt.append({
                "entity": "bank_statement_lines", "entity_id": pid,
                "issue_type": "BANK_DATE_OUT_OF_WINDOW",
                "expected_exception_type": "DATE_OUT_OF_WINDOW",
                "expected_resolution": "REQUEST_STATEMENT", "severity": "MEDIUM",
                "description": f"Payout {bid} for {pid} value_date {vdate} is "
                               f"outside the match window.",
            })

        bank_rows.append({
            "bank_id": bid, "account_id": "ACCT-SETTLEMENT-001",
            "reference": pid, "amount": amount, "currency": currency,
            "value_date": vdate.isoformat(),
            "booking_date": (vdate - timedelta(days=1)).isoformat(),
            "description": f"STTL {g['batch_id']} PAYOUT lines={g['line_count']}",
        })

    return bank_rows, gt


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Generate reconciliation synthetic data.")
    ap.add_argument("--scale", choices=sorted(SCALES), default="starter")
    ap.add_argument("--out", required=True, help="Output directory")
    ap.add_argument("--seed", type=int, default=SEED_DEFAULT)
    args = ap.parse_args()

    cfg = SCALES[args.scale]
    an = cfg["anomalies"]
    rng = random.Random(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"[gen] scale={args.scale} seed={args.seed} anomalies={an}")

    merchants = gen_merchants(rng, cfg["merchants"])
    orders, attempts, events, captures, refunds = gen_orders_attempts_events(
        rng, merchants, cfg["orders"], an["dup_events"])
    print(f"[gen] merchants={len(merchants)} orders={len(orders)} "
          f"attempts={len(attempts)} events={len(events)} "
          f"captures={len(captures)} refunds={len(refunds)}")

    batches, lines, gt_settle, skip_indices, swap_pids = gen_settlement(
        rng, captures, refunds, cfg["batches"], an)

    # --- anomaly: refund-before-capture timestamp swaps ---
    for pid in swap_pids:
        cap_evs = [e for e in events if e["payment_id"] == pid
                   and e["event_type"] == "CAPTURED"]
        ref_evs = [e for e in events if e["payment_id"] == pid
                   and e["event_type"] == "REFUNDED"]
        if cap_evs and ref_evs:
            cap_ev, ref_ev = cap_evs[0], ref_evs[0]
            cap_ev["occurred_at"], ref_ev["occurred_at"] = (
                ref_ev["occurred_at"], cap_ev["occurred_at"])
            cap_ev["ingested_at"], ref_ev["ingested_at"] = (
                ref_ev["ingested_at"], cap_ev["ingested_at"])

    bank_rows, gt_bank = gen_bank_lines(rng, lines, skip_indices, an)

    # --- duplicate processor event ground truth (aggregate) ---
    gt_bank.append({
        "entity": "processor_events",
        "entity_id": f"({an['dup_events']} duplicate webhooks)",
        "issue_type": "DUPLICATE_PROCESSOR_EVENT",
        "expected_exception_type": "NONE_IDEMPOTENT_DEDUP",
        "expected_resolution": "DEDUPE_BY_PROVIDER_EVENT_ID",
        "severity": "LOW",
        "description": "Duplicate CAPTURED webhook rows share provider_event_id; "
                       "reconciler must dedupe idempotently (no exception).",
    })

    gt = gt_settle + gt_bank
    for i, g in enumerate(gt, 1):
        g["anomaly_id"] = f"GT-2026-{i:04d}"
        g["step"] = 5

    # ---------------- write CSVs ----------------
    write_csv(out / "merchants.csv",
              ["merchant_id", "legal_name", "country", "settlement_currency",
               "status", "created_at"], merchants)
    write_csv(out / "payment_orders.csv",
              ["payment_id", "merchant_id", "amount", "currency", "status", "created_at"],
              orders)
    write_csv(out / "payment_attempts.csv",
              ["attempt_id", "payment_id", "merchant_id", "processor", "amount",
               "currency", "status", "created_at"], attempts)
    write_csv(out / "processor_events.csv",
              ["event_id", "provider_event_id", "attempt_id", "payment_id",
               "event_type", "amount", "currency", "occurred_at", "ingested_at"],
              events)
    write_csv(out / "settlement_batches.csv",
              ["batch_id", "currency", "period_start", "period_end", "value_date",
               "status", "line_count", "gross_total", "fee_total", "net_total"],
              batches)
    write_csv(out / "settlement_lines.csv",
              ["line_id", "batch_id", "payment_id", "merchant_id", "line_type",
               "gross_amount", "fee_amount", "net_amount", "currency", "value_date"],
              lines)
    write_csv(out / "bank_statement_lines.csv",
              ["bank_id", "account_id", "reference", "amount", "currency",
               "value_date", "booking_date", "description"], bank_rows)
    write_csv(out / "ground_truth.csv",
              ["anomaly_id", "step", "entity", "entity_id", "issue_type",
               "expected_exception_type", "expected_resolution", "severity",
               "description"], gt)

    # ---------------- manifest ----------------
    files = ["merchants.csv", "payment_orders.csv", "payment_attempts.csv",
             "processor_events.csv", "settlement_batches.csv", "settlement_lines.csv",
             "bank_statement_lines.csv", "ground_truth.csv"]
    manifest = {
        "generator": "src/generate_data.py",
        "scale": args.scale, "seed": args.seed,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "counts": {f: sum(1 for _ in open(out / f, encoding="utf-8")) - 1 for f in files},
        "sha256": {f: sha256(out / f) for f in files},
        "nomenclature": {
            "merchant": "MER-000001", "payment": "PAY-2026-00000001",
            "attempt": "ATT-<payment_id>-NN", "event": "PE-2026-0000001",
            "provider_event": "PEVD-...", "batch": "SET-2026-000001",
            "line": "STL-<batch_id>-NNN", "bank": "BANK-2026-000001",
            "ground_truth": "GT-2026-0001",
        },
        "parameters": {
            "match_window_days": MATCH_WINDOW_DAYS, "tolerance": TOLERANCE,
            "fee_model": "2.9% + 0.30", "currency_decimals": {"JPY": 0, "*": 2},
            "bank_payout_grain": "one bank line per payment (N:M vs settlement lines)",
        },
    }
    with open(out / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"[gen] batches={len(batches)} lines={len(lines)} bank_payouts={len(bank_rows)} "
          f"anomalies={len(gt)}")
    print(f"[gen] wrote {len(files)} CSVs + manifest.json -> {out}")


if __name__ == "__main__":
    main()
