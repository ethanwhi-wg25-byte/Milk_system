#!/usr/bin/env python3
"""
Forced-Flow Primitive — TDD Tests (OKX-primary, Malaysia-adapted)
=================================================================

Covers:
  1. OKXFuturesProvider — funding + OI + LS ratio + taker flow
  2. Signal primitives in evaluate_asset_policy:
       funding_extreme, oi_divergence, ls_ratio_extreme,
       funding_flip, taker_flow_imbalance
  3. Anti-crowding gate: investigate requires watchlist + forced-flow
  4. counterparty_thesis numeric discipline
  5. ProviderBundle OKX slot
  6. DeribitOptionsProvider — max_pain, put_call_ratio, gamma_cluster
  7. BitgetFuturesProvider — funding + OI (cross-validation source)
  8. Phase 2 primitives: options_gamma_cluster, cross_exchange_confirm
  9. ProviderBundle Phase 2 slots: bitget_futures, deribit_options

All tests use static stubs. Zero network calls.
"""
import datetime as dt
import re
import unittest
from unittest import mock

import council_intel


# ──────────────────────────────────────────────────────────────
# Helpers — mock OKX API responses
# ──────────────────────────────────────────────────────────────

def _okx_funding_rate_response(rate: float) -> dict:
    """OKX GET /api/v5/public/funding-rate response."""
    return {"code": "0", "data": [{
        "instId": "BTC-USDT-SWAP",
        "fundingRate": str(rate),
        "nextFundingRate": str(rate * 0.9),
        "fundingTime": "1714100000000",
    }]}


def _okx_funding_history_response(rates: list) -> dict:
    """OKX GET /api/v5/public/funding-rate-history response."""
    return {"code": "0", "data": [
        {"instId": "BTC-USDT-SWAP", "fundingRate": str(r),
         "realizedRate": str(r), "fundingTime": str(1714000000000 + i * 28800000)}
        for i, r in enumerate(rates)
    ]}


def _okx_oi_response(oi_value: float) -> dict:
    """OKX GET /api/v5/public/open-interest response."""
    return {"code": "0", "data": [{
        "instId": "BTC-USDT-SWAP",
        "oi": str(oi_value / 94000),  # contracts
        "oiCcy": str(oi_value / 94000),
        "ts": "1714100000000",
    }]}


def _okx_ls_ratio_response(ratio: float) -> dict:
    """OKX GET /api/v5/rubik/stat/contracts/long-short-account-ratio."""
    return {"code": "0", "data": [
        [str(1714100000000), str(ratio)]
    ]}


def _okx_taker_volume_response(buy_vol: float, sell_vol: float) -> dict:
    """OKX GET /api/v5/rubik/stat/contracts/taker-volume."""
    return {"code": "0", "data": [
        [str(1714100000000), str(buy_vol), str(sell_vol)]
    ]}


# ──────────────────────────────────────────────────────────────
# 1. OKXFuturesProvider
# ──────────────────────────────────────────────────────────────

class OKXFuturesProviderTests(unittest.TestCase):
    """OKXFuturesProvider contract tests."""

    def test_provider_exists_and_implements_contract(self):
        self.assertTrue(hasattr(council_intel, "OKXFuturesProvider"))
        provider = council_intel.OKXFuturesProvider()
        self.assertTrue(callable(getattr(provider, "fetch_symbol_data", None)))
        self.assertTrue(callable(getattr(provider, "describe", None)))

    def test_fetch_returns_funding_fields(self):
        provider = council_intel.OKXFuturesProvider()
        normal_rates = [0.0001, 0.00011, 0.00009] * 30
        responses = [
            _okx_funding_rate_response(0.00142),
            _okx_funding_history_response(normal_rates),
            _okx_oi_response(8_200_000_000),
            _okx_ls_ratio_response(1.5),
            _okx_ls_ratio_response(0.8),  # contract ratio
            _okx_taker_volume_response(500, 300),
        ]
        with mock.patch.object(council_intel, "fetch_json", side_effect=responses):
            data = provider.fetch_symbol_data("BTC")

        self.assertIsNotNone(data)
        self.assertIsInstance(data, dict)
        self.assertIn("funding_rate", data)
        self.assertAlmostEqual(data["funding_rate"], 0.00142)
        self.assertIn("funding_z_score", data)
        self.assertGreater(data["funding_z_score"], 3.0)

    def test_fetch_returns_oi_fields(self):
        provider = council_intel.OKXFuturesProvider()
        responses = [
            _okx_funding_rate_response(0.0001),
            _okx_funding_history_response([0.0001] * 10),
            _okx_oi_response(8_200_000_000),
            _okx_ls_ratio_response(1.2),
            _okx_ls_ratio_response(1.1),
            _okx_taker_volume_response(400, 400),
        ]
        with mock.patch.object(council_intel, "fetch_json", side_effect=responses):
            data = provider.fetch_symbol_data("BTC")

        self.assertIn("oi_value", data)
        self.assertGreater(data["oi_value"], 0)

    def test_fetch_returns_ls_ratio_fields(self):
        provider = council_intel.OKXFuturesProvider()
        responses = [
            _okx_funding_rate_response(0.0001),
            _okx_funding_history_response([0.0001] * 10),
            _okx_oi_response(8_000_000_000),
            _okx_ls_ratio_response(2.5),
            _okx_ls_ratio_response(0.6),
            _okx_taker_volume_response(400, 400),
        ]
        with mock.patch.object(council_intel, "fetch_json", side_effect=responses):
            data = provider.fetch_symbol_data("BTC")

        self.assertIn("ls_ratio_account", data)
        self.assertAlmostEqual(data["ls_ratio_account"], 2.5)
        self.assertIn("ls_ratio_contract", data)
        self.assertAlmostEqual(data["ls_ratio_contract"], 0.6)

    def test_fetch_returns_taker_flow_fields(self):
        provider = council_intel.OKXFuturesProvider()
        responses = [
            _okx_funding_rate_response(0.0001),
            _okx_funding_history_response([0.0001] * 10),
            _okx_oi_response(8_000_000_000),
            _okx_ls_ratio_response(1.2),
            _okx_ls_ratio_response(1.1),
            _okx_taker_volume_response(300, 700),  # 70% sell
        ]
        with mock.patch.object(council_intel, "fetch_json", side_effect=responses):
            data = provider.fetch_symbol_data("BTC")

        self.assertIn("taker_buy_ratio", data)
        self.assertAlmostEqual(data["taker_buy_ratio"], 0.3, places=1)

    def test_fetch_returns_unavailable_on_error(self):
        provider = council_intel.OKXFuturesProvider()
        with mock.patch.object(council_intel, "fetch_json", side_effect=OSError("timeout")):
            result = provider.fetch_symbol_data("BTC")
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("okx_unavailable:"))

    def test_z_score_none_when_insufficient_history(self):
        provider = council_intel.OKXFuturesProvider()
        responses = [
            _okx_funding_rate_response(0.0003),
            _okx_funding_history_response([0.0001, 0.0002]),  # only 2
            _okx_oi_response(8_000_000_000),
            _okx_ls_ratio_response(1.2),
            _okx_ls_ratio_response(1.1),
            _okx_taker_volume_response(400, 400),
        ]
        with mock.patch.object(council_intel, "fetch_json", side_effect=responses):
            data = provider.fetch_symbol_data("BTC")
        self.assertIsNone(data.get("funding_z_score"))

    def test_describe_returns_status(self):
        provider = council_intel.OKXFuturesProvider()
        desc = provider.describe()
        self.assertIn("status", desc)


# ──────────────────────────────────────────────────────────────
# 2. Signal primitives in evaluate_asset_policy
# ──────────────────────────────────────────────────────────────

class ForcedFlowPrimitiveSignalTests(unittest.TestCase):

    def _funding(self, z_score=0.5, rate=0.0001, rate_24h_ago=None):
        return {
            "funding_rate": rate,
            "funding_z_score": z_score,
            "funding_annualized": rate * 3 * 365 * 100,
            "is_anomalous": abs(z_score) > 3.0,
            "is_turning": False,
            "rate_24h_ago": rate_24h_ago,
            "rate_1sigma": 0.0001,
        }

    def _oi(self, delta_pct=0.0):
        return {"oi_value": 8_200_000_000, "oi_delta_pct_24h": delta_pct}

    def _ls(self, account=1.2, contract=1.1):
        return {"ls_ratio_account": account, "ls_ratio_contract": contract}

    def _taker(self, buy_ratio=0.5):
        return {"taker_buy_ratio": buy_ratio}

    def test_funding_extreme_triggers_investigate(self):
        policy = council_intel.evaluate_asset_policy(
            symbol="BTC", tags=["watchlist"],
            market_data={"symbol": "BTC", "price": 94000},
            watchlist_symbols={"BTC"},
            funding_data=self._funding(z_score=3.4),
            oi_data=self._oi(), ls_data=self._ls(), taker_data=self._taker(),
        )
        self.assertEqual(policy["recommended_action"], "investigate")
        self.assertIn("funding_extreme", policy.get("forced_flow_primitives", []))

    def test_oi_divergence_triggers_investigate(self):
        policy = council_intel.evaluate_asset_policy(
            symbol="BTC", tags=["watchlist"],
            market_data={"symbol": "BTC", "price": 94000, "change_24h_pct": 5.0},
            watchlist_symbols={"BTC"},
            funding_data=self._funding(),
            oi_data=self._oi(delta_pct=-0.08),  # price up, OI down
            ls_data=self._ls(), taker_data=self._taker(),
        )
        self.assertEqual(policy["recommended_action"], "investigate")
        self.assertIn("oi_divergence", policy.get("forced_flow_primitives", []))

    def test_ls_extreme_triggers_investigate(self):
        policy = council_intel.evaluate_asset_policy(
            symbol="BTC", tags=["watchlist"],
            market_data={"symbol": "BTC", "price": 94000},
            watchlist_symbols={"BTC"},
            funding_data=self._funding(),
            oi_data=self._oi(),
            ls_data=self._ls(account=2.5, contract=0.6),  # retail long, pros short
            taker_data=self._taker(),
        )
        self.assertEqual(policy["recommended_action"], "investigate")
        self.assertIn("ls_ratio_extreme", policy.get("forced_flow_primitives", []))

    def test_funding_flip_triggers(self):
        policy = council_intel.evaluate_asset_policy(
            symbol="BTC", tags=["watchlist"],
            market_data={"symbol": "BTC", "price": 94000},
            watchlist_symbols={"BTC"},
            funding_data=self._funding(z_score=1.5, rate=0.0003, rate_24h_ago=-0.0002),
            oi_data=self._oi(), ls_data=self._ls(), taker_data=self._taker(),
        )
        self.assertIn("funding_flip", policy.get("forced_flow_primitives", []))

    def test_taker_flow_imbalance_triggers(self):
        policy = council_intel.evaluate_asset_policy(
            symbol="BTC", tags=["watchlist"],
            market_data={"symbol": "BTC", "price": 94000},
            watchlist_symbols={"BTC"},
            funding_data=self._funding(),
            oi_data=self._oi(), ls_data=self._ls(),
            taker_data=self._taker(buy_ratio=0.25),  # 75% sell
        )
        self.assertIn("taker_flow_imbalance", policy.get("forced_flow_primitives", []))

    def test_no_forced_flow_no_investigate(self):
        """Watchlist + funding_turning alone is no longer sufficient."""
        policy = council_intel.evaluate_asset_policy(
            symbol="BTC", tags=["watchlist"],
            market_data={"symbol": "BTC", "price": 94000},
            watchlist_symbols={"BTC"},
            funding_data=self._funding(z_score=1.2),  # below 3.0
            oi_data=self._oi(delta_pct=0.02),  # <5%
            ls_data=self._ls(account=1.3, contract=1.1),  # not extreme
            taker_data=self._taker(buy_ratio=0.48),  # balanced
        )
        self.assertNotEqual(policy["recommended_action"], "investigate")

    def test_non_watchlist_does_not_investigate(self):
        policy = council_intel.evaluate_asset_policy(
            symbol="RAND", tags=[],
            market_data={"symbol": "RAND", "price": 10},
            watchlist_symbols={"BTC"},
            funding_data=self._funding(z_score=5.0),
            oi_data=self._oi(), ls_data=self._ls(), taker_data=self._taker(),
        )
        self.assertNotEqual(policy["recommended_action"], "investigate")

    def test_multiple_primitives_detected(self):
        policy = council_intel.evaluate_asset_policy(
            symbol="BTC", tags=["watchlist"],
            market_data={"symbol": "BTC", "price": 94000, "change_24h_pct": 8.0},
            watchlist_symbols={"BTC"},
            funding_data=self._funding(z_score=4.1, rate=0.0015, rate_24h_ago=-0.0003),
            oi_data=self._oi(delta_pct=-0.12),
            ls_data=self._ls(account=2.8, contract=0.5),
            taker_data=self._taker(buy_ratio=0.2),
        )
        primitives = policy.get("forced_flow_primitives", [])
        self.assertIn("funding_extreme", primitives)
        self.assertIn("oi_divergence", primitives)
        self.assertIn("ls_ratio_extreme", primitives)
        self.assertIn("taker_flow_imbalance", primitives)
        self.assertIn("funding_flip", primitives)


# ──────────────────────────────────────────────────────────────
# 3. ProviderBundle OKX slot
# ──────────────────────────────────────────────────────────────

class ProviderBundleOKXTests(unittest.TestCase):

    def test_bundle_accepts_okx_provider(self):
        provider = council_intel.OKXFuturesProvider()
        bundle = council_intel.ProviderBundle(
            coingecko=council_intel.StaticCoinGeckoProvider(),
            okx_futures=provider,
        )
        self.assertIs(bundle.okx_futures, provider)

    def test_bundle_okx_defaults_none(self):
        bundle = council_intel.ProviderBundle(
            coingecko=council_intel.StaticCoinGeckoProvider(),
        )
        self.assertIsNone(getattr(bundle, "okx_futures", None))


# ──────────────────────────────────────────────────────────────
# 4. counterparty_thesis numeric discipline
# ──────────────────────────────────────────────────────────────

class CounterpartyThesisTests(unittest.TestCase):

    def _policy_with_thesis(self, thesis):
        return council_intel.evaluate_asset_policy(
            symbol="BTC", tags=["watchlist"],
            market_data={"symbol": "BTC", "price": 94000},
            watchlist_symbols={"BTC"},
            funding_data={
                "funding_rate": 0.00142, "funding_z_score": 3.4,
                "is_anomalous": True, "is_turning": False,
                "rate_24h_ago": None, "rate_1sigma": 0.0001,
            },
            counterparty_thesis=thesis,
        )

    def test_thesis_with_two_numbers_passes(self):
        policy = self._policy_with_thesis(
            "OKX perp: funding 0.142%/8h (z=3.4). OI $8.2B +18%."
        )
        self.assertTrue(policy.get("thesis_numeric_ok", False))

    def test_thesis_with_zero_numbers_fails(self):
        policy = self._policy_with_thesis(
            "Late shorts who haven't recognized the trend shift."
        )
        self.assertFalse(policy.get("thesis_numeric_ok", True))

    def test_thesis_with_one_number_fails(self):
        policy = self._policy_with_thesis("Funding rate is high at 0.14% level.")
        self.assertFalse(policy.get("thesis_numeric_ok", True))

    def test_no_thesis_not_penalized(self):
        policy = council_intel.evaluate_asset_policy(
            symbol="BTC", tags=["watchlist"],
            market_data={"symbol": "BTC", "price": 94000},
            watchlist_symbols={"BTC"},
            funding_data={
                "funding_rate": 0.00142, "funding_z_score": 3.4,
                "is_anomalous": True, "is_turning": False,
            },
        )
        self.assertIsNone(policy.get("thesis_numeric_ok"))


if __name__ == "__main__":
    unittest.main()


# ──────────────────────────────────────────────────────────────
# 5. DeribitOptionsProvider
# ──────────────────────────────────────────────────────────────

def _deribit_book_summary(instruments: list) -> dict:
    """Deribit /api/v2/public/get_book_summary_by_currency response shape."""
    return {"jsonrpc": "2.0", "id": 1, "result": instruments}


def _deribit_instrument(name: str, oi: float, underlying: float = 94000.0) -> dict:
    return {
        "instrument_name": name,
        "open_interest": oi,
        "underlying_price": underlying,
        "mark_price": 0.05,
    }


def _expiry_str(days_from_now: int) -> str:
    """Return a Deribit-formatted expiry string like '26APR26' for a date N days from today."""
    d = dt.date.today() + dt.timedelta(days=days_from_now)
    return d.strftime("%d%b%y").upper()


class DeribitOptionsProviderTests(unittest.TestCase):

    def test_provider_exists_and_implements_contract(self):
        self.assertTrue(hasattr(council_intel, "DeribitOptionsProvider"))
        provider = council_intel.DeribitOptionsProvider()
        self.assertTrue(callable(getattr(provider, "fetch_symbol_data", None)))
        self.assertTrue(callable(getattr(provider, "describe", None)))

    def test_fetch_returns_required_fields(self):
        provider = council_intel.DeribitOptionsProvider()
        exp = _expiry_str(5)
        instruments = [
            _deribit_instrument(f"BTC-{exp}-94000-C", 800),
            _deribit_instrument(f"BTC-{exp}-93000-P", 300),
        ]
        with mock.patch.object(council_intel, "fetch_json",
                               return_value=_deribit_book_summary(instruments)):
            data = provider.fetch_symbol_data("BTC")

        self.assertIsNotNone(data)
        self.assertIsInstance(data, dict)
        self.assertIn("max_pain", data)
        self.assertIn("put_call_ratio", data)
        self.assertIn("gamma_cluster", data)

    def test_gamma_cluster_detected_near_atm_expiry_lt_7d(self):
        """OI concentrated at strike ±2% of spot, expiry < 7d => gamma_cluster=True."""
        provider = council_intel.DeribitOptionsProvider()
        exp = _expiry_str(3)
        instruments = [
            _deribit_instrument(f"BTC-{exp}-94000-C", 5000),  # ±0%, 3d
            _deribit_instrument(f"BTC-{exp}-94000-P", 4000),
            _deribit_instrument(f"BTC-{_expiry_str(30)}-80000-C", 100),  # far OTM, long
        ]
        with mock.patch.object(council_intel, "fetch_json",
                               return_value=_deribit_book_summary(instruments)):
            data = provider.fetch_symbol_data("BTC")

        self.assertTrue(data.get("gamma_cluster"))

    def test_no_gamma_cluster_when_expiry_gte_7d(self):
        """Near-ATM OI but expiry >= 7d: gamma_cluster=False."""
        provider = council_intel.DeribitOptionsProvider()
        exp = _expiry_str(14)
        instruments = [
            _deribit_instrument(f"BTC-{exp}-94000-C", 5000),
            _deribit_instrument(f"BTC-{exp}-94000-P", 4000),
        ]
        with mock.patch.object(council_intel, "fetch_json",
                               return_value=_deribit_book_summary(instruments)):
            data = provider.fetch_symbol_data("BTC")

        self.assertFalse(data.get("gamma_cluster"))

    def test_no_gamma_cluster_when_oi_far_from_spot(self):
        """OI concentrated far outside ±2% of spot: gamma_cluster=False."""
        provider = council_intel.DeribitOptionsProvider()
        exp = _expiry_str(3)
        instruments = [
            _deribit_instrument(f"BTC-{exp}-80000-C", 5000),  # ~15% below spot
            _deribit_instrument(f"BTC-{exp}-80000-P", 4000),
        ]
        with mock.patch.object(council_intel, "fetch_json",
                               return_value=_deribit_book_summary(instruments)):
            data = provider.fetch_symbol_data("BTC")

        self.assertFalse(data.get("gamma_cluster"))

    def test_fetch_returns_unavailable_on_error(self):
        provider = council_intel.DeribitOptionsProvider()
        with mock.patch.object(council_intel, "fetch_json", side_effect=OSError("timeout")):
            result = provider.fetch_symbol_data("BTC")
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("deribit_unavailable:"))

    def test_describe_returns_status(self):
        provider = council_intel.DeribitOptionsProvider()
        desc = provider.describe()
        self.assertIn("status", desc)


# ──────────────────────────────────────────────────────────────
# 6. BitgetFuturesProvider
# ──────────────────────────────────────────────────────────────

def _bitget_funding_response(rate: float) -> dict:
    """Bitget GET /api/v2/mix/market/current-fund-rate response shape."""
    return {
        "code": "00000",
        "msg": "success",
        "data": {
            "symbol": "BTCUSDT",
            "fundingRate": str(rate),
            "nextFundingTime": "1714108800000",
        },
    }


def _bitget_oi_response(oi: float) -> dict:
    """Bitget GET /api/v2/mix/market/open-interest response shape."""
    return {
        "code": "00000",
        "data": {
            "symbol": "BTCUSDT",
            "openInterestList": [{"size": str(oi)}],
        },
    }


class BitgetFuturesProviderTests(unittest.TestCase):

    def test_provider_exists_and_implements_contract(self):
        self.assertTrue(hasattr(council_intel, "BitgetFuturesProvider"))
        provider = council_intel.BitgetFuturesProvider()
        self.assertTrue(callable(getattr(provider, "fetch_symbol_data", None)))
        self.assertTrue(callable(getattr(provider, "describe", None)))

    def test_fetch_returns_funding_rate(self):
        provider = council_intel.BitgetFuturesProvider()
        with mock.patch.object(council_intel, "fetch_json",
                               side_effect=[_bitget_funding_response(0.00085),
                                            _bitget_oi_response(8_000_000)]):
            data = provider.fetch_symbol_data("BTC")

        self.assertIsNotNone(data)
        self.assertIsInstance(data, dict)
        self.assertIn("funding_rate", data)
        self.assertAlmostEqual(data["funding_rate"], 0.00085)

    def test_fetch_returns_oi(self):
        provider = council_intel.BitgetFuturesProvider()
        with mock.patch.object(council_intel, "fetch_json",
                               side_effect=[_bitget_funding_response(0.0001),
                                            _bitget_oi_response(8_500_000)]):
            data = provider.fetch_symbol_data("BTC")

        self.assertIn("oi_value", data)
        self.assertAlmostEqual(data["oi_value"], 8_500_000)

    def test_fetch_returns_unavailable_on_error(self):
        provider = council_intel.BitgetFuturesProvider()
        with mock.patch.object(council_intel, "fetch_json", side_effect=OSError("timeout")):
            result = provider.fetch_symbol_data("BTC")
        self.assertIsInstance(result, str)
        self.assertTrue(result.startswith("bitget_unavailable:"))

    def test_describe_returns_status(self):
        provider = council_intel.BitgetFuturesProvider()
        desc = provider.describe()
        self.assertIn("status", desc)


# ──────────────────────────────────────────────────────────────
# 7. ProviderBundle Phase 2 slots
# ──────────────────────────────────────────────────────────────

class ProviderBundlePhase2Tests(unittest.TestCase):

    def test_bundle_accepts_bitget_provider(self):
        provider = council_intel.BitgetFuturesProvider()
        bundle = council_intel.ProviderBundle(
            coingecko=council_intel.StaticCoinGeckoProvider(),
            bitget_futures=provider,
        )
        self.assertIs(bundle.bitget_futures, provider)

    def test_bundle_accepts_deribit_provider(self):
        provider = council_intel.DeribitOptionsProvider()
        bundle = council_intel.ProviderBundle(
            coingecko=council_intel.StaticCoinGeckoProvider(),
            deribit_options=provider,
        )
        self.assertIs(bundle.deribit_options, provider)

    def test_bundle_phase2_fields_default_to_none(self):
        bundle = council_intel.ProviderBundle(
            coingecko=council_intel.StaticCoinGeckoProvider(),
        )
        self.assertIsNone(getattr(bundle, "bitget_futures", None))
        self.assertIsNone(getattr(bundle, "deribit_options", None))


# ──────────────────────────────────────────────────────────────
# 8. Phase 2 primitives in evaluate_asset_policy
# ──────────────────────────────────────────────────────────────

class Phase2PrimitiveSignalTests(unittest.TestCase):

    def _base_funding(self, z_score=0.5, rate=0.0001):
        return {
            "funding_rate": rate,
            "funding_z_score": z_score,
            "is_anomalous": abs(z_score) > 3.0,
            "is_turning": False,
            "rate_24h_ago": None,
            "rate_1sigma": 0.0001,
        }

    def test_options_gamma_cluster_triggers_investigate(self):
        """deribit_data gamma_cluster=True + watchlist => investigate."""
        policy = council_intel.evaluate_asset_policy(
            symbol="BTC", tags=["watchlist"],
            market_data={"symbol": "BTC", "price": 94000},
            watchlist_symbols={"BTC"},
            funding_data=self._base_funding(),
            deribit_data={
                "gamma_cluster": True,
                "gamma_strike": 94000,
                "gamma_expiry_days": 3,
                "max_pain": 93000,
                "put_call_ratio": 0.8,
            },
        )
        self.assertEqual(policy["recommended_action"], "investigate")
        self.assertIn("options_gamma_cluster", policy.get("forced_flow_primitives", []))

    def test_gamma_cluster_false_does_not_trigger(self):
        """gamma_cluster=False produces no options_gamma_cluster primitive."""
        policy = council_intel.evaluate_asset_policy(
            symbol="BTC", tags=["watchlist"],
            market_data={"symbol": "BTC", "price": 94000},
            watchlist_symbols={"BTC"},
            funding_data=self._base_funding(),
            deribit_data={"gamma_cluster": False, "max_pain": 90000, "put_call_ratio": 1.2},
        )
        self.assertNotIn("options_gamma_cluster", policy.get("forced_flow_primitives", []))

    def test_cross_exchange_confirm_triggers_when_both_agree(self):
        """OKX extreme positive funding + Bitget positive elevated => cross_exchange_confirm."""
        policy = council_intel.evaluate_asset_policy(
            symbol="BTC", tags=["watchlist"],
            market_data={"symbol": "BTC", "price": 94000},
            watchlist_symbols={"BTC"},
            funding_data=self._base_funding(z_score=3.5, rate=0.0015),
            bitget_data={"funding_rate": 0.0012, "oi_value": 8_000_000},
        )
        self.assertIn("cross_exchange_confirm", policy.get("forced_flow_primitives", []))

    def test_cross_exchange_confirm_not_triggered_without_bitget(self):
        """No bitget_data: cross_exchange_confirm must not fire."""
        policy = council_intel.evaluate_asset_policy(
            symbol="BTC", tags=["watchlist"],
            market_data={"symbol": "BTC", "price": 94000},
            watchlist_symbols={"BTC"},
            funding_data=self._base_funding(z_score=3.5, rate=0.0015),
        )
        self.assertNotIn("cross_exchange_confirm", policy.get("forced_flow_primitives", []))

    def test_cross_exchange_confirm_not_triggered_when_signals_disagree(self):
        """OKX positive extreme, Bitget negative: no confirmation."""
        policy = council_intel.evaluate_asset_policy(
            symbol="BTC", tags=["watchlist"],
            market_data={"symbol": "BTC", "price": 94000},
            watchlist_symbols={"BTC"},
            funding_data=self._base_funding(z_score=3.5, rate=0.0015),
            bitget_data={"funding_rate": -0.0008, "oi_value": 8_000_000},
        )
        self.assertNotIn("cross_exchange_confirm", policy.get("forced_flow_primitives", []))
