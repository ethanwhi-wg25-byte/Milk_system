# Forced-Flow Cuts (2026-04-26)

## Doctrine basis

**P&L conservation.** Every trade is zero-sum (fees negative-sum). For an
edge to exist, the system must be able to name the counterparty class
that is *structurally compelled* to lose — funding carry that can't be
paid forever, liquidation thresholds, OI being burned through, options
gamma the market makers must hedge, contractual unlock dates.

Signals that don't identify a forced counterparty produce decoration
masquerading as thesis. They lower the noise floor without raising the
edge ceiling, and the only thing they reliably do is let weak agents
clear a relaxed `MIN_CONFIDENCE`.

This plan records the cuts that removed those signals, restored the
strict floor, and marked which forward-looking providers are *not*
in scope for the immediately next sprint.

---

## 5 cuts applied

| # | Cut | Commit | Why |
|---|---|---|---|
| 1 | `ContrarianAgent` | `45b061f` | TrendAgent inverted = same EMA, no orthogonal information. Existed only to patch RC-4 fragility by adding a vote, not a signal. |
| 2 | `MeanReversionAgent` | `a1b4c56` | Bollinger-derived; counterparty_thesis was templated copy ("Panic sellers...", "FOMO buyers...") — fires on any deviation regardless of whether anyone is funded into the position. |
| 3 | `SignalMemory` (α=0.4 EMA) | `ca6efe8` | Smoothing over binary directional votes encodes no new information; the 10% momentum boost was a hidden way to lower effective `MIN_CONFIDENCE` without touching `IronLaws`. |
| 4 | IronLaws relaxation reverted | `4f2f5eb` | `MIN_CONFIDENCE 0.60→0.75`, `min_agents_agree 3→4`. The lowered floors existed to let cuts #1–#3 vote effectively; with those gone, the strict production floor returns. |
| 5 | `is_funding_turning` helper | `97b0d4b` | Sign-flip alone is not forced flow — it can be mean reversion, calendar effect, or sponsor-driven. Without magnitude (≥1σ) and history context (z-score vs 30d), it's a tag without an edge thesis. |

---

## 1 quality gate added

| Add | Commit | What |
|---|---|---|
| `evaluate_asset_policy` returns `thesis_numeric_ok` | `e1cd9ab` | Tight regex (`%`, `$`, decimals, K/M/B units) requires ≥2 quantitative tokens. Bare integers (`"45 of 60 cycles"`) fail by design — that's narrative, not data. Empty/missing thesis returns no key — never penalized for absence, only for shape. |

---

## 2 providers deferred (out of scope, blueprint level)

| Deferred | Why |
|---|---|
| `UnlockScheduleProvider` | HTML scrape against TokenUnlocks / CryptoRank — fragile parser, rate limits on the free tier, and the marginal information advantage over what the market already prices in is unclear. Revisit only if a specific event (e.g., a large vesting cliff for a held asset) demands it. |
| `LiquidationProvider` | Coinglass free-tier liquidation aggregates have unverified stability; Binance/OKX expose individual liquidations only via WebSocket, which breaks the CLI one-shot model the rest of `council_intel.py` is built around. Liquidation pressure is currently inferred indirectly via funding extreme + OI divergence; that proxy is sufficient until a specific failure case proves otherwise. |

---

## Net effect

- **Agents**: 7 → 5 (`TrendAgent`, `FundingFlowAgent`, `LiquidationPressureAgent`, `RiskAgent`, plus the support/resistance agent surviving from the original set)
- **Production floor**: strict (`MIN_CONFIDENCE=0.75`, `min_agents_agree=4`) restored
- **`investigate` gate**: now reachable only via forced-flow primitives — `funding_extreme`, `oi_divergence`, plus whatever the OKX-primary blueprint adds for `ls_ratio_extreme` and `taker_flow_imbalance`. Watchlist + funding-signal alone now caps at `watch`.
- **`council_mvp` removed** (snapshot `1c03c71`, delete `7b737c8`) — pre-v2 archive, unreferenced by source.

---

## Verification

All 64 non-blueprint tests green at HEAD. 12 forward-looking RED tests
in `tests/test_forced_flow.py` remain — these gate the next sprint's
provider work and should be made green commit-by-commit, not in bulk.

Run:

```bash
python3 -m unittest discover tests -v
```

---

## Out of scope for this plan (intentionally)

- Any *additive* change to council_v2.py beyond restoring the strict floor.
- Any new agent. Cuts only. Adds belong in the blueprint plan, not here.
- Any change to `sim.py`. It will produce 0 trades against random walks
  under the strict floor — that is the correct evidence, not a regression.
