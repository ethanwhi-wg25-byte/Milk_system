# Repository Guidelines

## Scope And Intent

- Work from `/Users/ethanwong/Council_System`.
- This repo is a two-track crypto decision system with paper/read-only defaults. Keep production safety assumptions strict.
- The repo root mixes live app code with research artifacts. Treat model-specific folders such as `Claude 4.5 thinking/`, `Claude 4.6 thinking/`, `Gemini/`, `DeepSeek/`, `Grok/`, `Qwen/`, `ChatGPT/`, and `Perplexity/` as reference material unless a task points to them directly.

## Project Structure And Module Ownership

- `council_v2.py`: paper-trading loop with agent voting, judge, guardian, sentinel, exits, and paper broker state.
- `council_intel.py`: read-only intelligence CLI and report generation.
- `sim.py`: deterministic sentinel-on vs sentinel-off simulation harness.
- `tests/`: `unittest` coverage for trading and intel behavior.
- `config/universe.json`: default intel universe configuration.
- `artifacts/intel/<UTC>/`: generated `report.json` and `report.md` outputs.
- `council_state_v2.json`, `council_log_v2.jsonl`, `sim_base.jsonl`, `sim_sentinel.jsonl`: runtime artifacts that should be reviewed before commit.
- `docs/plans/`: implementation plans and doctrine notes.
- `memory/`: session handoff context; read the latest archive when a task depends on prior discussion or market context.
- `council_dashboard.html`: local dashboard artifact; screenshots only matter when this file changes.

## Build, Test, And Run Commands

Run from the repo root:

```bash
python3 -m unittest discover tests -v
python3 council_intel.py run --config config/universe.json --out-dir artifacts/intel
python3 council_v2.py run --symbol BTC/USDT --sentinel on --interval 60
python3 council_v2.py sentinel --symbol BTC/USDT
python3 council_v2.py analyze --log council_log_v2.jsonl
python3 sim.py
```

There is no package manager or lockfile. This project is intentionally Python standard-library only.

## Coding Style And Conventions

- Use Python 3 with 4-space indentation, type hints, `dataclass` models, and small stdlib-first helpers.
- Keep names conventional: `snake_case` for functions and variables, `CapWords` for classes, `UPPER_CASE` for thresholds and policy constants.
- Do not add third-party dependencies such as `requests`, `pandas`, or `pytest`.
- Preserve strict defaults for guardian, sentinel, and risk controls unless the task explicitly changes system policy.
- When editing `council_intel.py`, keep policy evaluation and markdown report rendering aligned.
- When editing `council_v2.py`, preserve persisted hysteresis and halt behavior unless the task explicitly requires a behavioral change.

## Testing Expectations

- Use `unittest` only.
- Add tests under `tests/test_*.py` and name methods `test_*`.
- Prefer deterministic fixtures with `tempfile`, `unittest.mock`, and static provider doubles.
- Run the full suite plus any focused tests that cover the behavior you changed.

## Commit And PR Guidance

- Use short imperative commit subjects with prefixes like `feat:`, `fix:`, `test:`, or `docs:`.
- In PRs or handoff notes, call out which track changed: `council_v2`, `council_intel`, docs, or dashboard.
- List the commands you ran and any required env vars such as `NANSEN_API_KEY` or `DUNE_API_KEY`.
- Mention generated artifacts separately if they are intentionally included.

## Security And Repo Hygiene

- Keep the system paper/read-only. Never add exchange write paths or live-trading behavior.
- Do not commit secrets.
- The repo contains many generated JSON, JSONL, HTML, and report files. Review diffs carefully before committing transient artifacts.
