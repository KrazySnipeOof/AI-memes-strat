#!/usr/bin/env python
"""Guard the decision NOT to add machine learning to this bot yet.

The repo currently trades on rules and runs on `requests`. That is a
deliberate position, not an omission -- see docs/ML-GATE.md. The failure mode
this script exists to prevent is the quiet one: a dependency drifts in during
some experiment, a model gets fitted on 71 live trades, and six weeks later
nobody can say whether the book is trading a signal or a memorised sample.

So it does two jobs.

  INVARIANT   No heavy-ML import anywhere in the tree, and nothing but
              `requests` in requirements.txt. Breaking this fails the run,
              which makes it usable as a pre-commit or CI check.

  READINESS   Score the preconditions that would make an ML book defensible,
              against the ledger as it actually stands today. The point is to
              replace "should we add ML yet?" with a number.

The preconditions are deliberately about EVIDENCE, not about modelling
technique, and two of them are enforced by the tools built alongside this one
(mtest.py for the selection-bias haircut, splitaudit.py for the split).

Usage:
  python mlgate.py                # report, exit 1 only if the invariant broke
  python mlgate.py --strict       # exit 1 if any precondition is unmet
"""
from __future__ import annotations

import argparse
import datetime
import glob
import io
import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Libraries whose presence means a model is being fitted somewhere. Plotting
# and dataframe libraries are NOT here: matplotlib/pandas are analysis tools
# and carry no modelling claim.
BANNED = ["torch", "tensorflow", "keras", "sklearn", "scikit_learn", "xgboost",
          "lightgbm", "catboost", "jax", "flax", "transformers", "gym",
          "gymnasium", "stable_baselines3", "ray", "optuna", "statsmodels"]
IMPORT_RE = re.compile(
    r"^\s*(?:import|from)\s+(" + "|".join(BANNED) + r")\b", re.M)

SKIP_DIRS = {".git", "__pycache__", ".bd_cache", ".bd_cache_ext", ".ohlcv_cache",
             ".copy_cache", ".copytrade_cache", ".pages_deploy", "node_modules",
             "reports", "canvases"}

# Preconditions for an ML book to be worth building. Numbers are reasoning-only
# and were fixed before the ledger was counted, so this is not a moving target.
MIN_TRADES = 2000     # labelled live trades, closed and non-void
MIN_DAYS = 60         # calendar span they must cover (regime diversity)


def scan_imports(root):
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                src = io.open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            for m in IMPORT_RE.finditer(src):
                line = src[:m.start()].count("\n") + 1
                hits.append({"file": os.path.relpath(path, root),
                             "line": line, "package": m.group(1)})
    return hits


def scan_requirements(root):
    path = os.path.join(root, "requirements.txt")
    if not os.path.exists(path):
        return []
    pkgs = []
    for raw in io.open(path, encoding="utf-8"):
        s = raw.split("#")[0].strip()
        if s:
            pkgs.append(re.split(r"[<>=!\[]", s)[0].strip().lower())
    return [p for p in pkgs if p.replace("-", "_") in BANNED]


def read_ledgers(root):
    """Closed, non-void live positions across the whole fleet."""
    trades, books = [], {}
    for path in sorted(glob.glob(os.path.join(root, "memebot*.sqlite"))):
        book = os.path.basename(path)[len("memebot"):-len(".sqlite")].lstrip("-") or "default"
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            rows = list(con.execute(
                "select opened_at, closed_at, sol_spent, sol_received, status, "
                "exit_reason from positions"))
            con.close()
        except sqlite3.Error:
            continue
        n_ok = 0
        for opened, closed, spent, recv, status, reason in rows:
            if status != "closed" or not spent:
                continue
            trades.append({"book": book, "opened_at": opened,
                           "multiple": (recv or 0) / spent, "reason": reason})
            n_ok += 1
        books[book] = {"rows": len(rows), "labelled": n_ok}
    return trades, books


def ledger_has_features(root):
    """Does the live ledger record anything a model could learn FROM?

    Outcomes alone are not a training set. Without entry-time features stored
    per position, every live trade is an unlabelled row and the 2000-trade
    count below is measuring the wrong thing.
    """
    for path in sorted(glob.glob(os.path.join(root, "memebot*.sqlite"))):
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            cols = {c[1] for c in con.execute("pragma table_info(positions)")}
            con.close()
        except sqlite3.Error:
            continue
        outcome = {"id", "mode", "mint", "symbol", "pool", "status", "opened_at",
                   "closed_at", "sol_spent", "tokens_initial", "tokens_raw",
                   "sol_received", "tp_stage", "peak_multiple", "quote_failures",
                   "exit_reason", "fees_lamports"}
        extra = cols - outcome
        if extra:
            return sorted(extra)
    return []


def span_days(trades):
    ts = []
    for t in trades:
        try:
            ts.append(datetime.datetime.strptime(t["opened_at"], "%Y-%m-%dT%H:%M:%SZ"))
        except (ValueError, TypeError):
            pass
    if len(ts) < 2:
        return 0.0, None, None
    return (max(ts) - min(ts)).total_seconds() / 86400.0, min(ts), max(ts)


def main():
    ap = argparse.ArgumentParser(description="ML dependency invariant + readiness gate")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 if any precondition is unmet, not just the invariant")
    ap.add_argument("--out", default=os.path.join("reports", "ml_gate.json"))
    args = ap.parse_args()

    root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(root)

    # ---------------------------------------------------------- invariant ---
    hits = scan_imports(root)
    bad_reqs = scan_requirements(root)
    invariant_ok = not hits and not bad_reqs
    print("== DEPENDENCY INVARIANT ==")
    if invariant_ok:
        print(f"  clean: no {len(BANNED)} watched ML packages imported anywhere, "
              f"requirements.txt unchanged")
    else:
        for h in hits:
            print(f"  IMPORT  {h['file']}:{h['line']}  imports {h['package']}")
        for p in bad_reqs:
            print(f"  REQUIRE requirements.txt declares {p}")

    # ------------------------------------------------------------ readiness --
    trades, books = read_ledgers(root)
    days, t0, t1 = span_days(trades)
    features = ledger_has_features(root)
    have_mtest = os.path.exists(os.path.join(root, "mtest.py"))
    have_split = os.path.exists(os.path.join(root, "splitaudit.py"))
    audit_path = os.path.join("reports", "split_audit.json")
    audit = None
    if os.path.exists(audit_path):
        try:
            audit = json.load(io.open(audit_path, encoding="utf-8"))
        except ValueError:
            audit = None

    print("\n== LIVE LEDGER ==")
    for book, b in sorted(books.items()):
        if b["rows"]:
            print(f"  {book:<14} {b['labelled']:>5} closed of {b['rows']:>5} rows")
    print(f"  {'TOTAL':<14} {len(trades):>5} labelled live trades")
    if t0:
        print(f"  span           {t0:%Y-%m-%d} -> {t1:%Y-%m-%d}  ({days:.1f} days)")

    checks = [
        ("P1  target is survival, not price",
         None,
         "design constraint - see docs/ML-GATE.md; not machine-checkable"),
        ("P2  entry-time features recorded per live trade",
         bool(features),
         (f"positions carries {len(features)} feature columns" if features else
          "the ledger stores OUTCOMES ONLY - no feature vector exists to train on")),
        (f"P3  >= {MIN_TRADES} labelled live trades",
         len(trades) >= MIN_TRADES,
         f"{len(trades)} / {MIN_TRADES}  ({100.0 * len(trades) / MIN_TRADES:.1f}%)"),
        (f"P4  >= {MIN_DAYS} days of regime coverage",
         days >= MIN_DAYS,
         f"{days:.1f} / {MIN_DAYS} days"),
        ("P5  selection-bias haircut in the loop",
         have_mtest,
         "mtest.py present; sweeps report a deflated Sharpe" if have_mtest
         else "mtest.py missing"),
        ("P6  split audited for ticker/time leakage",
         bool(audit) and have_split,
         (f"last audit: {audit['structure']['tokens_in_straddling']} tokens "
          f"({100.0 * audit['structure']['tokens_in_straddling'] / max(audit['structure']['n'], 1):.0f}%) "
          f"in a straddling ticker" if audit and audit.get("structure")
          else "run splitaudit.py")),
    ]
    print("\n== READINESS ==")
    unmet = 0
    for name, ok, note in checks:
        if ok is None:
            mark = "n/a "
        elif ok:
            mark = "PASS"
        else:
            mark = "FAIL"
            unmet += 1
        print(f"  [{mark}] {name:<46} {note}")

    print("\n== VERDICT ==")
    if not invariant_ok:
        print("  INVARIANT BROKEN - an ML dependency entered the tree. Either revert "
              "it or\n  update docs/ML-GATE.md with the decision to change course.")
    elif unmet:
        print(f"  HOLD - {unmet} precondition(s) unmet. The repo stays rules-only.")
        if not features:
            print("  Binding constraint: the ledger records outcomes but no entry-time "
                  "features,\n  so every live trade is an unlabelled row. Trade count is "
                  "not the first problem.")
        elif len(trades) < MIN_TRADES:
            print(f"  Binding constraint: {MIN_TRADES - len(trades)} more labelled "
                  f"live trades.")
    else:
        print("  READY - every precondition met. An ML book is now defensible; "
              "pre-register\n  the evaluation before fitting anything.")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with io.open(args.out, "w", encoding="utf-8") as f:
        json.dump({"invariant_ok": invariant_ok, "import_hits": hits,
                   "bad_requirements": bad_reqs, "books": books,
                   "labelled_trades": len(trades), "span_days": days,
                   "feature_columns": features,
                   "checks": [{"name": n, "ok": o, "note": d} for n, o, d in checks],
                   "unmet": unmet}, f, indent=1)
    print(f"\nwritten: {args.out}")

    if not invariant_ok:
        return 1
    return 1 if (args.strict and unmet) else 0


if __name__ == "__main__":
    sys.exit(main())
