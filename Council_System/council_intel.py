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


def is_funding_turning(data: Optional[Dict[str, Any]]) -> bool:
    return bool(data and data.get("is_turning"))


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


@dataclass
class ProviderBundle:
    coingecko: object
    funding: Optional[object] = None


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


def _has_quantitative_thesis(text: str) -> bool:
    if not text:
        return False
    return len(_QUANT_TOKEN.findall(text)) >= 2


def evaluate_asset_policy(
    symbol: str,
    tags: Sequence[str],
    market_data: Optional[Dict],
    watchlist_symbols: Set[str],
    funding_data: Optional[Dict] = None,
    oi_data: Optional[Dict] = None,
    counterparty_thesis: str = "",
) -> Dict:
    has_watchlist = normalize_symbol(symbol) in watchlist_symbols
    has_market_data = bool(market_data)
    has_market_move = "market-move" in tags
    has_funding_signal = has_meaningful_funding_signal(funding_data)
    has_funding_turning = is_funding_turning(funding_data)

    evidence: List[str] = []
    if has_market_data:
        evidence.append("market_data")
    if has_watchlist:
        evidence.append("watchlist")
    if has_funding_signal:
        evidence.append("funding")

    recommended_action = "no_trade"
    crowding_risk = "moderate"
    rationale = ["default_refusal"]

    if has_market_move and len(evidence) == 1:
        crowding_risk = "high"
        rationale = ["public_market_move_only"]
    elif has_watchlist and has_funding_turning:
        recommended_action = "investigate"
        crowding_risk = "low"
        rationale = ["orthogonal_watchlist_confirmation"]
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
    }
    if counterparty_thesis:
        result["thesis_numeric_ok"] = _has_quantitative_thesis(counterparty_thesis)
    return result


def build_asset_card(
    symbol: str,
    market_data: Optional[Dict],
    watchlist_symbols: Set[str],
    funding_data: Optional[Dict] = None,
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
    if is_funding_turning(funding_data):
        tags.append("funding-turning")

    providers = {
        "coingecko": {
            "status": "ok" if market_data else "no_data",
            "data": market_data or {},
        },
        "funding": {
            "status": "ok" if funding_data else "no_data",
            "data": funding_data or {},
        },
    }
    policy = evaluate_asset_policy(
        symbol,
        tags,
        market_data,
        watchlist_symbols,
        funding_data=funding_data,
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
        1 if "funding-turning" in tags else 0,
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

    lines.extend(["", "## Funding Turning Assets"])

    funding_turning_assets = report["summary"].get("funding_turning_assets", [])
    if funding_turning_assets:
        for symbol in funding_turning_assets:
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
        assets.append(
            build_asset_card(
                symbol,
                market_data,
                watchlist_symbols,
                funding_data=funding_data,
            )
        )

    assets.sort(key=card_priority, reverse=True)
    highest_interest = [asset["symbol"] for asset in assets[: min(5, len(assets))]]
    funding_signal_assets = [asset["symbol"] for asset in assets if "funding-signal" in asset.get("tags", [])]
    funding_turning_assets = [asset["symbol"] for asset in assets if "funding-turning" in asset.get("tags", [])]
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
            "funding_turning_assets": funding_turning_assets,
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
