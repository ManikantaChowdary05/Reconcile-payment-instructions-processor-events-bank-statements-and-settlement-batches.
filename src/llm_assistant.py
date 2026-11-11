#!/usr/bin/env python3
"""
LLM-assisted exception triage.

Reads OPEN exceptions from a reconciliation run and asks an LLM to propose a
resolution action + rationale for each. Results are written to
llm_recommendations.csv — they are ADVISORY ONLY and never overwrite the
append-only resolutions.csv (analyst acceptance is a separate step).

Configuration (env vars):
    OPENAI_API_KEY   — required for live LLM calls
    OPENAI_BASE_URL  — optional, for OpenAI-compatible endpoints
    OPENAI_MODEL     — optional, default "gpt-4o-mini"

If no API key / openai package is available, a deterministic heuristic
fallback is used so the pipeline still runs end-to-end offline.

Usage:
    python src/llm_assistant.py --recon output/full
    python src/llm_assistant.py --recon output/full --limit 50 --summary
"""

import argparse
import csv
import json
import os
from pathlib import Path


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path, fieldnames, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


SYSTEM_PROMPT = (
    "You are a payments-reconciliation analyst. Given a reconciliation "
    "exception, recommend ONE concrete resolution action and a one-sentence "
    "rationale. Respond as JSON: {\"action\": str, \"rationale\": str, "
    "\"confidence\": float 0-1}. Allowed actions: INVESTIGATE, "
    "ESCALATE_TO_RISK, REQUEST_STATEMENT, VOID_DUPLICATE_LINE, "
    "REQUEST_MERCHANT_CONFIRMATION, CLOSE_FALSE_POSITIVE."
)


def heuristic_recommendation(exc):
    """Deterministic offline fallback (no external calls)."""
    t = exc["exception_type"]
    table = {
        "DUPLICATE_SETTLEMENT_LINE": ("VOID_DUPLICATE_LINE",
            "Identical settlement line exists; keep earliest line_id and void the clone.", 0.95),
        "UNMATCHED_SETTLEMENT_LINE": ("INVESTIGATE",
            "No bank representation found; check settlement run and bank feed coverage.", 0.7),
        "UNMATCHED_BANK_LINE": ("INVESTIGATE",
            "Orphan bank credit; trace reference to processor payout or manual journal.", 0.7),
        "FEE_VARIANCE": ("ESCALATE_TO_RISK",
            "Amount differs by exactly the processor fee; verify fee schedule version.", 0.8),
        "CURRENCY_MISMATCH": ("ESCALATE_TO_RISK",
            "Currency mismatch is a compliance risk; halt auto-resolution and review.", 0.9),
        "AMOUNT_MISMATCH": ("ESCALATE_TO_RISK",
            "Material amount variance; possible partial capture or tampering.", 0.85),
        "DATE_OUT_OF_WINDOW": ("REQUEST_STATEMENT",
            "Value date outside matching window; request corrected bank statement.", 0.75),
        "LIFECYCLE_ORDER_VIOLATION": ("INVESTIGATE",
            "Refund predates capture; verify event timestamps at the processor.", 0.9),
    }
    action, rationale, conf = table.get(t, ("INVESTIGATE", "Manual review required.", 0.5))
    return {"action": action, "rationale": rationale, "confidence": conf}


def llm_recommendation(exc, client, model):
    user = (f"Exception type: {exc['exception_type']}\n"
            f"Severity: {exc['severity']}\n"
            f"Payment: {exc['payment_id']}\n"
            f"Settlement line: {exc['settlement_line_id']}\n"
            f"Bank line: {exc['bank_line_id']}\n"
            f"Description: {exc['description']}")
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": user}],
        response_format={"type": "json_object"},
        temperature=0.1,
    )
    return json.loads(resp.choices[0].message.content)


def main():
    ap = argparse.ArgumentParser(description="LLM triage of open reconciliation exceptions.")
    ap.add_argument("--recon", required=True, help="Reconciliation output directory")
    ap.add_argument("--limit", type=int, default=0, help="Max exceptions to triage (0=all)")
    ap.add_argument("--summary", action="store_true", help="Also write llm_summary.md")
    args = ap.parse_args()

    recon = Path(args.recon)
    exceptions = [e for e in read_csv(recon / "exceptions.csv") if e["status"] == "OPEN"]
    if args.limit:
        exceptions = exceptions[:args.limit]

    api_key = os.environ.get("OPENAI_API_KEY")
    client, model, mode = None, os.environ.get("OPENAI_MODEL", "gpt-4o-mini"), "heuristic-fallback"
    if api_key:
        try:
            from openai import OpenAI
            client = OpenAI(base_url=os.environ.get("OPENAI_BASE_URL") or None,
                            api_key=api_key)
            mode = f"llm:{model}"
        except ImportError:
            print("[llm] openai package not installed; using heuristic fallback")

    rows = []
    for exc in exceptions:
        if client:
            try:
                rec = llm_recommendation(exc, client, model)
            except Exception as ex:  # never let one failure kill the batch
                print(f"[llm] call failed for {exc['exception_id']}: {ex}")
                rec = heuristic_recommendation(exc)
                mode = "heuristic-fallback(partial)"
        else:
            rec = heuristic_recommendation(exc)
        rows.append({
            "exception_id": exc["exception_id"],
            "exception_type": exc["exception_type"],
            "payment_id": exc["payment_id"],
            "recommended_action": rec.get("action", "INVESTIGATE"),
            "rationale": rec.get("rationale", ""),
            "confidence": rec.get("confidence", ""),
            "mode": mode,
        })

    write_csv(recon / "llm_recommendations.csv",
              ["exception_id", "exception_type", "payment_id", "recommended_action",
               "rationale", "confidence", "mode"], rows)
    print(f"[llm] triaged {len(rows)} exceptions (mode={mode}) -> {recon/'llm_recommendations.csv'}")

    if args.summary:
        by_type = {}
        for r in rows:
            by_type.setdefault(r["exception_type"], []).append(r["recommended_action"])
        lines = ["# LLM Exception Triage Summary", "",
                 f"- Mode: `{mode}`", f"- Exceptions triaged: {len(rows)}", "",
                 "## Recommended actions by exception type", ""]
        for t, acts in sorted(by_type.items()):
            counts = {}
            for a in acts:
                counts[a] = counts.get(a, 0) + 1
            lines.append(f"- **{t}**: " + ", ".join(f"{a}×{c}" for a, c in counts.items()))
        (recon / "llm_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[llm] wrote {recon/'llm_summary.md'}")


if __name__ == "__main__":
    main()
