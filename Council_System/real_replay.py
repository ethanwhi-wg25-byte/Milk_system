#!/usr/bin/env python3

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import os
import statistics
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import council_v2


GRANULARITY_TO_MS: Dict[str, int] = {
    "1min": 60_000,
    "3min": 3 * 60_000,
    "5min": 5 * 60_000,
    "15min": 15 * 60_000,
    "30min": 30 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
}

COINBASE_GRANULARITY_TO_SEC: Dict[str, int] = {
    "1min": 60,
    "5min": 300,
    "15min": 900,
    "1h": 3600,
}

# Coinbase's ranged candles endpoint rejects oversized windows with HTTP 400.
# Keep pagination conservative even if the non-ranged "latest candles" view returns more.
COINBASE_MAX_POINTS = 300


def parse_bitget_candles(raw: List[List[str]]) -> List[Tuple[int, float, float, float, float, float]]:
    candles: List[Tuple[int, float, float, float, float, float]] = []
    for candle in raw:
        ts_sec = int(candle[0]) // 1000
        o = float(candle[1])
        h = float(candle[2])
        l = float(candle[3])
        c = float(candle[4])
        vol = float(candle[5]) if len(candle) > 5 else 0.0
        candles.append((ts_sec, o, h, l, c, vol))
    candles.sort(key=lambda row: row[0])
    return candles


def parse_coinbase_candles(raw: List[List[float]]) -> List[Tuple[int, float, float, float, float, float]]:
    candles: List[Tuple[int, float, float, float, float, float]] = []
    for candle in raw:
        ts_sec = int(candle[0])
        low = float(candle[1])
        high = float(candle[2])
        open_price = float(candle[3])
        close_price = float(candle[4])
        vol = float(candle[5]) if len(candle) > 5 else 0.0
        candles.append((ts_sec, open_price, high, low, close_price, vol))
    candles.sort(key=lambda row: row[0])
    return candles


def _to_coinbase_product_id(symbol: str) -> str:
    base, _, quote = symbol.partition("/")
    if not base:
        raise ValueError(f"Unsupported symbol format: {symbol}")
    if quote in ("", "USDT", "USD"):
        quote = "USD"
    return f"{base}-{quote}"


def _iso8601_utc(ts_ms: int) -> str:
    stamp = dt.datetime.fromtimestamp(ts_ms / 1000.0, tz=dt.timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_bitget_spot_candles(
    symbol: str,
    granularity: str,
    required_candles: int,
    end_time_ms: Optional[int] = None,
) -> List[Tuple[int, float, float, float, float, float]]:
    if granularity not in GRANULARITY_TO_MS:
        raise ValueError(f"Unsupported granularity: {granularity}")

    bg_symbol = symbol.replace("/", "")
    interval_ms = GRANULARITY_TO_MS[granularity]
    end_ms = end_time_ms or int(time.time() * 1000)
    by_ts: Dict[int, Tuple[int, float, float, float, float, float]] = {}

    while len(by_ts) < required_candles:
        chunk = min(1000, required_candles - len(by_ts))
        start_ms = max(0, end_ms - (chunk * interval_ms))
        query = urllib.parse.urlencode(
            {
                "symbol": bg_symbol,
                "granularity": granularity,
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": chunk,
            }
        )
        url = f"https://api.bitget.com/api/v2/spot/market/candles?{query}"
        payload = council_v2._fetch_json_simple(url, timeout=8)
        raw = payload.get("data", [])
        if not raw:
            break
        parsed = parse_bitget_candles(raw)
        for candle in parsed:
            by_ts[candle[0]] = candle

        oldest_ms = parsed[0][0] * 1000
        next_end_ms = oldest_ms - interval_ms
        if next_end_ms >= end_ms:
            break
        end_ms = next_end_ms

    candles = sorted(by_ts.values(), key=lambda row: row[0])
    if len(candles) < required_candles:
        raise RuntimeError(
            f"Only fetched {len(candles)} candles, need {required_candles}. "
            "Bitget may have timed out, rate-limited, or returned a shorter history window."
        )
    return candles[-required_candles:]


def fetch_coinbase_spot_candles(
    symbol: str,
    granularity: str,
    required_candles: int,
    end_time_ms: Optional[int] = None,
) -> List[Tuple[int, float, float, float, float, float]]:
    if granularity not in COINBASE_GRANULARITY_TO_SEC:
        raise ValueError(
            f"Coinbase fallback does not support granularity={granularity}. "
            f"Supported: {', '.join(sorted(COINBASE_GRANULARITY_TO_SEC))}"
        )

    product_id = _to_coinbase_product_id(symbol)
    interval_sec = COINBASE_GRANULARITY_TO_SEC[granularity]
    interval_ms = interval_sec * 1000
    end_ms = end_time_ms or int(time.time() * 1000)
    by_ts: Dict[int, Tuple[int, float, float, float, float, float]] = {}

    while len(by_ts) < required_candles:
        chunk = min(COINBASE_MAX_POINTS, required_candles - len(by_ts))
        start_ms = max(0, end_ms - (chunk * interval_ms))
        query = urllib.parse.urlencode(
            {
                "granularity": interval_sec,
                "start": _iso8601_utc(start_ms),
                "end": _iso8601_utc(end_ms),
            }
        )
        url = f"https://api.exchange.coinbase.com/products/{product_id}/candles?{query}"
        raw = council_v2._fetch_json_simple(url, timeout=20)
        if not raw:
            break
        parsed = parse_coinbase_candles(raw)
        for candle in parsed:
            by_ts[candle[0]] = candle

        oldest_ms = parsed[0][0] * 1000
        next_end_ms = oldest_ms - interval_ms
        if next_end_ms >= end_ms:
            break
        end_ms = next_end_ms

    candles = sorted(by_ts.values(), key=lambda row: row[0])
    if len(candles) < required_candles:
        raise RuntimeError(
            f"Only fetched {len(candles)} candles from Coinbase, need {required_candles}. "
            "The public history window may be shorter than requested."
        )
    return candles[-required_candles:]


def _fetch_real_spot_candles_with_source(
    symbol: str,
    granularity: str,
    required_candles: int,
    source: str = "auto",
    end_time_ms: Optional[int] = None,
) -> Tuple[str, List[Tuple[int, float, float, float, float, float]]]:
    if source == "bitget":
        return "bitget", fetch_bitget_spot_candles(symbol, granularity, required_candles, end_time_ms=end_time_ms)
    if source == "coinbase":
        return "coinbase", fetch_coinbase_spot_candles(symbol, granularity, required_candles, end_time_ms=end_time_ms)
    if source != "auto":
        raise ValueError(f"Unsupported source: {source}")

    failures: List[str] = []
    for name, fetcher in (
        ("bitget", fetch_bitget_spot_candles),
        ("coinbase", fetch_coinbase_spot_candles),
    ):
        try:
            return name, fetcher(symbol, granularity, required_candles, end_time_ms=end_time_ms)
        except Exception as exc:
            failures.append(f"{name}: {exc}")

    raise RuntimeError("All replay data sources failed. " + " | ".join(failures))


def fetch_real_spot_candles(
    symbol: str,
    granularity: str,
    required_candles: int,
    source: str = "auto",
    end_time_ms: Optional[int] = None,
) -> List[Tuple[int, float, float, float, float, float]]:
    _, candles = _fetch_real_spot_candles_with_source(
        symbol,
        granularity,
        required_candles,
        source=source,
        end_time_ms=end_time_ms,
    )
    return candles


class HistoricalReplayProvider(council_v2.MarketDataProvider):
    def __init__(
        self,
        symbol: str,
        candles: List[Tuple[int, float, float, float, float, float]],
        lookback: int,
    ) -> None:
        if len(candles) < lookback:
            raise ValueError("Need at least lookback candles for replay")
        self.symbol = symbol
        self.candles = candles
        self.lookback = lookback
        self.index = lookback - 1

    def remaining_steps(self) -> int:
        return max(0, len(self.candles) - self.index)

    def peek_ts(self) -> Optional[int]:
        """Return the timestamp of the next snapshot without advancing the index."""
        if self.index >= len(self.candles):
            return None
        return self.candles[self.index][0]

    def get_snapshot(self, symbol: str, lookback: int) -> council_v2.MarketSnapshot:
        if symbol != self.symbol:
            raise ValueError(f"Replay provider loaded for {self.symbol}, got {symbol}")
        if lookback != self.lookback:
            raise ValueError(f"Replay provider configured for lookback={self.lookback}, got {lookback}")
        if self.index >= len(self.candles):
            raise StopIteration("historical replay exhausted")

        window = self.candles[self.index - self.lookback + 1 : self.index + 1]
        ts, _, _, _, close_price, _ = window[-1]
        snap = council_v2.MarketSnapshot(
            symbol=self.symbol,
            ts=ts,
            price=close_price,
            ohlcv=window,
        )
        self.index += 1
        return snap


class HistoricalOKXBridge:
    """Serves time-indexed OKX microstructure data during historical replay.

    Reads the JSON files produced by fetch_okx_history.py and returns the
    most recent snapshot at or before a given replay timestamp.
    z-score is computed from a rolling 30-day (~90 funding periods) window.
    """

    Z_WINDOW = 90          # funding periods for z-score (90 × 8h = 30d)
    OI_DELTA_WINDOW_H = 24 # hours back for oi_delta_pct computation

    def __init__(self, history_dir: str, symbol: str) -> None:
        # Fetcher stores under bare base currency (BTC), not BTC_USDT.
        base_ccy = symbol.split("/")[0].upper()
        d = Path(history_dir) / base_ccy
        if not d.exists():
            # fallback: try slash-replaced form
            d = Path(history_dir) / symbol.replace("/", "_")

        self._funding: List[Dict] = self._load(d / "funding_rates.json")
        self._ls:      List[Dict] = self._load(d / "ls_ratio.json")
        self._taker:   List[Dict] = self._load(d / "taker_flow.json")
        self._oi:      List[Dict] = self._load(d / "oi_snapshots.json")

        # sorted timestamp arrays for bisect lookups
        self._f_ts  = [r["ts"] for r in self._funding]
        self._ls_ts = [r["ts"] for r in self._ls]
        self._tk_ts = [r["ts"] for r in self._taker]
        self._oi_ts = [r["ts"] for r in self._oi]

        self._symbol = symbol
        total = sum(len(x) for x in [self._funding, self._ls, self._taker, self._oi])
        print(f"  HistoricalOKXBridge: loaded {total} records for {symbol} from {d}")

    @staticmethod
    def _load(path: Path) -> List[Dict]:
        if not path.exists():
            return []
        with open(path) as f:
            data = json.load(f)
        return sorted(data, key=lambda r: r["ts"])

    def _at(self, ts_list: List[int], rows: List[Dict], ts_ms: int) -> Optional[Dict]:
        """Return the most recent row at or before ts_ms."""
        if not ts_list:
            return None
        idx = bisect.bisect_right(ts_list, ts_ms) - 1
        if idx < 0:
            return None
        return rows[idx]

    def _z_score(self, ts_ms: int) -> Optional[float]:
        """Rolling 30d z-score of funding rate at ts_ms."""
        idx = bisect.bisect_right(self._f_ts, ts_ms) - 1
        if idx < 0:
            return None
        window_start = max(0, idx - self.Z_WINDOW + 1)
        window = [r["rate"] for r in self._funding[window_start: idx + 1]]
        if len(window) < 5:
            return None
        current = window[-1]
        mean = statistics.mean(window)
        stdev = statistics.stdev(window) if len(window) > 1 else 0.0
        if stdev < 1e-12:
            return 0.0
        return (current - mean) / stdev

    def _oi_delta(self, ts_ms: int) -> Optional[float]:
        """24h OI delta as fraction (e.g. 0.08 = +8%)."""
        now_row = self._at(self._oi_ts, self._oi, ts_ms)
        if not now_row:
            return None
        past_ts = ts_ms - self.OI_DELTA_WINDOW_H * 3_600_000
        past_row = self._at(self._oi_ts, self._oi, past_ts)
        if not past_row or past_row["oi"] == 0:
            return None
        return (now_row["oi"] - past_row["oi"]) / abs(past_row["oi"])

    def set_ts(self, ts_sec: int) -> None:
        self._current_ts_ms = ts_sec * 1000

    def get_okx_data(self, symbol: str) -> Optional[Dict]:
        _ = symbol
        ts_ms = getattr(self, "_current_ts_ms", None) or int(time.time() * 1000)

        f_row  = self._at(self._f_ts,  self._funding, ts_ms)
        ls_row = self._at(self._ls_ts, self._ls,      ts_ms)
        tk_row = self._at(self._tk_ts, self._taker,   ts_ms)
        oi_row = self._at(self._oi_ts, self._oi,      ts_ms)

        if not f_row:
            return None

        rate = f_row["rate"]
        z = self._z_score(ts_ms)
        sigma = None
        rate_24h_ago = None
        # rate 24h ago = 3 funding periods back (8h each)
        idx = bisect.bisect_right(self._f_ts, ts_ms) - 1
        if idx >= 3:
            rate_24h_ago = self._funding[idx - 3]["rate"]
        window_start = max(0, idx - self.Z_WINDOW + 1)
        window = [r["rate"] for r in self._funding[window_start: idx + 1]]
        if len(window) > 1:
            sigma = statistics.stdev(window)

        buy  = tk_row["buy"]  if tk_row else 0.0
        sell = tk_row["sell"] if tk_row else 0.0
        total = buy + sell
        taker_buy_ratio = (buy / total) if total > 0 else 0.5

        return {
            "exchange": "okx_historical",
            "funding_rate": rate,
            "funding_z_score": z,
            "rate_24h_ago": rate_24h_ago,
            "rate_1sigma": sigma or 0.0001,
            "is_anomalous": z is not None and abs(z) > 3.0,
            "is_turning": (
                rate_24h_ago is not None and rate * rate_24h_ago < 0
            ),
            "ls_ratio_account":  ls_row["acct"] if ls_row else None,
            # OKX rubik ccy= endpoint only gives account ratio, not contract/professional ratio.
            # Always None so LiquidationPressureAgent uses account ratio alone.
            "ls_ratio_contract": None,
            "taker_buy_ratio": taker_buy_ratio,
            "oi_value":        oi_row["oi"] if oi_row else 0.0,
            "oi_delta_pct_24h": self._oi_delta(ts_ms),
        }

    def get_funding(self, symbol: str) -> Optional[Dict]:
        """Compatibility shim for legacy LiquidationPressureAgent fallback path."""
        data = self.get_okx_data(symbol)
        if not data:
            return None
        return {
            "current_rate": data["funding_rate"],
            "is_anomalous": data["is_anomalous"],
            "is_turning": data["is_turning"],
        }

    def get_signal(self, symbol: str) -> None:
        """No historical intel policy in shadow mode — agents use microstructure directly."""
        return None

    def get_deribit_data(self, symbol: str) -> Optional[Dict]:
        # No free historical Deribit data — RiskAgent/OptionsGammaAgent skip gracefully
        return None


class ReplayCouncilEngine(council_v2.CouncilEngine):
    def __init__(self, state_path: str, *args, **kwargs) -> None:
        self._replay_state_path = state_path
        self._historical_okx_bridge: Optional[HistoricalOKXBridge] = kwargs.pop("historical_okx_bridge", None)
        super().__init__(*args, **kwargs)

    def _state_file(self) -> str:
        return self._replay_state_path

    def run_once(self) -> None:
        if self._historical_okx_bridge is not None:
            ts = getattr(self.provider, "peek_ts", lambda: None)()
            if ts:
                self._historical_okx_bridge.set_ts(ts)
        super().run_once()


def build_engine(args: argparse.Namespace, provider: HistoricalReplayProvider) -> ReplayCouncilEngine:
    cfg = council_v2.CouncilConfig(
        symbol=args.symbol,
        timeframe_sec=GRANULARITY_TO_MS[args.granularity] // 1000,
        candle_lookback=args.lookback,
        seed=42,
    )

    laws = council_v2.IronLaws()
    laws.COOLING_PERIOD_SEC = args.cooling_sec
    laws.DAILY_TRADE_LIMIT = args.daily_limit

    fees = council_v2.Fees()
    risk = council_v2.RiskConfig(initial_leverage=args.leverage)

    # Historical OKX bridge takes priority over static intel bridge for microstructure data.
    historical_okx_bridge: Optional[HistoricalOKXBridge] = None
    intel_bridge: Any = None

    okx_history_dir = getattr(args, "okx_history_dir", "")
    if okx_history_dir:
        historical_okx_bridge = HistoricalOKXBridge(okx_history_dir, args.symbol)
        intel_bridge = historical_okx_bridge
    elif args.intel_dir:
        intel_bridge = council_v2.IntelBridge(intel_dir=args.intel_dir)

    agents: List[council_v2.Agent] = [
        council_v2.TrendAgent(),
        council_v2.SupportResistanceAgent(),
        council_v2.RiskAgent(vol_lookback=risk.vol_lookback, intel_bridge=intel_bridge),
        council_v2.LiquidationPressureAgent(intel_bridge=intel_bridge),
        council_v2.FundingFlowAgent(intel_bridge=intel_bridge),
        council_v2.OIFlowAgent(intel_bridge=intel_bridge),
        council_v2.OptionsGammaAgent(intel_bridge=intel_bridge),
    ]

    state_path = os.path.join(args.output_dir, f"state_{args.steps}.json")
    log_path = os.path.join(args.output_dir, f"log_{args.steps}.jsonl")
    for path in (state_path, log_path, os.path.join(args.output_dir, "edge_decay_history.json")):
        if os.path.exists(path):
            os.remove(path)

    return ReplayCouncilEngine(
        state_path,
        historical_okx_bridge=historical_okx_bridge,
        provider=provider,
        agents=agents,
        judge=council_v2.JudgeAgent(min_agents_agree=cfg.min_agents_agree),
        guardian=council_v2.PhilosophyGuardian(laws=laws, fees=fees, risk=risk),
        sentinel=council_v2.SafetySentinel(council_v2.SentinelConfig()),
        exit_engine=council_v2.ExitEngine(council_v2.ExitConfig()),
        broker=council_v2.PaperBroker(fees=fees),
        laws=laws,
        risk=risk,
        cfg=cfg,
        log_path=log_path,
        sentinel_enabled=(args.sentinel == "on"),
        intel_bridge=intel_bridge,
    )


def run_replay(args: argparse.Namespace) -> Dict[str, object]:
    os.makedirs(args.output_dir, exist_ok=True)
    required_candles = args.steps + args.lookback
    source_used, candles = _fetch_real_spot_candles_with_source(
        symbol=args.symbol,
        granularity=args.granularity,
        required_candles=required_candles,
        source=args.source,
    )
    provider = HistoricalReplayProvider(args.symbol, candles, args.lookback)
    engine = build_engine(args, provider)

    executed = 0
    while executed < args.steps and not engine.portfolio.halted:
        try:
            engine.run_once()
        except StopIteration:
            break
        executed += 1

    stats = council_v2.analyze_log(engine.log_path)
    stats["executed_steps"] = float(executed)
    stats["requested_steps"] = float(args.steps)
    stats["candles_loaded"] = float(len(candles))
    stats["data_source"] = source_used
    return stats


def build_cli() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="real_replay.py")
    ap.add_argument("--symbol", default="BTC/USDT")
    ap.add_argument("--granularity", default="1min", choices=sorted(GRANULARITY_TO_MS))
    ap.add_argument("--lookback", type=int, default=180)
    ap.add_argument("--steps", type=int, required=True)
    ap.add_argument("--output-dir", default="artifacts/replay")
    ap.add_argument("--source", choices=["auto", "bitget", "coinbase"], default="auto")
    ap.add_argument("--sentinel", choices=["on", "off"], default="on")
    ap.add_argument("--leverage", type=float, default=1.0)
    ap.add_argument("--cooling-sec", type=int, default=4 * 3600)
    ap.add_argument("--daily-limit", type=int, default=3)
    ap.add_argument(
        "--intel-dir",
        default="",
        help="Optional latest intel directory. Leave empty for price-only historical replay.",
    )
    ap.add_argument(
        "--okx-history-dir",
        default="",
        dest="okx_history_dir",
        help="Path to pre-fetched OKX history (from fetch_okx_history.py). "
             "Enables time-indexed microstructure signals during replay.",
    )
    return ap


def main() -> None:
    args = build_cli().parse_args()
    stats = run_replay(args)
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
