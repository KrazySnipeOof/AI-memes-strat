# HANDOFF — solana-memebot strategy optimization session (2026-07-19)

## 1. Goal

Kenny asked for a new trading strategy for the Solana memecoin bot targeting **60% winrate and +100% average gain per trade** (avg 2.0x multiple), then said: **"keep optimizing until the numbers are hit, and no cheating either."** The working interpretation (stated to Kenny, not objected to): optimize honestly toward the target, and if it is not reachable without cheating, prove that and present the best honestly-achievable config instead of manufacturing numbers.

**Immediate sub-goal when session ended:** a background run of `python backtest.py --max-tokens 150` was fetching OHLCV history to grow the sample from ~49 to ~130 tokens, after which `sweep.py` gets re-run on the larger sample to confirm or overturn the round-1 conclusion (target unreachable).

**Constraints and decisions locked this session:**
- **No cheating, enforced in code** — Kenny explicitly demanded this. Concretely: (a) the survivor-biased `trending` cohort is EXCLUDED from the optimization objective in sweep.py (shown as reference only); (b) train/validation split of tokens, deterministic by `sha1(mint)[0] % 2`, and a config only "hits target" if it clears 60% WR AND 2.0x avg on BOTH halves; (c) conservative candle rule kept (stop checked before TP within a candle, in `backtest.simulate`); (d) 4% round-trip cost haircut kept. Do not relax any of these.
- **No fabricated or cherry-picked numbers.** sweep.py prints a multiple-testing warning; the validation column is what counts.
- **Round-1 verdict (25 fair tokens, 10,080 combos): ZERO combos hit the target on both halves.** Honest frontier ≈ 43–62% WR with 1.17–1.33x avg. This is preliminary due to sample size — that's why the 150-token fetch was started.
- New strategy is named **"asym-runner"**: stop 30%, bank 50% at 1.35x (locks the trade as a net win once touched: 0.675 + 0.5·1.35·0.65 ≈ 1.11x pre-cost), trail remaining 35% off peak, hard cap 12x, max hold 480 min. It lives as backtest variant E and as `config.asym.json`.
- `config.asym.json` uses **separate db/log** (`memebot-asym.sqlite`, `memebot-asym.log`) so paper results never mix with the main config's portfolio. Keep it that way.

## 2. Current state of the code

**Works, verified this session:**
- `python backtest.py --max-tokens 60` ran to completion (exit 0). Results with new variant E included:
  - A (current config.json exits): 45 trades, 51% WR, avg 1.27x, expectancy +26.5%/trade; pump cohort 45% WR / 1.23x.
  - E (asym-runner): 45 trades, **53% WR, avg 1.29x, +28.8%/trade** (best of the five variants); pump cohort 45% WR / 1.16x; trending 61% / 1.41x.
  - Cohorts collected: pump 36, trending 24, **db 0** (bot's own scanner DB had no usable candidates — the only unbiased cohort is empty until the bot runs more).
- `python sweep.py --min-train 8` ran to completion (~10,080 combos over 49 cached tokens, a couple of minutes, pure CPU after ~20s of list fetches). Output verified; full results at `reports\sweep_results.json`.
- Per-trade journal from backtest at `reports\backtest_report.html`.

**In flight / incomplete:**
- Background process `python backtest.py --max-tokens 150` (started from Claude session, task id bomkyin05) was still running at handoff. Its stdout buffers, so its output file looked empty; progress is gauged by cache file count: `(Get-ChildItem C:\Users\kensm\solana-memebot\.ohlcv_cache | Measure-Object).Count` — was 49 at handoff, should reach ~100–140 when done. If the process died with the session, just re-run `python backtest.py --max-tokens 150` — already-cached tokens are free (6h disk cache, `.ohlcv_cache\`, TTL in `backtest.py` `CACHE_TTL`), only new tokens spend rate budget (~2.2s/call, GeckoTerminal ~27 req/min).
- The final re-sweep on the bigger sample has NOT been run yet.
- The final verdict message to Kenny has NOT been delivered yet.

**Git state:** `C:\Users\kensm\solana-memebot` is **not a git repository** (`git status` → exit 128). No version control; be careful with destructive edits.

**Commands:**
- Deps: already installed and working (Python on PATH, `requests`; bot modules import fine). No install step needed.
- Backtest: `python C:\Users\kensm\solana-memebot\backtest.py --max-tokens 150`
- Sweep: `python C:\Users\kensm\solana-memebot\sweep.py --min-train 20` (use `--min-train 8` only if fair sample stays small)
- Paper-run the new strategy: `python C:\Users\kensm\solana-memebot\run.py --config config.asym.json` (paper mode is default; `--once` for single cycle; `--report` for portfolio report)
- Both scripts `os.chdir` to the repo dir themselves; run from anywhere.

**Environment:** Windows 11, PowerShell 5.1. Note: `--report-file ""` cannot be passed from PowerShell (see Failed attempts). GeckoTerminal API needs no key. Nothing else was installed or configured this session.

## 3. Files being actively edited

- `C:\Users\kensm\solana-memebot\backtest.py` — **complete.** Added variant E "asym-runner (bank 50% @1.35x, trail 35% to 12x)" to the `variants` list in `main()` (right after variant D, with a comment explaining the 1.11x-precost lock-in math). No other changes.
- `C:\Users\kensm\solana-memebot\sweep.py` — **new file, complete, verified working.** Exit-parameter sweep over cached OHLCV. Imports `backtest` and reuses `plain_session`, `collect_tokens`, `fetch_candles` (cache-hit path only — it skips tokens with no cache file so it never spends rate budget), and `simulate`. Grid: entry_age {10,20}, stop {25,30,35,40}, tp1_mult {1.25…2.0}, tp1_frac {0,0.3,0.5,0.6,0.75}, tp2 {none,(3.0,0.3),(4.0,0.25)}, trail {25,30,35,45}, hard {8,12,20}, hold {480} = 10,080 combos. Key functions: `token_bucket(mint)` (train/valid split), `hits(r)` (the both-halves target test), `frontier()` (Pareto extraction). Writes `reports\sweep_results.json`.
- `C:\Users\kensm\solana-memebot\config.asym.json` — **new file, complete.** Copy of config.json with exits = asym-runner params and separate `db_path`/`log_path`. Not yet paper-traded.
- `C:\Users\kensm\solana-memebot\reports\sweep_results.json` — generated output (round 1). Will be overwritten by the next sweep run.

**Do NOT touch:** everything under `bot\` (working live-bot code: config.py, strategy.py, scanner.py, safety.py, execution.py, portfolio.py, main.py, etc.), `config.json` (Kenny's active paper config), `memebot.sqlite` (live paper portfolio + the future unbiased `db` cohort source), `dashboard\`.

## 4. Failed attempts — do not repeat

- **PowerShell + `--report-file ""`:** `python backtest.py --max-tokens 150 --report-file ""` failed exit 2 with verbatim error: `backtest.py: error: argument --report-file: expected one argument`. PowerShell swallows the empty string before argv. Just omit the flag (default HTML journal is harmless).
- **Reading background-task stdout for progress:** the task output file stays empty until the Python process flushes/exits (block buffering when redirected). Dead end for monitoring — count `.ohlcv_cache` files instead.
- **Round-1 optimization toward the target (the important one):** 10,080 exit-rule combos on 25 fair-cohort tokens (15 train / 10 valid): **zero** hit 60% WR + 2.0x avg on both halves. Best honest rows: `age20 stop40 tp1 1.5x/0% trail25` family → train 43% WR / 1.17x, valid 62% / 1.30x (n=8). The winrate↔avg trade-off is structural: tighter TP1 raises WR but caps avg; wider trail/higher cap raises avg but drops WR. The ONLY row brushing the target was trending-cohort-only (65% WR / 2.22x with hard 20x) — that cohort is survivor-biased and excluded by Kenny's no-cheating constraint. **Verdict: exit-tuning-only path to 60%/2.0x is almost certainly dead; awaiting the 150-token sample to confirm. Do not "fix" this by including trending in the objective, dropping the validation requirement, or shaving the cost haircut — that is the cheating Kenny forbade.**
- Also note: 60% WR × 2.0x avg = +100% expectancy per compounding trade. No real strategy sustains that; treat the target as aspirational and report honest numbers.

## 5. Next steps

1. **Check whether the 150-token fetch finished:** `(Get-ChildItem C:\Users\kensm\solana-memebot\.ohlcv_cache | Measure-Object).Count`. If ~49 and no `backtest.py` python process is running (`Get-Process python*`), re-run `python backtest.py --max-tokens 150` (background it; several minutes; cached tokens are free).
2. **Re-run the sweep on the larger sample:** `python sweep.py --min-train 20`. Compare the "hitting on BOTH halves" count and the Pareto frontier against round 1 (documented above and in `reports\sweep_results.json` before it's overwritten — archive it first if you want the round-1 numbers: copy to `reports\sweep_results_round1.json`).
3. **Deliver the verdict to Kenny.** Expected outcome (unless the frontier moves dramatically): the target is not honestly reachable by exit tuning; present the best both-halves-validated config, its real WR/avg/expectancy, and the exact gap to 60%/2.0x. Per Kenny's instructions the message must start with "Kenny" and acknowledge the prompt-optimization step (see his global CLAUDE.md).
4. **If a config does validate well**, update `config.asym.json` exits to the winner and tell Kenny it's ready to paper-trade side-by-side: `python run.py --config config.asym.json`. The live paper A/B (main config vs asym config, separate sqlite DBs) is the honest forward-test — backtest numbers are an upper bound (fills at trigger price, no MEV/latency; entry filters only approximated by age+volume).
5. **Entry-side selection is the one unexplored honest lever** (narrative keywords, buy/sell edge, liquidity bands cannot be reconstructed from GeckoTerminal history — see backtest.py CAVEATS). If Kenny wants to keep pushing after the verdict, the path is: let the bot run so `memebot.sqlite` accumulates candidates (the unbiased `db` cohort), then re-sweep including entry variations on that cohort.

**Open questions only Kenny can answer:**
- If (as expected) 60%/+100% is confirmed unreachable without cheating: adopt the best honest config into `config.asym.json` and paper-trade it, or keep iterating on entry-side ideas?
- Whether to put `solana-memebot` under git — it currently has no version control and this session added/modified files with no way to diff/revert.
