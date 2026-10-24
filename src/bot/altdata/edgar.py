# Author: Kai0421
"""SEC EDGAR client: ticker -> CIK, filing lists, Form 4 insider transactions.

Fair access (sec.gov): a declared User-Agent and at most 10 requests per second; we use
5 per second, with retry and backoff on 429/5xx. No API key is needed.
"""

from __future__ import annotations

import time as _time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import requests

from bot.config import ConfigError

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/{name}"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
MAX_PER_SECOND = 5
RETRIES = 3


class EdgarError(RuntimeError):
    pass


@dataclass(frozen=True)
class Filing:
    accession: str
    form: str
    filing_date: date
    accepted_at: datetime | None
    primary_document: str

    @property
    def raw_document(self) -> str:
        """Form 4 primary documents point at an XSL rendering; the raw XML has no prefix."""
        return self.primary_document.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class Owner:
    name: str
    is_officer: bool
    is_director: bool
    title: str


@dataclass(frozen=True)
class Txn:
    code: str
    shares: float
    price: float
    tx_date: date | None
    plan_10b5_1: bool


def _bool(text: str | None) -> bool:
    return (text or "").strip().lower() in {"1", "true"}


def _text(node: ET.Element | None, path: str) -> str | None:
    if node is None:
        return None
    found = node.find(path)
    return found.text.strip() if found is not None and found.text else None


def _float(text: str | None) -> float:
    try:
        return float(text) if text else 0.0
    except ValueError:
        return 0.0


def parse_form4(xml_text: str) -> tuple[Owner, list[Txn]]:
    """Non-derivative transactions of a Form 4 (options and other derivatives ignored)."""
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError as exc:
        raise EdgarError(f"bad Form 4 XML: {exc}") from exc
    owner_node = root.find("reportingOwner")
    rel = owner_node.find("reportingOwnerRelationship") if owner_node is not None else None
    owner = Owner(
        name=_text(owner_node, "reportingOwnerId/rptOwnerName") or "",
        is_officer=_bool(_text(rel, "isOfficer")),
        is_director=_bool(_text(rel, "isDirector")),
        title=_text(rel, "officerTitle") or "",
    )
    plan = _bool(_text(root, "aff10b5One"))
    txns = []
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        raw_date = _text(t, "transactionDate/value")
        txns.append(
            Txn(
                code=_text(t, "transactionCoding/transactionCode") or "",
                shares=_float(_text(t, "transactionAmounts/transactionShares/value")),
                price=_float(_text(t, "transactionAmounts/transactionPricePerShare/value")),
                tx_date=date.fromisoformat(raw_date[:10]) if raw_date else None,
                plan_10b5_1=plan,
            )
        )
    return owner, txns


class EdgarClient:
    def __init__(
        self,
        user_agent: str,
        get: Callable[..., Any] = requests.get,
        sleep: Callable[[float], None] = _time.sleep,
        monotonic: Callable[[], float] = _time.monotonic,
    ) -> None:
        if not user_agent.strip():
            raise ConfigError("SEC_USER_AGENT is required by sec.gov fair-access rules")
        self._headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        self._get = get
        self._sleep = sleep
        self._now = monotonic
        self._last = -1e9
        self._ciks: dict[str, int] | None = None

    def _request(self, url: str) -> Any:
        for attempt in range(RETRIES):
            wait = self._last + 1 / MAX_PER_SECOND - self._now()
            if wait > 0:
                self._sleep(wait)
            self._last = self._now()
            try:
                resp = self._get(url, headers=self._headers, timeout=15)
            except Exception as exc:
                if attempt == RETRIES - 1:
                    raise EdgarError(f"GET {url} failed: {exc}") from exc
                self._sleep(2**attempt)
                continue
            if resp.status_code == 200:
                return resp
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < RETRIES - 1:
                self._sleep(2**attempt)
                continue
            raise EdgarError(f"GET {url} returned HTTP {resp.status_code}")
        raise EdgarError(f"GET {url} failed")

    def ticker_ciks(self) -> dict[str, int]:
        """Ticker -> CIK, with SEC's '-' class separator mapped to Alpaca's '.'."""
        if self._ciks is None:
            raw = self._request(TICKERS_URL).json()
            self._ciks = {
                str(v["ticker"]).replace("-", "."): int(v["cik_str"]) for v in raw.values()
            }
        return self._ciks

    def filings(self, cik: int, forms: set[str], since: date) -> list[Filing]:
        data = self._request(SUBMISSIONS_URL.format(name=f"CIK{cik:010d}.json")).json()
        pages = [data["filings"]["recent"]]
        for extra in data["filings"].get("files", []):
            if date.fromisoformat(extra["filingTo"]) >= since:
                pages.append(self._request(SUBMISSIONS_URL.format(name=extra["name"])).json())
        out = []
        for page in pages:
            for i, form in enumerate(page["form"]):
                filed = date.fromisoformat(page["filingDate"][i])
                if form not in forms or filed < since:
                    continue
                accepted = page.get("acceptanceDateTime", [None] * (i + 1))[i]
                out.append(
                    Filing(
                        accession=page["accessionNumber"][i],
                        form=form,
                        filing_date=filed,
                        accepted_at=datetime.fromisoformat(accepted.replace("Z", "+00:00"))
                        if accepted
                        else None,
                        primary_document=page["primaryDocument"][i],
                    )
                )
        return out

    def form4(self, cik: int, filing: Filing) -> tuple[Owner, list[Txn]]:
        url = ARCHIVE_URL.format(
            cik=cik, acc=filing.accession.replace("-", ""), doc=filing.raw_document
        )
        return parse_form4(self._request(url).text)

    def company_facts(self, cik: int) -> dict[str, Any]:
        """XBRL facts for one company (all tags, all filings); see bot.desk.facts."""
        return self._request(FACTS_URL.format(cik=cik)).json()
