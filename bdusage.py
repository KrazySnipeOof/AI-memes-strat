#!/usr/bin/env python
"""Local ledger of Birdeye API usage, synced to the account's billing cycle.

Birdeye exposes no programmatic usage/balance endpoint (CU balance lives on
their web dashboard only), so every request made through bdfetch.bd_get() is
recorded here instead: reports/bd_usage.json, bucketed by BILLING PERIOD.
Kenny's Lite plan (verified against the account Packages page 2026-07-22):
2.5M CUs/mo included, cycle anchored on the 20th (Jul 20 - Aug 20), 15 RPS,
overage $15/1M CU.

reports/bd_usage_baseline.json holds a reconstructed floor estimate of the
CUs spent in the current period BEFORE local tracking began; snapshot() adds
it in. The Birdeye dashboard's Usages > Metrics page stays authoritative -
edit the baseline file to recalibrate against it.

CU costs from https://docs.birdeye.so/docs/compute-unit-cost. /defi/v3/ohlcv
is dynamically priced (no published formula); its entry below is an estimate
for the ~1000-candle calls the fetchers make - calibrate it against the real
numbers on the Birdeye dashboard if the two drift.
"""
from __future__ import annotations

import atexit
import calendar
import json
import os
import threading
import time

PLAN = {
    # Premium purchased 2026-07-23 (20M CU/mo, 50 RPS but 1000 req/min sustained,
    # $9.9/1M overage, websocket 500 conns). Anchor day assumes the cycle starts
    # at purchase - VERIFY against the account Packages page and correct if the
    # cycle is anchored differently. Old Lite buckets/baseline are period-keyed
    # ("2026-07-20") so they drop out of the new period automatically.
    "name": "Premium",
    "cu_per_month": 20_000_000,
    "rate_limit_rps": 50,
    "overage_usd_per_1m_cu": 9.9,
    "billing_anchor_day": 23,
}

CU_COST = {
    "/defi/v2/tokens/new_listing": 30,
    "/defi/v3/token/list": 75,
    "/defi/price": 3,
    # Dynamic-priced. Calibrated 2026-07-22: the account cap (2.5M) tripped at
    # ~22.4k ohlcv calls + known fixed costs -> ~110 CU per ~720-candle call.
    "/defi/v3/ohlcv": 110,
    # ws qualification path (estimates from the docs' CU table; recalibrate
    # against the Birdeye Metrics page if they drift)
    "/defi/v3/token/market-data": 15,
    "/defi/v3/token/trade-data/single": 30,
}
DEFAULT_CU = 30

_DIR = os.path.dirname(os.path.abspath(__file__))
USAGE_PATH = os.path.join(_DIR, "reports", "bd_usage.json")
BASELINE_PATH = os.path.join(_DIR, "reports", "bd_usage_baseline.json")


def set_data_dir(path: str) -> None:
    """Point the ledger at another checkout's reports/ directory.

    One Birdeye account bills one CU pool, so every process that spends CUs
    has to write into the same ledger or the dashboard under-reports. A runner
    started with `run.py --workdir <other checkout>` calls this so its usage
    lands with the trial's, not beside its own source.
    """
    global USAGE_PATH, BASELINE_PATH
    with _lock:
        _flush_locked()
        USAGE_PATH = os.path.join(path, "reports", "bd_usage.json")
        BASELINE_PATH = os.path.join(path, "reports", "bd_usage_baseline.json")

_lock = threading.Lock()
_pending: dict = {}  # path -> count, not yet flushed
_pending_n = 0
_last_flush = 0.0


def current_period(now: float | None = None):
    """(start_ts, end_ts, key) of the billing period containing `now`."""
    t = time.gmtime(now if now is not None else time.time())
    y, m = t.tm_year, t.tm_mon
    if t.tm_mday < PLAN["billing_anchor_day"]:
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    start = calendar.timegm((y, m, PLAN["billing_anchor_day"], 0, 0, 0))
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    end = calendar.timegm((ny, nm, PLAN["billing_anchor_day"], 0, 0, 0))
    return start, end, f"{y:04d}-{m:02d}-{PLAN['billing_anchor_day']:02d}"


def _load() -> dict:
    try:
        with open(USAGE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"tracked_since": time.strftime("%Y-%m-%d", time.gmtime()), "months": {}}


def _flush_locked() -> None:
    global _pending_n, _last_flush
    if not _pending:
        return
    data = _load()
    _, _, pkey = current_period()
    bucket = data["months"].setdefault(pkey, {"requests": 0, "cu_est": 0, "by_endpoint": {}})
    for path, n in _pending.items():
        bucket["requests"] += n
        bucket["cu_est"] += n * CU_COST.get(path, DEFAULT_CU)
        bucket["by_endpoint"][path] = bucket["by_endpoint"].get(path, 0) + n
    data["updated"] = time.time()
    _pending.clear()
    _pending_n = 0
    _last_flush = time.time()
    tmp = USAGE_PATH + ".tmp"
    try:
        os.makedirs(os.path.dirname(USAGE_PATH), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, USAGE_PATH)  # atomic: readers never see a partial file
    except OSError:
        pass


def record(path: str) -> None:
    """Count one request about to be sent to `path`. Buffered; flushed every
    20 requests / 5 seconds and at interpreter exit."""
    global _pending_n
    with _lock:
        _pending[path] = _pending.get(path, 0) + 1
        _pending_n += 1
        if _pending_n >= 20 or time.time() - _last_flush >= 5:
            _flush_locked()


def _in_period(key: str, data: dict, start: int, pkey: str) -> bool:
    """Whether a storage bucket belongs to the current billing period. Period
    keys (YYYY-MM-DD) match exactly; legacy calendar-month keys (YYYY-MM, from
    processes started before the billing-cycle sync) are attributed to the
    period containing tracked_since - all their writes happened after it."""
    if key == pkey:
        return True
    if len(key) == 7:
        try:
            ts = calendar.timegm(time.strptime(data.get("tracked_since", ""), "%Y-%m-%d"))
        except ValueError:
            return False
        _, _, since_pkey = current_period(ts)
        return since_pkey == pkey
    return False


def snapshot() -> dict:
    """Current billing period's usage vs the plan, baseline included."""
    with _lock:
        _flush_locked()
        data = _load()
    start, end, pkey = current_period()
    req, cu = 0, 0
    by_ep: dict = {}
    for key, b in data["months"].items():
        if _in_period(key, data, start, pkey):
            req += b["requests"]
            # re-price from request counts, not the stored cu_est, so CU_COST
            # recalibrations apply retroactively to already-recorded traffic
            for ep, n in b.get("by_endpoint", {}).items():
                by_ep[ep] = by_ep.get(ep, 0) + n
                cu += n * CU_COST.get(ep, DEFAULT_CU)
    baseline = None
    try:
        with open(BASELINE_PATH, "r", encoding="utf-8") as f:
            bl = json.load(f)
        if bl.get("period") == pkey:
            baseline = bl
            req += bl.get("requests", 0)
            cu += bl.get("cu_est", 0)
    except (OSError, ValueError):
        pass
    label = (time.strftime("%b %d", time.gmtime(start)) + " – "
             + time.strftime("%b %d", time.gmtime(end)))
    overage = max(0, cu - PLAN["cu_per_month"])
    return {
        "period": pkey,
        "period_label": label,
        "month": pkey,  # legacy field name, kept for older embedded pages
        "requests": req,
        "cu_est": cu,
        "plan_cu": PLAN["cu_per_month"],
        "pct_of_plan": round(100 * cu / PLAN["cu_per_month"], 2),
        "overage_usd_est": round(overage / 1e6 * PLAN["overage_usd_per_1m_cu"], 2),
        "by_endpoint": by_ep,
        "baseline": baseline,
        "plan": PLAN,
        "tracked_since": data.get("tracked_since"),
        "updated": data.get("updated"),
    }


atexit.register(lambda: (_lock.acquire(), _flush_locked(), _lock.release()))
