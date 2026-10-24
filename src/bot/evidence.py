# Author: Kai0421
"""Backtest evidence for the live success-probability gate.

The backtester writes per-signature statistics of its signal trades (closed before the
backtest end date) plus its verdict to ``memory/evidence/signatures.json``. The engine
gates each live setup on that file exactly as the walk-forward backtest did, and adds:

- **Strategy**: the evidence names the strategy it was backtested for; the engine refuses
  evidence for a different strategy than the one it trades.
- **Verdict**: no trades unless the strategy backtest was ACCEPTED, or ACCEPTED with
  advisory stability (the owner's paper experiment, decisions.md 2026-10-04). In the
  owner's paper trial (``PAPER_TRIAL=true``, paper only, decisions.md 2026-10-05) the
  verdict is waived; every per-signature check below still applies.
- **Universe**: the evidence names the universe it was backtested on (``UNIVERSE``); the
  engine refuses evidence from a different universe.
- **Per-strategy files**: ``memory/evidence/<strategy>.json`` is used when it exists
  (``<strategy>-<universe>.json`` outside the S&P 100), otherwise ``signatures.json``.
- **Staleness**: evidence older than ``MAX_EVIDENCE_AGE`` is refused; re-run the backtest.
- **Live disabling**: a signature whose last 30 live/paper trades win below the floor
  is disabled (docs/03), regardless of its backtest record.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from bot.config import RiskConfig
from bot.risk import GateResult, probability_gate, signature_disabled

if TYPE_CHECKING:
    from bot.backtest import Trade

DEFAULT_EVIDENCE_PATH = Path("memory/evidence/signatures.json")
MAX_EVIDENCE_AGE = timedelta(days=90)
TRADABLE_VERDICTS = frozenset({"ACCEPTED", "ACCEPTED (stability advisory)"})


@dataclass(frozen=True)
class SignatureEvidence:
    n: int
    wins: int
    mean_r: float
    sd_r: float | None = None  # sample SD of R; None in files written before 2026-10-05


@dataclass(frozen=True)
class Evidence:
    signatures: dict[str, SignatureEvidence]
    as_of: date
    backtest_id: str
    verdict: str
    strategy: str = "trend_pullback"  # files written before strategies were pluggable
    universe: str = "sp100"  # files written before UNIVERSE existed were S&P 100 runs


def evidence_file(base: Path, strategy: str, universe: str = "sp100") -> Path:
    """The strategy's own evidence file next to ``base`` if there is one, else ``base``.

    Outside the default universe the file is ``<strategy>-<universe>.json``, so evidence
    for a wider universe can sit beside the S&P 100 file without changing what it trades."""
    name = strategy if universe == "sp100" else f"{strategy}-{universe}"
    own = base.with_name(f"{name}.json")
    return own if own.exists() else base


def evidence_from_trades(
    trades: Iterable[Trade], as_of: pd.Timestamp
) -> dict[str, SignatureEvidence]:
    """Per-signature stats of trades that exited before ``as_of`` (forced exits excluded)."""
    groups: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        if t.exit_reason != "end_of_data" and t.exit_date < as_of:
            groups[t.signature].append(t)
    return {
        sig: SignatureEvidence(
            n=len(ts),
            wins=int(sum(t.win for t in ts)),
            mean_r=round(float(sum(t.r_multiple for t in ts)) / len(ts), 6),
            sd_r=round(float(pd.Series([t.r_multiple for t in ts]).std()), 6)
            if len(ts) > 1
            else None,
        )
        for sig, ts in sorted(groups.items())
    }


def save_evidence(path: Path, evidence: Evidence) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "as_of": evidence.as_of.isoformat(),
        "backtest_id": evidence.backtest_id,
        "verdict": evidence.verdict,
        "strategy": evidence.strategy,
        "universe": evidence.universe,
        "signatures": {k: asdict(v) for k, v in evidence.signatures.items()},
    }
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")


def load_evidence(path: Path) -> Evidence | None:
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    return Evidence(
        signatures={k: SignatureEvidence(**v) for k, v in raw["signatures"].items()},
        as_of=date.fromisoformat(raw["as_of"]),
        backtest_id=raw["backtest_id"],
        verdict=raw["verdict"],
        strategy=raw.get("strategy", "trend_pullback"),
        universe=raw.get("universe", "sp100"),
    )


class EvidenceGate:
    """Live gate with the same interface as ``backtest.SignatureBook``."""

    def __init__(
        self,
        evidence: Evidence,
        live_outcomes: Mapping[str, Sequence[bool]],
        paper_trial: bool = False,
    ) -> None:
        self.evidence = evidence
        self.live = live_outcomes
        self.paper_trial = paper_trial

    def disabled(self, cfg: RiskConfig | None = None) -> list[str]:
        cfg = cfg or RiskConfig()
        return sorted(s for s, o in self.live.items() if signature_disabled(o, cfg, self._wr(s)))

    def _wr(self, signature: str) -> float | None:
        s = self.evidence.signatures.get(signature)
        return s.wins / s.n if s is not None and s.n else None

    def gate(self, signature: str, as_of: pd.Timestamp, cfg: RiskConfig) -> GateResult:
        nan = float("nan")
        ev = self.evidence
        if ev.verdict not in TRADABLE_VERDICTS and not self.paper_trial:
            return GateResult(False, f"strategy backtest not accepted ({ev.verdict})", nan, nan)
        if pd.Timestamp(as_of).date() - ev.as_of > MAX_EVIDENCE_AGE:
            return GateResult(
                False, f"evidence stale (as of {ev.as_of}); re-run backtest", nan, nan
            )
        if signature_disabled(self.live.get(signature, ()), cfg, self._wr(signature)):
            return GateResult(False, "signature disabled: live win rate below floor", nan, nan)
        s = ev.signatures.get(signature)
        if s is None:
            return GateResult(False, "no matching trades", nan, nan)
        sd = s.sd_r if s.sd_r is not None else float("nan")
        return probability_gate(s.wins, s.n, s.mean_r, cfg, sd)
