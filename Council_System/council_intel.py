#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Council Agent System — Read-only intelligence CLI

Builds evidence-backed markdown/json artifacts from:
- CoinGecko top market data
- Perp funding rates (CoinGecko /derivatives)

No trading or exchange writes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def dedupe_preserve_order(items: Sequence[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def fetch_json(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 20,
    method: str = "GET",
    data: Optional[Dict[str, Any]] = None,
) -> Any:
    payload = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, headers=headers or {}, method=method, data=payload)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def fetch_json_via_curl(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 20,
    method: str = "GET",
    data: Optional[Dict[str, Any]] = None,
) -> Any:
    cmd = ["curl", "-sS", "--max-time", str(timeout), url]
    if method.upper() != "GET":
        cmd.extend(["-X", method.upper()])
    for key, value in (headers or {}).items():
        cmd.extend(["-H", f"{key}: {value}"])
    if data is not None:
        cmd.extend(["-d", json.dumps(data)])

    completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "curl request failed")
    return json.loads(completed.stdout)


def has_meaningful_funding_signal(data: Optional[Dict[str, Any]]) -> bool:
    return bool(data and data.get("is_anomalous"))


class StaticCoinGeckoProvider:
    def __init__(self, top_assets: Optional[List[Dict]] = None, asset_snapshots: Optional[Dict[str, Dict]] = None):
        self.top_assets = top_assets or []
        self.asset_snapshots = asset_snapshots or {}

    def fetch_top_assets(self, limit: int) -> List[Dict]:
        return self.top_assets[:limit]

    def fetch_asset_snapshot(self, symbol: str) -> Optional[Dict]:
        return self.asset_snapshots.get(normalize_symbol(symbol))

    def describe(self) -> Dict:
        return {"status": "static"}


class StaticOverlayProvider:
    """Generic static stub for any overlay-style provider (funding, etc.) in tests."""

    def __init__(self, overlays: Optional[Dict[str, Dict]] = None):
        self.overlays = overlays or {}

    def fetch_symbol_data(self, symbol: str, query_pack: Optional[str] = None) -> Optional[Dict]:
        _ = query_pack
        return self.overlays.get(normalize_symbol(symbol))

    def describe(self) -> Dict:
        return {"status": "static"}


class CoinGeckoProvider:
    def __init__(self, vs_currency: str = "usd"):
        self.vs_currency = vs_currency
        self.base_url = "https://api.coingecko.com/api/v3"
        self._symbol_to_id: Dict[str, str] = {}

    def fetch_top_assets(self, limit: int) -> List[Dict]:
        query = urllib.parse.urlencode(
            {
                "vs_currency": self.vs_currency,
                "order": "market_cap_desc",
                "per_page": limit,
                "page": 1,
                "sparkline": "false",
            }
        )
        data = fetch_json(f"{self.base_url}/coins/markets?{query}")
        out: List[Dict] = []
        for item in data:
            symbol = normalize_symbol(item.get("symbol", ""))
            if symbol:
                coin_id = item.get("id")
                if coin_id:
                    self._symbol_to_id[symbol] = coin_id
            out.append(
                {
                    "symbol": symbol,
                    "name": item.get("name", symbol),
                    "rank": item.get("market_cap_rank"),
                    "price": item.get("current_price"),
                    "market_cap": item.get("market_cap"),
                    "volume_24h": item.get("total_volume"),
                    "change_24h_pct": item.get("price_change_percentage_24h"),
                    "source": "coingecko_top",
                }
            )
        return out

    def fetch_asset_snapshot(self, symbol: str) -> Optional[Dict]:
        symbol = normalize_symbol(symbol)
        coin_id = self._symbol_to_id.get(symbol) or self._resolve_coin_id(symbol)
        if not coin_id:
            return None
        query = urllib.parse.urlencode(
            {
                "vs_currency": self.vs_currency,
                "ids": coin_id,
                "sparkline": "false",
            }
        )
        data = fetch_json(f"{self.base_url}/coins/markets?{query}")
        if not data:
            return None
        item = data[0]
        return {
            "symbol": normalize_symbol(item.get("symbol", symbol)),
            "name": item.get("name", symbol),
            "rank": item.get("market_cap_rank"),
            "price": item.get("current_price"),
            "market_cap": item.get("market_cap"),
            "volume_24h": item.get("total_volume"),
            "change_24h_pct": item.get("price_change_percentage_24h"),
            "source": "coingecko_lookup",
        }

    def _resolve_coin_id(self, symbol: str) -> Optional[str]:
        query = urllib.parse.urlencode({"query": symbol})
        data = fetch_json(f"{self.base_url}/search?{query}")
        matches: List[str] = []
        for coin in data.get("coins", []):
            if normalize_symbol(coin.get("symbol", "")) == symbol:
                coin_id = coin.get("id")
                if coin_id:
                    matches.append(coin_id)
        if len(matches) == 1:
            self._symbol_to_id[symbol] = matches[0]
            return matches[0]
        return None

    def describe(self) -> Dict:
        return {"status": "enabled", "vs_currency": self.vs_currency}


class FundingRateProvider:
    def __init__(self, cfg: Optional[Dict] = None):
        cfg = cfg or {}
        self.enabled = cfg.get("enabled", True)
        self.source_order = ["coingecko_derivatives"]
        self.category = cfg.get("category", "USDT-FUTURES")
        self.bybit_category = cfg.get("bybit_category", "linear")
        self.history_limit = int(cfg.get("history_limit", 3))
        self.anomaly_threshold = float(cfg.get("anomaly_threshold", 0.00005))
        self.turning_delta = float(cfg.get("turning_delta", 0.00001))
        self.request_timeout = int(cfg.get("request_timeout", 5))
        self.total_deadline = float(cfg.get("total_deadline", 12))
        configured_markets = cfg.get("market_preference")
        if configured_markets:
            self.market_preference = [str(item).lower() for item in configured_markets]
        else:
            self.market_preference = ["binance", "bitget", "bybit", "okx"]
        self.symbol_overrides = {
            normalize_symbol(symbol): value
            for symbol, value in (cfg.get("symbol_overrides", {}) or {}).items()
        }
        self._last_error: Optional[str] = None
        self._last_source: Optional[str] = None
        self._last_rates: Dict[str, float] = {}
        self.coingecko_base_url = "https://api.coingecko.com/api/v3"
        self.bitget_base_url = "https://api.bitget.com"
        self.binance_base_url = "https://fapi.binance.com"
        self.bybit_base_url = "https://api.bybit.com"

    def fetch_symbol_data(self, symbol: str, query_pack: Optional[str] = None) -> Optional[Dict]:
        _ = query_pack
        if not self.enabled:
            return None
        if self.total_deadline <= 0:
            self._last_error = "funding_unavailable:deadline_exceeded"
            return None

        market_symbol = self.symbol_overrides.get(normalize_symbol(symbol), f"{normalize_symbol(symbol)}USDT")
        self._last_source = None
        deadline_at = time.monotonic() + self.total_deadline
        errors: List[str] = []

        for source in self.source_order:
            if self._deadline_exceeded(deadline_at):
                errors.append("deadline_exceeded")
                break
            try:
                if source == "coingecko_derivatives":
                    data = self._fetch_from_coingecko_derivatives(normalize_symbol(symbol), market_symbol, deadline_at)
                else:
                    errors.append(f"{source}:unsupported_source")
                    continue
            except Exception as exc:
                errors.append(f"{source}:{exc}")
                continue

            if data:
                self._last_source = source
                self._last_error = None
                return data

            errors.append(f"{source}:no_data")

        joined_errors = "; ".join(errors) if errors else "all_funding_sources_failed"
        self._last_error = f"funding_unavailable:{joined_errors}"
        return None

    def describe(self) -> Dict:
        if not self.enabled:
            return {"status": "disabled"}
        if self._last_error:
            return {
                "status": "degraded",
                "error": self._last_error,
                "source_order": self.source_order,
                "last_source": self._last_source,
            }
        return {
            "status": "enabled",
            "source_order": self.source_order,
            "last_source": self._last_source,
            "endpoint": "coingecko_derivatives",
        }

    def _fetch_from_coingecko_derivatives(
        self, base_symbol: str, market_symbol: str, deadline_at: Optional[float] = None
    ) -> Optional[Dict]:
        payload = fetch_json(
            f"{self.coingecko_base_url}/derivatives",
            timeout=self._request_timeout(deadline_at),
        )
        rows = payload if isinstance(payload, list) else []
        candidate = self._select_derivatives_row(base_symbol, market_symbol, rows)
        if not candidate:
            return None

        current_rate = self._parse_float(candidate.get("funding_rate"))
        if current_rate is None:
            return None
        previous_rate = self._last_rates.get(base_symbol)
        history_rates = [previous_rate] if previous_rate is not None else []

        snapshot = self._build_funding_snapshot(
            exchange=self._normalize_exchange(candidate.get("market")),
            market_symbol=normalize_symbol(str(candidate.get("symbol") or market_symbol)),
            current_rate=current_rate,
            history_rates=history_rates,
            next_update=candidate.get("last_traded_at"),
            funding_interval_hours=None,
        )
        if not snapshot:
            return None

        snapshot["market"] = candidate.get("market")
        snapshot["basis"] = self._parse_float(candidate.get("basis"))
        snapshot["open_interest"] = self._parse_float(candidate.get("open_interest"))
        snapshot["volume_24h"] = self._parse_float(candidate.get("volume_24h"))
        self._last_rates[base_symbol] = current_rate
        return snapshot

    def _select_derivatives_row(self, base_symbol: str, market_symbol: str, rows: Sequence[Dict[str, Any]]) -> Optional[Dict]:
        aliases = {
            normalize_symbol(market_symbol),
            f"{base_symbol}USD_PERP",
            f"{base_symbol}USDT",
            f"{base_symbol}USD",
            f"{base_symbol}USDC",
        }
        candidates: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if self._parse_float(row.get("funding_rate")) is None:
                continue
            contract_type = str(row.get("contract_type", "")).lower()
            if contract_type and contract_type != "perpetual":
                continue

            row_symbol = normalize_symbol(str(row.get("symbol", "")))
            index_id = normalize_symbol(str(row.get("index_id", "")))
            if not (index_id == base_symbol or row_symbol in aliases or row_symbol.startswith(base_symbol)):
                continue
            candidates.append(row)

        if not candidates:
            return None
        ranked = sorted(candidates, key=self._market_preference_rank)
        return ranked[0]

    def _market_preference_rank(self, row: Dict[str, Any]) -> int:
        market_name = str(row.get("market", "")).lower()
        for idx, preferred in enumerate(self.market_preference):
            if preferred in market_name:
                return idx
        return len(self.market_preference)

    @staticmethod
    def _normalize_exchange(market_name: Any) -> str:
        market = str(market_name or "").lower()
        for exchange in ("binance", "bitget", "bybit", "okx"):
            if exchange in market:
                return exchange
        return "coingecko_derivatives"

    def _build_funding_snapshot(
        self,
        exchange: str,
        market_symbol: str,
        current_rate: Optional[float],
        history_rates: Sequence[float],
        next_update: Any,
        funding_interval_hours: Optional[int],
    ) -> Optional[Dict]:
        if current_rate is None:
            return None
        previous_rate = history_rates[0] if history_rates else None
        is_anomalous = self._is_anomalous(current_rate, history_rates)
        is_turning = self._is_turning(current_rate, previous_rate)
        return {
            "exchange": exchange,
            "symbol": market_symbol,
            "current_rate": current_rate,
            "current_rate_pct": current_rate * 100,
            "previous_rate": previous_rate,
            "history_rates": list(history_rates),
            "funding_interval_hours": funding_interval_hours,
            "next_update": next_update,
            "is_anomalous": is_anomalous,
            "is_turning": is_turning,
        }

    @staticmethod
    def _parse_float(value: Any) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _deadline_exceeded(deadline_at: Optional[float]) -> bool:
        return deadline_at is not None and (deadline_at - time.monotonic()) <= 0

    def _request_timeout(self, deadline_at: Optional[float]) -> float:
        if deadline_at is None:
            return float(self.request_timeout)
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("deadline_exceeded")
        return min(float(self.request_timeout), remaining)

    @staticmethod
    def _parse_int(value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _is_anomalous(self, current_rate: Optional[float], history_rates: Sequence[float]) -> bool:
        if current_rate is None:
            return False
        if abs(current_rate) >= self.anomaly_threshold:
            return True
        return any(abs(rate) >= self.anomaly_threshold for rate in history_rates)

    def _is_turning(self, current_rate: Optional[float], previous_rate: Optional[float]) -> bool:
        if current_rate is None or previous_rate is None:
            return False
        if previous_rate <= 0 < current_rate:
            return True
        if previous_rate >= 0 > current_rate:
            return True
        if previous_rate < 0 and current_rate > previous_rate + self.turning_delta:
            return True
        if previous_rate > 0 and current_rate < previous_rate - self.turning_delta:
            return True
        return False


class OKXFuturesProvider:
    """OKX perpetual swap public data — primary source for Malaysia.

    All endpoints are free and require no API key.
    Base: https://www.okx.com
    """

    BASE = "https://www.okx.com"
    MIN_HISTORY_FOR_Z = 10

    def __init__(self, cfg: Optional[Dict] = None):
        cfg = cfg or {}
        self.request_timeout = int(cfg.get("request_timeout", 8))
        self._last_error: Optional[str] = None

    @staticmethod
    def _to_okx_inst_id(symbol: str) -> str:
        """Map symbol like 'BTC' or 'BTC/USDT' to OKX instId."""
        base = normalize_symbol(symbol).replace("/USDT", "").replace("USDT", "")
        return f"{base}-USDT-SWAP"

    def fetch_symbol_data(self, symbol: str, query_pack: Optional[str] = None) -> Any:
        """Fetch funding + OI + LS ratio + taker flow from OKX.

        Returns dict on success, or 'okx_unavailable:{reason}' string on failure.
        """
        _ = query_pack
        inst_id = self._to_okx_inst_id(symbol)
        result: Dict[str, Any] = {"exchange": "okx", "symbol": inst_id}

        try:
            # 1. Current funding rate
            fr_data = self._fetch_endpoint(
                f"/api/v5/public/funding-rate?instId={inst_id}")
            if fr_data and fr_data.get("data"):
                row = fr_data["data"][0]
                result["funding_rate"] = self._pf(row.get("fundingRate"))
                result["next_funding_rate"] = self._pf(row.get("nextFundingRate"))
            else:
                result["funding_rate"] = None

            # 2. Funding history (for z-score)
            fh_data = self._fetch_endpoint(
                f"/api/v5/public/funding-rate-history?instId={inst_id}&limit=100")
            history_rates: List[float] = []
            if fh_data and fh_data.get("data"):
                for row in fh_data["data"]:
                    r = self._pf(row.get("fundingRate") or row.get("realizedRate"))
                    if r is not None:
                        history_rates.append(r)
            result["history_rates"] = history_rates
            result["funding_z_score"] = self._compute_z_score(
                result.get("funding_rate"), history_rates)
            if history_rates:
                result["funding_annualized"] = (result.get("funding_rate") or 0) * 3 * 365 * 100
                # rate_24h_ago: 3 funding periods ago (8h each)
                if len(history_rates) >= 3:
                    result["rate_24h_ago"] = history_rates[2]  # data is newest-first
                else:
                    result["rate_24h_ago"] = None
                # 1-sigma of history
                if len(history_rates) >= self.MIN_HISTORY_FOR_Z:
                    import statistics
                    result["rate_1sigma"] = statistics.stdev(history_rates)
                else:
                    result["rate_1sigma"] = 0.0001
            else:
                result["funding_annualized"] = None
                result["rate_24h_ago"] = None
                result["rate_1sigma"] = 0.0001

            # Anomaly flags for backward compat with old FundingRateProvider
            result["is_anomalous"] = (
                result.get("funding_z_score") is not None
                and abs(result["funding_z_score"]) > 3.0
            )
            result["is_turning"] = False
            if result.get("rate_24h_ago") is not None and result.get("funding_rate") is not None:
                now = result["funding_rate"]
                prev = result["rate_24h_ago"]
                if now * prev < 0:  # sign flip
                    result["is_turning"] = True

            # 3. Open Interest
            oi_data = self._fetch_endpoint(
                f"/api/v5/public/open-interest?instType=SWAP&instId={inst_id}")
            if oi_data and oi_data.get("data"):
                row = oi_data["data"][0]
                oi_contracts = self._pf(row.get("oi") or row.get("oiCcy"))
                result["oi_value"] = oi_contracts if oi_contracts else 0.0
            else:
                result["oi_value"] = 0.0

            # 4. Long/Short account ratio
            ls_acct = self._fetch_endpoint(
                f"/api/v5/rubik/stat/contracts/long-short-account-ratio"
                f"?instId={inst_id}&period=1H")
            if ls_acct and ls_acct.get("data") and ls_acct["data"]:
                row = ls_acct["data"][0]
                val = row[1] if isinstance(row, list) and len(row) > 1 else None
                result["ls_ratio_account"] = self._pf(val)
            else:
                result["ls_ratio_account"] = None

            # 5. Long/Short contract ratio
            ls_cont = self._fetch_endpoint(
                f"/api/v5/rubik/stat/contracts/long-short-ratio"
                f"?instId={inst_id}&period=1H")
            if ls_cont and ls_cont.get("data") and ls_cont["data"]:
                row = ls_cont["data"][0]
                val = row[1] if isinstance(row, list) and len(row) > 1 else None
                result["ls_ratio_contract"] = self._pf(val)
            else:
                result["ls_ratio_contract"] = None

            # 6. Taker buy/sell volume
            taker = self._fetch_endpoint(
                f"/api/v5/rubik/stat/contracts/taker-volume"
                f"?instId={inst_id}&period=1H")
            if taker and taker.get("data") and taker["data"]:
                row = taker["data"][0]
                if isinstance(row, list) and len(row) >= 3:
                    buy = self._pf(row[1]) or 0.0
                    sell = self._pf(row[2]) or 0.0
                    total = buy + sell
                    result["taker_buy_ratio"] = buy / total if total > 0 else 0.5
                else:
                    result["taker_buy_ratio"] = 0.5
            else:
                result["taker_buy_ratio"] = 0.5

            self._last_error = None
            return result

        except Exception as exc:
            self._last_error = str(exc)
            return f"okx_unavailable:{exc}"

    def _fetch_endpoint(self, path: str) -> Optional[Dict]:
        url = f"{self.BASE}{path}"
        return fetch_json(url, timeout=self.request_timeout)

    def _compute_z_score(
        self, current: Optional[float], history: List[float]
    ) -> Optional[float]:
        if current is None or len(history) < self.MIN_HISTORY_FOR_Z:
            return None
        import statistics
        mean = statistics.mean(history)
        stdev = statistics.stdev(history)
        if stdev < 1e-12:
            return 0.0
        return (current - mean) / stdev

    @staticmethod
    def _pf(value: Any) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def describe(self) -> Dict:
        if self._last_error:
            return {"status": "degraded", "error": self._last_error, "source": "okx"}
        return {"status": "enabled", "source": "okx"}


class DeribitOptionsProvider:
    """Deribit options public data — free, no API key required.

    Single endpoint: /api/v2/public/get_book_summary_by_currency
    Computes max_pain, put_call_ratio, and gamma_cluster signal.
    """

    BASE = "https://www.deribit.com"
    # Gamma cluster: OI concentrated at strike within ±2% of spot, expiry < 7 days.
    GAMMA_STRIKE_PCT = 0.02
    GAMMA_EXPIRY_DAYS = 7
    GAMMA_OI_CONCENTRATION = 0.30  # near-ATM OI must be >30% of total to qualify

    def __init__(self, cfg: Optional[Dict] = None):
        cfg = cfg or {}
        self.request_timeout = int(cfg.get("request_timeout", 10))
        self._last_error: Optional[str] = None

    def fetch_symbol_data(self, symbol: str, query_pack: Optional[str] = None) -> Any:
        _ = query_pack
        currency = normalize_symbol(symbol).replace("USDT", "").replace("/", "")
        try:
            resp = fetch_json(
                f"{self.BASE}/api/v2/public/get_book_summary_by_currency"
                f"?currency={currency}&kind=option",
                timeout=self.request_timeout,
            )
            instruments = resp.get("result", []) if isinstance(resp, dict) else []
            if not instruments:
                return f"deribit_unavailable:no_data"
            self._last_error = None
            return self._process(instruments)
        except Exception as exc:
            self._last_error = str(exc)
            return f"deribit_unavailable:{exc}"

    def _process(self, instruments: List[Dict]) -> Dict:
        today = dt.date.today()
        call_oi: Dict[tuple, float] = {}
        put_oi: Dict[tuple, float] = {}
        total_call = 0.0
        total_put = 0.0
        current_price: Optional[float] = None

        for inst in instruments:
            name = inst.get("instrument_name", "")
            parts = name.split("-")
            if len(parts) < 4:
                continue
            try:
                strike = float(parts[2])
                opt_type = parts[3]
                expiry = dt.datetime.strptime(parts[1], "%d%b%y").date()
            except (ValueError, IndexError):
                continue

            oi = float(inst.get("open_interest") or 0)
            if current_price is None:
                p = inst.get("underlying_price")
                if p:
                    current_price = float(p)

            key = (strike, expiry)
            if opt_type == "C":
                call_oi[key] = call_oi.get(key, 0.0) + oi
                total_call += oi
            elif opt_type == "P":
                put_oi[key] = put_oi.get(key, 0.0) + oi
                total_put += oi

        total_oi = total_call + total_put
        put_call_ratio = (total_put / total_call) if total_call > 0 else None

        # Max pain: strike with highest combined OI (simple proxy)
        all_keys = set(list(call_oi.keys()) + list(put_oi.keys()))
        strike_totals: Dict[float, float] = {}
        for k in all_keys:
            strike_totals[k[0]] = strike_totals.get(k[0], 0.0) + call_oi.get(k, 0.0) + put_oi.get(k, 0.0)
        max_pain = max(strike_totals, key=lambda s: strike_totals[s]) if strike_totals else None

        # Gamma cluster detection
        gamma_cluster = False
        gamma_strike: Optional[float] = None
        gamma_expiry_days: Optional[int] = None

        if current_price and current_price > 0 and total_oi > 0:
            lb = current_price * (1 - self.GAMMA_STRIKE_PCT)
            ub = current_price * (1 + self.GAMMA_STRIKE_PCT)
            near_oi = 0.0
            best_strike: Optional[float] = None
            best_days: Optional[int] = None

            for k in all_keys:
                strike, expiry = k
                days = (expiry - today).days
                if lb <= strike <= ub and 0 <= days < self.GAMMA_EXPIRY_DAYS:
                    oi_at_k = call_oi.get(k, 0.0) + put_oi.get(k, 0.0)
                    if oi_at_k > near_oi:
                        near_oi = oi_at_k
                        best_strike = strike
                        best_days = days

            if near_oi / total_oi > self.GAMMA_OI_CONCENTRATION:
                gamma_cluster = True
                gamma_strike = best_strike
                gamma_expiry_days = best_days

        return {
            "max_pain": max_pain,
            "put_call_ratio": put_call_ratio,
            "gamma_cluster": gamma_cluster,
            "gamma_strike": gamma_strike,
            "gamma_expiry_days": gamma_expiry_days,
        }

    def describe(self) -> Dict:
        if self._last_error:
            return {"status": "degraded", "error": self._last_error, "source": "deribit"}
        return {"status": "enabled", "source": "deribit"}


class BitgetFuturesProvider:
    """Bitget perpetual futures public data — cross-validation fallback for OKX.

    All endpoints are free and require no API key.
    Base: https://api.bitget.com
    """

    BASE = "https://api.bitget.com"

    def __init__(self, cfg: Optional[Dict] = None):
        cfg = cfg or {}
        self.request_timeout = int(cfg.get("request_timeout", 8))
        self._last_error: Optional[str] = None

    @staticmethod
    def _to_bitget_symbol(symbol: str) -> str:
        base = normalize_symbol(symbol).replace("/USDT", "").replace("USDT", "")
        return f"{base}USDT"

    def fetch_symbol_data(self, symbol: str, query_pack: Optional[str] = None) -> Any:
        _ = query_pack
        sym = self._to_bitget_symbol(symbol)
        try:
            fr_resp = fetch_json(
                f"{self.BASE}/api/v2/mix/market/current-fund-rate"
                f"?symbol={sym}&productType=USDT-FUTURES",
                timeout=self.request_timeout,
            )
            funding_rate: Optional[float] = None
            if fr_resp and fr_resp.get("data"):
                funding_rate = self._pf(fr_resp["data"].get("fundingRate"))

            oi_resp = fetch_json(
                f"{self.BASE}/api/v2/mix/market/open-interest"
                f"?symbol={sym}&productType=USDT-FUTURES",
                timeout=self.request_timeout,
            )
            oi_value: Optional[float] = None
            if oi_resp and oi_resp.get("data"):
                oi_list = oi_resp["data"].get("openInterestList", [])
                if oi_list:
                    oi_value = self._pf(oi_list[0].get("size"))

            self._last_error = None
            return {
                "exchange": "bitget",
                "symbol": sym,
                "funding_rate": funding_rate,
                "oi_value": oi_value,
            }
        except Exception as exc:
            self._last_error = str(exc)
            return f"bitget_unavailable:{exc}"

    @staticmethod
    def _pf(value: Any) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def describe(self) -> Dict:
        if self._last_error:
            return {"status": "degraded", "error": self._last_error, "source": "bitget"}
        return {"status": "enabled", "source": "bitget"}


@dataclass
class ProviderBundle:
    coingecko: object
    funding: Optional[object] = None
    okx_futures: Optional[object] = None
    bitget_futures: Optional[object] = None
    deribit_options: Optional[object] = None


def load_config(path: Path) -> Dict:
    return json.loads(path.read_text(encoding="utf-8"))


def merge_universe(top_assets: List[Dict], manual_watchlist: Sequence[str], manual_placeholders: Sequence[str]) -> Dict:
    resolved: List[Dict] = []
    seen: Set[str] = set()

    for asset in top_assets:
        symbol = normalize_symbol(asset.get("symbol", ""))
        if not symbol or symbol in seen:
            continue
        normalized = dict(asset)
        normalized["symbol"] = symbol
        normalized.setdefault("source", "coingecko_top")
        resolved.append(normalized)
        seen.add(symbol)

    for symbol in dedupe_preserve_order(normalize_symbol(item) for item in manual_watchlist if item.strip()):
        if symbol in seen:
            continue
        resolved.append({"symbol": symbol, "name": symbol, "source": "manual_watchlist"})
        seen.add(symbol)

    unresolved = [
        {"symbol": normalize_symbol(symbol), "status": "unresolved", "source": "manual_placeholder"}
        for symbol in dedupe_preserve_order(normalize_symbol(item) for item in manual_placeholders if item.strip())
    ]

    return {"resolved": resolved, "unresolved": unresolved}


# B2 Tight quantitative thesis pattern — token must carry %, $, decimal, or K/M/B unit.
# Bare integers ("45 of 60 cycles") do NOT qualify; that's narrative dressed as data.
_QUANT_TOKEN = re.compile(
    r"\$\s*\d+(?:\.\d+)?\s*[KMB]?"   # $5, $8.2B, $ 12K
    r"|\d+(?:\.\d+)?\s*%"            # 18%, 0.142%
    r"|\d+\.\d+"                     # 3.4, 0.142 (decimals)
    r"|\d+\s*[KMB]\b"                # 5M, 12 K
)
def _sign(x: float) -> int:
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


def _count_numbers(text: str) -> int:
    """Count numeric tokens in a string (e.g. '0.142', '3.4', '18')."""
    import re
    return len(re.findall(r'\d+\.?\d*', text))


def evaluate_asset_policy(
    symbol: str,
    tags: Sequence[str],
    market_data: Optional[Dict],
    watchlist_symbols: Set[str],
    funding_data: Optional[Dict] = None,
    oi_data: Optional[Dict] = None,
    ls_data: Optional[Dict] = None,
    taker_data: Optional[Dict] = None,
    deribit_data: Optional[Dict] = None,
    bitget_data: Optional[Dict] = None,
    counterparty_thesis: Optional[str] = None,
) -> Dict:
    has_watchlist = normalize_symbol(symbol) in watchlist_symbols
    has_market_data = bool(market_data)
    has_market_move = "market-move" in tags
    has_funding_signal = has_meaningful_funding_signal(funding_data)
    has_funding_turning = funding_data.get('is_turning', False) if isinstance(funding_data, dict) else False

    evidence: List[str] = []
    if has_market_data:
        evidence.append("market_data")
    if has_watchlist:
        evidence.append("watchlist")
    if has_funding_signal:
        evidence.append("funding")

    # ── Forced-Flow Primitive Detection ──
    forced_flow_primitives: List[str] = []

    # P0: Funding Extreme
    if funding_data and isinstance(funding_data, dict):
        z = funding_data.get("funding_z_score")
        if z is not None and abs(z) > 3.0:
            forced_flow_primitives.append("funding_extreme")

    # P0: OI Divergence
    if oi_data and isinstance(oi_data, dict):
        delta_pct = oi_data.get("oi_delta_pct_24h") or oi_data.get("delta_pct", 0)
        if abs(delta_pct) > 0.05:
            price_change = (market_data or {}).get("change_24h_pct", 0) or 0
            if _sign(price_change) != 0 and _sign(price_change) != _sign(delta_pct):
                forced_flow_primitives.append("oi_divergence")

    # P1: LS Ratio Extreme
    if ls_data and isinstance(ls_data, dict):
        acct = ls_data.get("ls_ratio_account")
        cont = ls_data.get("ls_ratio_contract")
        if acct is not None and cont is not None:
            if (acct > 2.0 and cont < 0.8) or (acct < 0.5 and cont > 1.25):
                forced_flow_primitives.append("ls_ratio_extreme")

    # P1: Funding Flip
    if funding_data and isinstance(funding_data, dict):
        now_rate = funding_data.get("funding_rate") or funding_data.get("current_rate", 0)
        prev_rate = funding_data.get("rate_24h_ago")
        sigma = funding_data.get("rate_1sigma", 0.0001)
        if prev_rate is not None and now_rate is not None:
            if now_rate * prev_rate < 0 and abs(now_rate) > sigma:
                forced_flow_primitives.append("funding_flip")

    # P1: Taker Flow Imbalance
    if taker_data and isinstance(taker_data, dict):
        buy_ratio = taker_data.get("taker_buy_ratio")
        if buy_ratio is not None and (buy_ratio < 0.3 or buy_ratio > 0.7):
            forced_flow_primitives.append("taker_flow_imbalance")

    # P2: Options Gamma Cluster (Deribit)
    if deribit_data and isinstance(deribit_data, dict):
        if deribit_data.get("gamma_cluster"):
            forced_flow_primitives.append("options_gamma_cluster")

    # P2: Cross-Exchange Confirm (OKX + Bitget agree on extreme)
    if (funding_data and isinstance(funding_data, dict)
            and bitget_data and isinstance(bitget_data, dict)):
        okx_rate = funding_data.get("funding_rate") or funding_data.get("current_rate", 0)
        okx_z = funding_data.get("funding_z_score")
        bitget_rate = bitget_data.get("funding_rate")
        if (okx_rate is not None and bitget_rate is not None
                and okx_z is not None and abs(okx_z) > 3.0
                and _sign(okx_rate) == _sign(bitget_rate)
                and abs(bitget_rate) > 0.0005):
            forced_flow_primitives.append("cross_exchange_confirm")

    if forced_flow_primitives:
        evidence.append("forced_flow")

    # ── Decision Logic ──
    recommended_action = "no_trade"
    crowding_risk = "moderate"
    rationale = ["default_refusal"]

    if has_market_move and len(evidence) == 1:
        crowding_risk = "high"
        rationale = ["public_market_move_only"]
    elif has_watchlist and len(forced_flow_primitives) >= 1:
        recommended_action = "investigate"
        crowding_risk = "low"
        rationale = ["forced_flow_confirmation"]
    elif has_watchlist and has_funding_turning:
        recommended_action = "watch"
        crowding_risk = "moderate"
        rationale = ["funding_turning_watch"]
    elif has_watchlist and has_funding_signal:
        recommended_action = "watch"
        crowding_risk = "moderate"
        rationale = ["funding_signal_watch"]
    elif has_market_move and len(evidence) <= 2:
        recommended_action = "watch"
        crowding_risk = "elevated"
        rationale = ["crowded_price_action"]

    result: Dict[str, Any] = {
        "recommended_action": recommended_action,
        "crowding_risk": crowding_risk,
        "evidence_diversity": len(evidence),
        "evidence": evidence,
        "rationale": rationale,
        "forced_flow_primitives": forced_flow_primitives,
    }

    # ── Counterparty Thesis Numeric Discipline ──
    if counterparty_thesis is not None:
        result["thesis_numeric_ok"] = _count_numbers(counterparty_thesis) >= 2

    return result


def build_asset_card(
    symbol: str,
    market_data: Optional[Dict],
    watchlist_symbols: Set[str],
    funding_data: Optional[Dict] = None,
    okx_data=None,
    deribit_data=None,
) -> Dict:
    symbol = normalize_symbol(symbol)
    tags: List[str] = []
    if symbol in watchlist_symbols:
        tags.append("watchlist")
    if market_data:
        tags.append("market-data")
        if market_data.get("change_24h_pct") is not None:
            change = market_data.get("change_24h_pct") or 0.0
            if abs(change) >= 5:
                tags.append("market-move")
    if has_meaningful_funding_signal(funding_data):
        tags.append("funding-signal")

    okx_ok = okx_data and isinstance(okx_data, dict)
    deribit_ok = deribit_data and isinstance(deribit_data, dict)

    providers = {
        "coingecko": {
            "status": "ok" if market_data else "no_data",
            "data": market_data or {},
        },
        "funding": {
            "status": "ok" if funding_data else "no_data",
            "data": funding_data or {},
        },
        "okx": {
            "status": "ok" if okx_ok else "no_data",
            "data": okx_data if okx_ok else {},
        },
        "deribit": {
            "status": "ok" if deribit_ok else "no_data",
            "data": deribit_data if deribit_ok else {},
        },
    }
    policy = evaluate_asset_policy(
        symbol,
        tags,
        market_data,
        watchlist_symbols,
        funding_data=funding_data,
        deribit_data=deribit_data if deribit_ok else None,
        bitget_data=None,
    )

    return {
        "symbol": symbol,
        "name": (market_data or {}).get("name", symbol),
        "tags": tags,
        "providers": providers,
        "policy": policy,
    }


def card_priority(card: Dict) -> tuple:
    tags = set(card.get("tags", []))
    return (
        1 if "watchlist" in tags else 0,
        1 if "funding-signal" in tags else 0,
        1 if "market-move" in tags else 0,
        card.get("symbol", ""),
    )


def build_markdown_report(report: Dict) -> str:
    lines = [
        "# Council Intel Report",
        "",
        f"Generated: {report['run']['started_at']}",
        "",
        "## Highest-Interest Assets",
    ]

    highest = report["summary"].get("highest_interest", [])
    if highest:
        for symbol in highest:
            lines.append(f"- {symbol}")
    else:
        lines.append("- None")

    lines.extend(["", "## Investigate Candidates"])

    investigate_candidates = report["summary"].get("investigate_candidates", [])
    if investigate_candidates:
        for symbol in investigate_candidates:
            lines.append(f"- {symbol}")
    else:
        lines.append("- None")

    lines.extend(["", "## No-Trade Assets"])

    no_trade_assets = report["summary"].get("no_trade_assets", [])
    if no_trade_assets:
        for symbol in no_trade_assets:
            lines.append(f"- {symbol}")
    else:
        lines.append("- None")

    lines.extend(["", "## Crowding Warnings"])

    crowding_warnings = report["summary"].get("crowding_warnings", [])
    if crowding_warnings:
        for symbol in crowding_warnings:
            lines.append(f"- {symbol}")
    else:
        lines.append("- None")

    lines.extend(["", "## Funding Signal Assets"])

    funding_signal_assets = report["summary"].get("funding_signal_assets", [])
    if funding_signal_assets:
        for symbol in funding_signal_assets:
            lines.append(f"- {symbol}")
    else:
        lines.append("- None")

    lines.extend(["", "## Watchlist Assets"])

    watchlist_cards = [asset for asset in report["assets"] if "watchlist" in asset.get("tags", [])]
    if watchlist_cards:
        for asset in watchlist_cards:
            lines.append(f"- {asset['symbol']}: {', '.join(asset.get('tags', []))}")
    else:
        lines.append("- No watchlist assets resolved")

    lines.extend(["", "## Unresolved And Future Assets"])

    for item in report["universe"].get("unresolved", []):
        lines.append(f"- {item['symbol']}: unresolved")

    lines.extend(["", "## Provider Freshness"])
    for provider_name, status in report.get("providers", {}).items():
        lines.append(f"- {provider_name}: {status.get('status', 'unknown')}")

    return "\n".join(lines) + "\n"


def write_report_artifacts(report: Dict, out_dir: Path) -> Dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "report.json"
    markdown_path = out_dir / "report.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    markdown_path.write_text(build_markdown_report(report), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(markdown_path)}


def build_default_provider_bundle(config: Dict) -> ProviderBundle:
    provider_cfg = config.get("providers", {})
    coingecko_cfg = provider_cfg.get("coingecko", {})
    coingecko = CoinGeckoProvider(vs_currency=coingecko_cfg.get("vs_currency", "usd"))
    funding = FundingRateProvider(provider_cfg.get("funding"))
    return ProviderBundle(coingecko=coingecko, funding=funding)


def run_intel_cycle(config: Dict, out_dir: Path, provider_bundle: Optional[ProviderBundle] = None) -> Dict:
    bundle = provider_bundle or build_default_provider_bundle(config)
    top_n = int(config.get("top_n", 20))
    coingecko_status = dict(bundle.coingecko.describe())
    top_assets: List[Dict] = []
    try:
        top_assets = bundle.coingecko.fetch_top_assets(top_n)
    except Exception as exc:
        coingecko_status["status"] = "degraded"
        coingecko_status["error"] = str(exc)
    manual_watchlist = [normalize_symbol(item) for item in config.get("manual_watchlist", []) if item.strip()]
    watchlist_symbols = set(manual_watchlist)
    merged = merge_universe(top_assets, manual_watchlist, config.get("manual_placeholders", []))
    unresolved = list(merged["unresolved"])

    assets: List[Dict] = []
    for seed_asset in merged["resolved"]:
        symbol = normalize_symbol(seed_asset["symbol"])
        if seed_asset.get("source") == "coingecko_top":
            market_data = seed_asset
        else:
            try:
                market_data = bundle.coingecko.fetch_asset_snapshot(symbol)
            except Exception as exc:
                coingecko_status["status"] = "degraded"
                coingecko_status["error"] = str(exc)
                market_data = None
        if market_data is None and seed_asset.get("source") == "manual_watchlist":
            unresolved.append(
                {
                    "symbol": symbol,
                    "status": "unresolved",
                    "source": "manual_watchlist",
                }
            )
            continue
        market_data = market_data or seed_asset
        funding_data = (
            bundle.funding.fetch_symbol_data(symbol)
            if bundle.funding and symbol in watchlist_symbols
            else None
        )
        okx_data = (
            bundle.okx_futures.fetch_symbol_data(symbol)
            if bundle.okx_futures and symbol in watchlist_symbols
            else None
        )
        deribit_data = (
            bundle.deribit_options.fetch_symbol_data(symbol)
            if bundle.deribit_options and symbol in watchlist_symbols
            else None
        )
        assets.append(
            build_asset_card(
                symbol,
                market_data,
                watchlist_symbols,
                funding_data=funding_data,
                okx_data=okx_data,
                deribit_data=deribit_data,
            )
        )

    assets.sort(key=card_priority, reverse=True)
    highest_interest = [asset["symbol"] for asset in assets[: min(5, len(assets))]]
    funding_signal_assets = [asset["symbol"] for asset in assets if "funding-signal" in asset.get("tags", [])]
    investigate_candidates = [
        asset["symbol"]
        for asset in assets
        if asset.get("policy", {}).get("recommended_action") == "investigate"
    ]
    no_trade_assets = [
        asset["symbol"]
        for asset in assets
        if asset.get("policy", {}).get("recommended_action") == "no_trade"
    ]
    crowding_warnings = [
        asset["symbol"]
        for asset in assets
        if asset.get("policy", {}).get("crowding_risk") in {"high", "elevated"}
    ]

    report = {
        "run": {
            "started_at": utc_now_iso(),
            "mode": "read_only_intel",
            "top_n": top_n,
        },
        "providers": {
            "coingecko": coingecko_status,
            "funding": bundle.funding.describe() if bundle.funding else {"status": "disabled"},
        },
        "universe": merged,
        "assets": assets,
        "summary": {
            "asset_count": len(assets),
            "unresolved_count": len(unresolved),
            "highest_interest": highest_interest,
            "investigate_candidates": investigate_candidates,
            "no_trade_assets": no_trade_assets,
            "crowding_warnings": crowding_warnings,
            "funding_signal_assets": funding_signal_assets,
        },
    }
    report["universe"]["unresolved"] = unresolved
    write_report_artifacts(report, out_dir)
    return report


def cmd_run(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config))
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.out_dir) / timestamp
    report = run_intel_cycle(config=config, out_dir=out_dir)
    print(
        json.dumps(
            {
                "out_dir": str(out_dir),
                "asset_count": report["summary"]["asset_count"],
                "unresolved_count": report["summary"]["unresolved_count"],
            },
            indent=2,
        )
    )


def build_cli() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="council_intel.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Build a read-only intelligence snapshot.")
    run.add_argument("--config", default="config/universe.json")
    run.add_argument("--out-dir", default="artifacts/intel")
    run.set_defaults(func=cmd_run)

    return ap


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = build_cli()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
