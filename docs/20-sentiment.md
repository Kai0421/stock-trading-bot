# Stocktwits sentiment shadow log

Research only. `bot.sentiment` records community sentiment and message volume for the
S&P 100 universe and everything we hold, flags extremes, and keeps a short watchlist. It
never touches the broker, and a test fails if any bot module imports it.

## Why a Claude job collects it

Stocktwits blocks the bot's own requests (HTTP 403, Cloudflare). Only the Stocktwits
connector in the owner's Claude account can read it. `prompts/sentiment_collect.md` is the
collector: it reads the data, writes a JSON file and runs `python -m bot.sentiment ingest`.
The scoring and the watchlist are plain Python; the model never decides anything.

## What is stored (`data/sentiment/`, git-ignored)

- `snapshots.jsonl`: one line per stock per run: time, price (full pulses only), community
  score 0-100 (normalised against the stock's own history, so 39 can sit beside 72% bullish
  posts), message volume 0-100, and a net score (-1..1) from posts users tagged Bullish or
  Bearish in the last 30 minutes (one vote per user, posts tagging more than 3 tickers
  dropped, engagement weight capped at 3).
- `watchlist.json`: stocks that signalled. Forgotten after 5 trading days or 2 neutral days
  (score 40-60) in a row, never while held.

## Signals (hypotheses, not tuned; `Thresholds`)

- `buy_watch` (not held): score >= 75, volume >= 60, >= 3 tagged posts, net > 0.
- `sell_warning` (held): score <= 30, volume >= 50, net <= -0.2.

## Cost

A light pass is two small calls per stock (about 230 calls for the S&P 100 universe); full pulses (about 5k
tokens each) are limited to held and extreme stocks.

## Sentiment exit (opt-in, sell only)

`SENTIMENT_EXIT=true` makes the swing-mode `manage` job (every 30 minutes, 10:00-15:30 ET)
sell a stock we hold when its newest snapshot is a `sell_warning`. Path: `Gatherer.sentiment`
reads the file (fresh snapshots only), `decide.sentiment_exits` decides (pure), the existing
`Executor.exit` sells: cancel the stop, market sell, put the stop back if the sell fails.

- Fails closed: missing, unreadable or older than `SENTIMENT_MAX_AGE_MIN` (90) means no sell.
- The bot's own book decides what is held; the collector's `held` flag is ignored.
- Needs a full pulse (the posts) for the stock, so the collector must pull one for every held
  symbol, at least every 30 minutes while the market is open.
- Skipped when the position already has a pending exit, an exit sent in this run, or a
  working sell order other than its stop; one sell per stock per day (client order id
  `<day>-<SYMBOL>-exit-sentiment`).
- Paper only like the rest of the bot; `DRY_RUN` still sends nothing. The exit is journalled
  with reason `sentiment`, so it counts in the live results for its signature.
- **Not used in overnight mode** (`manage` is a swing job): overnight positions already sell
  at the next open.

## Path to trading

None yet. Judge the logged history offline first: join snapshots to forward returns from
Alpaca bars and test whether `buy_watch` / `sell_warning` predict anything after costs, at
the 60% gate with n >= 100 (AGENTS.md). A sell-only mode is the cheaper first step, since
selling reduces risk; a buy mode needs the gate. Either needs an owner decision in
`memory/decisions.md`.
