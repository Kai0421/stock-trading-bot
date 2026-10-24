# Decisions

ADR-style log: date, decision, evidence, backtest id, status. Newest first.

## 2026-10-24 — Automatic backtest refresh before evidence goes stale (`AUTO_BACKTEST`)

- **Status:** requested by the owner in the project thread ("add that automation ... to automatically run back tests"); on by default, `AUTO_BACKTEST=false` turns it off. Swing mode only.
- **Change:** a Saturday 11:00 ET job, `refresh_evidence`, reruns the README's standard backtest (`--start 2016-01-01 --end <yesterday> --stability`, same strategy and universe as live) when the gate's evidence is 70+ days old, so it is renewed before the 90-day staleness limit. It writes the report to `memory/backtests/` and the evidence to the file the gate reads. `--stability-advisory` is passed only if the current evidence was already made with it (owner, 2026-10-05).
- **Rules unchanged:** the gate, its 60% floor, n >= 100 and the acceptance criteria are untouched; the new verdict decides trading exactly as a manual run would. A REJECTED refresh stops new swing trades and sends a warning. No strategy parameter changes.
- **Evidence:** not a strategy change; no backtest needed.
- **Review:** if a refresh flips the verdict, the owner decides what to do; the bot does not retry with other settings.

## 2026-10-11 — Stocktwits sentiment shadow log and an opt-in sell-only exit (`SENTIMENT_EXIT`)

- **Status:** requested by the owner in the project thread ("test sell part ... implement that first"); off by default, so the live bot is unchanged until `SENTIMENT_EXIT=true` is set. Swing mode only; overnight positions already sell at the next open.
- **Change:** `bot.sentiment` logs Stocktwits score, message volume and tagged-post sentiment per stock (collected by a Claude job, because Stocktwits blocks the bot's own requests). With the setting on, the `manage` job sells a held stock whose fresh snapshot is a `sell_warning` (score <= 30, volume >= 50, tagged posts net <= -0.2) through the existing exit path. No buy path.
- **Rules unchanged:** hard trading rules, stops, cash checks, kill switches, `PAPER=true`. Selling only reduces risk; stale or missing data sells nothing; the bot's own book decides what is held.
- **Evidence:** none yet. The thresholds are hypotheses. Stocktwits history is 6 months of daily scores only, so the forward log is the test: after 4-6 weeks join snapshots to forward returns and ask whether sell warnings preceded drops, before trusting the exit. A buy mode would need the 60% gate on that history first.
- **Review:** if sell warnings do not precede worse returns than the average held stock, turn it off.

## 2026-10-09 — Overnight buy: DAY limit with an attached stop instead of limit-on-close

- **Status:** approved by the owner in the project thread ("yes" to switching the paper buy to a normal limit order before the close); takes effect when merged.
- **Evidence:** the first overnight run (2026-10-09) sent limit-on-close buys for AMD (8 @ 613.00) and NVDA (21 @ 231.80) at 15:42. Alpaca paper expired both unfilled at 16:01-16:02 although AMD closed 608.10 and NVDA 229.28 (IEX daily bars), below both limits. Nothing was bought.
- **Change:** `close_buy` runs 15:50-15:56 and sends a DAY limit buy (1% over the last price, unchanged) as an OTO with its 5% stop attached; unfilled buys expire at the close. `eod` swaps the DAY stop for a GTC one. No strategy parameter changes.
- **Effect:** fills a few minutes and cents from the closing price instead of in the auction; the position now has a broker-side stop from the moment it fills (stricter than the 2026-10-08 waiver).

## 2026-10-08 — Owner decision: overnight trading mode on paper (`TRADING_MODE=overnight`)

- **Status:** adopted by the owner (the owner chose "Build overnight mode" after being told it needs the win-rate floor and the stop rule waived for it); takes effect when merged and `TRADING_MODE=overnight` is set.
- **Change:** a third engine mode. `close_buy` (15:40) sends limit-on-close buys for up to 3 of the chip basket (`OvernightConfig` defaults, the "Bot rules" preset); `eod` places a 5% stop on each fill; `open_sell` (09:00) swaps the stop for a market-on-open sell; `open_check` (09:35) sells anything the open missed. Paper only.
- **Rules waived for this mode only:** the 60% win-rate gate (its backtest wins about 50% of nights), and the stop "from the moment it fills" (placed after the closing fill; cannot protect the opening gap). Unchanged: fresh account fetch and `approve_buys` cash/sizing checks before every buy, cash-only lock, 2 per sector, 5% per position, kill switches, `DRY_RUN`.
- **Evidence:** Overnight Hold Lab backtest, Oct 2021–Oct 2026: about +2.4%/yr, −12% max drawdown, ~50% winning nights (docs/19-overnight-hold.md). The shadow log keeps recording real-price results.
- **Review:** after 100 settled overnight trades, compare against the shadow log and the backtest; switch back to swing if the drawdown passes 10% (the kill switch pauses it first).

## 2026-10-08 — Opt-in S&P 500 universe for `confluence_reversion` (`UNIVERSE=sp500`)

- **Status:** proposed in a draft PR; the owner switches by merging and setting `UNIVERSE=sp500` in `.env`. Default stays `sp100`.
- **Why:** the owner found the 60% gate still blocks every setup. Premarket 2026-10-08 found DE, UNP and TGT (`low|v2|*` signatures, calm market and 2 votes); all failed on the Wilson lower bound (59.9%, 59.6%, 57.2%) with mean R of +0.004R, +0.006R and -0.020R, so they had no edge. Lowering the floor further was rejected; a wider universe gives more setups in signatures that pass.
- **Change:** no parameter change. `UNIVERSE=sp500` scans the S&P 500 (snapshot 2026-10-08) plus the existing ETFs. Evidence records its universe; premarket refuses evidence from another universe. S&P 500 evidence lives in `memory/evidence/confluence_reversion-sp500.json`, so the S&P 100 bot is unaffected.
- **Evidence:** 2016-01-01 to 2026-09-30, same parameters and risk, both ACCEPTED (stability advisory):
  - S&P 100, `20261008-1b716cc0`: 930 trades, 63.9% win (LB 60.7%), +0.014R, CAGR 0.3%, max DD 2.3%, 6 of 12 stability variants fail.
  - S&P 500, `20261008-1b716cc0-sp500`: 1,382 trades (+49%), 62.6% win (LB 60.0%), +0.017R, CAGR 0.4%, max DD 3.5%, 2 of 12 fail (`bb_sd` both ways).
  - Signatures that pass the gate cover 64% of S&P 500 setups vs 33% of S&P 100 setups.
- **Caveats:** the edge is thin either way (about 0.4% a year at 0.25% risk and 5% exposure). Both universes lose in 2022, 2025 and 2026 to date. `low|v2|near` passes with +0.000R, a zero-edge signature. Survivorship bias is larger for the S&P 500 list.
- **After merge:** set `UNIVERSE=sp500`, run a dry-run premarket and check that the heartbeat reads the `-sp500` evidence before trading.

## 2026-10-07 — Split the order path into data, decision and execution layers

- **Status:** proposed in draft PR #20; the owner approves by merging.
- **Change:** structure only, no strategy or parameter change. `bot.gather` (data: every read), `bot.decide` (decision: pure functions, the pluggable strategies and risk rules live here), `bot.execute` (execution: the only broker writes, order and fill bookkeeping). `bot.engine` and `bot.intraday.live` only wire them together. See docs/19-layers.md.
- **Evidence:** behaviour-preserving. A golden trace recorded from main before the split (every broker write and account fetch, alerts, journal, memory files over 51 engine set-ups, swing and intraday) is reproduced exactly; `tests/test_layers.py` enforces the layer boundaries.
- **Unchanged:** `STRATEGY` / `TRADING_MODE` selection, gates, `PAPER_TRIAL`, `DRY_RUN`, fresh account fetch before every order, broker-side stops, kill switches, client order ids.
- **After merge:** update the Mac, run a dry run and confirm it before `DRY_RUN` is turned off (standing rule).
## 2026-10-07 — Owner decision: win-rate target 65% → 60% and floor 62% → 60% (rule change)

- **Status:** proposed in a draft PR, pending the owner's review and merge
- **Decided by:** the owner asked for a 60% target, then chose to lower the hard floor to 60% as well, after being told the target cannot sit below the floor and that this loosens the gate further and departs from the 2026-10-04 pre-registered plan.
- **Change:** `HARD_MIN_WIN_PROB_FLOOR` and `min_win_prob_floor` 0.62 → **0.60**; `min_win_prob_target` 0.65 → **0.60**. The 30-trade rolling disable now triggers below 60%. Unchanged: n ≥ 100, positive expectancy, stops, cash checks, kill switches, risk ceilings, PAPER=true.
- **Effect:** with target = floor, the point-estimate check never binds (the Wilson LB is always below the win rate), so the 60% LB is the only win-rate test. 70 wins in 100 trades now passes (LB 60.4%). Signatures with LB between 60% and 62% (e.g. 61.7%) can now pass.
- **Evaluation:** the 2026-10-04 experiment's pre-registered check (after 100 closed paper trades) now uses Wilson LB ≥ 60%; expectancy > 0 and max drawdown < 10% are unchanged. On failure, revert to the 65% floor as planned.

## 2026-10-04 — Run the engine on GitHub Actions (for now)

- **Status:** adopted by the owner ("option 2 for now"); a VPS may replace it later
- **Design:** `.github/workflows/engine.yml` fires `bot.engine tick` only at job times (~24 runs per weekday, both EDT/EST UTC slots; the engine's ET windows no-op the wrong one). State (`bot.db` SQL dump, `state.json`, packs) lives on the `bot-state` branch. Runs never overlap (concurrency group, no cancellation). `DRY_RUN` defaults to `true` via a repo variable; `PAPER=true` is fixed in the workflow.
- **Known limits:** GitHub may delay scheduled runs 5–15 min (job windows tolerate it; a manage slot can be skipped); Actions minutes ~800/month of 2,000 free (private repo); no macOS fallback for alerts; Claude usage snapshot unavailable (AI reviews still gated by nothing but routine limits).
- **Rule:** only one scheduler at a time. The Mac's launchd agent is uninstalled once Actions is verified.

## 2026-10-04 — Small position sizing for the paper experiment

- **Status:** adopted (owner asked to "trade small"); set in the local `.env`, not in code defaults
- **Change:** `RISK_PER_TRADE=0.0025` (0.25% of equity at risk per trade, ceiling 1%), `MAX_POSITION_PCT=0.05` (ceiling 20%), `MAX_OPEN_POSITIONS=3` (ceiling 6). Risk limits can only be tightened from `.env`; looser values refuse to start.
- **Why:** the experiment mainly tests the order path; smaller positions limit paper drawdown while fills, stops and reconciliation are verified.

## 2026-10-04 — Owner decision: paper experiment with a 62% floor (rule change)

- **Status:** adopted on branch `rule/paper-experiment-62`, pending the owner's review and merge
- **Decided by:** the owner, after reviewing 20261004-e753f02d. AI agents may not make further rule changes.
- **Change:**
  - Win-probability floor (95% Wilson LB) 65% → **62%**; target win rate 70% → **65%** (`HARD_MIN_WIN_PROB_FLOOR = 0.62`).
  - Strategy defaults: exit set `mean_reversion_full`, time stop 10 days (the held-out candidate).
  - ±20% stability becomes **advisory** for this experiment (`--stability-advisory`); the verdict reads `ACCEPTED (stability advisory)`, never plain ACCEPTED.
  - Unchanged: n ≥ 100, positive expectancy, broker-side stops, 1% risk, kill switches, cash-only lock, long-only, **PAPER=true enforced**.
- **Known weaknesses accepted knowingly:** held-out LB 63.7% (passes 62% by 1.7 points); avg loss ≈ 2× avg win; ~3%/yr; 3 of 18 stability variants fail (SMA50→40 collapses to 44% win / −0.24R).
- **Purpose:** test the full order path end to end on paper (OTO entries, broker stops, reconciliation, alerts, routines) and measure the strategy live.
- **Evaluation (pre-registered):** after **100 closed paper trades**: Wilson LB ≥ 62% AND expectancy > 0 AND max drawdown < 10% → continue paper trading; otherwise stop the experiment and revert to the 65% floor. A drawdown pause (−10%) also ends it. Going live is out of scope and would need a separate owner decision.

## 2026-10-04 — Full exit at first target: held-out confirmation REJECTED

- **Status:** evidence; strategy not cleared for paper trading. Pre-registered: one confirmation run, verdict accepted as is.
- **Backtest id:** 20261004-e753f02d (portfolio 2023-01-01 to 2026-09-30, gate evidence from 2014-11-27, `--set exit_set=mean_reversion_full --set time_stop_days=10`, stability run)
- **Design (2016–2022 only):** exit set `mean_reversion_full` (sell all when in profit and close > SMA(5), or at +1R). Time stop chosen from {5, 10, 15} by best per-signature Wilson LB with n ≥ 100 and positive expectancy: 10 days (LB 67.8%, n = 216).
- **Held-out result:** 325 out-of-sample portfolio trades, win rate 68.9%, Wilson LB 63.7% (needs 65%), expectancy +0.037R (avg win +0.36R, avg loss −0.67R), max DD 7.1%, CAGR 3.2%, exposure 13.8%. Fails the 70% target and the 65% floor. Per year: 2023 58.3%, 2024 73.0%, 2025 69.6%, 2026 68.4%.
- **Reading:** close to the floor but below it, with losses about twice the size of wins and a thin edge. No further variants on this data (each extra try raises the chance of a lucky pass). `memory/evidence/signatures.json` now holds this run's evidence with verdict REJECTED, so the live gate still refuses all entries.

## 2026-10-04 — Candidate high-win-rate exits (docs/03): none pass

- **Status:** evidence; strategy still not cleared for paper trading
- **Backtest:** same data as 20261004-5b8ee88e (2016-01-01 to 2026-09-30, 116 symbols), variants via `--set`
- **Interpretation recorded:** the mean-reversion first target (50% when close > SMA(5), or at `mr_target_r`) only fires when the trade is in profit.

| Variant | Signal trades | Win rate | Wilson LB | Expectancy | Best signature (n, win, LB) | Portfolio trades |
|---|---|---|---|---|---|---|
| trend (baseline) | 13,591 | 41.5% | 40.6% | +0.053R | 143, 53.1%, 45.0% | 0 |
| mean_reversion | 13,591 | 45.4% | 44.6% | +0.026R | 22, 72.7%, 51.8% | 0 |
| mean_reversion, time stop 10 | 14,089 | 46.9% | 46.1% | +0.023R | 26, 73.1%, 53.9% | 0 |
| mean_reversion, time stop 5 | 15,215 | 49.6% | 48.8% | +0.013R | 55, 74.5%, 61.7% | 0 |
| mean_reversion, target 0.5R | 13,591 | 45.4% | 44.6% | +0.027R | 22, 72.7%, 51.8% | 0 |
| trend, time stop 5 | 15,215 | 45.4% | 44.6% | +0.032R | 55, 69.1%, 56.0% | 0 |

- **Reading:** taking profit early lifts the win rate by up to ~8 points but pushes expectancy towards zero. No signature with n ≥ 100 comes near the 65% floor; the 70%+ signatures have 22–55 trades, too few to trust (and likely luck across ~100 signatures).
- **Caution for further variants:** every extra variant tried on the same 2016–2026 data raises the chance of a lucky pass. Any further search should hold out the last years (e.g. design on 2016–2022, confirm on 2023–2026 once).

## 2026-10-04 — First real backtest: Trend Pullback v1 REJECTED

- **Status:** evidence (strategy not cleared for paper trading)
- **Backtest id:** 20261004-5b8ee88e (2016-01-01 to 2026-09-30, 116 symbols, SIP, ±20% stability run)
- **Evidence:** 13,591 signal trades, win rate 41.5% (Wilson LB 40.6%), expectancy +0.053R (avg win +0.83R, avg loss −0.50R). Best signature by win rate: 47.8% (LB 43.2%). No signature reaches the 65% floor, so the walk-forward gate admitted 0 portfolio trades; every ±20% variant also 0 trades.
- **Consequence:** the live gate refuses all entries (evidence verdict REJECTED). Next per docs/03: test the candidate high-win-rate exits (50% at close > SMA5 or +1R; tighter time stop). The 65% floor stays.

## 2026-10-04 — Live trading path design (roadmap steps 4–9)

- **Status:** adopted for paper trading; not yet run against the paper account
- **Backtest id:** none yet
- **Evidence:** alpaca-py 0.44 rejects `bracket` orders without a take-profit (checked locally); docs/01–11.

- **Entries are OTO, not bracket:** GTC limit buy + attached GTC stop-loss. The strategy has no fixed target, and a DAY stop would expire overnight. Unfilled entries are cancelled by `manage` at 11:00 ET.
- **Exit timing:** exits decided on a close (`eod`) execute at 09:45 in `entries` before new buys; stop raises from `eod` are applied by `manage` (only up). Partial exit: shrink the stop's qty, then sell; if the sell fails the stop qty is restored.
- **Live gate:** uses `memory/evidence/signatures.json` written by the backtest. No entries unless that backtest's verdict is ACCEPTED and it is ≤ 90 days old; a signature is disabled when its last 30 live trades win < 65%.
- **Earnings:** no point-in-time earnings source yet. `EARNINGS_POLICY=flag` (default, per docs/03 "flag in live") trades and flags; `strict` refuses every entry until a source exists.
- **Unknown positions** (not opened by the bot) get a CRITICAL alert and a protective stop 5% below entry; a stop above the current price triggers an immediate market sell.
- **Scheduler:** one launchd agent runs `bot.engine tick` every 5 minutes; the engine decides due jobs in ET, runs each slot once and skips missed windows (robust to DST, sleep and the Mac's time zone). Replaces one plist per job.
- **Alt data:** every feature off by default. Point in time = filing date before the entry day, headlines before 09:00 ET. Vetoes fail closed. Adoption only via `--altdata` backtest + a new entry here.
- **Weekly report:** matplotlib PNGs embedded in a self-contained HTML instead of Plotly (one dependency fewer, same charts).

## 2026-10-04 — Backtest method (roadmap step 3)

- **Status:** adopted for validation; no strategy run on real data yet
- **Backtest id:** none yet
- **Evidence:** design follows docs/01 "Validating a strategy" and docs/03 acceptance criteria.

- **Walk-forward gate:** every setup becomes a hypothetical "signal trade". On each day the portfolio may only take a setup whose signature passes the gate on signal trades that **exited before that day** (expanding window). Portfolio trades are therefore out-of-sample with respect to the evidence that admitted them.
- **Fills:** signal on day t close → limit entry at t+1 open (limit = open + 0.2%), filled at open + 5 bps; stop = limit − 2×ATR(t), fixed at order time. Gap through stop fills at the open, intraday breach at the stop; exits decided on a close fill at the next open; every fill pays 5 bps (10 bps round trip).
- **Success:** net P&L after costs > 0. Trades force-closed at the end of data count in portfolio metrics but are excluded from signature evidence.
- **Constraints in the simulation:** `risk.approve_buys` (tradable cash, 2% reserve, 6 slots, 2 per sector, kill switches); same-day sale proceeds are unsettled (T+1). A drawdown pause stops new entries for the rest of the run (manual resume is not simulated).
- **Data:** Alpaca daily bars, split+dividend adjusted, SIP feed, 400 calendar days of indicator warm-up before the start date. Universe = today's S&P 100 + unleveraged ETFs (survivorship bias, noted in every report). Earnings blackout skipped (no point-in-time calendar).
- **Acceptance:** OOS win rate ≥ 70%, Wilson LB ≥ 65%, expectancy > 0, max DD < 20%, ≥ 100 trades, and every parameter ±20% must still pass (`--stability`). Without the stability run the verdict is at most PROVISIONAL.

## 2026-10-04 — Unspecified Trend Pullback details (roadmap step 2)

- **Status:** proposed (hypothesis; must pass the backtest gate in roadmap step 3)
- **Backtest id:** none yet
- **Evidence:** none. docs/03-strategy.md leaves these open; values chosen as coarse, round defaults so each signature bucket can reach n ≥ 100 trades.

| Parameter (`StrategyConfig`) | Value | Meaning |
|---|---|---|
| `spy_vol_edges` | (0.01, 0.02) | SPY ATR(14) / close → low / mid / high volatility |
| `trend_strength_edges` | (0.02, 0.05) | SMA50 / SMA200 − 1 → weak / moderate / strong |
| `rsi_depth_edges` | (5, 10) | Lowest RSI(2) in the 3-day lookback → deep / moderate / shallow |
| `ema_distance_edges` | (0.5, 1.5) | (close − EMA20) / ATR → near / mid / far |
| `trail_ema_buffer_atr` | 0.1 | "Small buffer" under the 20 EMA for the trailing stop, in ATRs |
| `partial_profit_fraction` | 0.5 | Share of the position sold at +2R |
| `avg_volume_days` | 20 | Window for the average dollar volume filter |

Other interpretations made in code (`src/bot/strategy.py`):
- The 3-day pullback lookback includes the trigger day.
- Breakeven, trailing and exits are evaluated on the daily close; full exits (trend break, time stop) take priority over the partial.
- Trailing starts only once breakeven is reached; trail levels at or above the close are ignored.
- An unknown earnings date rejects the entry (the backtester must pass `False` explicitly to skip the filter).

## 2026-10-05 — Pluggable strategies; RSI(2) reversion added as a candidate

- **Status:** proposed (architecture change; no parameter of the traded strategy changed)
- **Backtest id:** none yet for `rsi2_reversion`; compare with `python -m bot.compare`
- **Evidence:** refactor verified to give identical Trend Pullback backtest trades and equity on synthetic data (all three exit sets, gated and ungated).

- The trading decision now sits behind `bot.strategies.Strategy` (docs/12-strategies.md). The engine trades the strategy named by `STRATEGY` (default `trend_pullback`, unchanged).
- Evidence files now record their strategy; the engine refuses entries when the evidence is for a different strategy. Older files count as `trend_pullback`.
- Trend Pullback signatures stay unprefixed so the existing evidence stays valid; other strategies' signatures are prefixed with their name.
- `rsi2_reversion` parameters are hypotheses, not backtested:

| Parameter (`Rsi2Config`) | Value | Meaning |
|---|---|---|
| `sma_trend` | 200 | Close must be above this SMA, and SPY above its own |
| `rsi_entry_below` | 10 | RSI(2) on the signal day |
| `sma_exit` | 5 | Sell everything on a close above this SMA |
| `stop_atr_mult` | 2.5 | Fixed stop = entry − 2.5 × ATR(14) |
| `time_stop_days` | 10 | Sell everything after 10 trading days |
| `rsi_depth_edges` | (2, 5) | Signature: RSI(2) extreme / deep / mild |
| `trend_distance_edges` | (0.05, 0.15) | Signature: close / SMA200 − 1 near / mid / far |

## 2026-10-05 — Analyst desk added: research report and `analyst_desk` strategy candidate

- **Status:** proposed (new candidate strategy; the traded strategy is unchanged)
- **Backtest id:** none yet; compare with `python -m bot.compare --strategies trend_pullback,analyst_desk`
- **Evidence:** unit tests only (synthetic data); not run on real bars yet.

- `python -m bot.desk SYMBOL` prints a rule-based Buy to Sell rating from five analysts (docs/13-analyst-desk.md). Research only; the engine does not read it.
- `analyst_desk` uses the desk's technical and market analysts as a pluggable strategy. Parameters are hypotheses, not backtested:

| Parameter (`DeskConfig`) | Value | Meaning |
|---|---|---|
| `entry_score` | 0.2 | Overweight: enter when the rating is upgraded to this or better |
| `buy_score` | 0.5 | Buy (signature bucket only) |
| `exit_score` | -0.2 | Sell everything when the technical score falls below this |
| `stop_atr_mult` | 2.5 | Fixed stop = entry − 2.5 × ATR(14) |
| `time_stop_days` | 20 | Sell everything after 20 trading days |


## 2026-10-05 — Opt-in expectancy gate (`GATE_MODE=expectancy`)

- **Status:** proposed; the owner approves by merging. Default stays `GATE_MODE=win_rate` (65% target / 62% floor unchanged).
- **Backtest id:** none yet; re-run the Backtest workflow with `GATE_MODE=expectancy` before switching, because evidence files written before this change have no `sd_r` and fail closed in this mode.
- **Evidence:** 2016–2026 compare (signal level, ungated): analyst_desk 41.1% win, +0.100R over 3,529 setups; rsi2_reversion 63.7%, +0.026R; trend_pullback 57.8%, −0.009R. None passes the win-rate gate, though analyst_desk shows the highest average profit per trade.
- **Why:** the owner wants strategies with lower win rates but higher profit to be able to trade, with the risk held in check by other limits.

| Rule | `win_rate` (default) | `expectancy` |
|---|---|---|
| Trades needed per signature | ≥ 100 out-of-sample | ≥ 100 out-of-sample |
| Win-rate test | ≥ 65%, Wilson LB ≥ 62% | none |
| Profit test | mean R > 0 | 95% lower bound of mean R > 0 |
| Risk per trade | ≤ 1% | ≤ 0.5% |
| Backtest max drawdown | < 20% | < 6% |
| Live disable (last 30 trades) | win rate < 62% | win rate < backtest win rate − 15 points |
| Kill switches | −3% day, −10% from peak | unchanged |

## 2026-10-05 — Research strategies and generational tuning added as candidates

- **Status:** proposed (new candidate strategies and an advisory tool; the traded strategy, `STRATEGY` and `GATE_MODE` are unchanged)
- **Backtest id:** none yet; run the Compare and Evolve workflows on real bars
- **Evidence:** unit tests only (synthetic data). Research basis: [Swing Bot Strategy Research](https://claude.ai/artifact/JYjcnJpjvGYy8BD2MQjiqF), summarised in docs/14-research-strategies.md.
- **Why:** no existing strategy passes acceptance (2016–2026 compare); short-term mean reversion is the family with a naturally high win rate, and momentum is the strongest long-run anomaly for the expectancy gate.

New strategies (parameters are hypotheses, not backtested):

| Strategy | Key parameters |
|---|---|
| `ibs_reversion` | IBS entry < 0.2 on a down day, exit IBS > 0.8 or close above the previous high, 5-day time stop, fixed 3 × ATR(14) stop, SMA(200) trend and SPY regime |
| `confluence_reversion` | 2 of 4 votes (RSI(2) < 10, IBS < 0.25, 3 lower closes, close below Bollinger(20, 2)), exit above SMA(5) or the previous high, 7-day time stop, fixed 3 × ATR stop |
| `high_momentum` | within 5% of the 252-day high, 126-day return > 10%, close above the prior 20-day high; exit below SMA(50) or after 60 days; 2.5 × ATR stop trailed at 3 × ATR |

`python -m bot.evolve` (docs/15-evolution.md) runs walk-forward generations: a challenger replaces the champion only after beating it on an unseen year. It is advice only; adopting a champion needs its own backtest with `--stability` and a decision entry.

## 2026-10-05 — Backtests can restart after a drawdown pause (measurement only)

- **Status:** approved by the owner in the project thread ("ok" to resuming after a month in backtests only)
- **Change:** `BacktestConfig.resume_after_days` / `--resume-after-days N` (Compare workflow default 20): N trading days after a 10% drawdown pause the simulated bot restarts at half size until equity regains its pre-pause peak. Default 0 keeps the pause permanent, so `bot.backtest` evidence is unchanged unless the flag is passed.
- **Unchanged:** the live kill switch (−10% from peak pauses the bot until the owner reviews it), −3% daily halt, every other risk rule.
- **Why:** in the 2016–2026 compare every strategy's backtest stopped at its first 10% drawdown (e.g. ibs_reversion flat from mid-2018), so results measured "time to first bad patch" instead of the whole period.

## 2026-10-05 — Paper trial of `confluence_reversion` (`PAPER_TRIAL=true`)

- **Status:** approved by the owner in the project thread (chose "Paper trial" on a decision card)
- **Change:** opt-in `PAPER_TRIAL` setting (default false, paper only). The live gate no longer requires an ACCEPTED strategy verdict while it is on; every per-signature check (n ≥ 100, win-rate/expectancy test, 90-day freshness, live disabling) stays. Evidence can be per strategy (`memory/evidence/<strategy>.json`). The engine workflow now passes `STRATEGY` and `PAPER_TRIAL`.
- **Evidence:** confluence_reversion, 2016–2026 compare: 927 gated trades, 64.6% win (lower bound 61.5%), +0.015R, +1.5% CAGR, 9.7% max drawdown; it misses acceptance (65% / 62%) by under a point. Stability check REJECTED.
- **Unchanged:** stops, cash checks, sizing caps, −3% daily halt, −10% drawdown pause, `PAPER=true`.
- **Why:** paper money carries no real risk, and forward results are the only test that cannot be overfitted. Review after about 100 closed trades; end the trial (unset `PAPER_TRIAL`) if live mean R is negative.


## 2026-10-06 — Intraday trading mode (`TRADING_MODE=intraday`) and four intraday candidates

- **Status:** proposed (draft PR); the owner approves by merging. Default stays `TRADING_MODE=swing`, so the live bot is unchanged until the owner sets it.
- **Backtest id:** intraday-20261006-8b636075 (Compare workflow run 37530787997, 2016–2026 SIP 5-minute bars). All four REJECTED: setup-level mean R −0.17R to −0.48R after 10 bps costs, no signature near the 62% gate, zero gated trades. No paper trial recommended (docs/17-intraday.md).
- **Why:** the owner asked for slower, low-risk intraday strategies and a configuration switch between swing and intraday logic.
- **Rules (unchanged hard limits):** same `RiskConfig` ceilings, probability gate (n ≥ 100 per signature, walk-forward), `approve_buys` cash checks with a fresh account fetch per order, OTO broker-side stop on every entry, −3% daily halt (also checked intraday) and −10% drawdown pause, paper only.
- **New intraday-only rules:** flat by 15:50 (10 minutes before an early close); at most one entry per symbol per session; unfilled entries cancelled after 10 minutes; no new entries below $25,000 equity (pattern-day-trader rule); positions opened before today are never sold by the flatten job (they keep their stop and raise an alert); switch modes only when flat.

Intraday strategies (parameters are hypotheses, not tuned):

| Strategy | Key parameters |
|---|---|
| `orb_breakout` | 30-minute opening range, entries 10:00–12:00, daily close above SMA(50) and SPY above SMA(200), range < 0.8 daily ATR, stop at the range low capped at 3 bar-ATRs, breakeven at +1R then 2 ATR trail |
| `gap_fill` | gap down 0.5–3% in a stock above its SMA(200), entry on the first 5-minute close back above the open (09:45–11:00), target yesterday's close, stop 0.1 ATR under the day's low (max 3 ATR) |
| `intraday_reversion` | RSI(2) < 5 and close ≥ 1.5 ATR under the 20-bar EMA (10:30–14:30), daily uptrend; exit above the EMA, RSI(2) > 70 or after 12 bars; 2.5 ATR stop |
| `late_momentum` | SPY/QQQ only: previous close → 10:00 return > 0.25%, buy 15:30, sell 15:55; 3 ATR stop |
| `va_rotation` | (SSRN 7546200, long side) open inside yesterday's value area below the POC; up bar closing between VAL and POC; stop 0.1 ATR under VAL; target POC |
| `va_rejection` | probe under yesterday's VAL within 3 bars, up bar closing back inside; stop 0.1 ATR under the probe low; target POC |
| `ib_failure` | after 10:30, probe under the first-hour low within 3 bars, up bar closing back inside; stop under the probe low; target the first-hour midpoint |
| `noise_breakout` | (Zarattini, Aziz & Barbon 2024, long side) SPY/QQQ; at half hours from 10:00 buy a close above max(open, prior close) × (1 + 14-day mean move from the open at that time); sell at a half hour below max(band, VWAP); flat 15:55 |
| `vwap_trend` | (Zarattini & Aziz 2023, long side) QQQ/SPY; first 5-minute close above session VWAP, sell on a close below; stop 0.5 ATR under VWAP |
| `orb_in_play` | (Zarattini, Barbon & Aziz 2024, long side) up first 5-minute bar with opening volume ≥ its 14-day average, price > $5, daily ATR > $0.50; buy the next bar, stop 0.1 daily ATR below, hold to 15:55 |

All three: reward ≥ 1.2× risk, risk 0.5–3 bar ATRs, entries 10:00 (10:30) to 13:30, profile bins 0.05% of the open, 70% value area. **Backtest id:** intraday-20261006-2efd9e27 (run 37543041538): all three REJECTED, setup-level win rate 30–35%, mean R −0.32R to −0.39R, zero gated trades.

**Backtest id (published strategies):** intraday-20261007-53380e80 (run 37573539508): all three REJECTED, setup-level win rate 10–26%, mean R −0.36R to −0.55R after costs (+0.06R to +0.18R before), zero gated trades. No paper trial recommended.

## 2026-10-07 — Overnight hold (buy close, sell next open) added as research code only

- **Status:** rejected for trading; code kept for research. The owner asked for the algorithm to be committed but not used by the bot. Live strategy unchanged (`confluence_reversion`).
- **Evidence:** Overnight Hold Lab backtest, Alpaca SIP daily bars Oct 2021–Oct 2026: bot-rules preset (≤3 chip stocks, 5% each, 10 bps) +2.4%/yr, −12% drawdown, ~50% wins (docs/19-overnight-hold.md).
- **Why not traded:** fails the 62% win-rate floor, and a broker-side stop cannot protect an overnight gap.
- **Guard:** `src/bot/overnight_hold.py` is outside both strategy registries; tests fail if `STRATEGY`/`TRADING_MODE` can select it or any bot module imports it.

## 2026-10-09 — Kronos rank added as an opt-in strategy (not traded)

- **Status:** added, off. The owner asked to test Kronos (github.com/shiyu-coder/Kronos, MIT) as a strategy that can be unplugged without touching the existing ones. Live strategy unchanged; it only trades if `STRATEGY=kronos_rank` is set.
- **Rules:** buy when cached Kronos-small forecasts give P(up in 5 days) ≥ 0.6 and mean forecast return ≥ 1%, SPY above its 200-day SMA, price/liquidity filters; 2.5 ATR fixed stop, 5-day time stop. Forecasts come from `python -m bot.kronos` (optional `kronos` dependency group); the strategy only reads the cache and fails closed without it. The gate, stops, cash checks and kill switches are unchanged.
- **Evidence:** offline tests on the owner's Mac, 2025-09-02 to 2026-10-05 (reports/research/kronos-veto-2026-10-09.md, kronos-intraday-2026-10-09.md): as a veto on 3,584 swing signals the vetoed trades did as well or better (+0.033R vs +0.031R kept; rank correlation ~0.02); S&P 100 top-decile 5-day picks won 54.0% (n=100, Wilson LB 44.3%), edge +0.85% [−0.97%, +2.67%], won 5 of 10 dates; intraday 1-hour picks lost to the average stock. No reliable edge yet.
- **Not yet backtested in the bot.** Needs a forecast cache from 2025-09 on (earlier data may be in Kronos' training set).
