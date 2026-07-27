from __future__ import annotations

"""Entry screens for the family strategies (sniper / volume_anomaly / dip).

Pure, entry-time-decidable checks over pre-entry candle rows [ts,o,h,l,c,v_usd]
ascending. Shared by the live bot (bot/main.py entry_screen gate) and the
backtest (newstrat_backtest.py) so both judge entries identically. No I/O.
"""


def _cum_vol(rows):
    return sum(r[5] for r in rows)


def _two_sided(rows):
    tot = _cum_vol(rows) or 1e-9
    red = sum(r[5] for r in rows if r[4] < r[1])
    return red / tot


def sniper(rows, p):
    """Ultra-early: real two-sided trading right after launch (not a one-sided
    bot pump). Age is bounded by the config's min/max_age filters."""
    cum = _cum_vol(rows)
    if cum < p.get("min_cum_vol_usd", 3000):
        return False, f"sniper: cum vol ${cum:,.0f} thin"
    ts = _two_sided(rows)
    if ts < p.get("min_two_sided", 0.12):
        return False, f"sniper: {ts:.0%} sells (one-sided)"
    return True, f"sniper ok (vol ${cum:,.0f}, {ts:.0%} two-sided)"


def volume_anomaly(rows, p):
    """Abnormal volume spike vs the token's own recent baseline (median of
    prior candles), regardless of price pattern."""
    vols = [r[5] for r in rows]
    prior = sorted(vols[:-1])
    if len(prior) < 5:
        return False, "volume_anomaly: too few candles"
    med = prior[len(prior) // 2] or 1e-9
    last = vols[-1]
    ratio = last / med
    min_ratio = p.get("min_vol_ratio", 4.0)
    if ratio < min_ratio:
        return False, f"volume_anomaly: spike {ratio:.1f}x < {min_ratio}x baseline"
    if last < p.get("min_spike_vol_usd", 2000):
        return False, f"volume_anomaly: spike vol ${last:,.0f} thin"
    return True, f"volume_anomaly ok ({ratio:.1f}x spike, ${last:,.0f})"


def dip(rows, p):
    """Mean-reversion: pulled back a healthy amount off a recent local peak,
    now printing a green volume-backed bounce candle."""
    price = rows[-1][4]
    lookback = int(p.get("peak_lookback", 20))
    window = rows[-lookback:]
    peak = max(r[2] for r in window)
    if peak <= 0 or price <= 0:
        return False, "dip: bad price/peak"
    dd = 1 - price / peak
    lo, hi = p.get("min_drawdown", 0.18), p.get("max_drawdown", 0.45)
    if not (lo <= dd <= hi):
        return False, f"dip: drawdown {dd:.0%} outside {lo:.0%}-{hi:.0%}"
    last = rows[-1]
    if last[4] <= last[1]:
        return False, f"dip: last candle not green (dd {dd:.0%})"
    if last[5] < p.get("min_bounce_vol_usd", 1000):
        return False, f"dip: bounce vol ${last[5]:,.0f} thin"
    return True, f"dip ok (dd {dd:.0%}, green bounce ${last[5]:,.0f})"


_FAMILIES = {"sniper": sniper, "volume_anomaly": volume_anomaly, "dip": dip}


def check(family, rows, params):
    """(ok, reason) for a screen family. Thin candles fail closed."""
    fn = _FAMILIES.get(family)
    if fn is None:
        return True, "no screen"
    if not rows or len(rows) < 8:
        return False, f"{family}: thin candles ({len(rows) if rows else 0})"
    return fn(rows, params or {})
