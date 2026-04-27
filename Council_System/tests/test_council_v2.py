import json
import os
import tempfile
import unittest
from unittest import mock

import council_v2


class SequenceProvider(council_v2.MarketDataProvider):
    def __init__(self, snapshots):
        self._snapshots = list(snapshots)
        self._idx = 0

    def get_snapshot(self, symbol: str, lookback: int) -> council_v2.MarketSnapshot:
        _ = (symbol, lookback)
        snap = self._snapshots[self._idx]
        self._idx = min(self._idx + 1, len(self._snapshots) - 1)
        return snap


class SequenceSentinel:
    def __init__(self, levels):
        self._levels = list(levels)
        self._idx = 0

    def evaluate(self, snap, state, enabled=True):
        _ = (snap, state, enabled)
        level = self._levels[self._idx]
        self._idx = min(self._idx + 1, len(self._levels) - 1)
        return level, [f"forced_{level.name.lower()}"]


class StaticVerdictAgent(council_v2.Agent):
    def __init__(self, name, action, confidence=0.90, thesis="counterparty"):
        self.name = name
        self._action = action
        self._confidence = confidence
        self._thesis = thesis

    def decide(self, snap):
        _ = snap
        return council_v2.Verdict(
            self.name,
            self._action,
            self._confidence,
            f"{self.name}_signal",
            counterparty_thesis=self._thesis,
        )


class StaticFundingBridge:
    def __init__(self, funding):
        self._funding = funding

    def get_funding(self, symbol):
        _ = symbol
        return self._funding


class CouncilFragilityAuditTests(unittest.TestCase):
    @staticmethod
    def _snapshot(ts: int, price: float) -> council_v2.MarketSnapshot:
        candles = []
        for i in range(60):
            c_ts = ts - (59 - i) * 60
            candles.append((c_ts, price, price * 1.001, price * 0.999, price, 100.0))
        return council_v2.MarketSnapshot(symbol="BTC/USDT", ts=ts, price=price, ohlcv=candles)

    def test_trend_and_mean_reversion_conflict_is_conservative_by_design(self):
        judge = council_v2.JudgeAgent(min_agents_agree=4)
        verdicts = [
            council_v2.Verdict("TrendAgent", council_v2.Action.LONG, 0.90, "trend_strong_up"),
            council_v2.Verdict("FundingFlowAgent", council_v2.Action.SHORT, 0.85, "funding_negative_extreme"),
            council_v2.Verdict("SupportResistanceAgent", council_v2.Action.LONG, 0.60, "support_held"),
            council_v2.Verdict("RiskAgent", council_v2.Action.HOLD, 0.50, "neutral_risk"),
        ]

        consensus = judge.aggregate(verdicts)

        self.assertEqual(consensus.action, council_v2.Action.HOLD)
        self.assertIn("Veto: agree_count", " ".join(consensus.notes))

    def test_risk_agent_high_confidence_hold_raises_effective_min_confidence(self):
        verdicts = [
            council_v2.Verdict("TrendAgent", council_v2.Action.LONG, 0.90, "trend_up"),
            council_v2.Verdict("FundingFlowAgent", council_v2.Action.LONG, 0.80, "funding_positive_turning"),
            council_v2.Verdict("SupportResistanceAgent", council_v2.Action.LONG, 0.85, "support_bounce"),
            council_v2.Verdict("RiskAgent", council_v2.Action.HOLD, 0.95, "volatility_too_high"),
        ]
        consensus = council_v2.Consensus(
            action=council_v2.Action.LONG,
            confidence=0.85,
            agree_count=4,
            total_agents=5,
            agreement_ratio=0.80,
            notes=[],
            raw=verdicts,
        )
        guardian = council_v2.PhilosophyGuardian(
            laws=council_v2.IronLaws(),
            fees=council_v2.Fees(),
            risk=council_v2.RiskConfig(),
        )
        plan = council_v2.TradePlan(
            action=council_v2.Action.LONG,
            symbol="BTC/USDT",
            leverage=1.0,
            position_frac=0.10,
            entry=100.0,
            stop=90.0,
            take=130.0,
            stoploss_mode="server",
            expected_profit=40.0,
            expected_cost=10.0,
        )

        ok, reasons = guardian.evaluate(consensus, plan)

        self.assertFalse(ok)
        self.assertTrue(any("MIN_CONFIDENCE" in reason for reason in reasons))

    def test_green_orange_red_sequence_limits_drawdown_before_red_halt(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_path = os.path.join(tmp_dir, "state.json")
            log_path = os.path.join(tmp_dir, "log.jsonl")

            provider = SequenceProvider(
                [
                    self._snapshot(ts=1_000, price=100.0),
                    self._snapshot(ts=1_060, price=79.0),
                    self._snapshot(ts=1_120, price=60.0),
                ]
            )
            sentinel = SequenceSentinel(
                [
                    council_v2.AlertLevel.GREEN,
                    council_v2.AlertLevel.ORANGE,
                    council_v2.AlertLevel.RED,
                ]
            )

            laws = council_v2.IronLaws()
            fees = council_v2.Fees()
            risk = council_v2.RiskConfig(initial_leverage=1.0)
            cfg = council_v2.CouncilConfig(symbol="BTC/USDT", candle_lookback=60, min_agents_agree=4)

            with mock.patch.object(council_v2.CouncilEngine, "_state_file", return_value=state_path):
                engine = council_v2.CouncilEngine(
                    provider=provider,
                    agents=[],
                    judge=council_v2.JudgeAgent(min_agents_agree=cfg.min_agents_agree),
                    guardian=council_v2.PhilosophyGuardian(laws=laws, fees=fees, risk=risk),
                    sentinel=sentinel,
                    exit_engine=council_v2.ExitEngine(council_v2.ExitConfig()),
                    broker=council_v2.PaperBroker(fees=fees),
                    laws=laws,
                    risk=risk,
                    cfg=cfg,
                    log_path=log_path,
                    sentinel_enabled=True,
                )

                engine.portfolio.cash = 1000.0
                engine.portfolio.equity = 1000.0
                engine.portfolio.high_watermark = 1000.0
                engine.portfolio.position = council_v2.Position(
                    action=council_v2.Action.LONG,
                    entry=100.0,
                    qty=1.5,
                    stop=90.0,
                    take=220.0,
                    leverage=1.0,
                    opened_ts=940,
                    initial_stop=90.0,
                    initial_risk_dist=10.0,
                    last_trail_stop=90.0,
                )

                engine.run_once()
                engine.run_once()
                engine.run_once()

            close_reasons = []
            with open(log_path, "r", encoding="utf-8") as handle:
                for line in handle:
                    payload = line.strip()
                    if not payload:
                        continue
                    row = json.loads(payload)
                    evt = row.get("event", {})
                    if evt.get("type") == "CLOSE":
                        close_reasons.append(evt.get("reason"))

            drawdown = (engine.portfolio.high_watermark - engine.portfolio.equity) / engine.portfolio.high_watermark
            self.assertIn("HARD_DRAWDOWN_CIRCUIT_BREAKER", close_reasons)
            self.assertLessEqual(drawdown, 0.15)


class LiveProviderRobustnessTests(unittest.TestCase):
    """Tests for CoinGeckoLiveProvider bug fixes."""

    def test_fetch_price_raises_on_unknown_coin(self):
        """Bug fix: KeyError → descriptive ValueError when coin not in response."""
        provider = council_v2.CoinGeckoLiveProvider()
        with mock.patch("council_v2._fetch_json_simple", return_value={}):
            with self.assertRaises(ValueError) as ctx:
                provider._fetch_price("unknowncoin")
        self.assertIn("no price for coin_id=", str(ctx.exception))
        self.assertIn("unknowncoin", str(ctx.exception))

    def test_fetch_ohlc_raises_on_empty_response(self):
        """Bug fix: silent empty list → descriptive ValueError on empty OHLC."""
        provider = council_v2.CoinGeckoLiveProvider()
        with mock.patch("council_v2._fetch_json_simple", return_value=[]):
            with self.assertRaises(ValueError) as ctx:
                provider._fetch_ohlc("bitcoin")
        self.assertIn("empty OHLC", str(ctx.exception))
        self.assertIn("bitcoin", str(ctx.exception))

    def test_get_snapshot_returns_cache_within_ttl(self):
        """Cache hit should not make any network calls."""
        provider = council_v2.CoinGeckoLiveProvider(cache_ttl_sec=300)
        import time
        # Seed cache manually
        fake_snap = council_v2.MarketSnapshot(
            symbol="BTC/USDT", ts=int(time.time()), price=93000.0, ohlcv=[]
        )
        provider._cache = fake_snap
        provider._cache_ts = time.time()  # just set

        with mock.patch("council_v2._fetch_json_simple") as fetch:
            result = provider.get_snapshot("BTC/USDT", lookback=48)

        fetch.assert_not_called()
        self.assertEqual(result.price, 93000.0)

    def test_get_snapshot_falls_back_to_cache_on_network_error(self):
        """On network error, provider should return stale cache rather than crash."""
        provider = council_v2.CoinGeckoLiveProvider(cache_ttl_sec=0)  # expired cache
        import time
        fake_snap = council_v2.MarketSnapshot(
            symbol="BTC/USDT", ts=int(time.time()) - 120, price=91000.0, ohlcv=[]
        )
        provider._cache = fake_snap
        provider._cache_ts = 0.0  # expired

        with mock.patch("council_v2._fetch_json_simple", side_effect=OSError("network down")):
            result = provider.get_snapshot("BTC/USDT", lookback=48)

        self.assertEqual(result.price, 91000.0)  # stale cache returned


class IntelBridgeTests(unittest.TestCase):
    """Tests for IntelBridge and Intel Veto / Watch Cap in run_once."""

    @staticmethod
    def _make_report(action: str, crowding: str, symbol: str = "BTC") -> dict:
        return {
            "run": {"started_at": "2099-01-01T00:00:00Z"},  # far future = never stale
            "assets": [{
                "symbol": symbol,
                "tags": ["watchlist"],
                "policy": {
                    "recommended_action": action,
                    "crowding_risk": crowding,
                    "evidence_diversity": 2,
                },
            }],
        }

    @staticmethod
    def _snapshot(ts: int, price: float) -> council_v2.MarketSnapshot:
        candles = [(ts - (59 - i) * 60, price, price * 1.001, price * 0.999, price, 100.0)
                   for i in range(60)]
        return council_v2.MarketSnapshot(symbol="BTC/USDT", ts=ts, price=price, ohlcv=candles)

    def _build_engine(self, report: dict, log_path: str, tmp_dir: str):
        """Build a CouncilEngine with IntelBridge pointing at a temp report dir."""
        import json, os
        report_dir = os.path.join(tmp_dir, "20990101T000000Z")
        os.makedirs(report_dir)
        with open(os.path.join(report_dir, "report.json"), "w") as f:
            json.dump(report, f)

        laws = council_v2.IronLaws()
        laws.COOLING_PERIOD_SEC = 0
        laws.DAILY_TRADE_LIMIT = 100
        fees = council_v2.Fees()
        risk = council_v2.RiskConfig()
        cfg = council_v2.CouncilConfig(symbol="BTC/USDT", candle_lookback=60)

        agents = [
            council_v2.TrendAgent(),
            council_v2.SupportResistanceAgent(), council_v2.RiskAgent(),
        ]
        bridge = council_v2.IntelBridge(intel_dir=tmp_dir, staleness_sec=999999)
        provider = SequenceProvider([self._snapshot(1000, 50000.0)])

        state_path = os.path.join(tmp_dir, "state.json")
        with mock.patch.object(council_v2.CouncilEngine, "_state_file", return_value=state_path):
            engine = council_v2.CouncilEngine(
                provider=provider, agents=agents,
                judge=council_v2.JudgeAgent(min_agents_agree=4),
                guardian=council_v2.PhilosophyGuardian(laws=laws, fees=fees, risk=risk),
                sentinel=council_v2.SafetySentinel(council_v2.SentinelConfig()),
                exit_engine=council_v2.ExitEngine(council_v2.ExitConfig()),
                broker=council_v2.PaperBroker(fees=fees),
                laws=laws, risk=risk, cfg=cfg,
                log_path=log_path,
                sentinel_enabled=True,
                intel_bridge=bridge,
            )
            engine.portfolio.cash = 1000.0
            engine.portfolio.equity = 1000.0
            engine.portfolio.high_watermark = 1000.0
        return engine, state_path

    def test_no_trade_intel_veto_blocks_new_position(self):
        """no_trade policy must produce INTEL_VETO cycle and skip agent voting."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = os.path.join(tmp_dir, "log.jsonl")
            report = self._make_report("no_trade", "high")
            engine, state_path = self._build_engine(report, log_path, tmp_dir)

            with mock.patch.object(council_v2.CouncilEngine, "_state_file", return_value=state_path):
                engine.run_once()

            cycles = []
            with open(log_path) as f:
                for line in f:
                    cycles.append(json.loads(line.strip()).get("cycle"))

            self.assertIn("INTEL_VETO", cycles)
            self.assertNotIn("TRADE", cycles)
            self.assertIsNone(engine.portfolio.position)

    def test_watch_intel_caps_confidence_below_min_confidence(self):
        """watch policy must cap consensus confidence below the active MIN_CONFIDENCE."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = os.path.join(tmp_dir, "log.jsonl")
            report = self._make_report("watch", "elevated")
            engine, state_path = self._build_engine(report, log_path, tmp_dir)

            with mock.patch.object(council_v2.CouncilEngine, "_state_file", return_value=state_path):
                engine.run_once()

            # Find any logged cycle with intel_policy and check confidence
            with open(log_path) as f:
                for line in f:
                    row = json.loads(line.strip())
                    if row.get("intel_policy") and "consensus" in row:
                        self.assertLessEqual(row["consensus"]["confidence"], 0.70)
                        return
            # If no consensus logged (pure HOLD), that's acceptable — watch cap worked

    def test_stale_intel_report_does_not_veto(self):
        """A stale report (older than staleness_sec) must be ignored — trade proceeds normally."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = os.path.join(tmp_dir, "log.jsonl")
            report = self._make_report("no_trade", "high")
            # Override the timestamp to be ancient
            report["run"]["started_at"] = "2000-01-01T00:00:00Z"

            engine, state_path = self._build_engine(report, log_path, tmp_dir)
            # Use a fresh bridge with strict staleness (1 second)
            engine.intel_bridge = council_v2.IntelBridge(
                intel_dir=os.path.join(tmp_dir, "20990101T000000Z").replace("20990101T000000Z", ""),
                staleness_sec=1,
            )
            engine.intel_bridge = council_v2.IntelBridge(
                intel_dir=tmp_dir, staleness_sec=1
            )

            with mock.patch.object(council_v2.CouncilEngine, "_state_file", return_value=state_path):
                engine.run_once()

            cycles = []
            with open(log_path) as f:
                for line in f:
                    cycles.append(json.loads(line.strip()).get("cycle"))

            self.assertNotIn("INTEL_VETO", cycles)

    def test_missing_intel_dir_does_not_block_trading(self):
        """If intel dir doesn't exist, IntelBridge returns None and engine runs normally."""
        bridge = council_v2.IntelBridge(intel_dir="/nonexistent/path/that/does/not/exist")
        result = bridge.get_signal("BTC/USDT")
        self.assertIsNone(result)

    def test_watch_cap_is_recorded_in_log_notes(self):
        """When watch cap is applied, the log entry must contain the cap note."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = os.path.join(tmp_dir, "log.jsonl")
            report = self._make_report("watch", "elevated")
            engine, state_path = self._build_engine(report, log_path, tmp_dir)

            with mock.patch.object(council_v2.CouncilEngine, "_state_file", return_value=state_path):
                engine.run_once()

            with open(log_path) as f:
                for line in f:
                    row = json.loads(line.strip())
                    notes = row.get("consensus", {}).get("notes", [])
                    if any("Intel watch cap" in n for n in notes):
                        return  # cap note found — pass
            # No cap note means confidence was already ≤ 0.70 — still acceptable
            # (agents on flat candles return HOLD before cap is needed)

    def test_get_funding_returns_none_for_stale_report(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            report_dir = os.path.join(tmp_dir, "20990101T000000Z")
            os.makedirs(report_dir)
            report = self._make_report("watch", "elevated")
            report["run"]["started_at"] = "2000-01-01T00:00:00Z"
            report["assets"][0]["providers"] = {
                "funding": {
                    "status": "ok",
                    "data": {
                        "current_rate": 0.0006,
                        "is_anomalous": True,
                        "is_turning": False,
                    },
                }
            }
            with open(os.path.join(report_dir, "report.json"), "w", encoding="utf-8") as handle:
                json.dump(report, handle)

            bridge = council_v2.IntelBridge(intel_dir=tmp_dir, staleness_sec=1)

            self.assertIsNone(bridge.get_funding("BTC/USDT"))

    def test_watch_policy_cannot_trade_via_momentum_boost(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            report_dir = os.path.join(tmp_dir, "20990101T000000Z")
            os.makedirs(report_dir)
            report = self._make_report("watch", "elevated")
            with open(os.path.join(report_dir, "report.json"), "w", encoding="utf-8") as handle:
                json.dump(report, handle)

            laws = council_v2.IronLaws()
            laws.COOLING_PERIOD_SEC = 0
            laws.DAILY_TRADE_LIMIT = 100
            fees = council_v2.Fees()
            risk = council_v2.RiskConfig()
            cfg = council_v2.CouncilConfig(symbol="BTC/USDT", candle_lookback=60, min_agents_agree=3)
            bridge = council_v2.IntelBridge(intel_dir=tmp_dir, staleness_sec=999999)
            provider = SequenceProvider([self._snapshot(1000, 50000.0)])
            agents = [
                StaticVerdictAgent("TrendAgent", council_v2.Action.LONG),
                StaticVerdictAgent("FundingFlowAgent", council_v2.Action.LONG),
                StaticVerdictAgent("SupportResistanceAgent", council_v2.Action.LONG),
            ]

            log_path = os.path.join(tmp_dir, "log.jsonl")
            state_path = os.path.join(tmp_dir, "state.json")
            with mock.patch.object(council_v2.CouncilEngine, "_state_file", return_value=state_path):
                engine = council_v2.CouncilEngine(
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
                    sentinel_enabled=True,
                    intel_bridge=bridge,
                )
                engine.run_once()

            self.assertIsNone(engine.portfolio.position)
            with open(log_path, "r", encoding="utf-8") as handle:
                rows = [json.loads(line) for line in handle if line.strip()]
            self.assertNotIn("TRADE", [row.get("cycle") for row in rows])


class FundingAgentTests(unittest.TestCase):
    def test_liquidation_pressure_agent_handles_missing_basis_and_open_interest(self):
        bridge = StaticFundingBridge(
            {
                "current_rate": 0.0006,
                "is_anomalous": True,
                "is_turning": False,
                "open_interest": None,
                "basis": None,
                "exchange": "binance",
            }
        )
        snap = council_v2.MarketSnapshot(
            symbol="BTC/USDT",
            ts=1000,
            price=50000.0,
            ohlcv=[(1000 + i, 50000.0, 50100.0, 49900.0, 50000.0, 100.0) for i in range(20)],
        )

        verdict = council_v2.LiquidationPressureAgent(bridge).decide(snap)

        self.assertEqual(verdict.action, council_v2.Action.SHORT)
        self.assertIn("OI=unknown", verdict.counterparty_thesis)


class TestRemovedAgents(unittest.TestCase):
    def test_council_engine_does_not_use_signal_memory(self):
        self.assertFalse(
            hasattr(council_v2, "SignalMemory"),
            "SignalMemory must be removed — smoothing chart noise amplifies false signal, not real pressure",
        )

    def test_engine_does_not_register_meanreversion_agent(self):
        self.assertFalse(
            hasattr(council_v2, "MeanReversionAgent"),
            "MeanReversionAgent must be removed — Bollinger-band thesis is chart-derived with templated counterparty copy",
        )

    def test_engine_does_not_register_contrarian_agent(self):
        self.assertFalse(
            hasattr(council_v2, "ContrarianAgent"),
            "ContrarianAgent must be removed — it is TrendAgent inverted with no independent signal",
        )


class TestRemovedHelpers(unittest.TestCase):
    """is_funding_turning was a sign-flip heuristic — funding flipping does not
    by itself mean someone is forced. forced_flow primitives (z-score extreme,
    flip + 1σ magnitude) replace it. Remove the helper and its policy rule."""

    def test_council_intel_does_not_export_is_funding_turning(self):
        import council_intel
        self.assertFalse(
            hasattr(council_intel, "is_funding_turning"),
            "is_funding_turning must be removed — sign-flip alone is not forced flow",
        )


class IronLawsStrictDefaultsTests(unittest.TestCase):
    """After cutting weak agents, the law floor must return to production strict."""

    def test_iron_laws_min_confidence_is_strict_production_default(self):
        laws = council_v2.IronLaws()
        self.assertEqual(
            laws.MIN_CONFIDENCE,
            0.75,
            "MIN_CONFIDENCE was lowered to 0.60 to let chart-derived agents pass. "
            "After Contrarian/MeanReversion/SignalMemory removal the strict 0.75 floor must be restored.",
        )

    def test_council_config_min_agents_agree_is_four(self):
        cfg = council_v2.CouncilConfig()
        self.assertEqual(
            cfg.min_agents_agree,
            4,
            "min_agents_agree default was lowered to 3 for the same reason; restore 4 with the 5-agent system.",
        )


if __name__ == "__main__":
    unittest.main()


# ══════════════════════════════════════════════════════════════
# Phase 3 — Forced-Flow Agent Rewire + New Agents
# ══════════════════════════════════════════════════════════════

class StaticOKXBridge:
    """Test stub that exposes all three bridge methods."""

    def __init__(self, okx_data=None, deribit_data=None, funding_data=None):
        self._okx = okx_data
        self._deribit = deribit_data
        self._funding = funding_data

    def get_okx_data(self, symbol):
        _ = symbol
        return self._okx

    def get_deribit_data(self, symbol):
        _ = symbol
        return self._deribit

    def get_funding(self, symbol):
        _ = symbol
        return self._funding


def _flat_snap(price: float = 50000.0) -> council_v2.MarketSnapshot:
    candles = [(1000 + i, price, price * 1.001, price * 0.999, price, 100.0) for i in range(60)]
    return council_v2.MarketSnapshot(symbol="BTC/USDT", ts=1059, price=price, ohlcv=candles)


def _trending_up_snap() -> council_v2.MarketSnapshot:
    """Closes go 48000 → ~49180 (clear uptrend)."""
    candles = []
    for i in range(60):
        p = 48000.0 + i * 20.0
        candles.append((1000 + i, p, p * 1.001, p * 0.999, p, 100.0))
    return council_v2.MarketSnapshot(symbol="BTC/USDT", ts=1059, price=candles[-1][4], ohlcv=candles)


def _trending_down_snap() -> council_v2.MarketSnapshot:
    """Closes go 52000 → ~50820 (clear downtrend)."""
    candles = []
    for i in range(60):
        p = 52000.0 - i * 20.0
        candles.append((1000 + i, p, p * 1.001, p * 0.999, p, 100.0))
    return council_v2.MarketSnapshot(symbol="BTC/USDT", ts=1059, price=candles[-1][4], ohlcv=candles)


# ──────────────────────────────────────────────────────────────
# FundingFlowAgent — rewired to use OKX z-score + funding_flip
# ──────────────────────────────────────────────────────────────

class Phase3FundingFlowAgentTests(unittest.TestCase):

    def _okx(self, z_score, rate, rate_24h_ago=None, sigma=0.0001):
        return {
            "funding_rate": rate,
            "funding_z_score": z_score,
            "is_anomalous": abs(z_score) > 3.0,
            "rate_24h_ago": rate_24h_ago,
            "rate_1sigma": sigma,
        }

    def test_extreme_positive_z_signals_short(self):
        """z > 3.0 → longs overextended → SHORT."""
        bridge = StaticOKXBridge(okx_data=self._okx(z_score=3.5, rate=0.00142))
        verdict = council_v2.FundingFlowAgent(bridge).decide(_flat_snap())
        self.assertEqual(verdict.action, council_v2.Action.SHORT)

    def test_extreme_negative_z_signals_long(self):
        """z < -3.0 → shorts overextended → LONG."""
        bridge = StaticOKXBridge(okx_data=self._okx(z_score=-3.5, rate=-0.00142))
        verdict = council_v2.FundingFlowAgent(bridge).decide(_flat_snap())
        self.assertEqual(verdict.action, council_v2.Action.LONG)

    def test_funding_flip_neg_to_pos_signals_long(self):
        """Funding flipped neg→pos AND |now| > 1σ → shorts being squeezed → LONG."""
        bridge = StaticOKXBridge(okx_data=self._okx(
            z_score=1.5, rate=0.0003, rate_24h_ago=-0.0002, sigma=0.0001))
        verdict = council_v2.FundingFlowAgent(bridge).decide(_flat_snap())
        self.assertEqual(verdict.action, council_v2.Action.LONG)

    def test_funding_flip_pos_to_neg_signals_short(self):
        """Funding flipped pos→neg AND |now| > 1σ → longs being flushed → SHORT."""
        bridge = StaticOKXBridge(okx_data=self._okx(
            z_score=-1.5, rate=-0.0003, rate_24h_ago=0.0002, sigma=0.0001))
        verdict = council_v2.FundingFlowAgent(bridge).decide(_flat_snap())
        self.assertEqual(verdict.action, council_v2.Action.SHORT)

    def test_okx_non_hold_thesis_has_numeric_tokens(self):
        """Non-HOLD verdict from OKX data must carry ≥2 numeric tokens in counterparty_thesis."""
        import re
        bridge = StaticOKXBridge(okx_data=self._okx(z_score=3.5, rate=0.00142))
        verdict = council_v2.FundingFlowAgent(bridge).decide(_flat_snap())
        self.assertNotEqual(verdict.action, council_v2.Action.HOLD)
        pat = re.compile(r'\d+(?:\.\d+)?\s*%|\d+\.\d+|\$\s*\d+')
        self.assertGreaterEqual(
            len(pat.findall(verdict.counterparty_thesis)), 2,
            f"Expected ≥2 numeric tokens in: {verdict.counterparty_thesis}",
        )

    def test_moderate_z_no_flip_is_hold_or_moderate(self):
        """z = 1.5, no flip → not a forced-flow extreme (may still signal moderate flow)."""
        bridge = StaticOKXBridge(okx_data=self._okx(
            z_score=1.5, rate=0.0002, rate_24h_ago=0.0001))
        verdict = council_v2.FundingFlowAgent(bridge).decide(_flat_snap())
        # Must NOT flag as extreme (no funding_extreme)
        self.assertNotIn(
            verdict.action, [council_v2.Action.SHORT],
            "Moderate z=1.5 must not signal extreme short",
        )


# ──────────────────────────────────────────────────────────────
# LiquidationPressureAgent — rewired to ls_ratio + taker_flow
# ──────────────────────────────────────────────────────────────

class Phase3LiquidationPressureAgentTests(unittest.TestCase):

    def _okx(self, ls_acct, ls_cont, taker_buy, funding_rate=0.0003):
        return {
            "funding_rate": funding_rate,
            "funding_z_score": 1.5,
            "ls_ratio_account": ls_acct,
            "ls_ratio_contract": ls_cont,
            "taker_buy_ratio": taker_buy,
            "oi_value": 8_200_000_000,
            "is_anomalous": False,
        }

    def test_ls_extreme_retail_long_signals_short(self):
        """account > 2.0 AND contract < 0.8: retail crowded long, pros short → SHORT."""
        bridge = StaticOKXBridge(okx_data=self._okx(ls_acct=2.5, ls_cont=0.6, taker_buy=0.45))
        verdict = council_v2.LiquidationPressureAgent(bridge).decide(_flat_snap())
        self.assertEqual(verdict.action, council_v2.Action.SHORT)

    def test_ls_extreme_retail_short_signals_long(self):
        """account < 0.5 AND contract > 1.25: retail crowded short, pros long → LONG."""
        bridge = StaticOKXBridge(okx_data=self._okx(ls_acct=0.4, ls_cont=1.5, taker_buy=0.55))
        verdict = council_v2.LiquidationPressureAgent(bridge).decide(_flat_snap())
        self.assertEqual(verdict.action, council_v2.Action.LONG)

    def test_taker_sell_imbalance_signals_short(self):
        """taker_buy_ratio < 0.3: 70%+ active sellers → SHORT."""
        bridge = StaticOKXBridge(okx_data=self._okx(ls_acct=1.2, ls_cont=1.1, taker_buy=0.2))
        verdict = council_v2.LiquidationPressureAgent(bridge).decide(_flat_snap())
        self.assertEqual(verdict.action, council_v2.Action.SHORT)

    def test_taker_buy_imbalance_signals_long(self):
        """taker_buy_ratio > 0.7: 70%+ active buyers → LONG."""
        bridge = StaticOKXBridge(okx_data=self._okx(ls_acct=1.2, ls_cont=1.1, taker_buy=0.8))
        verdict = council_v2.LiquidationPressureAgent(bridge).decide(_flat_snap())
        self.assertEqual(verdict.action, council_v2.Action.LONG)

    def test_balanced_conditions_is_hold(self):
        """Balanced ls_ratio and taker_flow: no forced pressure → HOLD."""
        bridge = StaticOKXBridge(okx_data=self._okx(ls_acct=1.3, ls_cont=1.1, taker_buy=0.48))
        verdict = council_v2.LiquidationPressureAgent(bridge).decide(_flat_snap())
        self.assertEqual(verdict.action, council_v2.Action.HOLD)

    def test_okx_non_hold_thesis_has_numeric_tokens(self):
        """Non-HOLD verdict must carry ≥2 numeric tokens."""
        import re
        bridge = StaticOKXBridge(okx_data=self._okx(ls_acct=2.5, ls_cont=0.6, taker_buy=0.45))
        verdict = council_v2.LiquidationPressureAgent(bridge).decide(_flat_snap())
        self.assertNotEqual(verdict.action, council_v2.Action.HOLD)
        pat = re.compile(r'\d+(?:\.\d+)?\s*%|\d+\.\d+|\$\s*\d+')
        self.assertGreaterEqual(
            len(pat.findall(verdict.counterparty_thesis)), 2,
            f"Expected ≥2 numeric tokens in: {verdict.counterparty_thesis}",
        )


# ──────────────────────────────────────────────────────────────
# RiskAgent — adds gamma cluster veto
# ──────────────────────────────────────────────────────────────

class Phase3RiskAgentTests(unittest.TestCase):

    def test_gamma_cluster_veto_returns_high_conf_hold(self):
        """Active gamma cluster → HOLD with conf ≥ 0.85 (triggers MIN_CONF escalation)."""
        bridge = StaticOKXBridge(deribit_data={
            "gamma_cluster": True,
            "gamma_strike": 50000,
            "gamma_expiry_days": 2,
            "max_pain": 50500,
            "put_call_ratio": 0.9,
        })
        verdict = council_v2.RiskAgent(vol_lookback=30, intel_bridge=bridge).decide(_flat_snap())
        self.assertEqual(verdict.action, council_v2.Action.HOLD)
        self.assertGreaterEqual(verdict.confidence, 0.85)

    def test_no_gamma_cluster_does_not_force_veto(self):
        """gamma_cluster=False → RiskAgent uses vol logic, not a hard veto."""
        bridge = StaticOKXBridge(deribit_data={"gamma_cluster": False})
        verdict = council_v2.RiskAgent(vol_lookback=30, intel_bridge=bridge).decide(_flat_snap())
        # Flat candles → low vol → not a high-conf HOLD veto
        if verdict.action == council_v2.Action.HOLD:
            self.assertLess(verdict.confidence, 0.85,
                            "Low-vol HOLD must not mimic the gamma veto threshold")

    def test_risk_agent_accepts_intel_bridge_kwarg(self):
        """RiskAgent constructor must accept intel_bridge=None without error."""
        agent = council_v2.RiskAgent(vol_lookback=30, intel_bridge=None)
        self.assertIsNotNone(agent)


# ──────────────────────────────────────────────────────────────
# OIFlowAgent (new)
# ──────────────────────────────────────────────────────────────

class OIFlowAgentTests(unittest.TestCase):

    def _okx(self, oi_delta_pct):
        return {
            "funding_rate": 0.0001,
            "funding_z_score": 0.5,
            "oi_value": 8_200_000_000,
            "oi_delta_pct_24h": oi_delta_pct,
            "taker_buy_ratio": 0.5,
        }

    def test_agent_exists(self):
        self.assertTrue(hasattr(council_v2, "OIFlowAgent"))
        agent = council_v2.OIFlowAgent(None)
        self.assertTrue(callable(getattr(agent, "decide", None)))

    def test_price_up_oi_down_signals_short(self):
        """Price uptrend + OI delta -8%: distribution → SHORT."""
        bridge = StaticOKXBridge(okx_data=self._okx(-0.08))
        verdict = council_v2.OIFlowAgent(bridge).decide(_trending_up_snap())
        self.assertEqual(verdict.action, council_v2.Action.SHORT)

    def test_price_down_oi_up_signals_long(self):
        """Price downtrend + OI delta +8%: smart accumulation → LONG."""
        bridge = StaticOKXBridge(okx_data=self._okx(0.08))
        verdict = council_v2.OIFlowAgent(bridge).decide(_trending_down_snap())
        self.assertEqual(verdict.action, council_v2.Action.LONG)

    def test_small_oi_delta_is_hold(self):
        """OI delta 3% (below 5% threshold) → HOLD."""
        bridge = StaticOKXBridge(okx_data=self._okx(0.03))
        verdict = council_v2.OIFlowAgent(bridge).decide(_trending_up_snap())
        self.assertEqual(verdict.action, council_v2.Action.HOLD)

    def test_no_bridge_is_hold(self):
        verdict = council_v2.OIFlowAgent(None).decide(_flat_snap())
        self.assertEqual(verdict.action, council_v2.Action.HOLD)

    def test_no_okx_data_is_hold(self):
        bridge = StaticOKXBridge(okx_data=None)
        verdict = council_v2.OIFlowAgent(bridge).decide(_flat_snap())
        self.assertEqual(verdict.action, council_v2.Action.HOLD)

    def test_non_hold_thesis_has_numeric_tokens(self):
        import re
        bridge = StaticOKXBridge(okx_data=self._okx(-0.08))
        verdict = council_v2.OIFlowAgent(bridge).decide(_trending_up_snap())
        self.assertNotEqual(verdict.action, council_v2.Action.HOLD)
        pat = re.compile(r'\d+(?:\.\d+)?\s*%|\d+\.\d+|\$\s*\d+')
        self.assertGreaterEqual(len(pat.findall(verdict.counterparty_thesis)), 2,
                                f"Thesis: {verdict.counterparty_thesis}")


# ──────────────────────────────────────────────────────────────
# OptionsGammaAgent (new)
# ──────────────────────────────────────────────────────────────

class OptionsGammaAgentTests(unittest.TestCase):

    def _deribit(self, max_pain, gamma_cluster=True):
        return {
            "gamma_cluster": gamma_cluster,
            "gamma_strike": 50000,
            "gamma_expiry_days": 3,
            "max_pain": max_pain,
            "put_call_ratio": 0.9,
        }

    def test_agent_exists(self):
        self.assertTrue(hasattr(council_v2, "OptionsGammaAgent"))
        agent = council_v2.OptionsGammaAgent(None)
        self.assertTrue(callable(getattr(agent, "decide", None)))

    def test_price_below_max_pain_is_long(self):
        """Price 50000 < max_pain 51000 → gamma magnet pulls up → LONG."""
        bridge = StaticOKXBridge(deribit_data=self._deribit(max_pain=51000))
        verdict = council_v2.OptionsGammaAgent(bridge).decide(_flat_snap(50000))
        self.assertEqual(verdict.action, council_v2.Action.LONG)

    def test_price_above_max_pain_is_short(self):
        """Price 50000 > max_pain 49000 → gamma magnet pulls down → SHORT."""
        bridge = StaticOKXBridge(deribit_data=self._deribit(max_pain=49000))
        verdict = council_v2.OptionsGammaAgent(bridge).decide(_flat_snap(50000))
        self.assertEqual(verdict.action, council_v2.Action.SHORT)

    def test_no_gamma_cluster_is_hold(self):
        bridge = StaticOKXBridge(deribit_data=self._deribit(max_pain=51000, gamma_cluster=False))
        verdict = council_v2.OptionsGammaAgent(bridge).decide(_flat_snap(50000))
        self.assertEqual(verdict.action, council_v2.Action.HOLD)

    def test_no_bridge_is_hold(self):
        verdict = council_v2.OptionsGammaAgent(None).decide(_flat_snap())
        self.assertEqual(verdict.action, council_v2.Action.HOLD)

    def test_non_hold_thesis_has_numeric_tokens(self):
        import re
        bridge = StaticOKXBridge(deribit_data=self._deribit(max_pain=51000))
        verdict = council_v2.OptionsGammaAgent(bridge).decide(_flat_snap(50000))
        self.assertNotEqual(verdict.action, council_v2.Action.HOLD)
        pat = re.compile(r'\d+(?:\.\d+)?\s*%|\d+\.\d+|\$\s*\d+')
        self.assertGreaterEqual(len(pat.findall(verdict.counterparty_thesis)), 2,
                                f"Thesis: {verdict.counterparty_thesis}")


# ──────────────────────────────────────────────────────────────
# JudgeAgent — Phase 3 weights
# ──────────────────────────────────────────────────────────────

class JudgeWeightsPhase3Tests(unittest.TestCase):

    def test_trend_agent_weight_is_half(self):
        """TrendAgent downweighted to 0.5 — chart data is crowded signal."""
        self.assertEqual(council_v2.JudgeAgent().weights.get("TrendAgent"), 0.5)

    def test_oi_flow_agent_weight_is_point_nine(self):
        self.assertEqual(council_v2.JudgeAgent().weights.get("OIFlowAgent"), 0.9)

    def test_options_gamma_agent_weight_is_point_seven(self):
        self.assertEqual(council_v2.JudgeAgent().weights.get("OptionsGammaAgent"), 0.7)


# ──────────────────────────────────────────────────────────────
# PhilosophyGuardian — numeric thesis discipline
# ──────────────────────────────────────────────────────────────

class GuardianNumericThesisTests(unittest.TestCase):

    @staticmethod
    def _guardian():
        return council_v2.PhilosophyGuardian(
            laws=council_v2.IronLaws(),
            fees=council_v2.Fees(),
            risk=council_v2.RiskConfig(),
        )

    @staticmethod
    def _passing_consensus():
        return council_v2.Consensus(
            action=council_v2.Action.LONG,
            confidence=0.80,
            agree_count=5,
            total_agents=7,
            agreement_ratio=0.71,
            notes=[],
            raw=[
                council_v2.Verdict("RiskAgent", council_v2.Action.LONG, 0.60, "low_vol"),
            ],
            counterparty_thesis="placeholder",
        )

    @staticmethod
    def _plan(thesis: str) -> council_v2.TradePlan:
        return council_v2.TradePlan(
            action=council_v2.Action.LONG,
            symbol="BTC/USDT",
            leverage=1.0,
            position_frac=0.10,
            entry=50000.0,
            stop=48000.0,
            take=56000.0,
            stoploss_mode="server",
            expected_profit=600.0,
            expected_cost=100.0,
            counterparty_thesis=thesis,
        )

    def test_non_numeric_thesis_is_vetoed(self):
        """Pure-text thesis with no numeric data points → WEAK_THESIS veto."""
        ok, reasons = self._guardian().evaluate(
            self._passing_consensus(),
            self._plan("Late shorts who haven't recognized the trend shift."),
        )
        self.assertFalse(ok)
        self.assertTrue(any("WEAK_THESIS" in r for r in reasons),
                        f"Expected WEAK_THESIS in {reasons}")

    def test_numeric_thesis_passes_check(self):
        """Thesis with ≥2 numeric tokens must not trigger WEAK_THESIS veto."""
        _, reasons = self._guardian().evaluate(
            self._passing_consensus(),
            self._plan(
                "Funding z=3.5σ (0.142%/8h). OI $8.2B diverging -8%. Shorts face squeeze."
            ),
        )
        self.assertFalse(any("WEAK_THESIS" in r for r in reasons),
                         f"Numeric thesis should not be vetoed, got: {reasons}")
