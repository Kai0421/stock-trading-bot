# Author: Kai0421
"""Alternative data as point-in-time features and vetoes (docs/10-alternative-data.md).

Never a trade trigger: a setup still needs the trend-pullback rules, the regime filter,
a broker-side stop and the probability gate. Each feature is off until a backtest shows
it keeps the win-probability floor and improves expectancy (enable via ``ALTDATA_FEATURES``).

Point-in-time rule: a feature for entry day ``as_of`` only uses filings whose filing date
is before ``as_of`` and headlines published before 09:00 ET on ``as_of``, so live and
backtest see the same information. Vetoes fail closed: if the data can't be fetched, the
veto is on (no entry); positive flags are off.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from bot.altdata.news import fetch_news, is_major
from bot.altdata.store import AltStore, InsiderTxn
from bot.config import ALTDATA_FEATURES, ConfigError, Settings
from bot.journal import Journal

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
VETO_FEATURES = ("news_shock_veto", "insider_heavy_selling_30d")
EDGAR_FEATURES = frozenset(ALTDATA_FEATURES - {"news_shock_veto"})
ACTIVIST_FORMS = {"SC 13D", "SCHEDULE 13D"}
CLUSTER_MIN_INSIDERS = 2
CLUSTER_MIN_VALUE = 250_000
HEAVY_SELLING_MIN = 3
_CEO_CFO = re.compile(r"\b(ceo|cfo|chief executive|chief financial)\b", re.IGNORECASE)


def compute_features(store: AltStore, symbol: str, as_of: date) -> dict[str, bool]:
    def window(days: int) -> list[dict[str, Any]]:
        return store.txns(symbol, as_of - timedelta(days=days), as_of)

    insiders = [t for t in window(90) if t["is_officer"] or t["is_director"]]
    buys90 = [t for t in insiders if t["code"] == "P"]
    cluster_value = sum((t["shares"] or 0) * (t["price"] or 0) for t in buys90)
    recent = window(30)
    ceo_cfo = any(t["code"] == "P" and _CEO_CFO.search(t["title"] or "") for t in recent)
    heavy = [t for t in recent if t["code"] == "S" and t["is_officer"] and not t["plan_10b5_1"]]
    activist = store.filings(symbol, ACTIVIST_FORMS, as_of - timedelta(days=60), as_of)
    news_start = datetime.combine(
        (pd.Timestamp(as_of) - pd.offsets.BDay(2)).date(), time(0), tzinfo=ET
    )
    news_end = datetime.combine(as_of, time(9, 0), tzinfo=ET)
    shock = any(is_major(n["headline"]) for n in store.news(symbol, news_start, news_end))
    return {
        "insider_cluster_buy_90d": len({t["owner"] for t in buys90}) >= CLUSTER_MIN_INSIDERS
        and cluster_value >= CLUSTER_MIN_VALUE,
        "insider_ceo_cfo_buy_30d": ceo_cfo,
        "insider_heavy_selling_30d": len(heavy) >= HEAVY_SELLING_MIN,
        "activist_13d_60d": bool(activist),
        "news_shock_veto": shock,
    }


class LiveSource:
    """Refreshes the store from EDGAR and Alpaca News for one symbol and entry day."""

    def __init__(self, store: AltStore, edgar: Any, news_client: Any, features: frozenset[str]):
        self.store = store
        self.edgar = edgar
        self.news_client = news_client
        self.features = features

    def refresh(self, symbol: str, as_of: date, lookback_days: int = 120) -> None:
        if self.edgar is not None and self.features & EDGAR_FEATURES:
            cik = self.edgar.ticker_ciks().get(symbol)
            if cik is not None:  # ETFs usually have no company CIK
                since = as_of - timedelta(days=lookback_days)
                for f in self.edgar.filings(cik, {"4", *ACTIVIST_FORMS}, since):
                    if self.store.has_filing(f.accession):
                        continue
                    if f.form == "4":
                        owner, txns = self.edgar.form4(cik, f)
                        self.store.add_txns(
                            [
                                InsiderTxn(f.accession, i, symbol, owner.name, owner.is_officer,
                                           owner.is_director, owner.title, t.code, t.shares,
                                           t.price, t.tx_date, f.filing_date, t.plan_10b5_1)
                                for i, t in enumerate(txns)
                            ]
                        )  # fmt: skip
                    self.store.add_filing(f.accession, symbol, f.form, f.filing_date)
        if self.news_client is not None and "news_shock_veto" in self.features:
            start = datetime.combine(as_of - timedelta(days=lookback_days if lookback_days > 7
                                                       else 7), time(0), tzinfo=ET)  # fmt: skip
            end = datetime.combine(as_of, time(9, 0), tzinfo=ET)
            for news_id, created, headline in fetch_news(self.news_client, symbol, start, end):
                self.store.add_news(news_id, symbol, created, headline)


class AltDataProvider:
    """Callable ``(symbol, entry_day) -> features`` used by the engine's premarket job."""

    def __init__(self, store: AltStore, source: Any, features: frozenset[str]) -> None:
        self.store = store
        self.source = source
        self.features = features

    def __call__(self, symbol: str, as_of: date) -> dict[str, bool]:
        key = f"altdata_refreshed:{symbol}:{as_of.isoformat()}"
        try:
            if self.store.journal.get_kv(key) is None:
                self.source.refresh(symbol, as_of)
                self.store.journal.set_kv(key, "1")
            return {**compute_features(self.store, symbol, as_of), "altdata_error": False}
        except Exception as exc:
            log.warning("alt data for %s failed: %s; vetoes on", symbol, exc)
            out = {k: False for k in ALTDATA_FEATURES}
            out.update({k: True for k in VETO_FEATURES})
            out["altdata_error"] = True
            return out


def make_provider(settings: Settings, journal: Journal, data_client: Any) -> AltDataProvider | None:
    features = settings.altdata_features
    if not features:
        return None
    edgar = None
    if features & EDGAR_FEATURES:
        from bot.altdata.edgar import EdgarClient

        edgar = EdgarClient(settings.sec_user_agent)  # raises ConfigError without a UA
    news_client = None
    if "news_shock_veto" in features:
        if not (settings.alpaca_api_key and settings.alpaca_secret_key):
            raise ConfigError("news_shock_veto needs Alpaca keys for the News API")
        from alpaca.data.historical.news import NewsClient

        news_client = NewsClient(settings.alpaca_api_key, settings.alpaca_secret_key)
    store = AltStore(journal)
    return AltDataProvider(store, LiveSource(store, edgar, news_client, features), features)
