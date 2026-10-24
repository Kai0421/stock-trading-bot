# Analyst desk

A free, rule-based take on [TradingAgents](https://github.com/TauricResearch/TradingAgents).
Five analysts each read one data source, and a judge combines them into one rating:
Buy, Overweight, Hold, Underweight or Sell. No LLM, no API costs, and the same inputs always
give the same rating.

It comes in two forms:

- **Research report** (`python -m bot.desk`): all five analysts for one stock and day. The
  engine doesn't use it, and nothing in `bot.desk` places, changes or cancels orders.
- **Strategy `analyst_desk`** (docs/12-strategies.md): the two analysts that only need
  daily bars, behind the pluggable `Strategy` interface, so it can be backtested and
  compared like any other strategy. Trading it follows the usual switch: backtest through
  the probability gate, a `memory/decisions.md` entry, then `STRATEGY=analyst_desk`.

```bash
uv run python -m bot.desk AAPL                          # premarket view for today
uv run python -m bot.desk AAPL --date 2026-09-15 --json
uv run python -m bot.desk AAPL --out reports/desk/AAPL.md
```

## Analysts

| Analyst | Source | Bullish points | Bearish points | Risk flag |
|---|---|---|---|---|
| technical | Alpaca daily bars | above 200-day SMA, 50 > 200 SMA, +10% in 6 months, RSI(14) < 30 in an uptrend | the opposite, RSI(14) > 75, > 20% below 52-week high, ATR > 4% of price | `high_volatility` |
| market | SPY bars | SPY above its 200-day SMA, 50 > 200, up over 3 months | the opposite | `market_downtrend` |
| fundamentals | SEC EDGAR XBRL (10-K) | revenue growth, net margin, diluted EPS growth | revenue decline, net loss, EPS decline | |
| insiders | SEC Form 4 / 13D (`bot.altdata`) | insider cluster buys, CEO/CFO buys, activist 13D | heavy officer selling outside 10b5-1 plans | |
| news | Alpaca News headlines, last 3 sessions | positive headline words (beats, upgrade, raises guidance) | negative words; major news (M&A, FDA, probes, guidance cuts) | `news_shock` |

Each analyst scores from -1 to +1 by adding its points (weights in `bot/desk/analysts.py`).
An analyst without data is marked unavailable and left out. It never guesses.

## The judge (`bot/desk/judge.py`)

- **Score:** the weighted average of available analysts: technical 30%, fundamentals 25%,
  market, insiders and news 15% each.
- **Rating:** Buy ≥ 0.5, Overweight ≥ 0.2, Hold > -0.2, Underweight > -0.5, else Sell.
- **Risk caps** (applied after the score; a cap never makes a rating more bullish):
  - Fewer than 3 analysts had data: at most Hold. If in doubt, don't trade.
  - `news_shock`: at most Underweight.
  - `market_downtrend` or `high_volatility`: at most Hold.
- **Report:** the bull case (every positive point), the bear case (every negative point),
  the caps, and a confidence (share of analyst weight agreeing with the final direction).

## As a strategy (`analyst_desk`)

```bash
uv run python -m bot.compare --start 2016-01-01 --end 2026-09-30 --strategies trend_pullback,analyst_desk
uv run python -m bot.backtest --strategy analyst_desk --start 2016-01-01 --end 2026-09-30 --stability
```

| Rule | Value (`DeskConfig`) |
|---|---|
| Score | desk weights over technical (30) and market (15) only |
| Entry | the rating turns Overweight or Buy (score ≥ 0.2) after not being so the session before; price > $10, 20-day average dollar volume > $50M |
| Caps | SPY below its 200-day SMA or ATR > 4% of price: no entry (at most Hold) |
| Stop | entry − 2.5 × ATR(14), fixed |
| Exits | technical score below −0.2 ("downgrade"), or after 20 trading days |
| Signature | rating (Buy / Overweight) and SPY volatility bucket: 6 signatures |
| Ranking | highest desk score first |

Fundamentals, insiders and news are left out of the strategy because the backtester has no
point-in-time history for them yet. Adding them would need that history (EDGAR and Alpaca
News can supply it) and a new backtest.

## Point in time

The desk gives a premarket view of the `--date` day. It uses bars dated before that day,
10-K figures filed before it (the first-filed value when a year is later restated), Form 4
and 13D filings filed before it, and headlines published before 09:00 ET on it. A run for a
past date therefore sees what was known then, so the desk can be backtested.

## Setup

- `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`: bars and news (free plan).
- `SEC_USER_AGENT`: fundamentals and insiders (free; sec.gov requires a contact).

Missing keys only make those analysts unavailable; the run still prints a report.
