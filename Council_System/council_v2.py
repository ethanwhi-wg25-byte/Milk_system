#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Council Agent System — MVP v2 (paper trading)

Adds:
- SafetySentinel (GREEN/YELLOW/ORANGE/RED) for "something feels off" signals
- ExitEngine: BE+fees at +1R, partial TP at +2R, ATR trailing stop; ORANGE => force trailing, RED => kill-switch close + halt
- CLI-first workflow: `sentinel`, `run`, `analyze`
- EdgeDecayTracker: "一切优势都会腐烂" — monitors rolling win rates per agent/strategy,
  detects when an edge is decaying, and blocks trades from rotting strategies.
- counterparty_thesis: every trade signal must explain WHO is on the other side
  and WHY they will lose. No thesis → no trade.

No external deps. Educational / prototyping use.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import math
import os
import random
import statistics
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# =========================
# Types
# =========================
class Action(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    HOLD = "HOLD"


class AlertLevel(int, Enum):
    GREEN = 0
    YELLOW = 1
    ORANGE = 2
    RED = 3

    def __str__(self) -> str:
        return self.name


@dataclass
class MarketSnapshot:
    symbol: str
    ts: int
    price: float
    ohlcv: List[Tuple[int, float, float, float, float, float]]  # (ts, o, h, l, c, v)


@dataclass
class Verdict:
    agent: str
    action: Action
    confidence: float  # 0..1
    rationale: str
    counterparty_thesis: str = ""  # WHO loses if we win, and WHY
    size_hint: float = 1.0  # relative suggestion (0..1)


@dataclass
class Consensus:
    action: Action
    confidence: float
    agree_count: int
    total_agents: int
    agreement_ratio: float
    notes: List[str]
    raw: List[Verdict]
    counterparty_thesis: str = ""  # merged thesis from agreeing agents


@dataclass
class IronLaws:
    # Five Iron Laws (MVP defaults)
    MAX_LEVERAGE: float = 10.0
    MAX_POSITION: float = 0.15          # fraction of equity
    STOP_LOSS_MODE: str = "server"      # required
    MIN_CONFIDENCE: float = 0.60
    MIN_PROFIT_MULT: float = 2.0        # profit >= 2 * cost

    # Ops guardrails
    COOLING_PERIOD_SEC: int = 4 * 3600   # 4h — replay data shows 24h kills ~40% of all rounds
    DAILY_TRADE_LIMIT: int = 10            # raised to not conflict with shorter cooling


@dataclass
class Fees:
    taker_fee: float = 0.0006  # 0.06%
    maker_fee: float = 0.0002


@dataclass
class RiskConfig:
    initial_leverage: float = 1.0
    rr_target: float = 3.0
    max_drawdown_soft: float = 0.03
    max_drawdown_hard: float = 0.08
    vol_lookback: int = 30


@dataclass
class SentinelConfig:
    # "Something feels off" detection (MVP with only OHLCV)
    vol_window: int = 30
    vol_z_yellow: float = 2.5             # raised: real BTC 1min hits 2.0 too often
    vol_z_orange: float = 4.0             # raised: replay shows 3.0 fires 15% of rounds (not anomalous)
    volume_drop_orange: float = 0.30     # >30% drop vs mean volume
    confirm_cycles: int = 2              # require consecutive hits to escalate
    clear_cycles: int = 2                # require consecutive clears to de-escalate
    api_fail_red: int = 3                # execution failures => RED kill switch


@dataclass
class ExitConfig:
    # Profit-running mechanics
    tp1_r: float = 2.0
    tp1_frac: float = 0.40   # close 40% at TP1
    move_be_r: float = 1.0   # at +1R move stop to BE+buffer
    be_extra_r: float = 0.20 # extra beyond BE for "fees/slippage buffer" in R

    # ATR trailing multipliers by alert
    trail_mult_green: float = 3.0
    trail_mult_yellow: float = 2.5
    trail_mult_orange: float = 2.0

    # For losing positions under ORANGE, tighten even more
    trail_mult_orange_losing: float = 1.5


@dataclass
class CouncilConfig:
    symbol: str = "BTC/USDT"
    timeframe_sec: int = 60
    candle_lookback: int = 180
    min_agents_agree: int = 3
    seed: int = 42


@dataclass
class TradePlan:
    action: Action
    symbol: str
    leverage: float
    position_frac: float   # fraction of equity
    entry: float
    stop: float
    take: float
    stoploss_mode: str
    expected_profit: float
    expected_cost: float
    counterparty_thesis: str = ""  # inherited from Consensus


@dataclass
class Position:
    action: Action
    entry: float
    qty: float
    stop: float
    take: float
    leverage: float
    opened_ts: int

    # Needed for R-based exit logic
    initial_stop: float
    initial_risk_dist: float  # abs(entry - initial_stop)
    tp1_done: bool = False
    be_done: bool = False
    last_trail_stop: float = 0.0


@dataclass
class PortfolioState:
    equity: float = 1000.0
    cash: float = 1000.0
    position: Optional[Position] = None
    high_watermark: float = 1000.0
    last_trade_ts: int = 0
    trades_in_last_24h: List[int] = dataclasses.field(default_factory=list)
    halted: bool = False

    # Sentinel memory
    sentinel_level: AlertLevel = AlertLevel.GREEN
    sentinel_hit_streak: int = 0
    sentinel_clear_streak: int = 0
    api_failures: int = 0


# =========================
# Edge Decay Tracker — "一切优势都会腐烂"
# =========================
@dataclass
class EdgeRecord:
    """One closed trade outcome attributed to the agents that voted for it."""
    ts: int
    agent: str
    won: bool          # True if the trade closed in profit
    r_multiple: float  # realized R-multiple at close


class EdgeDecayTracker:
    """Monitors per-agent rolling win rates and detects edge rot.

    Core principle: every edge decays. If an agent's rolling win rate
    drops below the minimum threshold, its signals are marked as
    "decaying" and the system can reduce confidence or veto entirely.

    The tracker also computes decay_velocity — how fast the win rate
    is dropping — to catch edges that are dying before they're dead.
    """

    def __init__(
        self,
        window: int = 20,           # rolling window size (trades)
        min_win_rate: float = 0.45,  # below this → edge is dead
        decay_warn: float = 0.52,    # below this → edge is decaying
        min_trades: int = 5,         # need at least N trades to judge
    ) -> None:
        self.window = window
        self.min_win_rate = min_win_rate
        self.decay_warn = decay_warn
        self.min_trades = min_trades
        self._records: List[EdgeRecord] = []

    def record_outcome(self, ts: int, agents: List[str], won: bool, r_multiple: float) -> None:
        """Record a trade outcome for all agents that voted for this action."""
        for agent in agents:
            self._records.append(EdgeRecord(ts=ts, agent=agent, won=won, r_multiple=r_multiple))

    def _agent_records(self, agent: str) -> List[EdgeRecord]:
        recs = [r for r in self._records if r.agent == agent]
        return recs[-self.window:]  # rolling window

    def win_rate(self, agent: str) -> Optional[float]:
        recs = self._agent_records(agent)
        if len(recs) < self.min_trades:
            return None  # insufficient data
        return sum(1 for r in recs if r.won) / len(recs)

    def decay_velocity(self, agent: str) -> Optional[float]:
        """Compute how fast the win rate is dropping.

        Compares win rate of the first half vs second half of the window.
        Negative = decaying. Positive = improving.
        """
        recs = self._agent_records(agent)
        if len(recs) < self.min_trades * 2:
            return None
        mid = len(recs) // 2
        first_half = sum(1 for r in recs[:mid] if r.won) / mid
        second_half = sum(1 for r in recs[mid:] if r.won) / (len(recs) - mid)
        return second_half - first_half

    def edge_status(self, agent: str) -> Tuple[str, Optional[float], Optional[float]]:
        """Returns (status, win_rate, decay_velocity).

        status: "healthy" | "decaying" | "dead" | "insufficient_data"
        """
        wr = self.win_rate(agent)
        dv = self.decay_velocity(agent)
        if wr is None:
            return ("insufficient_data", None, None)
        if wr < self.min_win_rate:
            return ("dead", wr, dv)
        if wr < self.decay_warn or (dv is not None and dv < -0.10):
            return ("decaying", wr, dv)
        return ("healthy", wr, dv)

    def system_edge_health(self, agents: List[str]) -> Dict[str, Any]:
        """Aggregate edge health across all agents."""
        report: Dict[str, Any] = {}
        dead_count = 0
        decaying_count = 0
        for agent in agents:
            status, wr, dv = self.edge_status(agent)
            report[agent] = {"status": status, "win_rate": wr, "decay_velocity": dv}
            if status == "dead":
                dead_count += 1
            elif status == "decaying":
                decaying_count += 1
        report["_summary"] = {
            "dead": dead_count,
            "decaying": decaying_count,
            "total": len(agents),
            "system_healthy": dead_count == 0,
        }
        return report

    def save(self, path: str) -> None:
        data = [dataclasses.asdict(r) for r in self._records]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load(self, path: str) -> None:
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._records = [EdgeRecord(**d) for d in data]
        except Exception:
            pass  # corrupt file — start fresh


# =========================
# Indicators (no deps)
# =========================
def atr(ohlcv: List[Tuple[int, float, float, float, float, float]], period: int = 14) -> float:
    if len(ohlcv) < 2:
        return 0.0
    trs = []
    for i in range(1, len(ohlcv)):
        _, _, h, l, c, _ = ohlcv[i]
        _, _, _, _, prev_c, _ = ohlcv[i - 1]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    window = trs[-period:] if len(trs) >= period else trs
    return sum(window) / max(1, len(window))


def realized_vol_from_closes(closes: List[float]) -> float:
    if len(closes) < 3:
        return 0.0
    rets = []
    for i in range(1, len(closes)):
        if closes[i - 1] <= 0:
            continue
        rets.append(math.log(closes[i] / closes[i - 1]))
    if len(rets) < 2:
        return 0.0
    mu = sum(rets) / len(rets)
    var = sum((x - mu) ** 2 for x in rets) / len(rets)
    return math.sqrt(var)


# =========================
# Market data provider (simulated MVP)
# =========================
class MarketDataProvider:
    def get_snapshot(self, symbol: str, lookback: int) -> MarketSnapshot:
        raise NotImplementedError


class SimulatedProvider(MarketDataProvider):
    def __init__(self, seed: int = 42, start_price: float = 50000.0):
        self.rng = random.Random(seed)
        self.price = start_price
        self.ts = int(time.time())

    def _step(self) -> float:
        shock = self.rng.gauss(0, 1)
        drift = 0.02 * self.rng.choice([-1, 1])
        pct = (drift + 0.28 * shock) / 100.0
        self.price = max(10.0, self.price * (1 + pct))
        self.ts += 60
        return self.price

    def get_snapshot(self, symbol: str, lookback: int) -> MarketSnapshot:
        ohlcv = []
        p = self.price
        ts = self.ts - lookback * 60
        for _ in range(lookback):
            o = p
            c = self._step()
            hi = max(o, c) * (1 + abs(self.rng.gauss(0, 0.0010)))
            lo = min(o, c) * (1 - abs(self.rng.gauss(0, 0.0010)))
            v = abs(self.rng.gauss(120, 40))
            ts += 60
            ohlcv.append((ts, o, hi, lo, c, v))
            p = c
        return MarketSnapshot(symbol=symbol, ts=self.ts, price=self.price, ohlcv=ohlcv)


# =========================
# Live market data provider (real CoinGecko prices)
# NOTE: Free-tier OHLC returns 30-min candles for days=1.
# V1 goal: pipeline connectivity only. Signal calibration is future work.
# =========================
def _fetch_json_simple(url: str, timeout: int = 15) -> Any:
    """Minimal JSON fetch using stdlib urllib. No third-party deps."""
    req = urllib.request.Request(url, headers={"User-Agent": "council-agent/2.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class CoinGeckoLiveProvider(MarketDataProvider):
    """Fetches real market data from CoinGecko public API (stdlib only, zero deps).

    ⚠️  Free-tier OHLC granularity: days=1 → 30-min candles (~48 candles).
        Agent thresholds were calibrated on 1-min simulated candles.
        V1 acceptance criteria: pipeline connectivity, NOT signal quality.
    ⚠️  Volume field is always 0.0 — CoinGecko OHLC endpoint omits volume.
        Sentinel volume-drop detection is silenced when all volume=0.
    """

    # Map from trading symbol (e.g. "BTC/USDT") to CoinGecko coin ID
    _DEFAULT_COIN_MAP: Dict[str, str] = {
        "BTC/USDT": "bitcoin",
        "ETH/USDT": "ethereum",
        "SOL/USDT": "solana",
        "BTC": "bitcoin",
        "ETH": "ethereum",
        "SOL": "solana",
    }

    def __init__(
        self,
        coin_map: Optional[Dict[str, str]] = None,
        cache_ttl_sec: int = 45,
    ) -> None:
        self._coin_map: Dict[str, str] = {**self._DEFAULT_COIN_MAP, **(coin_map or {})}
        self._cache_ttl = cache_ttl_sec
        self._cache: Optional[MarketSnapshot] = None
        self._cache_ts: float = 0.0
        self._base = "https://api.coingecko.com/api/v3"

    def _coin_id(self, symbol: str) -> str:
        """Resolve trading symbol to CoinGecko coin ID."""
        cid = self._coin_map.get(symbol) or self._coin_map.get(symbol.split("/")[0].upper())
        if cid:
            return cid
        # Fallback: lowercase base asset
        return symbol.split("/")[0].lower()

    def _fetch_price(self, coin_id: str) -> float:
        url = f"{self._base}/simple/price?ids={urllib.parse.quote(coin_id)}&vs_currencies=usd"
        data = _fetch_json_simple(url, timeout=10)
        price = data.get(coin_id, {}).get("usd")
        if price is None:
            raise ValueError(
                f"CoinGecko returned no price for coin_id={coin_id!r}. "
                f"Check COIN_MAP or symbol mapping. Response keys: {list(data.keys())}"
            )
        return float(price)

    def _fetch_ohlc(self, coin_id: str, days: int = 1) -> List[Tuple[int, float, float, float, float, float]]:
        """Fetch OHLC candles. Returns (ts_sec, open, high, low, close, volume=0.0).

        Volume is always 0.0 — CoinGecko OHLC endpoint omits it on the free tier.
        Raises ValueError if the response is empty (e.g. silent rate-limit 200).
        """
        url = f"{self._base}/coins/{urllib.parse.quote(coin_id)}/ohlc?vs_currency=usd&days={days}"
        raw = _fetch_json_simple(url, timeout=15)
        if not raw:
            raise ValueError(
                f"CoinGecko returned empty OHLC for coin_id={coin_id!r} (possible silent rate-limit). "
                "Retry after 60s or check API status."
            )
        ohlcv: List[Tuple[int, float, float, float, float, float]] = []
        for candle in raw:
            ts_sec = int(candle[0] / 1000)  # ms → sec
            o, h, l, c = float(candle[1]), float(candle[2]), float(candle[3]), float(candle[4])
            ohlcv.append((ts_sec, o, h, l, c, 0.0))  # volume always 0.0 on free tier
        return ohlcv

    def get_snapshot(self, symbol: str, lookback: int) -> MarketSnapshot:
        now = time.time()
        if self._cache is not None and (now - self._cache_ts) < self._cache_ttl:
            return self._cache  # rate-limit protection

        coin_id = self._coin_id(symbol)
        try:
            price = self._fetch_price(coin_id)
            ohlcv = self._fetch_ohlc(coin_id, days=1)
            ohlcv = ohlcv[-lookback:] if len(ohlcv) > lookback else ohlcv
            # Warn if volume data is missing (affects Sentinel volume-drop detection)
            if ohlcv and all(v == 0.0 for *_, v in ohlcv):
                print("⚠️  WARNING: volume=0 for all candles — Sentinel volume-drop detection silenced")
            snap = MarketSnapshot(symbol=symbol, ts=int(now), price=price, ohlcv=ohlcv)
            self._cache = snap
            self._cache_ts = now
            return snap
        except Exception as exc:
            if self._cache is not None:
                print(f"⚠️  CoinGeckoLiveProvider error ({exc}), using cached snapshot")
                return self._cache
            raise


class BitgetLiveProvider(MarketDataProvider):
    """Fetches real market data from Bitget public spot API (stdlib only, zero deps).

    Advantages over CoinGeckoLiveProvider:
    - Real-time ticker price (not 30-min delayed)
    - 15-min candles with REAL volume data (not volume=0)
    - Higher rate limit (20 req/s vs CoinGecko's ~10/min)
    - No API key required for public endpoints
    """

    _DEFAULT_SYMBOL_MAP: Dict[str, str] = {
        "BTC/USDT": "BTCUSDT",
        "ETH/USDT": "ETHUSDT",
        "SOL/USDT": "SOLUSDT",
    }

    def __init__(
        self,
        symbol_map: Optional[Dict[str, str]] = None,
        cache_ttl_sec: int = 30,
        granularity: str = "15min",
    ) -> None:
        self._symbol_map = {**self._DEFAULT_SYMBOL_MAP, **(symbol_map or {})}
        self._cache_ttl = cache_ttl_sec
        self._granularity = granularity
        self._cache: Optional[MarketSnapshot] = None
        self._cache_ts: float = 0.0
        self._base = "https://api.bitget.com/api/v2/spot/market"

    def _bitget_symbol(self, symbol: str) -> str:
        mapped = self._symbol_map.get(symbol)
        if mapped:
            return mapped
        # "BTC/USDT" → "BTCUSDT"
        return symbol.replace("/", "")

    def _fetch_price(self, bg_symbol: str) -> float:
        url = f"{self._base}/tickers?symbol={bg_symbol}"
        data = _fetch_json_simple(url, timeout=10)
        tickers = data.get("data", [])
        if not tickers:
            raise ValueError(f"Bitget returned no ticker for {bg_symbol}")
        return float(tickers[0].get("lastPr", 0))

    def _fetch_candles(self, bg_symbol: str, limit: int = 200) -> List[Tuple[int, float, float, float, float, float]]:
        """Fetch OHLCV candles from Bitget.

        Response format: [[ts_ms, open, high, low, close, baseVol, quoteVol], ...]
        """
        url = (
            f"{self._base}/candles"
            f"?symbol={bg_symbol}"
            f"&granularity={self._granularity}"
            f"&limit={min(limit, 1000)}"
        )
        data = _fetch_json_simple(url, timeout=15)
        raw = data.get("data", [])
        if not raw:
            raise ValueError(f"Bitget returned empty candles for {bg_symbol}")

        ohlcv: List[Tuple[int, float, float, float, float, float]] = []
        for candle in raw:
            # candle = [ts_ms, open, high, low, close, baseVol, quoteVol]
            ts_sec = int(candle[0]) // 1000
            o, h, l, c = float(candle[1]), float(candle[2]), float(candle[3]), float(candle[4])
            vol = float(candle[5]) if len(candle) > 5 else 0.0
            ohlcv.append((ts_sec, o, h, l, c, vol))

        # Bitget returns newest-first; reverse to oldest-first for agents
        ohlcv.sort(key=lambda x: x[0])
        return ohlcv

    def get_snapshot(self, symbol: str, lookback: int) -> MarketSnapshot:
        now = time.time()
        if self._cache is not None and (now - self._cache_ts) < self._cache_ttl:
            return self._cache

        bg_symbol = self._bitget_symbol(symbol)
        try:
            price = self._fetch_price(bg_symbol)
            ohlcv = self._fetch_candles(bg_symbol, limit=lookback)
            ohlcv = ohlcv[-lookback:] if len(ohlcv) > lookback else ohlcv
            snap = MarketSnapshot(symbol=symbol, ts=int(now), price=price, ohlcv=ohlcv)
            self._cache = snap
            self._cache_ts = now
            return snap
        except Exception as exc:
            if self._cache is not None:
                print(f"⚠️  BitgetLiveProvider error ({exc}), using cached snapshot")
                return self._cache
            raise



# =========================
# Intel Bridge (reads council_intel.py report.json artifacts)
# =========================
@dataclass
class IntelSignal:
    """Policy signal extracted from a council_intel.py report."""
    recommended_action: str   # "investigate" | "watch" | "no_trade"
    crowding_risk: str        # "low" | "moderate" | "elevated" | "high"
    tags: List[str]
    evidence_diversity: int
    stale: bool               # True if report is older than staleness_sec
    report_ts: str            # ISO timestamp of the report


class IntelBridge:
    """Reads the latest council_intel.py report.json and returns a policy signal.

    If no report is available or readable, returns None — trading continues normally.
    """

    def __init__(self, intel_dir: str = "artifacts/intel", staleness_sec: int = 1800) -> None:
        self.intel_dir = Path(intel_dir)
        self.staleness_sec = staleness_sec

    def _find_latest_report(self) -> Optional[Path]:
        if not self.intel_dir.exists():
            return None
        subdirs = sorted(
            [d for d in self.intel_dir.iterdir() if d.is_dir()],
            key=lambda d: d.name,
            reverse=True,
        )
        for d in subdirs:
            rpt = d / "report.json"
            if rpt.exists():
                return rpt
        return None

    def _is_stale(self, report_ts_str: str) -> bool:
        try:
            # Parse "2026-04-21T10:22:32Z"
            ts = dt.datetime.fromisoformat(report_ts_str.replace("Z", "+00:00"))
            age = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds()
            return age > self.staleness_sec
        except Exception:
            return True  # unparseable timestamp → treat as stale

    def get_signal(self, symbol: str) -> Optional[IntelSignal]:
        """Return the latest intel policy for symbol, or None if unavailable."""
        rpt_path = self._find_latest_report()
        if rpt_path is None:
            return None
        try:
            report: Dict = json.loads(rpt_path.read_text(encoding="utf-8"))
        except Exception:
            return None  # partial write / corrupt file — don't block trading

        report_ts = report.get("run", {}).get("started_at", "")
        stale = self._is_stale(report_ts)

        # Normalize: "BTC/USDT" → "BTC"
        base_symbol = symbol.split("/")[0].upper()

        for asset in report.get("assets", []):
            if asset.get("symbol", "").upper() == base_symbol:
                policy = asset.get("policy", {})
                return IntelSignal(
                    recommended_action=policy.get("recommended_action", "no_trade"),
                    crowding_risk=policy.get("crowding_risk", "moderate"),
                    tags=asset.get("tags", []),
                    evidence_diversity=int(policy.get("evidence_diversity", 0)),
                    stale=stale,
                    report_ts=report_ts,
                )
        return None  # symbol not found in report

    def get_funding(self, symbol: str) -> Optional[Dict]:
        """Return raw funding data for symbol from latest report.

        Returns dict with keys: current_rate, is_anomalous, is_turning,
        open_interest, basis, volume_24h, exchange, etc.
        Returns None if unavailable OR if the report is stale.
        """
        rpt_path = self._find_latest_report()
        if rpt_path is None:
            return None
        try:
            report: Dict = json.loads(rpt_path.read_text(encoding="utf-8"))
        except Exception:
            return None

        # Staleness check — same rule as get_signal()
        report_ts = report.get("run", {}).get("started_at", "")
        if self._is_stale(report_ts):
            return None  # stale funding must not steer votes

        base_symbol = symbol.split("/")[0].upper()
        for asset in report.get("assets", []):
            if asset.get("symbol", "").upper() == base_symbol:
                funding_provider = asset.get("providers", {}).get("funding", {})
                if funding_provider.get("status") == "ok":
                    return funding_provider.get("data")
        return None


# =========================
# Agents (simple deterministic heuristics)
# =========================
def ema(values: List[float], period: int) -> float:
    if not values or period <= 0:
        return float("nan")
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def bollinger(values: List[float], period: int = 20, k: float = 2.0) -> Tuple[float, float, float]:
    if len(values) < period:
        m = sum(values) / max(1, len(values))
        return (m, m, m)
    window = values[-period:]
    m = sum(window) / period
    if len(window) > 1:
        mu = sum(window) / len(window)
        sd = math.sqrt(sum((x - mu)**2 for x in window) / len(window))
    else:
        sd = 0.0
    return (m - k * sd, m, m + k * sd)


def pivot_levels(ohlcv: List[Tuple[int, float, float, float, float, float]]) -> Tuple[float, float]:
    if not ohlcv:
        return (float("nan"), float("nan"))
    _, _, h, l, c, _ = ohlcv[-1]
    p = (h + l + c) / 3
    r1 = 2 * p - l
    s1 = 2 * p - h
    return (s1, r1)


class Agent:
    name: str
    def decide(self, snap: MarketSnapshot) -> Verdict:
        raise NotImplementedError


class TrendAgent(Agent):
    name = "TrendAgent"
    def decide(self, snap: MarketSnapshot) -> Verdict:
        closes = [c for (_, _, _, _, c, _) in snap.ohlcv]
        fast = ema(closes[-80:], 9)
        slow = ema(closes[-80:], 21)
        a = atr(snap.ohlcv, 14) or 1.0
        diff = (fast - slow) / a
        if diff > 0.55:
            conf = min(0.95, 0.55 + abs(diff) * 0.22)
            return Verdict(self.name, Action.LONG, conf, f"EMA9>EMA21 by {diff:.2f} ATR",
                           counterparty_thesis="Late shorts and range-bound sellers who haven't recognized the trend shift")
        if diff < -0.55:
            conf = min(0.95, 0.55 + abs(diff) * 0.22)
            return Verdict(self.name, Action.SHORT, conf, f"EMA9<EMA21 by {diff:.2f} ATR",
                           counterparty_thesis="Dip buyers and bag holders averaging down against the trend")
        return Verdict(self.name, Action.HOLD, 0.60, f"No clear EMA separation (diff {diff:.2f})")



class SupportResistanceAgent(Agent):
    name = "SupportResistanceAgent"
    def decide(self, snap: MarketSnapshot) -> Verdict:
        price = snap.price
        s1, r1 = pivot_levels(snap.ohlcv)
        a = atr(snap.ohlcv, 14) or 1.0
        if not (math.isfinite(s1) and math.isfinite(r1)):
            return Verdict(self.name, Action.HOLD, 0.55, "No pivot levels")
        dist_to_s = (price - s1) / a
        dist_to_r = (r1 - price) / a
        if 0 <= dist_to_s <= 0.8:
            conf = min(0.88, 0.60 + (0.8 - dist_to_s) * 0.35)
            return Verdict(self.name, Action.LONG, conf, f"Near support S1 ({dist_to_s:.2f} ATR away)",
                           counterparty_thesis="Stop-loss clusters below support; breakout sellers who will cover if support holds")
        if 0 <= dist_to_r <= 0.8:
            conf = min(0.88, 0.60 + (0.8 - dist_to_r) * 0.35)
            return Verdict(self.name, Action.SHORT, conf, f"Near resistance R1 ({dist_to_r:.2f} ATR away)",
                           counterparty_thesis="Breakout buyers above resistance who will be trapped if rejection occurs")
        return Verdict(self.name, Action.HOLD, 0.55, "Not near key S/R zone")


class RiskAgent(Agent):
    """Risk-as-viscosity: high vol => HOLD (friction). Normal vol => follow trend (flow).

    Previous design always returned HOLD, making 4/5 agreement impossible.
    Now acts as a confirming signal when conditions are safe.
    """
    name = "RiskAgent"
    def __init__(self, vol_lookback: int = 30):
        self.vol_lookback = vol_lookback
    def decide(self, snap: MarketSnapshot) -> Verdict:
        closes = [c for (_, _, _, _, c, _) in snap.ohlcv]
        v = realized_vol_from_closes(closes[-self.vol_lookback:])
        vol_score = v * math.sqrt(60)
        if vol_score > 0.03:
            # High vol = viscosity: friction against new positions
            conf = min(0.95, 0.75 + (vol_score - 0.03) * 5)
            return Verdict(self.name, Action.HOLD, conf, f"High realized vol ({vol_score:.3f}) => reduce action")
        # Normal vol = flow: follow the short-term trend direction
        fast = ema(closes[-40:], 5)
        slow = ema(closes[-40:], 15)
        if fast > slow:
            return Verdict(self.name, Action.LONG, 0.60, f"Vol safe ({vol_score:.3f}), trend up => risk OK",
                           counterparty_thesis="Low-vol environment favors positioned capital; sidelined traders miss the move")
        if fast < slow:
            return Verdict(self.name, Action.SHORT, 0.60, f"Vol safe ({vol_score:.3f}), trend down => risk OK",
                           counterparty_thesis="Low-vol environment favors positioned capital; sidelined traders miss the move")
        return Verdict(self.name, Action.HOLD, 0.55, f"Vol safe ({vol_score:.3f}), no trend => neutral")



class LiquidationPressureAgent(Agent):
    """Reads funding rate / OI from IntelBridge to identify forced sellers.

    This agent looks at market microstructure, not price charts:
    - Extreme positive funding → longs are crowded → short (wait for liquidation cascade)
    - Extreme negative funding → shorts are crowded → long (wait for short squeeze)
    - Funding turning → someone is switching sides → follow the new direction
    """
    name = "LiquidationPressureAgent"

    def __init__(self, intel_bridge: Optional["IntelBridge"] = None):
        self.intel_bridge = intel_bridge

    def decide(self, snap: MarketSnapshot) -> Verdict:
        if self.intel_bridge is None:
            return Verdict(self.name, Action.HOLD, 0.40, "No intel bridge")

        funding = self.intel_bridge.get_funding(snap.symbol)
        if funding is None:
            return Verdict(self.name, Action.HOLD, 0.40, "No funding data available")

        rate = funding.get("current_rate", 0) or 0
        is_anomalous = funding.get("is_anomalous", False)
        is_turning = funding.get("is_turning", False)
        oi = funding.get("open_interest")
        basis = funding.get("basis")
        exchange = funding.get("exchange", "unknown")

        oi_str = f"${oi/1e9:.1f}B" if oi and oi > 0 else "unknown"
        basis_str = f"{basis:.2f}%" if basis is not None else "N/A"
        rate_pct = rate * 100

        # Extreme positive funding: longs pay, longs crowded → fade them
        if rate > 0.0005 and is_anomalous:
            thesis = (f"Longs paying {rate_pct:.3f}%/8h on {exchange}. "
                      f"OI={oi_str}, basis={basis_str}. "
                      f"Overleveraged longs face liquidation cascade if price drops 2-3%.")
            conf = min(0.85, 0.65 + abs(rate) * 50)
            return Verdict(self.name, Action.SHORT, conf,
                           f"Extreme +funding ({rate_pct:.3f}%): longs crowded",
                           counterparty_thesis=thesis)

        # Extreme negative funding: shorts pay, shorts crowded → fade them
        if rate < -0.0005 and is_anomalous:
            thesis = (f"Shorts paying {abs(rate_pct):.3f}%/8h on {exchange}. "
                      f"OI={oi_str}, basis={basis_str}. "
                      f"Overleveraged shorts face squeeze if price rises 2-3%.")
            conf = min(0.85, 0.65 + abs(rate) * 50)
            return Verdict(self.name, Action.LONG, conf,
                           f"Extreme -funding ({rate_pct:.3f}%): shorts crowded",
                           counterparty_thesis=thesis)

        # Funding turning direction: someone switching sides
        if is_turning:
            if rate > 0:
                thesis = f"Funding turning positive ({rate_pct:.3f}%). Previous shorts being squeezed. OI={oi_str}."
                return Verdict(self.name, Action.LONG, 0.62,
                               f"Funding turning +({rate_pct:.3f}%)",
                               counterparty_thesis=thesis)
            else:
                thesis = f"Funding turning negative ({rate_pct:.3f}%). Previous longs being liquidated. OI={oi_str}."
                return Verdict(self.name, Action.SHORT, 0.62,
                               f"Funding turning -({rate_pct:.3f}%)",
                               counterparty_thesis=thesis)

        return Verdict(self.name, Action.HOLD, 0.45,
                       f"Funding normal ({rate_pct:.3f}%), no liquidation pressure")


class FundingFlowAgent(Agent):
    """Orthogonal to TrendAgent: looks at funding rate direction, not price.

    Replaces ContrarianAgent which was just TrendAgent inverted (= noise).
    This agent provides genuinely different information:
    - TrendAgent sees price momentum
    - FundingFlowAgent sees capital flow momentum
    Together = binocular vision.
    """
    name = "FundingFlowAgent"

    def __init__(self, intel_bridge: Optional["IntelBridge"] = None):
        self.intel_bridge = intel_bridge

    def decide(self, snap: MarketSnapshot) -> Verdict:
        if self.intel_bridge is None:
            return Verdict(self.name, Action.HOLD, 0.40, "No intel bridge")

        funding = self.intel_bridge.get_funding(snap.symbol)
        if funding is None:
            return Verdict(self.name, Action.HOLD, 0.40, "No funding data")

        rate = funding.get("current_rate", 0) or 0
        oi = funding.get("open_interest")
        basis = funding.get("basis", 0) or 0
        rate_pct = rate * 100
        oi_str = f"${oi/1e9:.1f}B" if oi and oi > 0 else "?"

        # Moderate positive funding = capital flowing into longs = bullish flow
        if 0.0001 < rate <= 0.0005:
            thesis = f"Moderate long flow: funding +{rate_pct:.3f}%, OI={oi_str}. Shorts paying less = healthy positioning."
            return Verdict(self.name, Action.LONG, 0.58,
                           f"Positive funding flow ({rate_pct:.3f}%)",
                           counterparty_thesis=thesis)

        # Moderate negative funding = capital flowing into shorts = bearish flow
        if -0.0005 <= rate < -0.0001:
            thesis = f"Moderate short flow: funding {rate_pct:.3f}%, OI={oi_str}. Longs paying less = healthy positioning."
            return Verdict(self.name, Action.SHORT, 0.58,
                           f"Negative funding flow ({rate_pct:.3f}%)",
                           counterparty_thesis=thesis)

        # Extreme rates handled by LiquidationPressureAgent, not us
        return Verdict(self.name, Action.HOLD, 0.45,
                       f"Funding flat ({rate_pct:.3f}%), no clear flow")


# =========================
# Signal Memory (water momentum)
# =========================

# =========================
# Judge + Guardian (pre-trade)
# =========================
class JudgeAgent:
    def __init__(self, min_agents_agree: int = 4):
        self.min_agents_agree = min_agents_agree
        self.weights = {
            "TrendAgent": 1.0,
            "SupportResistanceAgent": 1.0,
            "RiskAgent": 0.5,
            "LiquidationPressureAgent": 1.2,  # highest weight: microstructure > chart patterns
            "FundingFlowAgent": 0.8,
        }

    def aggregate(self, verdicts: List[Verdict]) -> Consensus:
        notes: List[str] = []
        total = len(verdicts)
        score = {Action.LONG: 0.0, Action.SHORT: 0.0, Action.HOLD: 0.0}
        conf_sum = {Action.LONG: 0.0, Action.SHORT: 0.0, Action.HOLD: 0.0}
        count = {Action.LONG: 0, Action.SHORT: 0, Action.HOLD: 0}
        weight_sum = {Action.LONG: 0.0, Action.SHORT: 0.0, Action.HOLD: 0.0}

        for v in verdicts:
            w = self.weights.get(v.agent, 1.0)
            score[v.action] += w * v.confidence
            conf_sum[v.action] += v.confidence
            count[v.action] += 1
            weight_sum[v.action] += w

        best = max([Action.LONG, Action.SHORT], key=lambda a: score[a])
        chosen = best if score[best] > score[Action.HOLD] + 0.10 else Action.HOLD

        agree = count[chosen]
        agreement_ratio = agree / total if total else 0.0

        if chosen != Action.HOLD and agree < self.min_agents_agree:
            notes.append(f"Veto: agree_count {agree} < min_agents_agree {self.min_agents_agree}")
            chosen = Action.HOLD
            agree = count[chosen]
            agreement_ratio = agree / total if total else 0.0

        # Use weight-adjusted agreement: a 0.35-weight dissenter hurts less than a 1.0-weight one
        total_weight = sum(self.weights.get(v.agent, 1.0) for v in verdicts)
        chosen_weight = weight_sum[chosen]
        w_agreement = chosen_weight / total_weight if total_weight > 0 else 0.0

        avg_conf = (conf_sum[chosen] / count[chosen]) if count[chosen] else 0.0
        disagreement = 1.0 - w_agreement
        cons_conf = max(0.0, min(1.0, avg_conf * (1.0 - 0.5 * disagreement)))

        if w_agreement >= 0.90 and total >= 3:
            notes.append("Warning: agreement >= 90% (possible groupthink) => cap confidence")
            cons_conf = min(cons_conf, 0.80)

        # Merge counterparty theses from agreeing agents
        theses = []
        for v in verdicts:
            if v.action == chosen and v.counterparty_thesis:
                theses.append(f"[{v.agent}] {v.counterparty_thesis}")
        merged_thesis = "; ".join(theses)

        return Consensus(chosen, cons_conf, agree, total, agreement_ratio, notes, verdicts,
                         counterparty_thesis=merged_thesis)


class PhilosophyGuardian:
    def __init__(self, laws: IronLaws, fees: Fees, risk: RiskConfig):
        self.laws = laws
        self.fees = fees
        self.risk = risk

    def _estimate_cost(self, notional: float) -> float:
        return notional * (self.fees.taker_fee * 2)

    def build_plan(self, cons: Consensus, snap: MarketSnapshot, portfolio: PortfolioState) -> Optional[TradePlan]:
        if cons.action == Action.HOLD:
            return None
        price = snap.price
        a = atr(snap.ohlcv, 14) or (price * 0.002)

        # conservative sizing
        closes = [c for (_, _, _, _, c, _) in snap.ohlcv]
        rv = realized_vol_from_closes(closes[-self.risk.vol_lookback:]) or 0.0001
        vol_scale = 1.0 / (1.0 + 50.0 * rv)
        base_pos = self.laws.MAX_POSITION * 0.8
        position_frac = max(0.01, min(self.laws.MAX_POSITION, base_pos * vol_scale))
        leverage = max(1.0, min(self.laws.MAX_LEVERAGE, self.risk.initial_leverage))

        sl_dist = 1.2 * a
        tp_dist = self.risk.rr_target * sl_dist
        stop = price - sl_dist if cons.action == Action.LONG else price + sl_dist
        take = price + tp_dist if cons.action == Action.LONG else price - tp_dist

        equity = portfolio.equity
        notional = equity * position_frac * leverage
        expected_cost = self._estimate_cost(notional)
        qty = notional / price
        gross_profit = abs(take - price) * qty
        expected_profit = gross_profit - expected_cost

        return TradePlan(cons.action, snap.symbol, leverage, position_frac, price, stop, take,
                         self.laws.STOP_LOSS_MODE, expected_profit, expected_cost,
                         counterparty_thesis=cons.counterparty_thesis)

    def evaluate(self, cons: Consensus, plan: TradePlan) -> Tuple[bool, List[str]]:
        reasons: List[str] = []
        required_min_confidence = self.laws.MIN_CONFIDENCE
        risk_vote = next((v for v in cons.raw if v.agent == "RiskAgent"), None)
        if risk_vote and risk_vote.action == Action.HOLD and risk_vote.confidence >= 0.85:
            required_min_confidence = max(required_min_confidence, 0.90)
        if cons.confidence < required_min_confidence:
            reasons.append(f"MIN_CONFIDENCE fail: {cons.confidence:.2f} < {required_min_confidence:.2f}")
        if plan.leverage > self.laws.MAX_LEVERAGE:
            reasons.append(f"MAX_LEVERAGE fail: {plan.leverage:.2f} > {self.laws.MAX_LEVERAGE:.2f}")
        if plan.position_frac > self.laws.MAX_POSITION:
            reasons.append(f"MAX_POSITION fail: {plan.position_frac:.2f} > {self.laws.MAX_POSITION:.2f}")
        if plan.stoploss_mode != "server":
            reasons.append("STOP_LOSS_MODE fail: must be 'server'")
        if plan.expected_profit < self.laws.MIN_PROFIT_MULT * plan.expected_cost:
            reasons.append(
                f"MIN_PROFIT fail: profit {plan.expected_profit:.2f} < "
                f"{self.laws.MIN_PROFIT_MULT:.1f} * cost {plan.expected_cost:.2f}"
            )
        # "一切优势都会腐烂" — No trade without a counterparty thesis
        if not plan.counterparty_thesis.strip():
            reasons.append("NO_COUNTERPARTY_THESIS: cannot trade without knowing who loses")
        return (len(reasons) == 0), reasons


# =========================
# SafetySentinel (post-trade + ops)
# =========================
class SafetySentinel:
    def __init__(self, cfg: SentinelConfig):
        self.cfg = cfg

    def _vol_zscore(self, closes: List[float]) -> float:
        w = self.cfg.vol_window
        if len(closes) < w * 3:
            return 0.0
        vols = []
        for i in range(w, len(closes) + 1):
            window = closes[i - w:i]
            vols.append(realized_vol_from_closes(window))
        if len(vols) < 5:
            return 0.0
        current = vols[-1]
        hist = vols[:-1]
        mu = sum(hist) / len(hist) if hist else 0.0
        sd = math.sqrt(sum((x - mu)**2 for x in hist) / len(hist)) if hist else 1e-9
        if sd == 0: sd = 1e-9
        return (current - mu) / sd

    def _volume_drop(self, vols: List[float], lookback: int = 20) -> float:
        if len(vols) < lookback + 1:
            return 0.0
        now = vols[-1]
        base_slice = vols[-(lookback + 1):-1]
        base = sum(base_slice) / len(base_slice) if base_slice else 1e-9
        return max(0.0, (base - now) / base)

    def evaluate(self, snap: MarketSnapshot, state: PortfolioState, enabled: bool = True) -> Tuple[AlertLevel, List[str]]:
        if not enabled:
            return AlertLevel.GREEN, ["Sentinel disabled (baseline)"]

        reasons: List[str] = []
        closes = [c for (_, _, _, _, c, _) in snap.ohlcv]
        vols = [v for (_, _, _, _, _, v) in snap.ohlcv]

        if state.api_failures > self.cfg.api_fail_red:
            return AlertLevel.RED, [f"API failures {state.api_failures} > {self.cfg.api_fail_red}"]

        vol_z = self._vol_zscore(closes)
        vol_drop = self._volume_drop(vols, 20)

        candidate = AlertLevel.GREEN
        if vol_z >= self.cfg.vol_z_orange or vol_drop >= self.cfg.volume_drop_orange:
            candidate = AlertLevel.ORANGE
        elif vol_z >= self.cfg.vol_z_yellow:
            candidate = AlertLevel.YELLOW

        reasons.append(f"vol_z={vol_z:.2f} (y={self.cfg.vol_z_yellow}, o={self.cfg.vol_z_orange})")
        reasons.append(f"volume_drop={vol_drop:.0%} (o={self.cfg.volume_drop_orange:.0%})")

        current = state.sentinel_level
        if int(candidate) > int(current):
            state.sentinel_hit_streak += 1
            state.sentinel_clear_streak = 0
            if state.sentinel_hit_streak >= self.cfg.confirm_cycles:
                state.sentinel_level = candidate
                state.sentinel_hit_streak = 0
                reasons.append(f"Escalated to {state.sentinel_level} (confirmed)")
        elif int(candidate) < int(current):
            state.sentinel_clear_streak += 1
            state.sentinel_hit_streak = 0
            if state.sentinel_clear_streak >= self.cfg.clear_cycles:
                state.sentinel_level = candidate
                state.sentinel_clear_streak = 0
                reasons.append(f"De-escalated to {state.sentinel_level} (confirmed)")
        else:
            state.sentinel_hit_streak = 0
            state.sentinel_clear_streak = 0

        return state.sentinel_level, reasons


# =========================
# Paper broker (execution)
# =========================
class PaperBroker:
    def __init__(self, fees: Fees):
        self.fees = fees

    def _fee(self, notional: float) -> float:
        return notional * self.fees.taker_fee

    def mark_to_market(self, portfolio: PortfolioState, snap: MarketSnapshot) -> None:
        if portfolio.position is None:
            portfolio.equity = portfolio.cash
        else:
            pos = portfolio.position
            price = snap.price
            pnl = (price - pos.entry) * pos.qty * pos.leverage
            if pos.action == Action.SHORT:
                pnl = (pos.entry - price) * pos.qty * pos.leverage
            portfolio.equity = portfolio.cash + pnl
        portfolio.high_watermark = max(portfolio.high_watermark, portfolio.equity)

    def _pnl(self, pos: Position, price: float) -> float:
        if pos.action == Action.LONG:
            return (price - pos.entry) * pos.qty * pos.leverage
        return (pos.entry - price) * pos.qty * pos.leverage

    def maybe_close_by_sl_tp(self, portfolio: PortfolioState, snap: MarketSnapshot) -> Optional[Dict]:
        pos = portfolio.position
        if pos is None:
            return None
        price = snap.price
        hit_stop = (price <= pos.stop) if pos.action == Action.LONG else (price >= pos.stop)
        hit_take = (price >= pos.take) if pos.action == Action.LONG else (price <= pos.take)
        if not (hit_stop or hit_take):
            return None
        return self.force_close_all(portfolio, snap, reason="STOP" if hit_stop else "TAKE")

    def force_close_all(self, portfolio: PortfolioState, snap: MarketSnapshot, reason: str = "FORCE") -> Dict:
        pos = portfolio.position
        if pos is None:
            return {"type": "CLOSE", "reason": reason, "note": "no position"}
        price = snap.price
        notional = pos.qty * price * pos.leverage
        fee = self._fee(notional)
        pnl = self._pnl(pos, price)
        portfolio.cash = portfolio.cash + pnl - fee
        portfolio.position = None
        return {"type": "CLOSE", "reason": reason, "exit_price": price, "pnl": pnl, "fee": fee, "equity": portfolio.cash}

    def close_partial(self, portfolio: PortfolioState, snap: MarketSnapshot, frac: float, reason: str = "TP1") -> Optional[Dict]:
        pos = portfolio.position
        if pos is None:
            return None
        frac = max(0.0, min(1.0, frac))
        if frac <= 0:
            return None
        price = snap.price
        qty_close = pos.qty * frac
        notional = qty_close * price * pos.leverage
        fee = self._fee(notional)

        if pos.action == Action.LONG:
            pnl = (price - pos.entry) * qty_close * pos.leverage
        else:
            pnl = (pos.entry - price) * qty_close * pos.leverage

        portfolio.cash = portfolio.cash + pnl - fee
        pos.qty -= qty_close

        if pos.qty <= 1e-12:
            portfolio.position = None

        return {"type": "PARTIAL_CLOSE", "reason": reason, "frac": frac, "exit_price": price, "pnl": pnl, "fee": fee, "equity": portfolio.cash}

    def open(self, portfolio: PortfolioState, plan: TradePlan, ts: int) -> Dict:
        if portfolio.position is not None:
            raise RuntimeError("Position already open (MVP supports single position).")

        equity = portfolio.equity
        notional = equity * plan.position_frac * plan.leverage
        qty = notional / plan.entry
        fee = self._fee(notional)
        portfolio.cash -= fee

        init_risk = abs(plan.entry - plan.stop) or (plan.entry * 0.002)

        portfolio.position = Position(
            action=plan.action,
            entry=plan.entry,
            qty=qty,
            stop=plan.stop,
            take=plan.take,
            leverage=plan.leverage,
            opened_ts=ts,
            initial_stop=plan.stop,
            initial_risk_dist=init_risk,
            last_trail_stop=plan.stop,
        )
        portfolio.last_trade_ts = ts
        portfolio.trades_in_last_24h.append(ts)

        return {
            "type": "OPEN",
            "action": plan.action.value,
            "entry": plan.entry,
            "qty": qty,
            "stop": plan.stop,
            "take": plan.take,
            "leverage": plan.leverage,
            "position_frac": plan.position_frac,
            "fee": fee,
        }


# =========================
# ExitEngine (profit running + anomaly response)
# =========================
class ExitEngine:
    def __init__(self, exit_cfg: ExitConfig):
        self.cfg = exit_cfg

    def _r_multiple(self, pos: Position, price: float) -> float:
        d = pos.initial_risk_dist or 1e-9
        if pos.action == Action.LONG:
            return (price - pos.entry) / d
        return (pos.entry - price) / d

    def _be_price(self, pos: Position) -> float:
        if pos.action == Action.LONG:
            return pos.entry + self.cfg.be_extra_r * pos.initial_risk_dist
        return pos.entry - self.cfg.be_extra_r * pos.initial_risk_dist

    def _trail_mult(self, level: AlertLevel, r_now: float) -> float:
        if level == AlertLevel.ORANGE and r_now < 0:
            return self.cfg.trail_mult_orange_losing
        if level == AlertLevel.ORANGE:
            return self.cfg.trail_mult_orange
        if level == AlertLevel.YELLOW:
            return self.cfg.trail_mult_yellow
        return self.cfg.trail_mult_green

    def apply(self, portfolio: PortfolioState, snap: MarketSnapshot, broker: PaperBroker, level: AlertLevel) -> List[Dict]:
        events: List[Dict] = []
        pos = portfolio.position
        if pos is None:
            return events

        price = snap.price
        a = atr(snap.ohlcv, 14) or (price * 0.002)
        r_now = self._r_multiple(pos, price)

        if r_now <= -2.0:
            close_evt = broker.force_close_all(portfolio, snap, reason="HARD_DRAWDOWN_CIRCUIT_BREAKER")
            events.append(close_evt)
            return events

        if level == AlertLevel.RED:
            close_evt = broker.force_close_all(portfolio, snap, reason="KILL_SWITCH_RED")
            portfolio.halted = True
            events.append(close_evt)
            return events

        # Move to BE+buffer at +1R
        if (not pos.be_done) and r_now >= self.cfg.move_be_r:
            be = self._be_price(pos)
            if pos.action == Action.LONG:
                pos.stop = max(pos.stop, be)
            else:
                pos.stop = min(pos.stop, be)
            pos.be_done = True
            events.append({"type": "STOP_MOVE", "reason": "MOVE_BE", "new_stop": pos.stop, "r": r_now, "level": str(level)})

        # Partial TP at +2R
        if (not pos.tp1_done) and r_now >= self.cfg.tp1_r:
            evt = broker.close_partial(portfolio, snap, frac=self.cfg.tp1_frac, reason="TP1_2R")
            if evt:
                events.append(evt)
            pos = portfolio.position
            if pos is None:
                return events
            pos.tp1_done = True

        # Trailing policy
        must_trail = (level in (AlertLevel.YELLOW, AlertLevel.ORANGE)) or pos.be_done or pos.tp1_done
        if must_trail:
            mult = self._trail_mult(level, r_now)
            if pos.action == Action.LONG:
                pos.stop = max(pos.stop, price - mult * a)
            else:
                pos.stop = min(pos.stop, price + mult * a)
            pos.last_trail_stop = pos.stop
            events.append({"type": "TRAIL_UPDATE", "mult": mult, "atr": a, "new_stop": pos.stop, "r": r_now, "level": str(level)})

        return events


# =========================
# Engine (loop)
# =========================
class CouncilEngine:
    def __init__(
        self,
        provider: MarketDataProvider,
        agents: List[Agent],
        judge: JudgeAgent,
        guardian: PhilosophyGuardian,
        sentinel: SafetySentinel,
        exit_engine: ExitEngine,
        broker: PaperBroker,
        laws: IronLaws,
        risk: RiskConfig,
        cfg: CouncilConfig,
        log_path: str,
        sentinel_enabled: bool = True,
        intel_bridge: Optional[IntelBridge] = None,
    ):
        self.provider = provider
        self.agents = agents
        self.judge = judge
        self.guardian = guardian
        self.sentinel = sentinel
        self.exit_engine = exit_engine
        self.broker = broker
        self.laws = laws
        self.risk = risk
        self.cfg = cfg
        self.log_path = log_path
        self.sentinel_enabled = sentinel_enabled
        self.intel_bridge = intel_bridge

        # "一切优势都会腐烂" — track per-agent edge health
        self.edge_tracker = EdgeDecayTracker()
        self._last_voting_agents: List[str] = []  # agents who voted for the current position

        self.obs_count = 0
        self.action_count = 0
        self.portfolio = PortfolioState()
        self._load_state()

    def _state_file(self) -> str:
        return "council_state_v2.json"

    def _edge_file(self) -> str:
        """Derive edge decay file path from state file directory."""
        state_dir = os.path.dirname(os.path.abspath(self._state_file()))
        return os.path.join(state_dir, "edge_decay_history.json")

    def _load_state(self) -> None:
        # Load edge tracker first (uses _edge_file which depends on _state_file)
        self.edge_tracker.load(self._edge_file())

        p = self._state_file()
        if not os.path.exists(p):
            return
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.portfolio = PortfolioState(**{k: d[k] for k in d if k in PortfolioState.__annotations__})
            if d.get("position"):
                self.portfolio.position = Position(**d["position"])
            # Restore edge attribution for open positions
            if d.get("_last_voting_agents"):
                self._last_voting_agents = d["_last_voting_agents"]
        except Exception:
            pass

    def _save_state(self) -> None:
        d = dataclasses.asdict(self.portfolio)
        d["_last_voting_agents"] = self._last_voting_agents
        with open(self._state_file(), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        self.edge_tracker.save(self._edge_file())

    def _log(self, record: Dict) -> None:
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _cooling_ok(self, now_ts: int) -> Tuple[bool, str]:
        if self.portfolio.last_trade_ts == 0:
            return True, ""
        dt = now_ts - self.portfolio.last_trade_ts
        if dt < self.laws.COOLING_PERIOD_SEC:
            return False, f"Cooling: {dt}s < {self.laws.COOLING_PERIOD_SEC}s"
        return True, ""

    def _daily_limit_ok(self, now_ts: int) -> Tuple[bool, str]:
        cutoff = now_ts - 24 * 3600
        self.portfolio.trades_in_last_24h = [t for t in self.portfolio.trades_in_last_24h if t >= cutoff]
        if len(self.portfolio.trades_in_last_24h) >= self.laws.DAILY_TRADE_LIMIT:
            return False, f"DailyTradeLimit hit: {len(self.portfolio.trades_in_last_24h)} >= {self.laws.DAILY_TRADE_LIMIT}"
        return True, ""

    def _drawdown_ok(self) -> Tuple[bool, str]:
        if self.portfolio.high_watermark <= 0:
            return True, ""
        dd = (self.portfolio.high_watermark - self.portfolio.equity) / self.portfolio.high_watermark
        if dd >= self.risk.max_drawdown_hard:
            return False, f"Hard drawdown halt: {dd:.2%} >= {self.risk.max_drawdown_hard:.2%}"
        return True, ""

    def run_once(self) -> None:
        if self.portfolio.halted:
            print("🛑 HALTED (state).")
            return

        snap = self.provider.get_snapshot(self.cfg.symbol, self.cfg.candle_lookback)
        now_ts = snap.ts

        # MTM
        self.broker.mark_to_market(self.portfolio, snap)

        # Sentinel
        level, reasons = self.sentinel.evaluate(snap, self.portfolio, enabled=self.sentinel_enabled)

        # Manage position
        if self.portfolio.position is not None:
            pos_before_exit = self.portfolio.position  # snapshot for edge tracking
            exit_events = self.exit_engine.apply(self.portfolio, snap, self.broker, level)
            for ev in exit_events:
                self._log({"ts": now_ts, "symbol": snap.symbol, "event": ev, "level": str(level), "sentinel": reasons})
                if ev.get("type") == "CLOSE":
                    print(f"🧾 {ev['type']} {ev.get('reason')} pnl={ev.get('pnl',0):.2f} equity={ev.get('equity',self.portfolio.equity):.2f}")
                    # Record edge outcome — only on FULL close, not partials
                    pnl = ev.get("pnl", 0)
                    risk_d = pos_before_exit.initial_risk_dist * pos_before_exit.qty * pos_before_exit.leverage
                    r_mult = pnl / risk_d if risk_d > 0 else 0.0
                    self.edge_tracker.record_outcome(
                        ts=now_ts, agents=self._last_voting_agents,
                        won=(pnl > 0), r_multiple=r_mult,
                    )
                elif ev.get("type") == "PARTIAL_CLOSE":
                    print(f"🧾 {ev['type']} {ev.get('reason')} pnl={ev.get('pnl',0):.2f} equity={ev.get('equity',self.portfolio.equity):.2f}")
                    # Partials do NOT record edge outcome — wait for final close
                elif ev.get("type") in ("TRAIL_UPDATE", "STOP_MOVE"):
                    print(f"🧷 {ev['type']} stop={ev.get('new_stop', None)}")

            if self.portfolio.halted:
                print("🛑 KILL SWITCH triggered. Trading halted.")
                self._save_state()
                return

            close_event = self.broker.maybe_close_by_sl_tp(self.portfolio, snap)
            if close_event:
                self._log({"ts": now_ts, "symbol": snap.symbol, "event": close_event, "level": str(level), "sentinel": reasons})
                print(f"🧾 CLOSE {close_event['reason']} pnl={close_event.get('pnl',0):.2f} equity={close_event.get('equity',0):.2f}")
                # Record edge outcome for SL/TP close
                pnl = close_event.get("pnl", 0)
                risk_d = pos_before_exit.initial_risk_dist * pos_before_exit.qty * pos_before_exit.leverage
                r_mult = pnl / risk_d if risk_d > 0 else 0.0
                self.edge_tracker.record_outcome(
                    ts=now_ts, agents=self._last_voting_agents,
                    won=(pnl > 0), r_multiple=r_mult,
                )

            self._log({"ts": now_ts, "symbol": snap.symbol, "cycle": "MANAGE", "price": snap.price, "level": str(level), "sentinel": reasons, "equity": self.portfolio.equity})
            print(f"🧭 MANAGE | level={level} | equity={self.portfolio.equity:.2f}")
            self._save_state()
            return

        # Circuit breaker
        ok_dd, dd_reason = self._drawdown_ok()
        if not ok_dd:
            self.portfolio.halted = True
            self._log({"ts": now_ts, "symbol": snap.symbol, "cycle": "HALT", "reason": dd_reason, "equity": self.portfolio.equity})
            print(f"🛑 HALT: {dd_reason}")
            self._save_state()
            return

        # === Intel Veto Layer ===
        # Positioned after Sentinel, before Agent voting.
        # Only applies when no position is open.
        #
        # Policy mapping:
        #   no_trade    → hard VETO, return immediately
        #   watch       → soft veto via confidence cap kept below MIN_CONFIDENCE
        #                 and re-applied after momentum boosts
        #   investigate → no restriction, full agent pipeline runs
        intel_signal: Optional[IntelSignal] = None
        intel_watch_cap: Optional[float] = None  # applied after judge aggregation
        if self.intel_bridge is not None:
            intel_signal = self.intel_bridge.get_signal(self.cfg.symbol)
            if intel_signal is not None:
                if intel_signal.stale:
                    print(f"⚠️  Intel report stale ({intel_signal.report_ts}), intel veto skipped")
                    intel_signal = None
                elif intel_signal.recommended_action == "no_trade":
                    self._log({
                        "ts": now_ts, "symbol": snap.symbol, "cycle": "INTEL_VETO",
                        "reason": f"anti-crowding: crowding_risk={intel_signal.crowding_risk}",
                        "intel_policy": dataclasses.asdict(intel_signal),
                        "equity": self.portfolio.equity,
                    })
                    print(f"🚫 INTEL VETO: crowding_risk={intel_signal.crowding_risk} tags={intel_signal.tags}")
                    self._save_state()
                    return
                elif intel_signal.recommended_action == "watch":
                    # Soft veto: keep crowded assets below the current trade threshold.
                    intel_watch_cap = 0.55
                    print(f"👁️  Intel WATCH: confidence capped at {intel_watch_cap} (crowding={intel_signal.crowding_risk})")

        self.obs_count += 1

        # Decisions
        verdicts = [a.decide(snap) for a in self.agents]
        cons = self.judge.aggregate(verdicts)

        # Apply intel watch confidence cap (soft block for crowded assets)
        if intel_watch_cap is not None and cons.confidence > intel_watch_cap:
            cons = dataclasses.replace(
                cons,
                confidence=intel_watch_cap,
                notes=cons.notes + [f"Intel watch cap applied: conf→{intel_watch_cap} (was {cons.confidence:.2f})"],
            )

        # Re-apply watch cap if needed — watch is a hard ceiling, not a suggestion
        if intel_watch_cap is not None and cons.confidence > intel_watch_cap:
            cons = dataclasses.replace(
                cons,
                confidence=intel_watch_cap,
                notes=cons.notes + [f"Watch cap re-applied after momentum: conf→{intel_watch_cap}"],
            )

        intel_policy_dict = dataclasses.asdict(intel_signal) if intel_signal else None

        if cons.action == Action.HOLD:
            self._log({
                "ts": now_ts, "symbol": snap.symbol, "cycle": "HOLD",
                "price": snap.price, "level": str(level), "sentinel": reasons,
                "consensus": dataclasses.asdict(cons),
                "intel_policy": intel_policy_dict,
                "equity": self.portfolio.equity,
                "action_rate": self.action_count / max(1, self.obs_count),
            })
            print(f"⏸️ HOLD | level={level} conf={cons.confidence:.2f} agree={cons.agree_count}/{cons.total_agents} equity={self.portfolio.equity:.2f}")
            self._save_state()
            return

        # No new positions in ORANGE/RED
        if level in (AlertLevel.ORANGE, AlertLevel.RED):
            self._log({"ts": now_ts, "symbol": snap.symbol, "cycle": "VETO", "reason": f"Sentinel {level}: no new positions", "consensus": dataclasses.asdict(cons)})
            print(f"🛑 VETO: Sentinel {level} => no new positions")
            self._save_state()
            return

        ok_cool, cool_reason = self._cooling_ok(now_ts)
        if not ok_cool:
            self._log({"ts": now_ts, "symbol": snap.symbol, "cycle": "VETO", "reason": cool_reason})
            print(f"🧊 VETO: {cool_reason}")
            self._save_state()
            return

        ok_day, day_reason = self._daily_limit_ok(now_ts)
        if not ok_day:
            self._log({"ts": now_ts, "symbol": snap.symbol, "cycle": "VETO", "reason": day_reason})
            print(f"📆 VETO: {day_reason}")
            self._save_state()
            return

        plan = self.guardian.build_plan(cons, snap, self.portfolio)
        if plan is None:
            self._log({"ts": now_ts, "symbol": snap.symbol, "cycle": "HOLD", "reason": "No plan"})
            print("⏸️ HOLD (no plan)")
            self._save_state()
            return

        ok, g_reasons = self.guardian.evaluate(cons, plan)
        if not ok:
            self._log({"ts": now_ts, "symbol": snap.symbol, "cycle": "VETO", "reasons": g_reasons, "plan": dataclasses.asdict(plan)})
            print(f"🛡️ VETO by Guardian: {', '.join(g_reasons)}")
            self._save_state()
            return

        # Edge health check — "一切优势都会腐烂"
        edge_health = self.edge_tracker.system_edge_health([a.name for a in self.agents])
        dead_agents = [name for name, info in edge_health.items()
                       if name != "_summary" and isinstance(info, dict) and info.get("status") == "dead"]
        if dead_agents:
            self._log({"ts": now_ts, "symbol": snap.symbol, "cycle": "EDGE_DECAY_WARNING",
                       "dead_agents": dead_agents, "edge_health": edge_health})
            print(f"🦴 EDGE DECAY: agents with dead edge: {dead_agents}")
            # If majority of agreeing agents have dead edges, veto
            agreeing_agents = [v.agent for v in cons.raw if v.action == cons.action]
            dead_in_consensus = [a for a in agreeing_agents if a in dead_agents]
            if len(dead_in_consensus) > len(agreeing_agents) / 2:
                self._log({"ts": now_ts, "symbol": snap.symbol, "cycle": "VETO",
                           "reason": f"Edge rot: {dead_in_consensus} have dead edges"})
                print(f"💀 VETO: majority of agreeing agents have decayed edges: {dead_in_consensus}")
                self._save_state()
                return

        self.action_count += 1
        # Remember which agents voted for this trade (for edge tracking on close)
        self._last_voting_agents = [v.agent for v in cons.raw if v.action == cons.action]
        open_event = self.broker.open(self.portfolio, plan, now_ts)
        self._log({"ts": now_ts, "symbol": snap.symbol, "cycle": "TRADE", "price": snap.price,
                   "level": str(level), "sentinel": reasons, "open": open_event, "plan": dataclasses.asdict(plan),
                   "consensus": dataclasses.asdict(cons), "intel_policy": intel_policy_dict,
                   "counterparty_thesis": cons.counterparty_thesis,
                   "edge_health": edge_health,
                   "equity": self.portfolio.equity})
        print(f"✅ TRADE {open_event['action']} entry={open_event['entry']:.2f} SL={open_event['stop']:.2f} TP={open_event['take']:.2f} level={level}")
        print(f"   📋 Counterparty: {cons.counterparty_thesis[:120]}")
        self._save_state()


# =========================
# Log analysis
# =========================
def analyze_log(path: str) -> Dict[str, float]:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    equity_series = []
    trades = 0
    obs = 0
    closes = 0
    partials = 0
    halts = 0

    last_equity = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if "cycle" in d:
                obs += 1
                if "equity" in d:
                    last_equity = d["equity"]
                    equity_series.append(last_equity)
                if d["cycle"] == "TRADE":
                    trades += 1
                if d["cycle"] == "HALT":
                    halts += 1
            if "event" in d:
                ev = d["event"]
                if ev.get("type") == "CLOSE":
                    closes += 1
                    last_equity = ev.get("equity", last_equity)
                    if last_equity is not None:
                        equity_series.append(last_equity)
                if ev.get("type") == "PARTIAL_CLOSE":
                    partials += 1
                    last_equity = ev.get("equity", last_equity)
                    if last_equity is not None:
                        equity_series.append(last_equity)

    if not equity_series:
        equity_series = [0.0]
    peak = equity_series[0]
    mdd = 0.0
    for e in equity_series:
        peak = max(peak, e)
        dd = (peak - e) / peak if peak > 0 else 0.0
        mdd = max(mdd, dd)

    return {
        "observations": float(obs),
        "trades_opened": float(trades),
        "closes": float(closes),
        "partial_closes": float(partials),
        "halts": float(halts),
        "action_rate": (trades / obs) if obs else 0.0,
        "max_drawdown": mdd,
        "final_equity": float(equity_series[-1]),
    }


# =========================
# CLI commands
# =========================
def cmd_sentinel(args: argparse.Namespace) -> None:
    provider = SimulatedProvider(seed=args.seed, start_price=args.start_price)
    snap = provider.get_snapshot(args.symbol, args.lookback)
    state = PortfolioState()
    state.api_failures = args.api_failures
    sentinel = SafetySentinel(SentinelConfig())
    level, reasons = sentinel.evaluate(snap, state, enabled=True)
    print(f"Sentinel: {level}")
    for r in reasons:
        print(" -", r)


def cmd_run(args: argparse.Namespace) -> None:
    cfg = CouncilConfig(symbol=args.symbol, timeframe_sec=args.interval, candle_lookback=args.lookback, seed=args.seed)

    laws = IronLaws()
    laws.COOLING_PERIOD_SEC = args.cooling_sec
    laws.DAILY_TRADE_LIMIT = args.daily_limit

    fees = Fees()
    risk = RiskConfig(initial_leverage=args.leverage)

    # Provider selection
    if args.provider == "bitget":
        provider: MarketDataProvider = BitgetLiveProvider()
        print("📡 Provider: BitgetLiveProvider (real-time price, 15-min candles with volume)")
    elif args.provider == "live":
        provider = CoinGeckoLiveProvider()
        print("📡 Provider: CoinGeckoLiveProvider (real prices, 30-min candles, no volume)")
    else:
        provider = SimulatedProvider(seed=cfg.seed, start_price=args.start_price)
        print("🎲 Provider: SimulatedProvider (random walk)")

    # Intel Bridge: reads latest council_intel.py report.json (must be before agents)
    intel_bridge: Optional[IntelBridge] = None
    if args.intel_dir:
        intel_bridge = IntelBridge(intel_dir=args.intel_dir)
        print(f"🔗 IntelBridge: reading from {args.intel_dir}")

    agents: List[Agent] = [
        TrendAgent(),
        SupportResistanceAgent(),
        RiskAgent(vol_lookback=risk.vol_lookback),
        LiquidationPressureAgent(intel_bridge=intel_bridge),
        FundingFlowAgent(intel_bridge=intel_bridge),
    ]

    judge = JudgeAgent(min_agents_agree=cfg.min_agents_agree)
    guardian = PhilosophyGuardian(laws=laws, fees=fees, risk=risk)
    sentinel = SafetySentinel(SentinelConfig())
    exit_engine = ExitEngine(ExitConfig())
    broker = PaperBroker(fees=fees)

    engine = CouncilEngine(
        provider=provider,
        agents=agents,
        judge=judge,
        guardian=guardian,
        sentinel=sentinel,
        exit_engine=exit_engine,
        broker=broker,
        laws=laws,
        risk=risk,
        cfg=cfg,
        log_path=args.log,
        sentinel_enabled=(args.sentinel == "on"),
        intel_bridge=intel_bridge,
    )

    if engine.portfolio.cash == 1000.0 and not os.path.exists(engine._state_file()):
        engine.portfolio.cash = args.start_equity
        engine.portfolio.equity = args.start_equity
        engine.portfolio.high_watermark = args.start_equity

    print("🧠 Council MVP v2 started (paper). Ctrl+C to stop.")
    print(f"Symbol={cfg.symbol} Interval={cfg.timeframe_sec}s Sentinel={args.sentinel} Cooling={laws.COOLING_PERIOD_SEC}s")

    try:
        while True:
            engine.run_once()
            time.sleep(cfg.timeframe_sec)
    except KeyboardInterrupt:
        print("\n👋 stopped.")


def cmd_analyze(args: argparse.Namespace) -> None:
    stats = analyze_log(args.log)
    print(json.dumps(stats, indent=2))


def build_cli() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="council_v2.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sentinel", help="Run SafetySentinel on a snapshot (CLI-first).")
    s.add_argument("--symbol", default="BTC/USDT")
    s.add_argument("--lookback", type=int, default=180)
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--start-price", type=float, default=50000.0)
    s.add_argument("--api-failures", type=int, default=0)
    s.set_defaults(func=cmd_sentinel)

    r = sub.add_parser("run", help="Run the Council loop (paper trading).")
    r.add_argument("--symbol", default="BTC/USDT")
    r.add_argument("--interval", type=int, default=60)
    r.add_argument("--lookback", type=int, default=180)
    r.add_argument("--seed", type=int, default=42)
    r.add_argument("--start-price", type=float, default=50000.0)
    r.add_argument("--start-equity", type=float, default=1000.0)
    r.add_argument("--log", default="council_log_v2.jsonl")
    r.add_argument("--sentinel", choices=["on", "off"], default="on", help="A/B test switch.")
    r.add_argument("--leverage", type=float, default=1.0)
    r.add_argument("--cooling-sec", type=int, default=24 * 3600)
    r.add_argument("--daily-limit", type=int, default=3)
    r.add_argument("--provider", choices=["sim", "live", "bitget"], default="sim",
                   help="sim = random walk; live = CoinGecko; bitget = Bitget (recommended).")
    r.add_argument("--intel-dir", default="artifacts/intel",
                   help="Path to council_intel.py artifacts directory. Set to empty string to disable IntelBridge.")
    r.set_defaults(func=cmd_run)

    a = sub.add_parser("analyze", help="Analyze a jsonl log file (action rate, MDD, etc).")
    a.add_argument("--log", default="council_log_v2.jsonl")
    a.set_defaults(func=cmd_analyze)

    return ap


def main() -> None:
    ap = build_cli()
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
