# Strategy: Trend Pullback v1 (trend-pullback-v1)

Long-only swing trades on daily bars: buy short pullbacks in established uptrends when SPY is
above its 200-day SMA. Full rules: docs/03-strategy.md. Parameters: `StrategyConfig` and
`RiskConfig` in src/bot/config.py (defaults below). Changes need a decisions.md entry.

| Area | Current |
|---|---|
| Uptrend | close > SMA200 and SMA50 > SMA200 |
| Pullback | RSI(2) < 10 or low touched EMA20 within 3 days |
| Trigger | close > previous high |
| Gate | same-signature OOS trades: n >= 100, win rate >= 65%, Wilson LB >= 62%, expectancy > 0 |
| Entry | OTO: GTC limit (last + 0.2%) with GTC stop at limit - 2 x ATR(14); cancelled 11:00 ET |
| Exits | `mean_reversion_full`: sell all once in profit and close > SMA(5), or at +1R; close < SMA50; 10-day time stop; initial stop 2 x ATR |
| Risk | 1% per trade, <= 20% per position, <= 6 positions, <= 2 per sector, 2% cash reserve |
| Kill switches | -3% day: no orders next session; -10% from peak: paused until manual resume |

Status: paper experiment (decisions.md 2026-10-04). Held-out 2023-2026: 325 trades, win 68.9%, LB 63.7%, +0.037R; stability advisory (3 of 18 variants fail). Evaluate after 100 paper trades.
