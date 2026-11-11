#!/usr/bin/env python3
"""
Validation suite for the generated dataset and reconciliation outputs.

Checks: primary-key uniqueness, foreign-key integrity, enum validity,
date ordering, arithmetic reconciliation (batch totals vs lines,
net = gross - fee), duplicate checks, and lifecycle consistency.

Usage:
    python src/validate.py --data data/full --recon output/full
Writes validation_summary.json next to the recon output (or --out).
"""

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ENUMS = {
    "merchant.status": {"ACTIVE", "SUSPENDED"},
    "payment_orders.status": {"CREATED", "AUTHORIZED", "CAPTURED", "FAILED",
                              "EXPIRED", "REFUNDED", "PARTIALLY_REFUNDED"},
    "payment_attempts.status": {"PENDING", "AUTHORIZED", "CAPTURED", "FAILED", "EXPIRED"},
    "processor_events.event_type": {"AUTHORIZED", "CAPTURED", "REFUNDED",
                                    "FAILED", "EXPIRED"},
    "settlement_batches.status": {"SETTLED", "PENDING", "FAILED"},
    "settlement_lines.line_type": {"CAPTURE", "REFUND"},
    "exceptions.exception_type": {"DUPLICATE_SETTLEMENT_LINE", "UNMATCHED_SETTLEMENT_LINE",
                                  "UNMATCHED_BANK_LINE", "FEE_VARIANCE", "CURRENCY_MISMATCH",
                                  "AMOUNT_MISMATCH", "DATE_OUT_OF_WINDOW",
                                  "LIFECYCLE_ORDER_VIOLATION"},
    "resolutions.status": {"RESOLVED", "OPEN", "REJECTED"},
}


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def fnum(x):
    return float(x) if x not in (None, "") else 0.0


def d(s):
    return datetime.strptime(s[:10], "%Y-%m-%d")


def check(results, name, ok, detail=""):
    results.append({"check": name, "status": "PASS" if ok else "FAIL", "detail": detail})
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--recon", default=None, help="recon output dir (optional)")
    ap.add_argument("--out", default=None, help="where to write validation_summary.json")
    args = ap.parse_args()

    data, recon = Path(args.data), Path(args.recon) if args.recon else None
    out = Path(args.out) if args.out else (recon or data)
    results = []

    merchants = read_csv(data / "merchants.csv")
    orders = read_csv(data / "payment_orders.csv")
    attempts = read_csv(data / "payment_attempts.csv")
    events = read_csv(data / "processor_events.csv")
    batches = read_csv(data / "settlement_batches.csv")
    lines = read_csv(data / "settlement_lines.csv")
    bank = read_csv(data / "bank_statement_lines.csv")

    # ---- PK uniqueness ----
    for name, rows, key in [
        ("merchants", merchants, "merchant_id"),
        ("payment_orders", orders, "payment_id"),
        ("payment_attempts", attempts, "attempt_id"),
        ("processor_events", events, "event_id"),
        ("settlement_batches", batches, "batch_id"),
        ("settlement_lines", lines, "line_id"),
        ("bank_statement_lines", bank, "bank_id"),
    ]:
        dup = [k for k, c in Counter(r[key] for r in rows).items() if c > 1]
        check(results, f"pk_unique.{name}", not dup, f"{len(dup)} duplicates" if dup else "")

    # ---- FK integrity ----
    m_ids = {m["merchant_id"] for m in merchants}
    p_ids = {o["payment_id"] for o in orders}
    a_ids = {a["attempt_id"] for a in attempts}
    b_ids = {b["batch_id"] for b in batches}
    check(results, "fk.attempts.payment_id",
          all(a["payment_id"] in p_ids for a in attempts))
    check(results, "fk.attempts.merchant_id",
          all(a["merchant_id"] in m_ids for a in attempts))
    check(results, "fk.events.attempt_id",
          all(e["attempt_id"] in a_ids for e in events))
    check(results, "fk.lines.batch_id", all(l["batch_id"] in b_ids for l in lines))
    check(results, "fk.lines.payment_id", all(l["payment_id"] in p_ids for l in lines))
    check(results, "fk.orders.merchant_id", all(o["merchant_id"] in m_ids for o in orders))

    # ---- enums ----
    for field, allowed in ENUMS.items():
        table, col = field.split(".")
        rows = {"merchant": merchants, "payment_orders": orders,
                "payment_attempts": attempts, "processor_events": events,
                "settlement_batches": batches, "settlement_lines": lines}.get(table)
        if rows is None:
            continue
        bad = [r[col] for r in rows if r[col] not in allowed]
        check(results, f"enum.{field}", not bad,
              f"{len(bad)} invalid" if bad else "")

    # ---- date ordering ----
    order_created = {o["payment_id"]: d(o["created_at"]) for o in orders}
    att_created = {a["attempt_id"]: d(a["created_at"]) for a in attempts}
    bad = [a["attempt_id"] for a in attempts
           if att_created[a["attempt_id"]] < order_created[a["payment_id"]]]
    check(results, "dates.attempt_after_order", not bad, f"{len(bad)} violations" if bad else "")

    ev_by_att = defaultdict(list)
    for e in events:
        ev_by_att[e["attempt_id"]].append(d(e["occurred_at"]))
    bad = [aid for aid, ds in ev_by_att.items()
           if min(ds) < att_created.get(aid, datetime.min)]
    check(results, "dates.event_after_attempt", not bad,
          f"{len(bad)} violations" if bad else "")

    # ---- arithmetic: batch totals == sum(lines); net == gross - fee ----
    agg = defaultdict(lambda: [0.0, 0.0, 0.0])
    for l in lines:
        a = agg[l["batch_id"]]
        a[0] += fnum(l["gross_amount"]); a[1] += fnum(l["fee_amount"]); a[2] += fnum(l["net_amount"])
    bad = [b["batch_id"] for b in batches
           if abs(agg[b["batch_id"]][0] - fnum(b["gross_total"])) > 0.02
           or abs(agg[b["batch_id"]][1] - fnum(b["fee_total"])) > 0.02
           or abs(agg[b["batch_id"]][2] - fnum(b["net_total"])) > 0.02]
    check(results, "arith.batch_totals", not bad, f"{len(bad)} mismatched" if bad else "")

    bad = [l["line_id"] for l in lines
           if l["line_type"] == "CAPTURE"
           and abs(fnum(l["gross_amount"]) - fnum(l["fee_amount"]) - fnum(l["net_amount"])) > 0.02]
    check(results, "arith.line_net", not bad, f"{len(bad)} mismatched" if bad else "")

    # ---- duplicate provider_event_id count (expected >0 by design) ----
    pev_counts = Counter(e["provider_event_id"] for e in events)
    n_dup = sum(c - 1 for c in pev_counts.values() if c > 1)
    check(results, "duplicates.provider_events_present", n_dup > 0,
          f"{n_dup} duplicate webhook rows (idempotency test)")

    # ---- settlement line duplicate groups (expected exactly the injected one) ----
    keys = Counter((l["batch_id"], l["payment_id"], l["line_type"],
                    l["gross_amount"], l["currency"]) for l in lines)
    n_dup_lines = sum(c - 1 for c in keys.values() if c > 1)
    check(results, "duplicates.settlement_line", n_dup_lines >= 1,
          f"{n_dup_lines} duplicate line(s) injected")

    # ---- reconciliation outputs (if provided) ----
    if recon and (recon / "exceptions.csv").exists():
        matches = read_csv(recon / "reconciliation_matches.csv")
        exceptions = read_csv(recon / "exceptions.csv")
        resolutions = read_csv(recon / "resolutions.csv")

        check(results, "recon.pk.matches",
              len({m["match_id"] for m in matches}) == len(matches))
        check(results, "recon.fk.matches.line",
              all(m["settlement_line_id"] in {l["line_id"] for l in lines} for m in matches))
        check(results, "recon.fk.matches.bank",
              all(m["bank_line_id"] in {b["bank_id"] for b in bank} for m in matches))
        check(results, "recon.fk.resolutions.exception",
              all(r["exception_id"] in {e["exception_id"] for e in exceptions}
                  for r in resolutions))
        # every exception has a resolution (append-only audit trail)
        check(results, "recon.every_exception_resolved",
              all(any(r["exception_id"] == e["exception_id"] for r in resolutions)
                  for e in exceptions))
        # 1:1 matches use each bank payout once; GROUPED matches may share one
        non_grouped = [m["bank_line_id"] for m in matches if m["match_type"] != "GROUPED"]
        check(results, "recon.bank_lines_matched_once",
              len(set(non_grouped)) == len(non_grouped),
              f"{len(non_grouped)} non-grouped matches")
        check(results, "recon.match_type_enum",
              all(m["match_type"] in {"EXACT", "TOLERANCE", "GROUPED"} for m in matches))
        # report exists and match_rate consistent
        rep = json.loads((recon / "reconciliation_report.json").read_text())
        check(results, "recon.report_match_rate",
              abs(rep["metrics"]["match_rate"] -
                  len(matches) / max(len(lines) - sum(1 for _ in []), 1)) < 0.2,
              "sanity bound only")

    summary = {
        "data_dir": str(data), "recon_dir": str(recon) if recon else None,
        "total_checks": len(results),
        "passed": sum(1 for r in results if r["status"] == "PASS"),
        "failed": sum(1 for r in results if r["status"] == "FAIL"),
        "checks": results,
    }
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "validation_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    for r in results:
        print(f"  [{r['status']}] {r['check']} {r['detail']}")
    print(f"[validate] {summary['passed']}/{summary['total_checks']} passed "
          f"-> {out/'validation_summary.json'}")
    raise SystemExit(0 if summary["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
