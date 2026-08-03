# Decision record: no machine learning in this bot yet

**Status:** active
**Decided:** 2026-08-02
**Enforced by:** `mlgate.py` (run it; `--strict` for CI)

## Decision

This repo trades on rules. It depends on `requests`. No model is fitted
anywhere in the tree, and none will be until the preconditions below are met.

This is a position, not an oversight. It gets written down because the
alternative is drift: a dependency arrives during an experiment, a classifier
gets fitted on whatever the ledger holds that week, and the resulting book
cannot be distinguished from a memorised sample after the fact.

## Why not now

Three reasons, in order of how binding they are.

**There is no training set.** The `positions` table records outcomes —
`sol_spent`, `sol_received`, `peak_multiple`, `exit_reason`. It records nothing
about the state of the token at the moment of entry. Every live trade is
therefore an unlabelled row. This is the first problem and it is not solved by
waiting; it is solved by writing entry-time features into the ledger at open
time. Until that happens the trade count is measuring the wrong thing.

**The sample is three orders of magnitude too small.** 65 closed live trades
across the fleet, spanning 8.7 days. A model with even a handful of free
parameters will fit that sample exactly and tell you nothing. The backtest
sample is larger, but `splitaudit.py` shows 77% of its tokens belong to a
ticker that appears on both sides of the train/valid split, and 100% time
overlap between the halves — so its effective sample is smaller than its row
count suggests.

**The failure mode is already known.** The last several research rounds were
won by finding measurement bugs, not by finding signal: the data-end censoring
bug that turned 1.607x into 0.741x, the `vol_accel` leg that passed 100% of
tokens at its own entry age, the copy book that had taken zero copy entries.
Adding a model to a measurement stack with those properties buys a more
confident wrong answer. Fix the measurement first — that is what `mtest.py`
and `splitaudit.py` are for.

## Preconditions

`mlgate.py` scores these. All must hold.

| | Precondition | Why |
|---|---|---|
| P1 | The target is **survival**, not price | "Does this token reach +X% before −Y%" is a well-posed binary label the exit ladder can act on. Predicting the next price level is the classic trap — the model learns to output ≈ the last price and scores beautifully. |
| P2 | Entry-time features recorded per live trade | Without a feature vector at open time there is nothing to learn from. Requires a schema change to `positions`. |
| P3 | ≥ 2000 labelled live trades | Below this, any model beats the rules on the sample and loses off it. |
| P4 | ≥ 60 days of coverage | One regime is not a sample of regimes. |
| P5 | Selection-bias haircut in the loop | A model is one more config on the leaderboard. It must clear `mtest.py`'s deflated Sharpe against the trial count, same as every hand-tuned ladder. |
| P6 | Split audited for ticker and time leakage | `splitaudit.py` must have been run and its findings addressed, or the holdout is not a holdout. |

## What "ready" would look like

Not a framework. A gradient-boosted binary classifier on survival, trained on
the live ledger's own features, evaluated on a **pre-registered**, purged,
ticker-grouped chronological holdout, and required to beat the incumbent
rules-based book *after* the `mtest.py` haircut. If it cannot clear that bar it
does not ship, and the rules stay.

## Watched packages

`torch`, `tensorflow`, `keras`, `sklearn`, `xgboost`, `lightgbm`, `catboost`,
`jax`, `flax`, `transformers`, `gym`, `gymnasium`, `stable_baselines3`, `ray`,
`optuna`, `statsmodels`.

`matplotlib`, `pandas`, `numpy` and `scipy` are **not** watched — they are
analysis tools and carry no modelling claim. Note that `numpy`/`scipy` are
present in the environment but absent from `requirements.txt`; `mtest.py` and
`splitaudit.py` are stdlib-only so that stays true.

## Reversing this

Change the decision here first, then the code. `mlgate.py` failing is a signal
that the two have diverged, not a nuisance to silence.
