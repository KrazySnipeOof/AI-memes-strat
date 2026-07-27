#!/usr/bin/env python
"""The three validated strategies, in ONE place.

This module is the single definition of each screen. `validate.py` scores them
and `bot/birdeye_ws.py` trades them through the same `screen_ok()` call, so the
live gate and the backtest cannot drift apart - which is exactly how
`tradecards.py` ended up disagreeing with the gate's own verdict before P1.

Provenance (see STRATEGY-SEARCH-2026-07-27.md for the full evidence):
  found by  quest.py -> search.py -> finalists.py -> pick.py
  scored by validate.py against backtest.simulate() at shipped defaults
  bar met   mean >= 2.0x per trade AND >= 60% of Monte Carlo paths profitable

Every screen reads ONLY pre-entry candles. All three enter at 3 minutes of
token age and hold 5-10 minutes; see the entry-age caveat in the doc - the edge
does not survive to 5 minutes of entry age.
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import quest  # noqa: E402

# r10 ladder: bank half at 10x (which also arms the trailing stop), ride the
# rest to a 50x cap, 60% stop, exit on time otherwise.
_R10 = [{"multiple": 10.0, "sell_fraction_of_remaining": 0.5}]

# Keys are the `websocket.setup_family` value, so they must be lowercase and
# must not collide with the existing "base"/"bounce" families or with the older
# dip bot in config.dip.json - hence the q- (quest) namespace.
STRATEGIES = {
    "qvwap": {
        "label": "BELOW-VWAP",
        "why": "trading under its volume-weighted average price - everyone who "
               "bought the first three minutes is under water",
        "age": 3,
        "conds": ["vwap_ratio<0.8", "peak_age_frac>=0.6666666666666666"],
        "exits": {"stop_loss_pct": 60, "take_profits": _R10,
                  "hard_tp_multiple": 50.0, "trailing_stop_pct": 20,
                  "max_hold_min": 10},
    },
    "qdip": {
        "label": "DIP",
        "why": "a big green candle has printed and price has since fallen to "
               "under 45% of its own high - buy the giveback, not the spike",
        "age": 3,
        "conds": ["frac_of_peak<0.45", "best_body>=1.546375842675467"],
        "exits": {"stop_loss_pct": 60, "take_profits": _R10,
                  "hard_tp_multiple": 50.0, "trailing_stop_pct": 20,
                  "max_hold_min": 10},
    },
    "qsurge": {
        "label": "SURGE",
        "why": "volume is accelerating into the entry, price is off its VWAP "
               "but well up off the floor - flow arriving on a base",
        "age": 3,
        "conds": ["vol_accel>=1", "vwap_ratio<0.8988355094718489",
                  "x_from_low>=1.6707111705762403"],
        "exits": {"stop_loss_pct": 60, "take_profits": _R10,
                  "hard_tp_multiple": 50.0, "trailing_stop_pct": 50,
                  "max_hold_min": 5},
    },
}

_OPS = {">=": lambda a, b: a >= b, "<": lambda a, b: a < b}


def _parse(cond: str):
    for op in (">=", "<"):
        if op in cond:
            name, _, val = cond.partition(op)
            return quest.FI[name], _OPS[op], float(val)
    raise ValueError(cond)


_PARSED = {k: [_parse(c) for c in v["conds"]] for k, v in STRATEGIES.items()}


def screen_ok(pre, entry_ts: int, entry: float, name: str) -> bool:
    """True if `name`'s entry screen passes on these pre-entry candles.

    `pre` is a list of (ts, o, h, l, c, v) at or before entry_ts, ascending -
    the same slice `backtest.simulate()` derives internally.
    """
    if name not in _PARSED or len(pre) < 3 or entry <= 0:
        return False
    a = np.asarray(pre, dtype=np.float64)
    age = (entry_ts - int(a[0, 0])) / 60.0
    f = quest.features(a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5],
                       entry, age)
    return all(op(f[i], v) for i, op, v in _PARSED[name])


def describe(name: str) -> str:
    s = STRATEGIES[name]
    return (f"{s['label']} ({name}): enter at {s['age']}m if "
            f"{' & '.join(s['conds'])}; exits {s['exits']}")


if __name__ == "__main__":
    for k in STRATEGIES:
        print(describe(k))
