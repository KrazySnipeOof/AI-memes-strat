"""Per-trade HTML journal for backtest runs: one card per trade with contract
address, dates, outcome/PNL, and a candle chart with entry, TP/SL zones, and
every partial-exit marker. Self-contained file - open it in any browser."""
from __future__ import annotations

import html
import json
import os
import statistics
import time
from typing import List

UP, DOWN = "#00a869", "#ff3355"          # validated mark palette (dark surface)
AMBER, CYAN, INK2, INK3 = "#b37c12", "#2695c4", "#9a9aa8", "#5c5c6c"

EVENT_COLOR = {"TP1": UP, "TP2": UP, "HARD-TP": UP, "STOP": DOWN,
               "TRAIL": AMBER, "TIME": CYAN, "END": CYAN}


def _fp(p: float) -> str:
    return f"{p:.4g}"


def _hhmm(ts: int) -> str:
    return time.strftime("%H:%M", time.gmtime(ts))


def _downsample(candles: list, max_n: int = 200) -> list:
    if len(candles) <= max_n:
        return candles
    step = -(-len(candles) // max_n)
    out = []
    for i in range(0, len(candles), step):
        g = candles[i:i + step]
        out.append([g[0][0], g[0][1], max(c[2] for c in g), min(c[3] for c in g),
                    g[-1][4], sum(c[5] for c in g)])
    return out


def _window(candles: list, sim: dict) -> list:
    i0 = next((i for i, c in enumerate(candles) if c[0] > sim["entry_ts"]), 1)
    i1 = next((i for i, c in enumerate(candles) if c[0] >= sim["end_ts"]), len(candles) - 1)
    return candles[max(0, i0 - 12): min(len(candles), i1 + 25)]


def _svg(candles: list, sim: dict, exits: dict) -> str:
    sel = _downsample(_window(candles, sim))
    if len(sel) < 2:
        return "<p class='muted'>not enough candles to chart</p>"
    entry = sim["entry_price"]
    tp_price = entry * float(exits["take_profits"][0]["multiple"])
    sl_price = entry * (1 - exits["stop_loss_pct"] / 100)

    W, PL, PR, PT, PB = 840, 12, 748, 14, 208
    VT, VB, XL = 216, 258, 276
    lows = [c[3] for c in sel] + [sl_price * 0.96]
    highs = [c[2] for c in sel] + [tp_price * 1.05]
    ymin, ymax = min(lows), max(highs)
    if ymax <= ymin:
        ymax = ymin * 1.01 + 1e-12

    def x(i): return PL + (i + 0.5) * (PR - PL) / len(sel)
    def y(p): return PT + (ymax - p) * (PB - PT) / (ymax - ymin)

    def xt(ts):
        i = min(range(len(sel)), key=lambda j: abs(sel[j][0] - ts))
        return x(i)

    parts = [f'<rect x="0" y="0" width="{W}" height="290" fill="#0d0d13"/>']

    # TP / SL zones from entry to exit, like a TradingView long-position tool
    xe, xx = xt(sim["entry_ts"]), xt(sim["end_ts"])
    if xx - xe < 8:
        xx = xe + 8
    parts.append(f'<rect x="{xe:.1f}" y="{y(tp_price):.1f}" width="{xx - xe:.1f}"'
                 f' height="{max(1, y(entry) - y(tp_price)):.1f}" fill="{UP}" opacity="0.13"/>')
    parts.append(f'<rect x="{xe:.1f}" y="{y(entry):.1f}" width="{xx - xe:.1f}"'
                 f' height="{max(1, y(sl_price) - y(entry)):.1f}" fill="{DOWN}" opacity="0.10"/>')

    # candles
    bw = max(1.5, 0.62 * (PR - PL) / len(sel))
    for i, (ts, o, h, l, c, v) in enumerate(sel):
        col = UP if c >= o else DOWN
        cx = x(i)
        parts.append(f'<line x1="{cx:.1f}" y1="{y(h):.1f}" x2="{cx:.1f}" y2="{y(l):.1f}"'
                     f' stroke="{col}" stroke-width="1" opacity="0.85"/>')
        top, bot = max(o, c), min(o, c)
        parts.append(f'<rect x="{cx - bw / 2:.1f}" y="{y(top):.1f}" width="{bw:.1f}"'
                     f' height="{max(1, y(bot) - y(top)):.1f}" fill="{col}" opacity="0.95"/>')

    # volume
    vmax = max(c[5] for c in sel) or 1
    for i, c in enumerate(sel):
        vh = (VB - VT) * c[5] / vmax
        parts.append(f'<rect x="{x(i) - bw / 2:.1f}" y="{VB - vh:.1f}" width="{bw:.1f}"'
                     f' height="{vh:.1f}" fill="{INK3}" opacity="0.55"/>')

    # entry / TP / SL lines + right labels
    for price, col, dash, label in (
        (entry, INK2, "4 3", f"ENTRY {_fp(entry)}"),
        (tp_price, UP, "", f"TP {float(exits['take_profits'][0]['multiple']):.2g}x {_fp(tp_price)}"),
        (sl_price, DOWN, "", f"SL -{exits['stop_loss_pct']:.0f}% {_fp(sl_price)}"),
    ):
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        parts.append(f'<line x1="{PL}" y1="{y(price):.1f}" x2="{PR}" y2="{y(price):.1f}"'
                     f' stroke="{col}" stroke-width="1.4"{dash_attr} opacity="0.9"/>')
        parts.append(f'<text x="{PR + 6}" y="{y(price) + 3.5:.1f}" fill="{col}"'
                     f' font-size="10.5">{label}</text>')

    # entry marker + exit-event markers
    parts.append(f'<path d="M {xe:.1f} {y(entry) + 12:.1f} l -6 10 l 12 0 z" fill="{CYAN}"/>')
    for ev in sim["events"]:
        col = EVENT_COLOR.get(ev["kind"], INK2)
        ex, ey = xt(ev["ts"]), y(entry * ev["mult"])
        parts.append(f'<path d="M {ex:.1f} {ey - 12:.1f} l -6 -10 l 12 0 z" fill="{col}"/>')
        parts.append(f'<text x="{min(ex + 6, PR - 80):.1f}" y="{ey - 16:.1f}" fill="{col}"'
                     f' font-size="10">{ev["kind"]} {ev["portion"] * 100:.0f}%</text>')

    # time axis
    for i in (0, len(sel) // 3, 2 * len(sel) // 3, len(sel) - 1):
        parts.append(f'<text x="{x(i):.1f}" y="{XL}" fill="{INK3}" font-size="10"'
                     f' text-anchor="middle">{_hhmm(sel[i][0])}</text>')
    parts.append(f'<text x="{PL}" y="{PT - 2}" fill="{INK3}" font-size="10">{_fp(ymax)}</text>')
    parts.append(f'<text x="{PL}" y="{PB + 10}" fill="{INK3}" font-size="10">{_fp(ymin)}</text>')

    data = json.dumps([[c[0], c[4]] for c in sel])
    return (f'<svg class="chart" viewBox="0 0 {W} 290" data-t=\'{data}\''
            f' data-entry="{entry}" data-x0="{PL}" data-x1="{PR}">' + "".join(parts) + "</svg>")


def _stats_line(details: List[dict]) -> str:
    mults = [d["sim"]["multiple"] for d in details]
    if not mults:
        return "no trades"
    wins = [m for m in mults if m > 1.0]
    return (f"{len(mults)} trades &middot; win rate {100 * len(wins) / len(mults):.0f}%"
            f" &middot; avg {statistics.mean(mults):.2f}x"
            f" &middot; median {statistics.median(mults):.2f}x"
            f" &middot; expectancy {100 * (statistics.mean(mults) - 1):+.1f}%/trade")


def _card(d: dict, variant_name: str, nar_keys: List[str], cost_pct: float) -> str:
    tok, sim, exits = d["token"], d["sim"], d["exits"]
    pnl = (sim["multiple"] - 1) * 100
    win = sim["multiple"] > 1.0
    held = (sim["end_ts"] - sim["entry_ts"]) / 60
    nar = "HIT" if any(k in tok.symbol.lower() for k in nar_keys) else "&mdash;"
    sym = html.escape(tok.symbol)
    return f"""
<div class="card">
  <div class="chead"><span class="sym">{sym}</span>
    <span class="chip {'win' if win else 'loss'}">{'WIN' if win else 'LOSS'} {pnl:+.0f}%</span></div>
  <div class="props">
    <div><span class="k">Contract Address</span><span class="v mono">
      <a href="https://solscan.io/token/{tok.mint}" target="_blank">{tok.mint}</a></span></div>
    <div><span class="k">Pool</span><span class="v mono">
      <a href="https://www.geckoterminal.com/solana/pools/{tok.pool}" target="_blank">{tok.pool}</a></span></div>
    <div><span class="k">Date (entry, UTC)</span><span class="v">{time.strftime('%Y-%m-%d %H:%M', time.gmtime(sim['entry_ts']))}</span></div>
    <div><span class="k">Cohort</span><span class="v">{tok.cohort}</span></div>
    <div><span class="k">Narrative</span><span class="v">{nar}</span></div>
    <div><span class="k">Strategy</span><span class="v">{html.escape(variant_name)}</span></div>
    <div><span class="k">Entry price</span><span class="v">${_fp(sim['entry_price'])}</span></div>
    <div><span class="k">Outcome</span><span class="v">{sim['multiple']:.2f}x &middot; exit: {sim['reason']} &middot; held {held:.0f}m</span></div>
    <div><span class="k">PNL (after {cost_pct:.0f}% costs)</span><span class="v {'pos' if win else 'neg'}">{pnl:+.1f}%</span></div>
  </div>
  {_svg(d['candles'], sim, exits)}
</div>"""


def render_report(by_variant: dict, nar_keys: List[str], cost_pct: float, path: str) -> None:
    sections, toc = [], []
    for vi, (name, details) in enumerate(by_variant.items()):
        if not details:
            continue
        anchor = f"strategy-{vi}"
        toc.append(f'<a href="#{anchor}">{html.escape(name)}</a>')
        cards = "".join(
            _card(d, name, nar_keys, cost_pct)
            for d in sorted(details, key=lambda d: -d["sim"]["multiple"])
        )
        sections.append(
            f'<details id="{anchor}"{" open" if vi == 0 else ""}>'
            f'<summary><b>{html.escape(name)}</b><span class="muted"> &mdash; {_stats_line(details)}'
            f' &mdash; click to expand</span></summary>{cards}</details>'
        )

    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Backtest journal — all strategies</title>
<style>
  body{{background:#0a0a0e;color:#e8e8ea;font-family:Consolas,'Cascadia Mono',monospace;
       margin:0;padding:24px;line-height:1.5}}
  .wrap{{max-width:920px;margin:0 auto}}
  h1{{font-size:22px;margin:0 0 4px}} .muted{{color:{INK2};font-size:12.5px}}
  .card{{background:#101017;border:1px solid #2a2a38;margin:18px 0;padding:14px 16px}}
  .chead{{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}}
  .sym{{font-size:24px;font-weight:bold;letter-spacing:1px}}
  .chip{{padding:2px 10px;border:1px solid;font-size:12px;letter-spacing:1px}}
  .chip.win{{color:#34ffb0;border-color:{UP}}} .chip.loss{{color:#ff8ba0;border-color:{DOWN}}}
  .props div{{display:flex;gap:10px;padding:2px 0;border-bottom:1px dotted #1e1e2a;font-size:12.5px}}
  .props .k{{color:{INK3};width:190px;flex:none;letter-spacing:1px}}
  .props .v{{overflow-wrap:anywhere}} .mono{{font-size:11.5px}}
  .pos{{color:#34ffb0}} .neg{{color:#ff8ba0}}
  a{{color:{CYAN};text-decoration:none}} a:hover{{text-decoration:underline}}
  svg.chart{{width:100%;height:auto;display:block;margin-top:10px}}
  .caveats{{border:1px solid #2a2a38;padding:12px 16px;color:{INK2};font-size:12px;margin-top:22px}}
  #tip{{position:fixed;display:none;background:#000;border:1px solid {AMBER};color:#e8e8ea;
       padding:3px 8px;font-size:11px;pointer-events:none}}
  details{{border:1px solid #2a2a38;margin:14px 0;padding:0 12px}}
  summary{{cursor:pointer;padding:10px 4px;font-size:14px}}
  .toc{{display:flex;flex-wrap:wrap;gap:14px;font-size:12px;margin:10px 0}}
</style></head><body><div class="wrap">
<h1>Trade journal — every strategy, every trade</h1>
<p class="muted">Generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())} &middot; same tokens,
same entries, different exit rules — each section documents all of one strategy's trades</p>
<div class="toc">{' '.join(toc)}</div>
{''.join(sections)}
<div class="caveats"><b>Caveats:</b> simulated fills at exact trigger prices on 5-minute candles
(1-hour fallback for old pools); {cost_pct:.0f}% round-trip cost haircut; entry approximated by
pool age + early volume (live safety screens can't be reconstructed historically); trending/pump
cohorts are survivor-biased. Live results will be worse. Not financial advice.</div>
</div>
<div id="tip"></div>
<script>
const tip = document.getElementById('tip');
document.addEventListener('mousemove', e => {{
  const s = e.target.closest && e.target.closest('svg.chart');
  if (!s) {{ tip.style.display = 'none'; return; }}
  const d = JSON.parse(s.dataset.t), entry = +s.dataset.entry;
  const r = s.getBoundingClientRect();
  const x0 = +s.dataset.x0 / 840 * r.width, x1 = +s.dataset.x1 / 840 * r.width;
  const f = Math.min(1, Math.max(0, (e.clientX - r.left - x0) / (x1 - x0)));
  const [ts, c] = d[Math.round(f * (d.length - 1))];
  const t = new Date(ts * 1000).toISOString().slice(11, 16);
  tip.textContent = t + ' UTC  $' + c.toPrecision(4) + '  (' + (c / entry).toFixed(2) + 'x)';
  tip.style.left = (e.clientX + 14) + 'px'; tip.style.top = (e.clientY - 28) + 'px';
  tip.style.display = 'block';
}});
</script></body></html>"""

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
