# Changelog

Notable changes, newest first. Entries before 2026-08-02 are reconstructed from
commit history and are summaries only.

## 2026-08-02 — Validation tooling: selection-bias haircut, split audit, ML gate

Three research-integrity tools. No strategy behaviour changed, no config edits,
no runners touched.

**Added `mtest.py`** — multiple-testing correction for the parameter sweeps.
Stdlib-only Bailey/López de Prado: expected-max Sharpe, deflated Sharpe ratio
(carrying skew and kurtosis, which matter on a return distribution floored at 0
with a fat right tail), Bonferroni and Benjamini-Hochberg. Usable as a library
(`mtest.print_haircut`) or a CLI against any finished result file. Validated
against synthetic ground truth: it rejects an 800-config family with zero true
edge and still detects a single planted +8pp config among 799 dead ones.

The winrate track is deliberately *shrinkage only*, not a pass/fail — across a
grid mixing exit families the winrate spread is structural rather than noise, so
a winrate significance test is ill-posed. Verdicts come from the Sharpe track.
`--breakeven-wr` supplies a real null where one is known.

Headline: **`sweep4`'s reported best winrate of 74.3% is 57.9% after correcting
for best-of-12,300.**

**Added `splitaudit.py`** — audits `sweep.token_bucket` for leakage. The split
hashes the mint, which buys independence of mint and nothing else. On the
10,040-token 60-day sample: 813 tickers straddle the split covering **7,731
tokens (77% of the sample)**; train and valid windows overlap **100%** (median
gap 0.6h — the same three days); 49% of consecutive listings within five minutes
land on opposite halves.

It then re-runs a probe strategy under four splits (hash / symbol-grouped /
chronological / chronological with a purged embargo band). BOUNCE-P3 moves from
a −0.027x train/valid gap under the incumbent hash split to −0.224x under a
purged chronological one — directionally what leakage predicts, but the
difference is inside one standard error at n=236. **The structural leakage is
real and large; it is not yet demonstrated to move the number.** Verdict text
says so rather than overclaiming.

**Added `mlgate.py` and `docs/ML-GATE.md`** — records and enforces the decision
to stay rules-only. Scans the tree for 17 watched ML packages and fails the run
on any hit (exit 1, usable as a pre-commit or CI check), then scores six
readiness preconditions against the live ledger.

Finding: the binding constraint is not the trade count (65 of 2000). The
`positions` table records outcomes only — no entry-time features — so **every
live trade is an unlabelled row**, and waiting produces more of them. Capturing
features at open time is the prerequisite.

**Modified `sweep.py`, `sweep2.py`, `sweep3.py`, `sweep3b.py`, `sweep4.py`,
`sweepvol.py`** — each prints the haircut and persists it under `"mtest"` in its
result JSON. `sweep.stats()` now emits `std`/`skew`/`kurt` via `mtest.moments`;
purely additive, existing readers of `n`/`wr`/`avg`/`median` are unaffected. The
deflated-Sharpe verdict needs `std`, so it becomes available on sweeps re-run
after this change; the winrate shrinkage works on the result files already on
disk.

## 2026-07-27 — Honest engine, strategy research, rug guards, multi-strategy dashboard

## 2026-07-24 — Paper-trial workstream: Birdeye WS feed, shadow safety gates, GitHub Pages publisher

## 2026-07-19 — Initial commit: Solana memecoin paper/live bot with NERV dashboard
