# Solana Memecoin Trading Bot

Automated momentum bot for Solana memecoins. Discovers new and trending pools, filters out
likely rugs and honeypots, enters on buy-pressure momentum, and manages exits with a
stop-loss plus a 2x/3x take-profit ladder capped at a 5x hard target. **Paper trading is the
default** — simulated fills on live Jupiter quotes, no wallet, no funds at risk.

## Read this first — honest expectations

The "2x–5x per trade" goal is implemented as **take-profit targets on winning trades**, not
an expected return. No bot earns 2x–5x per trade on average; anyone claiming otherwise is
selling something. The realistic profile of this strategy:

- **Most trades lose.** The stop-loss exists because the common outcome is -35%. A good
  outcome for this style is a minority of trades reaching 2x+ while stop-outs stay small.
- The math only works if `(win rate x avg win) > (loss rate x avg loss)`. That edge is not
  guaranteed to exist, and even a real edge disappears in bad market regimes.
- **Paper results are an upper bound.** Live fills suffer latency, failed transactions,
  MEV/sandwiching, and worse prices than the quote you saw.
- Most new memecoins are scams. The filters (RugCheck, liquidity, sell-count, round-trip
  routing) reduce that risk; nothing eliminates it. A token can rug after every check passes.
- Run paper mode for at least a few weeks and look at the actual win rate and PnL before
  even considering live mode. Trade only money you can afford to lose entirely.

This is not financial advice; it's a piece of software that automates a high-risk strategy.

## What it does each cycle (default: every 45s)

1. **Manage open positions** — values each position by what its tokens would actually fetch
   on Jupiter right now (so exit price-impact is priced in), then applies, in order:
   hard take-profit at 5x → take-profit ladder (sell half at 2x, half of the rest at 3x) →
   stop-loss at -35% → trailing stop (-25% off peak, after first take-profit) → time stop (4h).
2. **Discover candidates** — GeckoTerminal new + trending Solana pools.
3. **Filter** — pool age 10m–24h, liquidity $20k–$2M, FDV/liquidity ≤ 60x, 5m volume ≥ $2k,
   at least 3 sells in 5m (honeypot tell), sane buy/sell ratio.
4. **Entry signal** — positive but not vertical 5m momentum, buys outpacing sells.
5. **Deep checks** — RugCheck risk score; then wallet/insider screening (below); then a
   Jupiter round-trip quote (buy + immediate sell at position size) that rejects unroutable
   tokens, unsellable honeypots, and pools too thin for the position size.
6. **Enter** — max 1 new position per cycle, 3 concurrent, with a daily realized-loss
   circuit breaker (0.75 SOL) that halts new entries.

## Quickstart (paper mode)

```
cd solana-memebot
pip install -r requirements.txt
python run.py            # continuous paper trading; Ctrl+C to stop
python run.py --once     # single cycle, then exit
python run.py --report   # positions, win rate, PnL
```

State lives in `memebot.sqlite`; every decision and its reason is logged to `memebot.log`.

## Dashboard (NERV terminal)

An Evangelion-themed read-only control room, stdlib only (no extra installs):

```
python dashboard/server.py          # http://localhost:8700
python dashboard/server.py --port 9000 --config other.json
```

Auto-refreshes every 5s. Shows: MAGI panels (scanner stats, risk/circuit-breaker meter,
execution config), active positions as EVA units with sync-ratio bars and value-multiple
sparklines (parsed from the bot's `hold` log lines), win rate / PnL tiles, the last 10
closed trades, and a live log terminal. "COMBAT MODE" warning stripes appear only in live
mode. The dashboard never trades — it only reads the SQLite ledger and quotes Jupiter to
value open positions (cached 8s). Run it alongside `python run.py` in a second terminal.

## Configuration

Everything is in `config.json`. The knobs that matter most:

| Key | Default | Meaning |
|---|---|---|
| `position_size_sol` | 0.25 | SOL per trade |
| `max_positions` | 3 | concurrent positions |
| `exits.stop_loss_pct` | 35 | full exit below entry value |
| `exits.take_profits` | 2x/50%, 3x/50% | ladder: value multiple / fraction of remaining sold |
| `exits.hard_tp_multiple` | 5.0 | sell everything here |
| `exits.trailing_stop_pct` | 25 | after TP1, exit this far below peak |
| `exits.max_hold_min` | 240 | time stop |
| `risk.daily_loss_limit_sol` | 0.75 | realized daily loss that halts new entries |
| `safety.require_rugcheck` | true | reject tokens RugCheck can't score |
| `safety.max_roundtrip_loss_pct` | 12 | max buy+sell quote round-trip loss |

Filters are deliberately strict; expect many cycles with zero entries. Loosening them buys
more trades at the price of more rugs.

## Strategy books (one bankroll per strategy)

The bot can run research strategies beside the base one, each with its **own paper
account**: its own starting balance (1 SOL by default), its own positions, its own entry
rule and exit doctrine, its own daily-loss breaker. Cash is enforced — a book that has
spent its SOL stops trading until an exit returns some.

Nothing a book does touches the base strategy. Book positions live in the same sqlite
under their own `mode` (`book:BOUNCE`), which every ledger query already filters on, and
books ride the candidate stream the base scan already produced, so discovery costs
nothing extra.

Configure with the `books` block (`enabled: false` by default, which leaves the base
bot's path byte-identical):

| Key | Default | Meaning |
|---|---|---|
| `starting_balance_sol` | 1.0 | bankroll each book starts with |
| `position_size_sol` | base value | SOL per book trade |
| `max_positions` | base value | concurrent positions per book |
| `max_entries_per_cycle` | 1 | entries per book per cycle |
| `max_gate_checks_per_cycle` | 8 | gate lookups that cost an API call, per book per cycle |
| `max_backfills_per_cycle` | 4 | REST candle backfills all books share per cycle |
| `max_trigger_age_sec` | 300 | how stale a candle trigger may be and still be traded |
| `daily_loss_limit_sol` | base value | realized daily loss that halts a book |
| `strategies.<KEY>.enabled` | per book | turn an individual book on or off |

Books ship in `bot/books.py`; their entry rules are imported verbatim from the
pre-registered research modules, never re-typed:

| Key | Entry rule | Exits | Default |
|---|---|---|---|
| `BOUNCE` | loop8 P3 bounce — 55% off peak, credible vol ≥ $500, ≤1 nuke | stop 50 / arm 1.5x / trail 25 / hard 10x / 8h | on |
| `V1` | vol/MC churn — trailing 30m volume ≥ 0.5 × MC, ≤1 nuke | stop 60 / arm 1.5x / trail 25 / hard 20x / 8h | on |
| `H1` | base-entry population + distributed holders (≥50 holders, top10 ≤50%, top1 ≤20%) | stop 60 / arm 1.5x / trail 25 / hard 10x / 8h | on |
| `H0` | base-entry population, no holder gate (the control for H1) | as H1 | off |
| `H2` | loose holder gate (≥30 holders, top10 ≤65%, top1 ≤30%) | as H1 | off |

Books are **paper-only** — they refuse to start in live mode — and need the Birdeye
websocket feed, because every rule reads candles. The honest caveats are in the
`bot/books.py` docstring; the short version: a candle trigger must be fresh to be traded,
V1's launchpad-universe filter cannot be applied live, an unknown holder metric passes
rather than rejects, and fills are Jupiter-quoted instead of the backtest's candle model.

Self-check (offline, no network): `python booktest.py`.

## Wallet / insider screening

Each candidate's holder base is screened for rug-shaped ownership before entry
(`insiders` block in config):

| Check | Default | Rejects when |
|---|---|---|
| `max_insider_pct` | 15 | insider-flagged wallets hold more of the supply |
| `max_sniper_pct` | 20 | sniper wallets hold more (only when the source reports it) |
| `max_top10_pct` | 45 | top-10 holders (AMM/locker vaults excluded) control more |
| `max_creator_pct` | 10 | the deployer wallet still holds more |
| `min_holders` | 50 | fewer total holders |
| `require_data` | true | no wallet data is available at all |

**Smart-money screen (Birdeye):** with a `BIRDEYE_API_KEY` in `.env` (free tier), each
candidate's top-10 traders (24h) are profiled before entry (`smart_money` config block):
rejects tokens with fewer than 6 active top traders, one wallet above 45% of top-trader
USD volume (wash trading), buy-flow outside the 35–80% band (either top wallets dumping,
or a one-sided pump), or more than 3 wallets Birdeye tags as bundle-snipers. Fails open
by default (`require_data: false`) so a missing key or rate limit doesn't halt the bot.
Results are cached 10 minutes per token to respect free-tier limits.

**Data sources:** GMGN has no official API — its endpoints sit behind Cloudflare bot
management and return 403 to any non-browser client, so the bot tries it opportunistically
(`try_gmgn`) and gives up for the session after 3 failures. The working source is
**RugCheck's full report**, which runs its own wallet-graph analysis: per-holder insider
flags, insider network detection, the creator wallet, and AMM/locker tagging (so pool
vaults don't inflate concentration numbers). Caveat: insider % is computed over the top-20
holders RugCheck returns, so a widely-scattered insider network can understate — but
concentrated holdings, the kind that dump on you, are exactly what it catches.

## Going live (checklist, in order)

1. Weeks of paper results with a positive total PnL you've actually read (`--report`).
2. `pip install solders`
3. Create a **dedicated burner wallet**; fund it only with what the bot may trade.
4. Copy `.env.example` to `.env`; set `MEMEBOT_PRIVATE_KEY` and a dedicated RPC
   (`MEMEBOT_RPC_URL` — public mainnet RPC is not reliable enough for live).
5. Set `MEMEBOT_I_UNDERSTAND_THE_RISKS=yes`.
6. Start tiny: `position_size_sol: 0.05` or less.
7. `python run.py --live`

Live mode refuses to start unless both the flag and the env acknowledgment are present.

**Wallet pinning:** `wallet_pubkey` in `config.json` pins the expected trading wallet
(a public address — safe to store). Live mode refuses to start if the private key in
`.env` controls a different address, so a wrong or stale key can never trade. The private
key itself goes ONLY in your local `.env` — never share it with anyone or paste it into
any chat, site, or "support" form. The dashboard shows the pinned wallet's SOL balance
(read-only RPC lookup).

## Narrative targeting

Memecoin attention clusters around whatever the world is watching (`narrative` config
block). Candidates whose symbol matches a keyword are tried first each cycle
(`require: true` restricts entries to matches only — higher conviction, fewer trades).
The shipped keyword list targets the live narrative at time of writing (2026 World Cup
final week — 27% of all scanned candidates matched it). **Narratives rot**: when
attention moves on, stale keywords target dead tokens, so rotate the list with the news
cycle. `python backtest.py --narrative "kw1,kw2"` reports the subset separately so you
can check whether the narrative actually outperforms before chasing it.

## Backtesting

```
python backtest.py                  # ~3-5 min: fetches history, tests 3 exit variants
python backtest.py --max-tokens 80 --entry-age 20 --cost-pct 4
```

Every run also writes a **per-trade journal** (`reports/backtest_report.html`, path
configurable via `--report-file`) for the hybrid scalp-runner variant: one card per trade
with contract address (Solscan link), entry date/price, outcome and PNL, and a candle
chart showing the entry, shaded TP/SL zones, and a marker for every partial exit. Open it
in a browser; hover a chart for price/multiple readout.

Simulates the exit rules over historical pool OHLCV from GeckoTerminal (Axiom and GMGN
have no public APIs). Three cohorts: `db` (candidates the bot recorded itself — unbiased,
grows as the bot runs), `pump` (top pump.fun/pumpswap/raydium pools — moderately
survivor-biased), `trending` (heavily survivor-biased; upper bound only). Compares the
current 2x/3x ladder against a let-winners-run variant and a tight-scalp variant. Read
the caveats the tool prints; the win-rate/average-multiple tradeoff it shows is the real
constraint on any "high win rate AND high multiple" goal.

## Architecture

```
run.py               CLI entry (--once / --report / --live)
config.json          all tunables
bot/
  scanner.py         GeckoTerminal pool discovery
  safety.py          structural filters, RugCheck, Jupiter round-trip check
  strategy.py        entry signal + exit state machine
  jupiter.py         quote + swap-transaction API client
  execution.py       PaperBroker (simulated) / LiveBroker (solders + JSON-RPC)
  portfolio.py       SQLite positions/fills ledger, daily PnL, cooldowns
  books.py           per-strategy paper books: one enforced bankroll each
  main.py            cycle orchestration, logging, report
booktest.py          offline self-check for the strategy books
```

## Known limitations

- No MEV protection (Jito bundles) — live entries on hot tokens can be sandwiched.
- Positions are valued via Jupiter quotes; if Jupiter can't quote a token the bot can only
  wait and retry — a hard-rugged token has no exit, by definition.
- GeckoTerminal/Jupiter/RugCheck are free public APIs: rate limits and schema changes happen.
- Paper mode doesn't simulate failed transactions or quote-to-fill latency.

## Disclaimer

Educational software. You are solely responsible for anything it trades. Expect the
possibility of losing 100% of allocated funds.
