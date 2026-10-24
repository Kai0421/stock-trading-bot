# Research strategies

Three strategies suggested by the October 2026 research report
([Swing Bot Strategy Research](https://claude.ai/artifact/JYjcnJpjvGYy8BD2MQjiqF)). All
use free daily bars only, plug in through `bot.strategies.Strategy` (docs/12-strategies.md)
and keep every hard rule: long only, a fixed broker-side stop from the fill, stops that
only move up, settled cash. They are candidates: `STRATEGY` and `GATE_MODE` are unchanged.

| Name | Research basis | Entry | Exits | Aimed at |
|---|---|---|---|---|
| `ibs_reversion` | Pagonidis (2013): US equity ETFs closing in the bottom fifth of their range rose 0.35% on average the next day | SPY and the stock above their 200-day SMA, IBS < 0.2, a down day | Close above the previous high, IBS > 0.8, 5 days; fixed 3 ATR stop | Win-rate gate |
| `confluence_reversion` | Connors RSI(2), IBS, short-term reversal; combining weak independent signals | 2+ of: RSI(2) < 10, IBS < 0.25, 3 lower closes, close below the lower Bollinger band (20, 2) | Close above SMA(5) or the previous high, 7 days; fixed 3 ATR stop | Win-rate gate |
| `high_momentum` | Jegadeesh & Titman (1993), George & Hwang (2004); crash risk from Daniel & Moskowitz (2016) | Market uptrend, within 5% of the 252-day high, 126-day return > 10%, close above the prior 20-day high | Close below SMA(50), 60 days; 2.5 ATR stop trailed at 3 ATR below the close | Expectancy gate |

IBS (internal bar strength) is `(close - low) / (high - low)`: 0 when the stock closes at
the day's low, 1 at the high.

## Signatures

Each keeps three coarse buckets so signatures can reach the gate's 100 trades:

- `ibs_reversion`: SPY volatility | IBS depth (`extreme` < 0.05, `deep` < 0.12, `mild`) |
  day range versus ATR (`narrow` < 0.8, `normal` < 1.3, `wide`).
- `confluence_reversion`: SPY volatility | votes (`v2`, `v3`, `v4`) | distance above the
  SMA(200).
- `high_momentum`: SPY volatility | 126-day return (`moderate` < 20%, `strong` < 40%,
  `extreme`) | distance below the 52-week high (`at_high` < 1%, `close` < 3%, `near`).

## Not built (and why)

- Post-earnings drift: gone for non-micro-cap stocks since 2006 (Martineau 2022).
- Turn-of-the-month: the classic window shows little edge in US stocks recently.
- Social media sentiment: next-day effects of 2 to 8 bp are below our 10 bp cost, and
  there is no free history to reach 100 out-of-sample trades. Log it forward first.
- Machine learning and LLM models: too little data for 100 stocks of daily bars, and LLMs
  need a paid API.

## Testing them

```bash
uv run python -m bot.compare --start 2016-01-01 --end 2026-09-30 \
  --strategies trend_pullback,rsi2_reversion,ibs_reversion,confluence_reversion,high_momentum
uv run python -m bot.evolve --start 2016-01-01 --end 2026-09-30 \
  --strategies ibs_reversion,confluence_reversion,high_momentum
```

Or on GitHub: Actions → **Compare strategies**, ticking the evolve option for the
generations. Neither writes the evidence file the live gate reads.

## Results (2016-01 to 2026-09, $100,000, 116 symbols)

Compare runs on the PR #13 branch, 2026-10-05. Live sizing = the repository variables
(0.25% risk per trade, at most 3 positions, 5% of equity each); default sizing = 1% risk.
Drawdown pause permanent unless noted. SPY buy and hold over the same period: +346.8%
total, +15.0% a year, 33.8% max drawdown.

| Strategy | Gate | Sizing | Trades | Win rate | Lower bound | Mean R | Final equity | Max DD | Accepted |
|---|---|---|---|---|---|---|---|---|---|
| `confluence_reversion` | win_rate | live | 553 | 67.3% | 63.3% | +0.041R | $105,508 | 1.4% | **yes** |
| `high_momentum` | expectancy | live | 191 | 37.2% | 30.6% | +0.156R | $105,946 | 2.8% | no (mean-R lower bound) |
| `ibs_reversion` | expectancy | live | 1,268 | 60.8% | 58.1% | −0.005R | $98,237 | 4.8% | no |
| `trend_pullback` | win_rate | live | 831 | 65.0% | 61.7% | −0.021R | $98,745 | 6.0% | no |
| `confluence_reversion` | win_rate | default | 927 | 64.6% | 61.5% | +0.015R | +1.5%/yr | 9.7% | no |

Restarting 20 trading days after a 10% pause (`--resume-after-days 20`, default sizing)
made no strategy pass and pushed drawdowns to 12–17%, so the live pause stays permanent.
Conclusion: `confluence_reversion` is the candidate for the paper trial (`PAPER_TRIAL`,
docs/12-strategies.md). Its edge is small and exposure about 2%, so judge it on about 100
live paper trades before relying on it.
