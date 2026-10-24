# Alternative Data: Social Sentiment & Public Trades of Notable Investors

Verified 2026-10-04 against official sources where possible. The goal is to find **extra evidence** for or against trades the strategy already likes. This data never overrides the [hard rules](01-trading-fundamentals.md#hard-rules--never-ever-do-risky-trades).

## Ground rules for using alternative data

1. **It is never a trade trigger on its own.** A trade still needs the trend-pullback setup, the regime filter, a broker-side stop, and the **success-probability gate (≥ 60% floor)**.
2. **It's only used if a backtest shows it helps.** Each data source becomes a *feature* in the setup signature (or a veto). It is adopted only if, out-of-sample, the strategy with the feature still meets the 60% floor (Wilson lower bound) **and** improves expectancy. Otherwise it is dropped.
3. **Point-in-time only.** Use the **filing/publication timestamp**, never the transaction date. Insiders and politicians report days to weeks after trading, and using the trade date would make the backtest look better than reality (look-ahead bias).
4. **Stocks only.** Notable people also trade options; we ignore those entries and never copy them. Rule 2 still applies: we never trade options.
5. **Respect terms of service.** Use only official APIs or permitted data. No scraping where the terms forbid it.

## Source evaluation

| Source | What it shows | Delay | Cost / access | Verdict |
|---|---|---|---|---|
| **StockTwits** | Retail social sentiment per ticker | Real-time | **API closed to new registrations** ("we won't be accepting new registrations until we have finished our review", [api.stocktwits.com/developers](https://api.stocktwits.com/developers)) | **Not usable now.** Revisit if registrations reopen. Retail sentiment is also noisy and hard to backtest point-in-time |
| **SEC Form 4: corporate insiders** (CEOs, CFOs, directors, 10% owners) | Insider buys and sells with price and size | Filed "before the end of the second business day" after the trade ([Form 4 instructions](https://www.sec.gov/forms/form4.pdf)) | **Free.** `data.sec.gov` needs no API key. Fair access: at most 10 requests per second, with a declared User-Agent | **Best candidate.** Fast, official, free, backtestable |
| **Schedule 13D/G: activists and > 5% holders** | A large holder building a stake | 13D within 5 business days | Free via EDGAR | Useful as a positive flag on large caps |
| **Form 13F: hedge funds and large managers** (e.g. Berkshire) | Quarter-end long holdings | Up to **45 days after quarter end** (Q3 2026 due Nov 16, 2026, [SEC FAQ](https://www.sec.gov/rules-regulations/staff-guidance/division-investment-management-frequently-asked-questions/frequently-asked-questions-about-form-13f)) | Free via EDGAR | Too stale for timing. Use only as a slow "quality / conviction" ranking feature |
| **US Congress trades (STOCK Act PTRs)** | Members' and spouses' trades in amount ranges | Reported within 30 days of notification and **no later than 45 days after the trade** ([House Ethics guide](https://ethics.house.gov/wp-content/uploads/2026/07/7-8-2026-2025-Published-Instruction-Guide.pdf)) | Free but messy (House Clerk / Senate eFD PDFs); clean API from **Quiver Quantitative: Hobbyist $30/month** ($25/month billed annually), no commercial use rights | Low priority. Long delay, amount ranges only, and academic evidence is mixed. Optional later experiment |
| **Alpaca News API (Benzinga)** | Headlines and articles per ticker, history since 2015 | Real-time | Included with Alpaca market data. The free Basic plan allows 200 historical calls per minute. Alpaca has said news pricing may change | **Use as a veto**: skip entries on fresh major news (M&A, FDA, guidance cuts, lawsuits) |
| OpenInsider, Finviz, Capitol Trades (websites) | Friendly views of Form 4 / Congress data | Same as source | Websites; no official API | Use manually to sanity-check. The bot reads the primary source (EDGAR) instead |
| Unusual Whales, other paid flow data | Options flow, dark pool, Congress | Real-time | Paid | Not needed; options-flow data clashes with the "keep it simple and safe" principle |

### What the evidence says (summary, not a promise)

- **Insider open-market purchases** have been studied extensively. *Opportunistic* or *clustered* buys (several insiders buying within weeks) are generally found to carry more information than routine or scheduled trades. Cohen, Malloy & Pomorski (2012), "Decoding Inside Information", *Journal of Finance*, is one example.
- **Insider sales** are weak signals: insiders sell for taxes, diversification, and pre-planned 10b5-1 programmes.
- **Congressional trading** results are mixed. Some studies of the 1990s found outperformance; Eggers & Hainmueller (2013), "Capitol Losses", found congressional portfolios lagged the market in 2004–2008. Combined with the 45-day delay, it is a weak candidate.
- None of this is assumed to work for *our* strategy. Only our own out-of-sample backtest decides.

## Candidate features (to be tested)

| Feature | Definition (point-in-time, by filing date) | Use |
|---|---|---|
| `insider_cluster_buy_90d` | ≥ 2 distinct officers or directors made open-market purchases (Form 4 code **P**) in the last 90 days, total ≥ $250k | Positive flag in the setup signature; ranking boost |
| `insider_ceo_cfo_buy_30d` | The CEO or CFO made an open-market purchase in the last 30 days | Positive flag |
| `insider_heavy_selling_30d` | Several non-10b5-1 open-market sales (code **S**) by officers in the last 30 days | Possible veto (test) |
| `activist_13d_60d` | New 13D filed in the last 60 days | Positive flag (test) |
| `fund_conviction_13f` | Number of top-50 13F managers increasing their position last quarter | Slow ranking feature (test) |
| `news_shock_veto` | Major-news keywords on the ticker in the last 2 trading days | **Veto**: no new entry |
| `congress_buy_45d` | ≥ 1 congressional stock purchase disclosed in the last 45 days | Optional experiment (needs paid Quiver or PDF parsing) |

## Pipeline

```
premarket job
  └─ altdata.refresh()
       ├─ edgar.form4(universe)      # submissions JSON → Form 4 XML, cached in bot.db
       ├─ edgar.schedule13d(universe)
       ├─ alpaca.news(universe, 2d)
       └─ (optional) quiver.congress()
  └─ strategy.signals() adds alt-data features to each setup signature
  └─ probability gate (≥ 60% target, ≥ 60% floor) decides; alt data cannot bypass it
```

- Cached in `data/bot.db` tables `insider_txns`, `filings`, `news` (by filing timestamp).
- EDGAR client: declared User-Agent (`SEC_USER_AGENT="StockTradingBot your-email@example.com"`), at most 5 requests per second (half the SEC limit), with retry and backoff.
- Backtest uses the same point-in-time tables, built from EDGAR historical filings.

## Weekly report integration

The [weekly report](09-weekly-report.md) shows which trades had alt-data flags and how they performed vs trades without them. The AI review may propose adding or removing a feature, but only the backtester and your approval change the strategy.
