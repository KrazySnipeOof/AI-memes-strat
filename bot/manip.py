from __future__ import annotations

"""Pure token manipulation/organic classifier (no I/O, no side effects).

Shared by manipscan.py (batch CLI over cached candles) and the live bot
(bot/main.py human-token entry gate). Kept dependency-free so importing it can
never touch the filesystem or cwd.

Candle format: [ts, o, h, l, c, v_usd], ascending.

Classes:
  RUG          nuke candle, or pumped >=2x then already back to <=12% of peak
  WASH_RAMP    bot staircase (Kaufman ER>=.70 + green>=.70 + up>=1.5x)
  SPIKE_DUMP   one candle is >=55% of all volume, or <10 active candles in <15m
  ORGANIC      none of the above - two-sided, sustained, choppy = real humans
"""

import statistics

VOL_FLOOR = 50.0  # USD; a candle below this had no real trading


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def metrics(candles: list):
    cs = [c for c in candles if c[4] > 0]
    if len(cs) < 8 or cs[0][4] <= 0:
        return None
    closes = [c[4] for c in cs]
    vols = [c[5] for c in cs]
    tot_v = sum(vols) or 1e-9
    active = [c for c in cs if c[5] >= VOL_FLOOR]
    span = active if len(active) >= 2 else cs
    lifespan_min = (span[-1][0] - span[0][0]) / 60
    green_frac = sum(1 for c in cs if c[4] > c[1]) / len(cs)
    red_vol_frac = sum(c[5] for c in cs if c[4] < c[1]) / tot_v
    net = closes[-1] - closes[0]
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))) or 1e-9
    er = abs(net) / path
    peak = max(c[2] for c in cs)
    rise = peak / closes[0] if closes[0] > 0 else 0.0
    end_from_peak = closes[-1] / peak if peak > 0 else 0.0
    max_cvol_frac = max(vols) / tot_v
    nuke = any(c[1] > 0 and c[4] / c[1] <= 0.30 and c[5] >= VOL_FLOOR for c in cs)
    sharp_legs = sum(1 for c in cs if c[1] > 0 and c[4] / c[1] >= 1.15 and c[5] >= VOL_FLOOR)
    return {"n": len(cs), "n_active": len(active), "lifespan_min": round(lifespan_min, 1),
            "green_frac": round(green_frac, 3), "red_vol_frac": round(red_vol_frac, 3),
            "er": round(er, 3), "rise": round(rise, 2), "end_from_peak": round(end_from_peak, 3),
            "max_candle_vol_frac": round(max_cvol_frac, 3), "nuke": nuke,
            "sharp_legs": sharp_legs}


def classify(candles: list, is_cluster: bool, up_to_ts: int | None = None):
    """Classify a token. With up_to_ts set, only candles at/before that time are
    used - the entry-time-honest view (no look-ahead into whether it rugged
    later). Without it, the full history is used (hindsight)."""
    if up_to_ts is not None:
        candles = [c for c in candles if c[0] <= up_to_ts]
    m = metrics(candles)
    if m is None:
        return None
    if m["nuke"] or (m["rise"] >= 2 and m["end_from_peak"] <= 0.12 and m["lifespan_min"] < 90):
        cls = "RUG"
    elif m["er"] >= 0.70 and m["green_frac"] >= 0.70 and m["rise"] >= 1.5:
        cls = "WASH_RAMP"
    elif m["max_candle_vol_frac"] >= 0.55 or (m["n_active"] < 10 and m["lifespan_min"] < 15):
        cls = "SPIKE_DUMP"
    else:
        cls = "ORGANIC"

    human = statistics.mean([
        _clamp((m["red_vol_frac"] - 0.10) / 0.30),           # two-sided flow
        _clamp(m["lifespan_min"] / 180),                      # sustained (up to 3h)
        _clamp(m["n_active"] / 60),                           # many active candles
        1 - _clamp((m["er"] - 0.2) / 0.6),                    # choppy, not a straight line
        1 - _clamp((m["max_candle_vol_frac"] - 0.3) / 0.4),   # not one whale candle
    ])
    manip = statistics.mean([
        _clamp((m["rise"] - 1.5) / 8),                        # big pump
        _clamp(m["sharp_legs"] / 8),                          # coordinated legs
        1.0 if is_cluster else 0.4,                           # smart-wallet co-accumulation
        _clamp((m["max_candle_vol_frac"] - 0.10) / 0.40),     # whale-driven candle
    ])
    return {"class": cls, "human": round(human, 3), "manip": round(manip, 3),
            "sweet": round(human * manip, 3), "is_cluster": is_cluster, **m}
