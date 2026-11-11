#!/usr/bin/env python3
"""
Reconciliation engine.

Matches settlement lines to bank statement lines on
(reference, amount, currency, value-date window). Bank payouts are N:M:
one bank line can cover several settlement lines of the same payment
(e.g. capture + split refunds) — recorded via reconciliation_matches with
match_type GROUPED. Detects duplicates and lifecycle violations, raises an
exception for every unmatched / partial record, records append-only
resolutions, auto-closes rule-resolvable exceptions, and scores detection
per anomaly type (precision / recall / F1) against ground_truth.csv.

Usage:
    python src/reconcile.py --data data/full --out output/full
    python src/reconcile.py --data data/full --out output/full --tolerance 0.01 --window-days 5
"""

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

TOLERANCE_DEFAULT = 0.01
WINDOW_DAYS_DEFAULT = 5


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path, fieldnames, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def fnum(x):
    return float(x) if x not in (None, "") else 0.0


def parse_date(s):
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def subsets(items):
    """All non-empty subsets of a small list (groups have <= ~4 lines)."""
    for r in range(1, len(items) + 1):
        for combo in combinations(items, r):
            yield list(combo)


def main():
    ap = argparse.ArgumentParser(description="Reconcile settlement lines vs bank payouts.")
    ap.add_argument("--data", required=True, help="Directory containing generated CSVs")
    ap.add_argument("--out", required=True, help="Output directory for recon results")
    ap.add_argument("--tolerance", type=float, default=TOLERANCE_DEFAULT)
    ap.add_argument("--window-days", type=int, default=WINDOW_DAYS_DEFAULT)
    args = ap.parse_args()

    data, out = Path(args.data), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tol, window = args.tolerance, args.window_days

    # ---------------- load ----------------
    lines = read_csv(data / "settlement_lines.csv")
    bank = read_csv(data / "bank_statement_lines.csv")
    events = read_csv(data / "processor_events.csv")
    gt = read_csv(data / "ground_truth.csv") if (data / "ground_truth.csv").exists() else []

    for l in lines:
        l["_net"] = fnum(l["net_amount"]); l["_fee"] = fnum(l["fee_amount"])
        l["_gross"] = fnum(l["gross_amount"]); l["_vdate"] = parse_date(l["value_date"])
    for b in bank:
        b["_amount"] = fnum(b["amount"]); b["_vdate"] = parse_date(b["value_date"])

    matches, exceptions, resolutions = [], [], []
    m_seq = e_seq = r_seq = 0

    def add_exception(etype, severity, payment_id="", line=None, bank_row=None,
                      description="", status="OPEN"):
        nonlocal e_seq
        e_seq += 1
        eid = f"EXC-2026-{e_seq:06d}"
        exceptions.append({
            "exception_id": eid, "exception_type": etype, "severity": severity,
            "settlement_line_id": line["line_id"] if line else "",
            "bank_line_id": bank_row["bank_id"] if bank_row else "",
            "payment_id": payment_id or (line["payment_id"] if line else
                                         (bank_row["reference"] if bank_row else "")),
            "description": description, "status": status,
            "created_at": "2026-07-01T00:00:00Z",
        })
        return eid

    def add_resolution(exception_id, action, resolved_by, notes, status="RESOLVED"):
        nonlocal r_seq
        r_seq += 1
        resolutions.append({
            "resolution_id": f"RES-2026-{r_seq:06d}", "exception_id": exception_id,
            "action": action, "resolved_by": resolved_by, "notes": notes,
            "status": status, "created_at": "2026-07-01T00:00:00Z",
        })

    # ---------------- 1. processor-event idempotency ----------------
    seen_pev, dup_events = set(), 0
    for e in events:
        pev = e["provider_event_id"]
        if pev in seen_pev:
            dup_events += 1
        else:
            seen_pev.add(pev)

    # ---------------- 2. lifecycle ordering (refund before capture) ----
    cap_t, ref_t = {}, {}
    for e in events:
        if e["event_type"] == "CAPTURED" and e["payment_id"] not in cap_t:
            cap_t[e["payment_id"]] = parse_date(e["occurred_at"])
        if e["event_type"] == "REFUNDED":
            ref_t.setdefault(e["payment_id"], parse_date(e["occurred_at"]))
    lifecycle_bad = {p for p in ref_t if p in cap_t and ref_t[p] < cap_t[p]}
    for p in sorted(lifecycle_bad):
        eid = add_exception("LIFECYCLE_ORDER_VIOLATION", "HIGH", payment_id=p,
                            description=f"REFUNDED event dated before CAPTURED for {p}.")
        add_resolution(eid, "INVESTIGATE", "rule-engine",
                       "Event timestamps violate capture->refund ordering.", "OPEN")

    # ---------------- 3. duplicate settlement lines -------------------
    groups = defaultdict(list)
    for l in lines:
        groups[(l["batch_id"], l["payment_id"], l["line_type"],
                l["gross_amount"], l["currency"])].append(l)
    voided = set()
    for key, grp in groups.items():
        if len(grp) > 1:
            grp_sorted = sorted(grp, key=lambda x: x["line_id"])
            for dup in grp_sorted[1:]:
                voided.add(dup["line_id"])
                eid = add_exception(
                    "DUPLICATE_SETTLEMENT_LINE", "HIGH", line=dup,
                    description=f"Duplicate of {grp_sorted[0]['line_id']} "
                                f"(same batch/payment/type/amount).")
                # auto-resolvable: void the clone, keep the earliest line
                add_resolution(eid, "VOID_DUPLICATE_LINE", "rule-engine",
                               f"Keep {grp_sorted[0]['line_id']}, void {dup['line_id']}.",
                               "RESOLVED")
                exceptions[-1]["status"] = "RESOLVED"

    active_lines = [l for l in lines if l["line_id"] not in voided]

    # ---------------- 4. N:M matching: payment groups vs bank payouts ---
    bank_by_ref = defaultdict(list)
    for b in bank:
        bank_by_ref[b["reference"]].append(b)
    used_bank = set()

    pay_groups = defaultdict(list)
    for l in active_lines:
        pay_groups[l["payment_id"]].append(l)

    def in_window(b, vdate):
        return abs((b["_vdate"] - vdate).days) <= window

    def emit_matches(group_lines, b, mtype):
        nonlocal m_seq
        variance = round(b["_amount"] - sum(l["_net"] for l in group_lines), 4)
        # variance is group-level; recorded identically on each row of the match
        for l in group_lines:
            m_seq += 1
            matches.append({
                "match_id": f"REC-2026-{m_seq:06d}",
                "settlement_line_id": l["line_id"], "bank_line_id": b["bank_id"],
                "match_type": mtype, "amount_variance": variance,
                "status": "MATCHED",
            })

    def diagnose_group(pid, glines, cands):
        """Group-level anomaly classification; returns (etype, bank_row)."""
        gsum = sum(l["_net"] for l in glines)
        gmax = max(l["_vdate"] for l in glines)
        gcurrency = glines[0]["currency"]
        fee_sum = sum(l["_fee"] for l in glines)
        for b in cands:
            if b["currency"] != gcurrency and abs(b["_amount"] - gsum) <= tol \
                    and in_window(b, gmax):
                return "CURRENCY_MISMATCH", b
        for b in cands:
            if b["currency"] == gcurrency and in_window(b, gmax):
                diff = b["_amount"] - gsum
                if abs(abs(diff) - abs(fee_sum)) <= tol and fee_sum != 0:
                    return "FEE_VARIANCE", b
        for b in cands:
            if b["currency"] == gcurrency and abs(b["_amount"] - gsum) <= tol \
                    and not in_window(b, gmax):
                return "DATE_OUT_OF_WINDOW", b
        for b in cands:
            if (b["currency"] == gcurrency and in_window(b, gmax)
                    and (b["_amount"] > 0) == (gsum > 0)):
                return "AMOUNT_MISMATCH", b
        return None, None

    for pid in sorted(pay_groups):
        glines = sorted(pay_groups[pid], key=lambda l: l["line_id"])
        gsum = sum(l["_net"] for l in glines)
        gmax = max(l["_vdate"] for l in glines)
        gcurrency = glines[0]["currency"]
        cands = [b for b in bank_by_ref.get(pid, []) if b["bank_id"] not in used_bank]

        # try every bank candidate x every subset of lines (N:M matching)
        best = None  # (bank, subset_lines, variance, n_lines)
        for b in cands:
            if b["currency"] != gcurrency:
                continue
            for sub in subsets(glines):
                sub_max = max(l["_vdate"] for l in sub)
                if not in_window(b, sub_max):
                    continue
                ssum = sum(l["_net"] for l in sub)
                var = abs(b["_amount"] - ssum)
                if var <= tol:
                    # prefer largest subset (full group), then smallest variance
                    key = (len(sub), -var)
                    if best is None or key > best[0]:
                        best = (key, b, sub)
        if best is not None:
            _, b, sub = best
            used_bank.add(b["bank_id"])
            sub_ids = {l["line_id"] for l in sub}
            matched = [l for l in glines if l["line_id"] in sub_ids]
            leftover = [l for l in glines if l["line_id"] not in sub_ids]
            mtype = "GROUPED" if len(matched) > 1 else (
                "EXACT" if abs(b["_amount"] - sum(l["_net"] for l in matched)) <= 0.005
                else "TOLERANCE")
            emit_matches(matched, b, mtype)
            # leftover lines = silently dropped from the payout -> exceptions
            for l in leftover:
                eid = add_exception(
                    "UNMATCHED_SETTLEMENT_LINE", "MEDIUM", line=l,
                    description=f"Line {l['line_id']} not covered by payout "
                                f"{b['bank_id']} (dropped from bank feed).")
                add_resolution(eid, "INVESTIGATE", "analyst-pending",
                               "Awaiting bank statement or settlement re-run.", "OPEN")
            continue

        # no subset matched -> group-level anomaly or fully unmatched
        etype, b = diagnose_group(pid, glines, cands)
        if etype:
            used_bank.add(b["bank_id"])  # accounted for by the specific exception
            sev = {"CURRENCY_MISMATCH": "HIGH", "FEE_VARIANCE": "MEDIUM",
                   "AMOUNT_MISMATCH": "HIGH", "DATE_OUT_OF_WINDOW": "MEDIUM"}.get(etype, "MEDIUM")
            eid = add_exception(etype, sev, payment_id=pid, bank_row=b,
                                description=f"{etype} for payout of {pid}: "
                                            f"bank={b['bank_id']}")
            if etype == "FEE_VARIANCE":
                # policy: variance equal to processor fee is auto-accepted
                add_resolution(eid, "ACCEPT_FEE_VARIANCE", "rule-engine",
                               "Amount differs by exactly the fee; accepted per policy.",
                               "RESOLVED")
                exceptions[-1]["status"] = "RESOLVED"
            else:
                action = {"CURRENCY_MISMATCH": "ESCALATE_TO_RISK",
                          "AMOUNT_MISMATCH": "ESCALATE_TO_RISK",
                          "DATE_OUT_OF_WINDOW": "REQUEST_STATEMENT"}.get(etype, "INVESTIGATE")
                add_resolution(eid, action, "rule-engine",
                               f"Auto-classified as {etype}.", "OPEN")
        else:
            for l in glines:
                eid = add_exception(
                    "UNMATCHED_SETTLEMENT_LINE", "MEDIUM", line=l,
                    description="No bank payout covers this settlement line.")
                add_resolution(eid, "INVESTIGATE", "analyst-pending",
                               "Awaiting bank statement or settlement re-run.", "OPEN")

    # ---------------- 5. unmatched bank lines ---------------------------
    for b in bank:
        if b["bank_id"] not in used_bank:
            eid = add_exception(
                "UNMATCHED_BANK_LINE", "MEDIUM", bank_row=b,
                description=f"Bank payout {b['bank_id']} ref={b['reference']} "
                            f"has no matching settlement lines.")
            add_resolution(eid, "INVESTIGATE", "analyst-pending",
                           "Possible orphan bank credit.", "OPEN")

    # ---------------- 6. ground-truth scoring (per-type P/R/F1) ----------
    exc_by_pid_type = defaultdict(set)   # payment_id -> set(exception_type)
    for e in exceptions:
        if e["payment_id"]:
            exc_by_pid_type[e["payment_id"]].add(e["exception_type"])

    gt_refs = defaultdict(set)           # expected_type -> set(payment_id refs)
    gt_detail = []
    per_type = defaultdict(lambda: {"gt_total": 0, "gt_detected": 0,
                                    "exc_total": 0, "exc_tp": 0})
    # ensure keys exist even for types with zero exceptions
    for g in gt:
        exp = g["expected_exception_type"]
        ref = g["entity_id"]
        t = per_type[exp]
        t["gt_total"] += 1
        if exp == "NONE_IDEMPOTENT_DEDUP":
            detected = dup_events > 0
        elif exp.startswith("NONE"):
            detected = ref not in exc_by_pid_type  # no exception for this payment
            gt_refs[exp].add(ref)
        else:
            detected = exp in exc_by_pid_type.get(ref, set())
            gt_refs[exp].add(ref)
        if detected:
            t["gt_detected"] += 1
        gt_detail.append({"anomaly_id": g["anomaly_id"], "issue_type": g["issue_type"],
                          "expected": exp, "entity_id": ref, "detected": detected})

    for e in exceptions:
        t = per_type[e["exception_type"]]
        t["exc_total"] += 1
        if e["payment_id"] in gt_refs.get(e["exception_type"], set()):
            t["exc_tp"] += 1

    prf1 = {}
    for t, v in per_type.items():
        recall = v["gt_detected"] / v["gt_total"] if v["gt_total"] else 0.0
        precision = v["exc_tp"] / v["exc_total"] if v["exc_total"] else 1.0
        f1 = (2 * precision * recall / (precision + recall)
              if precision + recall else 0.0)
        prf1[t] = {"gt_total": v["gt_total"], "gt_detected": v["gt_detected"],
                   "exceptions": v["exc_total"], "true_positive_exceptions": v["exc_tp"],
                   "recall": round(recall, 4), "precision": round(precision, 4),
                   "f1": round(f1, 4)}

    # ---------------- 7. metrics & report --------------------------------
    n_lines = len(active_lines)
    report = {
        "generated_from": str(data), "tolerance": tol, "window_days": window,
        "inputs": {"settlement_lines": len(lines), "active_lines": n_lines,
                   "voided_duplicates": len(voided), "bank_payouts": len(bank),
                   "processor_events": len(events),
                   "duplicate_provider_events": dup_events},
        "outputs": {"matches": len(matches), "exceptions": len(exceptions),
                    "resolutions": len(resolutions)},
        "metrics": {
            "match_rate": round(len(matches) / max(n_lines, 1), 4),
            "grouped_matches": sum(1 for m in matches if m["match_type"] == "GROUPED"),
            "exception_rate": round(len(exceptions) / max(n_lines + len(bank), 1), 4),
            "auto_resolved": sum(1 for r in resolutions if r["status"] == "RESOLVED"),
            "open_exceptions": sum(1 for e in exceptions if e["status"] == "OPEN"),
            "lifecycle_violations": len(lifecycle_bad),
            "ground_truth_detection_rate": round(
                sum(1 for g in gt_detail if g["detected"]) / max(len(gt_detail), 1), 4),
        },
        "exceptions_by_type": {},
        "per_type_prf1": prf1,
        "ground_truth_detail": gt_detail,
    }
    for e in exceptions:
        report["exceptions_by_type"][e["exception_type"]] = \
            report["exceptions_by_type"].get(e["exception_type"], 0) + 1

    # ---------------- write outputs --------------------------------------
    write_csv(out / "reconciliation_matches.csv",
              ["match_id", "settlement_line_id", "bank_line_id", "match_type",
               "amount_variance", "status"], matches)
    write_csv(out / "exceptions.csv",
              ["exception_id", "exception_type", "severity", "settlement_line_id",
               "bank_line_id", "payment_id", "description", "status", "created_at"],
              exceptions)
    write_csv(out / "resolutions.csv",
              ["resolution_id", "exception_id", "action", "resolved_by", "notes",
               "status", "created_at"], resolutions)
    with open(out / "reconciliation_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(f"[recon] lines={len(lines)} active={n_lines} payouts={len(bank)} "
          f"matches={len(matches)} exceptions={len(exceptions)} "
          f"match_rate={report['metrics']['match_rate']}")
    print(f"[recon] exceptions_by_type={report['exceptions_by_type']}")
    print(f"[recon] gt_detection={report['metrics']['ground_truth_detection_rate']} "
          f"dup_events={dup_events} auto_resolved={report['metrics']['auto_resolved']}")
    for t, v in sorted(prf1.items()):
        print(f"[recon]   {t}: P={v['precision']} R={v['recall']} F1={v['f1']} "
              f"(gt {v['gt_detected']}/{v['gt_total']}, exc {v['exceptions']})")
    print(f"[recon] wrote outputs -> {out}")


if __name__ == "__main__":
    main()
