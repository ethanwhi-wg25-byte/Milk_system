# Council Agent System

## Project Overview

The Council Agent System is a two-track crypto decision system. It is strictly **paper/read-only** and does not execute writes to any exchange. The project is built entirely using the Python 3 standard library, intentionally avoiding third-party dependencies to remain lightweight and self-contained.

The system consists of two primary tracks:
*   **`council_v2.py` (Paper-Trading Loop):** Simulates a trading lifecycle using a multi-agent consensus model. It employs five heuristic agents, a "Judge" for consensus, a "PhilosophyGuardian" to enforce strict "Iron Laws" (e.g., maximum leverage, stop-loss modes), and a "SafetySentinel" that monitors market regimes (GREEN/YELLOW/ORANGE/RED) and acts as a kill-switch. State is persisted locally to JSON/JSONL files.
*   **`council_intel.py` (Intelligence CLI):** A read-only data gatherer that pulls from sources like CoinGecko, Nansen (smart-money), Dune analytics, and perpetual funding rates. It evaluates assets based on an "anti-crowding doctrine" (categorizing them as `no_trade`, `watch`, or `investigate`) and generates timestamped markdown and JSON reports.

Additionally, `sim.py` acts as a deterministic A/B testing harness that compares trading outcomes with the Safety Sentinel turned on versus off.

## Building and Running

Because the project relies solely on the Python 3 standard library, there is no package manager or lockfile (do not use `pip install`).

**Paper Trading Loop**
*   Run the main paper-trading loop:
    ```bash
    python3 council_v2.py run --symbol BTC/USDT --sentinel on --interval 60
    ```
*   Evaluate the sentinel on a simulated snapshot:
    ```bash
    python3 council_v2.py sentinel --symbol BTC/USDT
    ```
*   Analyze a generated log:
    ```bash
    python3 council_v2.py analyze --log council_log_v2.jsonl
    ```

**Intelligence CLI**
*   Generate an intelligence report:
    ```bash
    python3 council_intel.py run --config config/universe.json --out-dir artifacts/intel
    ```

**Simulation and Testing**
*   Run the deterministic A/B simulation:
    ```bash
    python3 sim.py
    ```
*   Run the test suite:
    ```bash
    python3 -m unittest discover tests -v
    ```

## Development Conventions

*   **Zero Dependencies Constraint:** Do not introduce third-party libraries such as `requests`, `pandas`, or `pytest`. Rely exclusively on standard library modules like `urllib`, `json`, `dataclasses`, `argparse`, and `unittest`.
*   **Test-Driven Development (TDD):** New features must follow a strict TDD rhythm: write a failing red test, verify it fails, implement the minimal solution, verify it passes, and commit.
*   **Architectural Guardrails:**
    *   **Iron Laws:** Do not relax the defaults in `PhilosophyGuardian.evaluate` for production. They are strict contracts for trade vetoes.
    *   **Anti-Crowding Doctrine:** Ensure the evaluation policy logic in `council_intel.py` always stays in sync with the markdown report rendering.
    *   **Sentinel State:** State hysteresis (like `sentinel_hit_streak`) is persisted; do not reset it without adhering to the documented escalation/de-escalation cycles.
*   **Handoff Notes:** Check the `memory/` directory for the most recent `*-session-archive.md` files to understand the current context and focus before proceeding with ambiguous requests.