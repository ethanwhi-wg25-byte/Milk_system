#!/usr/bin/env python3
"""Fetch 90-day historical OKX microstructure data for shadow replay.

Saves time-indexed JSON files under --out-dir/<SYMBOL>/:
  funding_rates.json   — 8h settlement funding rates (ts_ms, rate)
  ls_ratio.json        — 1h long/short account + contract ratios (ts_ms, acct, cont)
  taker_flow.json      — 1h taker buy/sell volumes (ts_ms, buy_vol, sell_vol)
  oi_snapshots.json    — 1h open interest (ts_ms, oi_contracts)

All timestamps stored as epoch milliseconds (UTC).

Usage:
  python3 fetch_okx_history.py --symbol BTC --days 90 --out-dir artifacts/okx_history
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


BASE = "https://www.okx.com"
REQUEST_TIMEOUT = 12
PAUSE_BETWEEN_CALLS = 0.25  # seconds — stay well within OKX rate limits


def _to_inst_id(symbol: str) -> str:
    base = symbol.upper().replace("/USDT", "").replace("USDT", "").strip()
    return f"{base}-USDT-SWAP"


def _fetch(path: str, params: Optional[Dict] = None) -> Optional[Dict]:
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "council-intel/1.0"})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        print(f"  WARN fetch failed {url}: {exc}")
        return None


def fetch_funding_history(inst_id: str, days: int) -> List[Tuple[int, float]]:
    """Returns list of (ts_ms, funding_rate), newest → oldest, de-duped."""
    since_ms = int((time.time() - days * 86400) * 1000)
    results: List[Tuple[int, float]] = []
    after: Optional[str] = None

    while True:
        params: Dict[str, Any] = {"instId": inst_id, "limit": 100}
        if after:
            params["after"] = after
        data = _fetch("/api/v5/public/funding-rate-history", params)
        time.sleep(PAUSE_BETWEEN_CALLS)
        if not data or not data.get("data"):
            break
        rows = data["data"]
        for row in rows:
            ts = int(row.get("fundingTime", 0))
            rate = row.get("fundingRate") or row.get("realizedRate")
            if ts and rate is not None:
                try:
                    results.append((ts, float(rate)))
                except (TypeError, ValueError):
                    pass
        if not rows:
            break
        oldest_ts = min(r[0] for r in results) if results else 0
        if oldest_ts <= since_ms:
            break
        # paginate: after = oldest fundingTime in this page
        after = str(min(int(r.get("fundingTime", 0)) for r in rows))

    results.sort(key=lambda x: x[0])
    # de-dup by ts
    seen: Dict[int, float] = {}
    for ts, r in results:
        seen[ts] = r
    return [(ts, r) for ts, r in sorted(seen.items())]


def _fetch_rubik_paginated(
    path: str,
    inst_id: str,
    period: str,
    days: int,
) -> List[List]:
    """Fetch a rubik stat endpoint that returns [ts, val, ...] rows, paginated."""
    since_ms = int((time.time() - days * 86400) * 1000)
    results: List[List] = []
    end: Optional[str] = None

    while True:
        params: Dict[str, Any] = {"instId": inst_id, "period": period, "limit": 100}
        if end:
            params["end"] = end
        data = _fetch(path, params)
        time.sleep(PAUSE_BETWEEN_CALLS)
        if not data or not data.get("data"):
            break
        rows = data["data"]
        if not rows:
            break
        for row in rows:
            if isinstance(row, list) and row:
                results.append(row)
        oldest_ts = min(int(r[0]) for r in rows if r)
        if oldest_ts <= since_ms:
            break
        end = str(oldest_ts)

    results.sort(key=lambda r: int(r[0]))
    # de-dup by ts
    seen: Dict[int, List] = {}
    for row in results:
        seen[int(row[0])] = row
    return [v for _, v in sorted(seen.items())]


def fetch_ls_ratio(inst_id: str, days: int) -> List[Tuple[int, float, float]]:
    """Returns (ts_ms, ls_account_ratio, ls_contract_ratio) tuples."""
    acct_rows = _fetch_rubik_paginated(
        "/api/v5/rubik/stat/contracts/long-short-account-ratio",
        inst_id, "1H", days,
    )
    cont_rows = _fetch_rubik_paginated(
        "/api/v5/rubik/stat/contracts/long-short-ratio",
        inst_id, "1H", days,
    )
    # Index contract rows by ts for fast lookup
    cont_by_ts: Dict[int, float] = {}
    for row in cont_rows:
        try:
            cont_by_ts[int(row[0])] = float(row[1])
        except (IndexError, TypeError, ValueError):
            pass

    results: List[Tuple[int, float, float]] = []
    for row in acct_rows:
        try:
            ts = int(row[0])
            acct = float(row[1])
            cont = cont_by_ts.get(ts, acct)  # fallback: use acct ratio
            results.append((ts, acct, cont))
        except (IndexError, TypeError, ValueError):
            pass
    return results


def fetch_taker_flow(inst_id: str, days: int) -> List[Tuple[int, float, float]]:
    """Returns (ts_ms, buy_vol, sell_vol) tuples."""
    rows = _fetch_rubik_paginated(
        "/api/v5/rubik/stat/contracts/taker-volume",
        inst_id, "1H", days,
    )
    results: List[Tuple[int, float, float]] = []
    for row in rows:
        try:
            ts = int(row[0])
            buy = float(row[1])
            sell = float(row[2])
            results.append((ts, buy, sell))
        except (IndexError, TypeError, ValueError):
            pass
    return results


def fetch_oi_snapshots(inst_id: str, days: int) -> List[Tuple[int, float]]:
    """Returns (ts_ms, oi_contracts) tuples. Uses open-interest-volume rubik endpoint."""
    rows = _fetch_rubik_paginated(
        "/api/v5/rubik/stat/contracts/open-interest-volume",
        inst_id, "1H", days,
    )
    results: List[Tuple[int, float]] = []
    for row in rows:
        try:
            ts = int(row[0])
            oi = float(row[1])
            results.append((ts, oi))
        except (IndexError, TypeError, ValueError):
            pass
    return results


def run(args: argparse.Namespace) -> None:
    inst_id = _to_inst_id(args.symbol)
    out_dir = Path(args.out_dir) / args.symbol.replace("/", "_")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Fetching {args.days}d OKX history for {inst_id} → {out_dir}")

    print("  [1/4] Funding rate history…")
    funding = fetch_funding_history(inst_id, args.days)
    print(f"        {len(funding)} records")
    with open(out_dir / "funding_rates.json", "w") as f:
        json.dump([{"ts": ts, "rate": rate} for ts, rate in funding], f)

    print("  [2/4] Long/Short ratio (account + contract)…")
    ls = fetch_ls_ratio(inst_id, args.days)
    print(f"        {len(ls)} records")
    with open(out_dir / "ls_ratio.json", "w") as f:
        json.dump([{"ts": ts, "acct": a, "cont": c} for ts, a, c in ls], f)

    print("  [3/4] Taker flow (buy/sell volume)…")
    taker = fetch_taker_flow(inst_id, args.days)
    print(f"        {len(taker)} records")
    with open(out_dir / "taker_flow.json", "w") as f:
        json.dump([{"ts": ts, "buy": b, "sell": s} for ts, b, s in taker], f)

    print("  [4/4] Open interest snapshots…")
    oi = fetch_oi_snapshots(inst_id, args.days)
    print(f"        {len(oi)} records")
    with open(out_dir / "oi_snapshots.json", "w") as f:
        json.dump([{"ts": ts, "oi": oi_val} for ts, oi_val in oi], f)

    print(f"Done. Files written to {out_dir}/")


def main() -> None:
    ap = argparse.ArgumentParser(prog="fetch_okx_history.py")
    ap.add_argument("--symbol", default="BTC", help="e.g. BTC or BTC/USDT")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--out-dir", default="artifacts/okx_history")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
