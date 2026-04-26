import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import council_intel


class MergeUniverseTests(unittest.TestCase):
    def test_merge_universe_preserves_manual_adds_and_unresolved_placeholders(self):
        top_assets = [
            {"symbol": "btc", "name": "Bitcoin"},
            {"symbol": "eth", "name": "Ethereum"},
            {"symbol": "sol", "name": "Solana"},
        ]

        merged = council_intel.merge_universe(
            top_assets=top_assets,
            manual_watchlist=["TAO", "ETH", "XRP"],
            manual_placeholders=["PUMPX"],
        )

        resolved_symbols = [asset["symbol"] for asset in merged["resolved"]]

        self.assertEqual(resolved_symbols.count("ETH"), 1)
        self.assertIn("BTC", resolved_symbols)
        self.assertIn("ETH", resolved_symbols)
        self.assertIn("SOL", resolved_symbols)
        self.assertIn("TAO", resolved_symbols)
        self.assertIn("XRP", resolved_symbols)
        self.assertEqual(
            merged["unresolved"],
            [{"symbol": "PUMPX", "status": "unresolved", "source": "manual_placeholder"}],
        )


class AssetCardTests(unittest.TestCase):
    def test_asset_card_marks_missing_funding_data_as_no_data(self):
        card = council_intel.build_asset_card(
            symbol="TAO",
            market_data={"symbol": "TAO", "name": "Bittensor", "rank": 31},
            watchlist_symbols={"TAO"},
        )

        self.assertEqual(card["symbol"], "TAO")
        self.assertEqual(card["providers"]["funding"]["status"], "no_data")
        self.assertIn("watchlist", card["tags"])

    def test_asset_card_marks_market_only_moves_as_no_trade_with_high_crowding_risk(self):
        card = council_intel.build_asset_card(
            symbol="BTC",
            market_data={
                "symbol": "BTC",
                "name": "Bitcoin",
                "rank": 1,
                "change_24h_pct": 8.1,
            },
            watchlist_symbols=set(),
        )

        self.assertEqual(card["policy"]["recommended_action"], "no_trade")
        self.assertEqual(card["policy"]["crowding_risk"], "high")
        self.assertEqual(card["policy"]["evidence_diversity"], 1)

    def test_asset_card_marks_funding_signal_tag_and_caps_at_watch(self):
        card = council_intel.build_asset_card(
            symbol="BTC",
            market_data={"symbol": "BTC", "name": "Bitcoin", "rank": 1},
            watchlist_symbols={"BTC"},
            funding_data={
                "current_rate": 0.00003,
                "current_rate_pct": 0.003,
                "previous_rate": -0.00011,
                "history_rates": [-0.00012, -0.0001, -0.00009],
                "is_anomalous": True,
                "is_turning": True,
            },
        )

        self.assertIn("funding-signal", card["tags"])
        self.assertNotIn("funding-turning", card["tags"])
        self.assertEqual(card["providers"]["funding"]["status"], "ok")
        # Funding signal alone — even with is_turning — caps at watch.
        # investigate is reserved for forced-flow primitives (z-score extreme, OI divergence).
        self.assertEqual(card["policy"]["recommended_action"], "watch")


class CoinGeckoProviderTests(unittest.TestCase):
    def test_resolve_coin_id_returns_none_for_ambiguous_symbol_matches(self):
        provider = council_intel.CoinGeckoProvider()

        with mock.patch.object(
            council_intel,
            "fetch_json",
            return_value={
                "coins": [
                    {"id": "token-a", "symbol": "tao"},
                    {"id": "token-b", "symbol": "TAO"},
                ]
            },
        ):
            resolved = provider._resolve_coin_id("TAO")

        self.assertIsNone(resolved)


class FundingRateProviderTests(unittest.TestCase):
    def test_provider_reads_coingecko_derivatives_and_prefers_binance_market(self):
        provider = council_intel.FundingRateProvider(
            {
                "enabled": True,
                "anomaly_threshold": 0.00005,
                "turning_delta": 0.00001,
            }
        )

        with mock.patch.object(
            council_intel,
            "fetch_json",
            return_value=[
                {
                    "market": "Bitget (Futures)",
                    "symbol": "BTCUSD_PERP",
                    "index_id": "BTC",
                    "contract_type": "perpetual",
                    "funding_rate": "0.00003",
                },
                {
                    "market": "Binance (Futures)",
                    "symbol": "BTCUSDT",
                    "index_id": "BTC",
                    "contract_type": "perpetual",
                    "funding_rate": "-0.00008",
                    "open_interest": 7411494848.95,
                    "volume_24h": 7993829793.081,
                },
            ],
        ):
            data = provider.fetch_symbol_data("BTC")

        self.assertEqual(data["exchange"], "binance")
        self.assertEqual(data["symbol"], "BTCUSDT")
        self.assertAlmostEqual(data["current_rate"], -0.00008)
        self.assertIsNone(data["previous_rate"])
        self.assertTrue(data["is_anomalous"])
        self.assertFalse(data["is_turning"])
        self.assertEqual(provider.describe()["last_source"], "coingecko_derivatives")

    def test_provider_uses_previous_cycle_rate_to_classify_turning_signal(self):
        provider = council_intel.FundingRateProvider(
            {
                "enabled": True,
                "anomaly_threshold": 0.00005,
                "turning_delta": 0.00001,
            }
        )

        responses = [
            [
                {
                    "market": "Binance (Futures)",
                    "symbol": "BTCUSDT",
                    "index_id": "BTC",
                    "contract_type": "perpetual",
                    "funding_rate": "-0.00012",
                }
            ],
            [
                {
                    "market": "Binance (Futures)",
                    "symbol": "BTCUSDT",
                    "index_id": "BTC",
                    "contract_type": "perpetual",
                    "funding_rate": "0.00003",
                }
            ],
        ]
        with mock.patch.object(council_intel, "fetch_json", side_effect=responses):
            first = provider.fetch_symbol_data("BTC")
            second = provider.fetch_symbol_data("BTC")

        self.assertAlmostEqual(first["current_rate"], -0.00012)
        self.assertIsNone(first["previous_rate"])
        self.assertEqual(second["symbol"], "BTCUSDT")
        self.assertAlmostEqual(second["current_rate"], 0.00003)
        self.assertAlmostEqual(second["previous_rate"], -0.00012)
        self.assertTrue(second["is_anomalous"])
        self.assertTrue(second["is_turning"])

    def test_provider_stops_when_total_deadline_is_exceeded(self):
        provider = council_intel.FundingRateProvider(
            {
                "enabled": True,
                "request_timeout": 5,
                "total_deadline": 0.01,
            }
        )

        observed_urls = []

        def fake_fetch_json(url, timeout=20, method="GET", headers=None, data=None):
            _ = (timeout, method, headers, data)
            observed_urls.append(url)
            time.sleep(0.02)
            return []

        with mock.patch.object(council_intel, "fetch_json", side_effect=fake_fetch_json):
            data = provider.fetch_symbol_data("BTC")

        self.assertIsNone(data)
        self.assertEqual(len(observed_urls), 1)
        self.assertIn("funding_unavailable", provider.describe()["error"])


class ArtifactTests(unittest.TestCase):
    def test_write_report_artifacts_creates_json_and_markdown_with_expected_shape(self):
        report = {
            "run": {"started_at": "2026-03-23T12:00:00Z"},
            "providers": {"coingecko": {"status": "ok"}},
            "universe": {
                "resolved": [{"symbol": "BTC"}],
                "unresolved": [{"symbol": "PUMPX", "status": "unresolved"}],
            },
            "assets": [
                {
                    "symbol": "BTC",
                    "tags": ["market-move", "funding-signal"],
                    "providers": {"funding": {"status": "ok"}},
                }
            ],
            "summary": {
                "highest_interest": ["BTC"],
                "funding_signal_assets": ["BTC"],
            },
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            paths = council_intel.write_report_artifacts(report, Path(tmp_dir))

            json_path = Path(paths["json"])
            markdown_path = Path(paths["markdown"])

            self.assertTrue(json_path.exists())
            self.assertTrue(markdown_path.exists())

            saved_report = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(
                set(saved_report.keys()),
                {"run", "providers", "universe", "assets", "summary"},
            )

            markdown = markdown_path.read_text(encoding="utf-8")
            self.assertIn("## Highest-Interest Assets", markdown)
            self.assertIn("## Funding Signal Assets", markdown)
            self.assertIn("## Watchlist Assets", markdown)
            self.assertIn("## Unresolved And Future Assets", markdown)
            self.assertIn("## Provider Freshness", markdown)
            self.assertIn("PUMPX", markdown)


class CliRunTests(unittest.TestCase):
    def test_run_command_builds_report_from_provider_bundle(self):
        config = {
            "top_n": 2,
            "manual_watchlist": ["TAO"],
            "manual_placeholders": ["PUMPX"],
            "providers": {
                "coingecko": {"enabled": True},
            },
        }

        provider_bundle = council_intel.ProviderBundle(
            coingecko=council_intel.StaticCoinGeckoProvider(
                top_assets=[
                    {"symbol": "btc", "name": "Bitcoin"},
                    {"symbol": "eth", "name": "Ethereum"},
                ],
                asset_snapshots={
                    "BTC": {"symbol": "BTC", "name": "Bitcoin", "rank": 1},
                    "ETH": {"symbol": "ETH", "name": "Ethereum", "rank": 2},
                    "TAO": {"symbol": "TAO", "name": "Bittensor", "rank": 31},
                },
            ),
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            report = council_intel.run_intel_cycle(
                config=config,
                out_dir=Path(tmp_dir),
                provider_bundle=provider_bundle,
            )

            self.assertEqual(report["run"]["mode"], "read_only_intel")
            self.assertEqual(report["summary"]["unresolved_count"], 1)
            self.assertEqual(report["summary"]["asset_count"], 3)
            self.assertTrue((Path(tmp_dir) / "report.json").exists())
            self.assertTrue((Path(tmp_dir) / "report.md").exists())

    def test_unresolvable_manual_watchlist_symbol_is_moved_to_unresolved(self):
        config = {
            "top_n": 1,
            "manual_watchlist": ["TAO", "FUTUREX"],
            "manual_placeholders": [],
            "providers": {
                "coingecko": {"enabled": True},
            },
        }

        provider_bundle = council_intel.ProviderBundle(
            coingecko=council_intel.StaticCoinGeckoProvider(
                top_assets=[{"symbol": "btc", "name": "Bitcoin"}],
                asset_snapshots={
                    "BTC": {"symbol": "BTC", "name": "Bitcoin", "rank": 1},
                    "TAO": {"symbol": "TAO", "name": "Bittensor", "rank": 31},
                },
            ),
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            report = council_intel.run_intel_cycle(
                config=config,
                out_dir=Path(tmp_dir),
                provider_bundle=provider_bundle,
            )

            asset_symbols = [asset["symbol"] for asset in report["assets"]]
            unresolved_symbols = [asset["symbol"] for asset in report["universe"]["unresolved"]]

            self.assertIn("TAO", asset_symbols)
            self.assertNotIn("FUTUREX", asset_symbols)
            self.assertIn("FUTUREX", unresolved_symbols)

    def test_main_run_command_writes_timestamped_report_directory(self):
        config = {
            "top_n": 1,
            "manual_watchlist": [],
            "manual_placeholders": [],
            "providers": {
                "coingecko": {"enabled": True},
            },
        }

        provider_bundle = council_intel.ProviderBundle(
            coingecko=council_intel.StaticCoinGeckoProvider(
                top_assets=[{"symbol": "btc", "name": "Bitcoin"}],
                asset_snapshots={"BTC": {"symbol": "BTC", "name": "Bitcoin", "rank": 1}},
            ),
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")

            with mock.patch.object(council_intel, "build_default_provider_bundle", return_value=provider_bundle):
                with contextlib.redirect_stdout(io.StringIO()):
                    council_intel.main(["run", "--config", str(config_path), "--out-dir", tmp_dir])

            subdirs = [path for path in Path(tmp_dir).iterdir() if path.is_dir()]
            self.assertEqual(len(subdirs), 1)
            self.assertTrue((subdirs[0] / "report.json").exists())
            self.assertTrue((subdirs[0] / "report.md").exists())

    def test_run_cycle_summarizes_investigate_candidates_and_no_trade_assets(self):
        config = {
            "top_n": 1,
            "manual_watchlist": ["TAO"],
            "manual_placeholders": [],
            "providers": {
                "coingecko": {"enabled": True},
            },
        }

        provider_bundle = council_intel.ProviderBundle(
            coingecko=council_intel.StaticCoinGeckoProvider(
                top_assets=[
                    {"symbol": "btc", "name": "Bitcoin", "change_24h_pct": 9.2},
                ],
                asset_snapshots={
                    "TAO": {"symbol": "TAO", "name": "Bittensor", "rank": 31},
                },
            ),
            funding=council_intel.StaticOverlayProvider(
                {
                    "TAO": {
                        "current_rate": 0.00003,
                        "current_rate_pct": 0.003,
                        "previous_rate": -0.00011,
                        "history_rates": [-0.00012, -0.0001],
                        "is_anomalous": True,
                        "is_turning": True,
                    },
                }
            ),
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            report = council_intel.run_intel_cycle(
                config=config,
                out_dir=Path(tmp_dir),
                provider_bundle=provider_bundle,
            )

            # TAO has funding signal (anomalous + watchlist) → caps at watch, surfaces in funding_signal list.
            # investigate is reserved for forced-flow primitives not yet wired.
            self.assertIn("TAO", report["summary"]["funding_signal_assets"])
            self.assertNotIn("TAO", report["summary"]["no_trade_assets"])
            self.assertIn("BTC", report["summary"]["no_trade_assets"])
            self.assertIn("BTC", report["summary"]["crowding_warnings"])

            markdown = (Path(tmp_dir) / "report.md").read_text(encoding="utf-8")
            self.assertIn("## Investigate Candidates", markdown)
            self.assertIn("## No-Trade Assets", markdown)
            self.assertIn("## Crowding Warnings", markdown)

    def test_run_cycle_does_not_refetch_market_snapshots_for_top_assets(self):
        config = {
            "top_n": 1,
            "manual_watchlist": ["TAO"],
            "manual_placeholders": [],
            "providers": {
                "coingecko": {"enabled": True},
            },
        }

        class TrackingCoinGeckoProvider(council_intel.StaticCoinGeckoProvider):
            def __init__(self):
                super().__init__(
                    top_assets=[{"symbol": "btc", "name": "Bitcoin", "source": "coingecko_top"}],
                    asset_snapshots={"TAO": {"symbol": "TAO", "name": "Bittensor", "rank": 31}},
                )
                self.fetch_snapshot_calls = []

            def fetch_asset_snapshot(self, symbol: str):
                normalized = council_intel.normalize_symbol(symbol)
                self.fetch_snapshot_calls.append(normalized)
                return super().fetch_asset_snapshot(normalized)

        tracking_provider = TrackingCoinGeckoProvider()
        provider_bundle = council_intel.ProviderBundle(
            coingecko=tracking_provider,
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            council_intel.run_intel_cycle(
                config=config,
                out_dir=Path(tmp_dir),
                provider_bundle=provider_bundle,
            )

        self.assertEqual(tracking_provider.fetch_snapshot_calls, ["TAO"])

    def test_run_cycle_degrades_gracefully_when_coingecko_top_fetch_fails(self):
        config = {
            "top_n": 20,
            "manual_watchlist": ["TAO", "XRP"],
            "manual_placeholders": ["FUTUREX"],
            "providers": {
                "coingecko": {"enabled": True},
            },
        }

        class FailingCoinGeckoProvider:
            def fetch_top_assets(self, limit):
                raise RuntimeError(f"network unavailable for top {limit}")

            def fetch_asset_snapshot(self, symbol):
                return None

            def describe(self):
                return {"status": "enabled"}

        provider_bundle = council_intel.ProviderBundle(
            coingecko=FailingCoinGeckoProvider(),
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            report = council_intel.run_intel_cycle(
                config=config,
                out_dir=Path(tmp_dir),
                provider_bundle=provider_bundle,
            )

            self.assertEqual(report["summary"]["asset_count"], 0)
            self.assertEqual(report["summary"]["unresolved_count"], 3)
            self.assertEqual(
                report["providers"]["coingecko"]["status"],
                "degraded",
            )
            self.assertIn("TAO", [item["symbol"] for item in report["universe"]["unresolved"]])
            self.assertIn("XRP", [item["symbol"] for item in report["universe"]["unresolved"]])
            self.assertIn("FUTUREX", [item["symbol"] for item in report["universe"]["unresolved"]])
            self.assertTrue((Path(tmp_dir) / "report.json").exists())


class FragilityAuditIntelTests(unittest.TestCase):
    def test_funding_unavailable_changes_policy_and_evidence_diversity(self):
        common_kwargs = {
            "symbol": "BTC",
            "market_data": {"symbol": "BTC", "name": "Bitcoin", "change_24h_pct": 2.0},
            "watchlist_symbols": {"BTC"},
        }
        funding_available = council_intel.build_asset_card(
            funding_data={
                "current_rate": 0.00003,
                "current_rate_pct": 0.003,
                "previous_rate": -0.00011,
                "history_rates": [-0.00012, -0.00010],
                "is_anomalous": True,
                "is_turning": True,
            },
            **common_kwargs,
        )
        funding_unavailable = council_intel.build_asset_card(
            funding_data=None,
            **common_kwargs,
        )

        self.assertNotEqual(
            funding_available["policy"]["recommended_action"],
            funding_unavailable["policy"]["recommended_action"],
        )
        self.assertNotEqual(
            funding_available["policy"]["evidence_diversity"],
            funding_unavailable["policy"]["evidence_diversity"],
        )


if __name__ == "__main__":
    unittest.main()
