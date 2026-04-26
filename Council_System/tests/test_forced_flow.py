#!/usr/bin/env python3
"""
Forced-Flow Primitive — Step 1 TDD Red Tests
=============================================

Covers:
  1. OpenInterestProvider — Binance /openInterestHist
  2. FundingRateProvider rewire — Binance /premiumIndex (direct, not via CoinGecko)
  3. Signal primitives in evaluate_asset_policy:
       funding_extreme  : |z(funding, 30d window)| > 3.0
       oi_divergence    : sign(Δprice_24h) ≠ sign(Δoi_24h) AND |Δoi_24h| > 5%
  4. Anti-crowding gate upgrade:
       investigate requires watchlist + at least one forced-flow primitive
  5. counterparty_thesis numeric discipline:
       thesis must contain ≥ 2 numeric tokens — enforced at evaluate_asset_policy level

All tests use static stubs.  Zero network calls.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import council_intel


# ──────────────────────────────────────────────────────────────────────────────
# 1. OpenInterestProvider
# ──────────────────────────────────────────────────────────────────────────────

class OpenInterestProviderTests(unittest.TestCase):
    """OpenInterestProvider must exist and implement the provider contract."""

    def _make_binance_oi_response(self, oi_value: float, timestamp_ms: int) -> list:
        """Binance /fapi/v1/openInterestHist response shape."""
        return [
            {
                "symbol": "BTCUSDT",
                "sumOpenInterest": str(oi_value),
                "sumOpenInterestValue": str(oi_value * 94000),
                "timestamp": timestamp_ms,
            }
        ]

    def test_provider_exists_and_implements_contract(self):
        """OpenInterestProvider must have fetch_symbol_data and describe."""
        self.assertTrue(hasattr(council_intel, "OpenInterestProvider"))
        provider = council_intel.OpenInterestProvider()
        self.assertTrue(callable(getattr(provider, "fetch_symbol_data", None)))
        self.assertTrue(callable(getattr(provider, "describe", None)))

    def test_fetch_returns_oi_dict_with_required_fields(self):
        """fetch_symbol_data('BTC') must return dict with oi_now, oi_24h_ago, delta_pct."""
        provider = council_intel.OpenInterestProvider()

        # Two data points: 24h ago and now
        now_ms = 1_714_100_000_000
        ago_ms = now_ms - 24 * 3600 * 1000

        fake_response = (
            self._make_binance_oi_response(100_000.0, ago_ms)
            + self._make_binance_oi_response(110_000.0, now_ms)
        )

        with mock.patch.object(council_intel, "fetch_json", return_value=fake_response):
            data = provider.fetch_symbol_data("BTC")

        self.assertIsNotNone(data)
        self.assertIn("oi_now", data)
        self.assertIn("oi_24h_ago", data)
        self.assertIn("delta_pct", data)          # (oi_now - oi_24h_ago) / oi_24h_ago
        self.assertAlmostEqual(data["delta_pct"], 0.10, places=4)   # +10%

    def test_fetch_returns_unavailable_string_on_network_error(self):
        """On fetch failure, returns 'oi_unavailable:...' string — never raises."""
        provider = council_intel.OpenInterestProvider()

        with mock.patch.object(council_intel, "fetch_json", side_effect=OSError("timeout")):
            result = provider.fetch_symbol_data("BTC")

        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("oi_unavailable:"))

    def test_fetch_returns_unavailable_when_fewer_than_two_data_points(self):
        """Single data point is insufficient for delta — return unavailable."""
        provider = council_intel.OpenInterestProvider()
        fake_response = self._make_binance_oi_response(100_000.0, 1_714_100_000_000)

        with mock.patch.object(council_intel, "fetch_json", return_value=fake_response):
            result = provider.fetch_symbol_data("BTC")

        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("oi_unavailable:"))

    def test_describe_returns_status_dict(self):
        provider = council_intel.OpenInterestProvider()
        desc = provider.describe()
        self.assertIn("status", desc)


# ──────────────────────────────────────────────────────────────────────────────
# 2. FundingRateProvider — Binance direct rewire
# ──────────────────────────────────────────────────────────────────────────────

class BinanceFundingRewireTests(unittest.TestCase):
    """FundingRateProvider.fetch_symbol_data must prefer Binance /premiumIndex
    over CoinGecko when source_order contains 'binance_direct'."""

    def _make_premium_index_response(self, rate: float) -> dict:
        """Binance GET /fapi/v1/premiumIndex shape."""
        return {
            "symbol": "BTCUSDT",
            "markPrice": "94000.00",
            "lastFundingRate": str(rate),
            "nextFundingTime": 1_714_108_800_000,
            "time": 1_714_100_000_000,
        }

    def _make_funding_history_response(self, rates: list) -> list:
        """Binance GET /fapi/v1/fundingRate shape (list of historical records)."""
        return [
            {"symbol": "BTCUSDT", "fundingRate": str(r), "fundingTime": 1_714_000_000_000 + i * 28800_000}
            for i, r in enumerate(rates)
        ]

    def test_binance_direct_source_fetches_premiumIndex(self):
        """With source_order=['binance_direct'], provider calls Binance API."""
        provider = council_intel.FundingRateProvider({
            "enabled": True,
            "source_order": ["binance_direct"],
            "anomaly_threshold": 0.001,   # 0.1%/8h
            "total_deadline": 30,
        })

        current_resp = self._make_premium_index_response(0.00142)
        hist_resp = self._make_funding_history_response([0.0005, 0.0008, 0.0012])

        call_responses = [current_resp, hist_resp]

        with mock.patch.object(council_intel, "fetch_json", side_effect=call_responses):
            data = provider.fetch_symbol_data("BTC")

        self.assertIsNotNone(data)
        self.assertAlmostEqual(data["current_rate"], 0.00142)
        self.assertIn("history_rates", data)
        self.assertEqual(data["exchange"], "binance")

    def test_funding_extreme_z_score_computed_from_history(self):
        """fetch_symbol_data must include funding_z_score when history ≥ 10 readings."""
        provider = council_intel.FundingRateProvider({
            "enabled": True,
            "source_order": ["binance_direct"],
            "anomaly_threshold": 0.001,
            "total_deadline": 30,
        })

        # z = (0.00142 - mean) / std where mean≈0.0001, std≈0.0001 → z >> 3
        normal_rates = [0.0001] * 90
        current_resp = self._make_premium_index_response(0.00142)
        hist_resp = self._make_funding_history_response(normal_rates)

        with mock.patch.object(council_intel, "fetch_json", side_effect=[current_resp, hist_resp]):
            data = provider.fetch_symbol_data("BTC")

        self.assertIsNotNone(data)
        self.assertIn("funding_z_score", data)
        self.assertGreater(data["funding_z_score"], 3.0)   # extreme positive

    def test_funding_z_score_absent_when_history_insufficient(self):
        """funding_z_score must be None (or absent) when fewer than 10 history points."""
        provider = council_intel.FundingRateProvider({
            "enabled": True,
            "source_order": ["binance_direct"],
            "total_deadline": 30,
        })

        current_resp = self._make_premium_index_response(0.0003)
        hist_resp = self._make_funding_history_response([0.0001, 0.0002])  # only 2 pts

        with mock.patch.object(council_intel, "fetch_json", side_effect=[current_resp, hist_resp]):
            data = provider.fetch_symbol_data("BTC")

        # Either key absent or explicitly None
        z = (data or {}).get("funding_z_score")
        self.assertIsNone(z)

    def test_binance_direct_falls_through_to_coingecko_on_error(self):
        """If binance_direct fails, provider falls through to next source in order."""
        provider = council_intel.FundingRateProvider({
            "enabled": True,
            "source_order": ["binance_direct", "coingecko_derivatives"],
            "total_deadline": 30,
        })

        coingecko_resp = [{
            "market": "Binance (Futures)",
            "symbol": "BTCUSDT",
            "index_id": "BTC",
            "contract_type": "perpetual",
            "funding_rate": "0.00042",
        }]

        # First call (binance_direct) fails, second (coingecko) succeeds
        with mock.patch.object(
            council_intel, "fetch_json",
            side_effect=[OSError("network"), coingecko_resp]
        ):
            data = provider.fetch_symbol_data("BTC")

        self.assertIsNotNone(data)
        self.assertAlmostEqual(data["current_rate"], 0.00042)


# ──────────────────────────────────────────────────────────────────────────────
# 3. Signal primitives in evaluate_asset_policy
# ──────────────────────────────────────────────────────────────────────────────

class ForcedFlowPrimitiveSignalTests(unittest.TestCase):
    """funding_extreme and oi_divergence primitives gate the 'investigate' policy."""

    def _base_funding(self, z_score: float, is_anomalous: bool = False) -> dict:
        return {
            "current_rate": 0.00142,
            "current_rate_pct": 0.142,
            "previous_rate": 0.0008,
            "history_rates": [0.0001] * 30,
            "is_anomalous": is_anomalous,
            "is_turning": False,
            "funding_z_score": z_score,
        }

    def _base_oi(self, delta_pct: float, price_delta_pct: float = 3.0) -> dict:
        return {
            "oi_now": 110_000.0,
            "oi_24h_ago": 100_000.0,
            "delta_pct": delta_pct,
            "price_delta_pct_24h": price_delta_pct,  # caller sets direction
        }

    def test_funding_extreme_z_above_3_triggers_investigate(self):
        """funding_z_score > 3.0 on a watchlist symbol => investigate."""
        policy = council_intel.evaluate_asset_policy(
            symbol="BTC",
            tags=["watchlist"],
            market_data={"symbol": "BTC", "price": 94000},
            watchlist_symbols={"BTC"},
            funding_data=self._base_funding(z_score=3.4, is_anomalous=True),
            oi_data=None,
        )
        self.assertEqual(policy["recommended_action"], "investigate")
        self.assertIn("funding_extreme", policy.get("forced_flow_primitives", []))

    def test_oi_divergence_triggers_investigate_on_watchlist(self):
        """OI drops -8% while price +3% => divergence => investigate."""
        policy = council_intel.evaluate_asset_policy(
            symbol="BTC",
            tags=["watchlist"],
            market_data={"symbol": "BTC", "price": 94000},
            watchlist_symbols={"BTC"},
            funding_data=None,
            oi_data=self._base_oi(delta_pct=-0.08, price_delta_pct=3.0),
        )
        self.assertEqual(policy["recommended_action"], "investigate")
        self.assertIn("oi_divergence", policy.get("forced_flow_primitives", []))

    def test_watchlist_alone_without_forced_flow_is_watch_not_investigate(self):
        """watchlist + funding turning (old gate) is no longer sufficient for investigate.
        A forced-flow primitive is required."""
        policy = council_intel.evaluate_asset_policy(
            symbol="BTC",
            tags=["watchlist"],
            market_data={"symbol": "BTC", "price": 94000},
            watchlist_symbols={"BTC"},
            funding_data={
                "current_rate": 0.00003,
                "is_anomalous": False,
                "is_turning": True,       # old gate — now insufficient alone
                "funding_z_score": 1.2,   # below 3.0
            },
            oi_data=self._base_oi(delta_pct=0.02, price_delta_pct=1.0),  # |Δoi| < 5%
        )
        # Must NOT auto-promote to investigate without a forced-flow primitive
        self.assertNotEqual(policy["recommended_action"], "investigate")

    def test_both_primitives_together_still_yields_investigate(self):
        """Both funding_extreme AND oi_divergence active => investigate."""
        policy = council_intel.evaluate_asset_policy(
            symbol="BTC",
            tags=["watchlist"],
            market_data={"symbol": "BTC", "price": 94000},
            watchlist_symbols={"BTC"},
            funding_data=self._base_funding(z_score=4.1, is_anomalous=True),
            oi_data=self._base_oi(delta_pct=-0.12, price_delta_pct=5.0),
        )
        self.assertEqual(policy["recommended_action"], "investigate")
        primitives = policy.get("forced_flow_primitives", [])
        self.assertIn("funding_extreme", primitives)
        self.assertIn("oi_divergence", primitives)

    def test_non_watchlist_with_extreme_funding_does_not_investigate(self):
        """Forced-flow primitive alone, without watchlist, should not trigger investigate."""
        policy = council_intel.evaluate_asset_policy(
            symbol="RAND",
            tags=[],
            market_data={"symbol": "RAND", "price": 10},
            watchlist_symbols={"BTC"},  # RAND not in watchlist
            funding_data=self._base_funding(z_score=5.0, is_anomalous=True),
            oi_data=None,
        )
        self.assertNotEqual(policy["recommended_action"], "investigate")

    def test_oi_divergence_requires_5pct_threshold(self):
        """OI delta of only 3% should NOT trigger oi_divergence primitive."""
        policy = council_intel.evaluate_asset_policy(
            symbol="BTC",
            tags=["watchlist"],
            market_data={"symbol": "BTC", "price": 94000},
            watchlist_symbols={"BTC"},
            funding_data=None,
            oi_data=self._base_oi(delta_pct=-0.03, price_delta_pct=2.0),  # 3% < 5%
        )
        self.assertNotIn("oi_divergence", policy.get("forced_flow_primitives", []))


# ──────────────────────────────────────────────────────────────────────────────
# 4. ProviderBundle — OI slot
# ──────────────────────────────────────────────────────────────────────────────

class ProviderBundleOITests(unittest.TestCase):
    """ProviderBundle must accept an 'oi' slot."""

    def test_provider_bundle_accepts_oi_provider(self):
        provider = council_intel.OpenInterestProvider()
        bundle = council_intel.ProviderBundle(
            coingecko=council_intel.StaticCoinGeckoProvider(),
            oi=provider,
        )
        self.assertIs(bundle.oi, provider)

    def test_provider_bundle_oi_defaults_to_none(self):
        bundle = council_intel.ProviderBundle(
            coingecko=council_intel.StaticCoinGeckoProvider(),
        )
        self.assertIsNone(getattr(bundle, "oi", None))


# ──────────────────────────────────────────────────────────────────────────────
# 5. counterparty_thesis numeric discipline
# ──────────────────────────────────────────────────────────────────────────────

class CounterpartyThesisNumericTests(unittest.TestCase):
    """evaluate_asset_policy must tag thesis quality in the returned dict."""

    def _policy_with_thesis(self, thesis: str) -> dict:
        return council_intel.evaluate_asset_policy(
            symbol="BTC",
            tags=["watchlist"],
            market_data={"symbol": "BTC", "price": 94000},
            watchlist_symbols={"BTC"},
            funding_data={
                "current_rate": 0.00142,
                "is_anomalous": True,
                "is_turning": False,
                "funding_z_score": 3.4,
            },
            oi_data=None,
            counterparty_thesis=thesis,
        )

    def test_thesis_with_two_numbers_passes_quality_gate(self):
        """'funding 0.142%/8h, OI +18%' contains 2 numbers → passes."""
        policy = self._policy_with_thesis(
            "Binance perp: funding 0.142%/8h (z=3.4). OI $8.2B +18% in 24h."
        )
        self.assertTrue(policy.get("thesis_numeric_ok", False))

    def test_thesis_with_zero_numbers_fails_quality_gate(self):
        """Pure text thesis with no numbers → thesis_numeric_ok=False."""
        policy = self._policy_with_thesis(
            "Late shorts and range-bound sellers who haven't recognized the trend shift."
        )
        self.assertFalse(policy.get("thesis_numeric_ok", True))

    def test_thesis_with_one_number_fails_quality_gate(self):
        """Only one number is insufficient — need at least 2."""
        policy = self._policy_with_thesis("Funding rate is 0.14% which is high.")
        self.assertFalse(policy.get("thesis_numeric_ok", True))

    def test_no_thesis_provided_is_not_penalized(self):
        """When no thesis is provided, thesis_numeric_ok is absent or None — not False."""
        policy = council_intel.evaluate_asset_policy(
            symbol="BTC",
            tags=["watchlist"],
            market_data={"symbol": "BTC", "price": 94000},
            watchlist_symbols={"BTC"},
            funding_data={
                "current_rate": 0.00142,
                "is_anomalous": True,
                "is_turning": False,
                "funding_z_score": 3.4,
            },
            oi_data=None,
        )
        # thesis_numeric_ok should be absent or None, not False
        self.assertIsNone(policy.get("thesis_numeric_ok"))

    def test_thesis_with_bare_integers_fails_tight_quality_gate(self):
        """B2 Tight: bare integers ('45 of 60 cycles took 12 hours') do NOT count as quantitative.
        Tokens must carry %, $, decimal point, or M/B/K unit — otherwise the thesis is
        narrative dressed up as data, exactly the failure mode this gate exists to catch.
        """
        policy = self._policy_with_thesis(
            "There are 45 of 60 cycles taking 12 hours per round."
        )
        self.assertFalse(policy.get("thesis_numeric_ok", True))


if __name__ == "__main__":
    unittest.main()
