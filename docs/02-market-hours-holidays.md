# NYSE Market Hours & Holidays

Source: [nyse.com/markets/hours-calendars](https://www.nyse.com/markets/hours-calendars), fetched 2026-10-04. All times US Eastern (ET).

## Sessions (NYSE)

| Time (ET) | Event |
|---|---|
| 06:30 | Pre-opening: orders can be entered and queue for the open |
| 09:30 | Core open auction — regular trading begins |
| 15:50–16:00 | Closing imbalance period |
| 16:00 | Closing auction — regular trading ends |

Extended sessions exist on NYSE Arca (04:00–09:30, 16:00–20:00) and NYSE American / National / Texas (07:00–09:30, 16:00–20:00). Liquidity is thin and spreads wide — **this bot trades the core session only.**

## Holidays (market closed)

| Holiday | 2026 | 2027 |
|---|---|---|
| New Year's Day | Thu Jan 1 | Fri Jan 1 |
| Martin Luther King Jr. Day | Mon Jan 19 | Mon Jan 18 |
| Washington's Birthday | Mon Feb 16 | Mon Feb 15 |
| Good Friday | Fri Apr 3 | Fri Mar 26 |
| Memorial Day | Mon May 25 | Mon May 31 |
| Juneteenth | Fri Jun 19 | Fri Jun 18 (observed) |
| Independence Day | Fri Jul 3 (observed) | Mon Jul 5 (observed) |
| Labor Day | Mon Sep 7 | Mon Sep 6 |
| Thanksgiving | Thu Nov 26 | Thu Nov 25 |
| Christmas | Fri Dec 25 | Fri Dec 24 (observed) |

2028: no New Year's holiday is observed (Jan 1 falls on a Saturday).

## Early closes (13:00 ET)

- Fri Nov 27, 2026 (day after Thanksgiving)
- Thu Dec 24, 2026 (Christmas Eve)
- Fri Nov 26, 2027 (day after Thanksgiving)
- Mon Jul 3, 2028

## How the bot uses this

- **Never hardcode as the primary source.** Each job calls Alpaca `get_clock()` / `get_calendar()` first and exits if the market is closed.
- Keep this table as a local **fallback** (and as test fixtures) if the API is unreachable.
- On early-close days: no new entries; management jobs shift earlier (last run ≤ 12:45 ET).
- Weekends are always closed. Bank holidays where NYSE is open (Columbus Day, Veterans Day) are normal trading days.
