# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository purpose

Two-track crypto decision system. Both tracks are **paper/read-only — no exchange writes**.

- **`council_v2.py`** — Paper-trading loop. Five heuristic agents → Judge consensus → PhilosophyGuardian (Iron Laws veto) → SafetySentinel (GREEN/YELLOW/ORANGE/RED regime) → ExitEngine (BE-at-1R, partial-TP-at-2R, ATR trailing) → PaperBroker. State persists to `council_state_v2.json`, events to `council_log_v2.jsonl`.
- **`council_intel.py`** — Read-only intelligence CLI. Pulls CoinGecko top N + Nansen smart-money + Dune query packs + perp funding rates (CoinGecko `/derivatives`, market preference filter) and emits timestamped `report.json` + `report.md` artifacts under `artifacts/intel/<UTC>/`. Each asset card carries a `policy` block (`recommended_action`, `crowding_risk`, `evidence_diversity`) — default verdict is `no_trade`.

`sim.py` is a deterministic A/B harness that runs `council_v2` twice (sentinel on vs. off) and writes `sim_base.jsonl` / `sim_sentinel.jsonl`.

## Common commands

```bash
# Paper-trading loop (writes state + log in CWD)
python3 council_v2.py run --symbol BTC/USDT --sentinel on --interval 60

# One-shot sentinel evaluation on a simulated snapshot
python3 council_v2.py sentinel --symbol BTC/USDT

# Summarize a jsonl log (action rate, max drawdown, final equity)
python3 council_v2.py analyze --log council_log_v2.jsonl

# Read-only intel report (consumes config/universe.json by default)
python3 council_intel.py run --config config/universe.json --out-dir artifacts/intel

# Deterministic A/B sim (overwrites sim_base.jsonl + sim_sentinel.jsonl)
python3 sim.py

# Tests — stdlib unittest, no pytest
python3 -m unittest discover tests -v
python3 -m unittest tests.test_council_intel.AssetCardTests -v
python3 -m unittest tests.test_council_intel.AssetCardTests.test_asset_card_promotes_orthogonal_signals_to_investigate -v
```

No package manager, no lockfile, **no third-party deps** — everything runs on Python 3 stdlib (`urllib`, `json`, `dataclasses`, `argparse`, `subprocess` for curl fallback). Do not introduce `requests`, `pandas`, or similar; the zero-dep constraint is intentional.

External API keys read from env: `NANSEN_API_KEY`, `DUNE_API_KEY`. Funding endpoints are unauthenticated.

## Architecture notes that span files

**Iron Laws are the trade-veto contract.** `PhilosophyGuardian.evaluate` in `council_v2.py` enforces `MIN_CONFIDENCE=0.75`, `MAX_LEVERAGE=10`, `MAX_POSITION=0.15`, `STOP_LOSS_MODE="server"`, `MIN_PROFIT_MULT=3.0`. Changing any of these changes what trades survive — `sim.py` deliberately relaxes them to force activity for testing. Production defaults must stay strict. Note the RiskAgent escalation rule inside `evaluate`: when `RiskAgent` votes `HOLD` with `confidence >= 0.85`, the effective `MIN_CONFIDENCE` floor is raised to `0.90` for that consensus only. Preserve that rule when touching the guardian — it gives Risk a dedicated veto path without rewriting the law constants.

**Sentinel state machine lives in `PortfolioState`.** Escalation requires `confirm_cycles` consecutive hits; de-escalation requires `clear_cycles` consecutive clears. `sentinel_hit_streak` / `sentinel_clear_streak` are part of the persisted JSON — don't reset them without understanding the hysteresis contract. RED triggers kill-switch close + `halted=True`; once halted, `run_once` returns immediately until state is manually cleared.

**ExitEngine and SafetySentinel share a level, but ExitEngine has its own hard breaker.** Sentinel level feeds `ExitEngine.apply` to pick trail multiplier (`trail_mult_green/yellow/orange`, and `trail_mult_orange_losing` when R < 0). No new positions may open under ORANGE/RED — that veto is in `CouncilEngine.run_once`, not in Guardian. Independent of sentinel level, `ExitEngine.apply` force-closes any open position when unrealized `R <= -2.0` with reason `HARD_DRAWDOWN_CIRCUIT_BREAKER` — this is a per-position drawdown floor that fires before sentinel can escalate to RED, and it must keep firing in GREEN. **Trend vs MeanReversion conflict collapses to HOLD by design** in the Judge — that veto is intentional, not a bug to fix.

**Funding evidence is "missing", not "neutral".** When `FundingRateProvider` returns `funding_unavailable:*`, `evaluate_asset_policy` treats funding as absent from the orthogonal-evidence count, not as a neutral confirming signal. Tests in `FragilityAuditIntelTests` lock this in — don't "helpfully" coerce missing funding into a default value.

**Anti-crowding doctrine (`council_intel.py`).** `evaluate_asset_policy` maps `(watchlist, market-move, smart-money, dune, funding-signal, funding-turning)` tuples to `{no_trade, watch, investigate}`. The doctrine: a lone market move → `no_trade` + `crowding_risk=high` (public signal, no edge). `investigate` requires orthogonal evidence (watchlist + at least two of smart-money/dune/funding-turning). This logic is consumed by `build_markdown_report`'s `## Investigate Candidates` / `## No-Trade Assets` / `## Crowding Warnings` sections — keep the policy function and markdown renderer in sync.

**Provider bundle pattern.** `run_intel_cycle` accepts an optional `ProviderBundle`; tests inject `StaticCoinGeckoProvider` / `StaticOverlayProvider` for hermetic runs. Production path (`build_default_provider_bundle`) wires real HTTP providers. When adding a provider, implement `fetch_symbol_data(symbol, query_pack=None)` + `describe()` and update `ProviderBundle` — the runner does not care about transport.

**Funding source is single-domain.** `FundingRateProvider` reads CoinGecko `/derivatives`, filters rows per asset symbol and preferred exchange market, then builds a funding snapshot. `total_deadline` caps wall-clock latency for the whole funding fetch; on timeout/failure it degrades to `funding_unavailable:*` without breaking the intel cycle. `_is_turning` still detects sign-flips/directional reversals when previous cycle funding exists.

## Development workflow

The plan in `docs/plans/2026-03-23-anti-crowding-council-doctrine.md` sets the working rhythm: **strict TDD per task** — write red test → verify it fails → minimal implementation → verify it passes → commit, one feature per commit. Follow that rhythm for new features in either `council_v2.py` or `council_intel.py`. `docs/plans/2026-04-19-council-fragility-audit.md` is the most recent doctrine update — it records the five fragility scenarios, which were `[FIXED]` vs. `[BY_DESIGN]` / `[WONTFIX: conservative-by-design]`, and the tests that lock those decisions in (`FragilityAuditIntelTests` in `tests/test_council_intel.py`, `CouncilFragilityAuditTests` in `tests/test_council_v2.py`). Read it before touching guardian, sentinel, exit, or funding-evidence logic.

Commits in this repo are manual and untagged; there is no CI. When tests pass locally, that is the gate.

## Context files at repo root

- `Council_Agent_System_Consolidated_Paper.pdf` and the `Council Agent System Overview.canvas` / `Council Vision.canvas` files are the design doctrine Ethan works from. The model-specific folders (`Claude 4.5 thinking/`, `Gemini/`, `DeepSeek/`, etc.) are AI research artifacts, not code — ignore them unless asked.
- `memory/` contains handoff notes between sessions. Read the most recent `*-session-archive.md` before responding to ambiguous requests about market state or Ethan's current focus.
