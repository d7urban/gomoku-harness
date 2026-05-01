# Gomoku Harness — Plan

Goal: run head-to-head matches between two existing gomoku engines:

- `../GomokuZero-player` — Python 3 + TensorFlow AlphaZero-style MCTS (`AIPlayer` in `entrypoint_shared.py`, game in `gomoku.py`, 15×15, freestyle rules).
- `../gomoku-minimax` — C++20 alpha-beta / PVS engine with multiple controller levels (`gomoku_cli` in `src/cli/main.cpp`, self-play tool in `src/tools/selfplay.cpp`). Supports `freestyle15`, `standard15`, `swap16`.

**Scope: freestyle 15×15 only.** No swap openings, no forbidden moves, no overline restrictions — any 5+ in a row wins for either side. This is the only ruleset the harness will support; out-of-scope rulesets are not in any milestone.

## Tournament questions

The harness exists to answer concrete head-to-head questions; the design must support them, not just "two engines can play".

**Q1 (primary): For each GomokuZero difficulty (`easy` = 250 sims, `medium` = 500 sims, `hard` = 2000 sims), what `aiMoveTimeMs` setting on minimax `expert` produces a 50/50 result?**

This is a calibration sweep — Zero's strength is fixed per difficulty, minimax's only knob (with controller pinned to `expert`) is the per-move time budget. The harness must therefore make it cheap to:
- Run many games per `(zero_difficulty, minimax_time_ms)` cell with color alternation.
- Sweep `minimax_time_ms` (e.g. 50, 100, 250, 500, 1000, 2500, 5000 ms) then bisect around the crossover.
- Report a win-rate matrix with confidence intervals, and the interpolated 50% point per Zero difficulty.

Implication for the design: keep engine processes alive across games (`RESTART`), parameterize move-time per game from the harness CLI rather than per-engine launch flags, and emit machine-readable per-game results so a sweep driver can aggregate them.

## Approach

Use a **coordinator + engine-adapter** design (the pattern used by chess UCI / Gomocup managers). The harness does not embed either engine; it spawns each engine as a subprocess and speaks a line-oriented text protocol over stdin/stdout. This keeps the Python/TF runtime and the C++ binary isolated, and means either engine can be upgraded or swapped without touching the other.

Picking a protocol: **Gomocup protocol** ([spec](https://plastovicka.github.io/protocl2en.htm)) is the de-facto standard for gomoku engines. Coordinates are `(X, Y)` zero-indexed with origin top-left (`X` = column, `Y` = row). Use it so the harness can later pit either engine against third-party Gomocup engines / managers.

Mandatory commands to implement on each adapter: `START [size]`, `BEGIN`, `TURN [X],[Y]`, `BOARD … DONE`, `INFO [key] [value]`, `END`, `ABOUT`. Brain responses use `[X],[Y]`, plus `OK` / `ERROR` / `UNKNOWN` / `MESSAGE` / `DEBUG`.

Optional commands worth implementing now:
- **`RESTART`** — lets the harness reuse a live process across games in a tournament. Critical for the TF-backed engine where model load is multi-second.
- **`INFO timeout_turn` / `INFO timeout_match` / `INFO max_memory`** — use these to push per-engine time/memory limits instead of inventing custom flags or env vars; keeps both adapters compatible with third-party Gomocup managers.

Out of scope: `SWAP2BOARD` (swap16 not supported), `RECTSTART` (15×15 only), `TAKEBACK`, `PLAY`.

## Milestones

### M0 — Decisions & scaffolding ✅
- [x] Lock ruleset to **freestyle15**. Hardcode at the harness boundary; reject any other ruleset hint from either engine.
- [x] Confirm protocol: Gomocup over stdio (`(X,Y)` 0-indexed, `X` = column, `Y` = row, origin top-left). Mandatory subset + `RESTART` + `INFO timeout_turn`/`timeout_match`/`max_memory`. Documented in [`docs/protocol.md`](docs/protocol.md).
- [x] Harness implementation language: **Python 3**. Easy subprocess + async I/O, no build step, natural place for stats/plotting; the rules referee is small enough that sharing C++ types with `gomoku-minimax` is not worth the build coupling.
- [x] Repo layout: `harness/` (coordinator), `adapters/zero/` (Python wrapper around GomokuZero), `adapters/minimax/` (Gomocup binary built inside `gomoku-minimax`, plus any launch shim here), `docs/`, `results/`.

### M1 — GomokuZero adapter ✅
- [x] `adapters/zero/engine.py` — Gomocup stdio loop wrapping `AIPlayer` over a `GomokuGame`. Handles `START 15`, `BEGIN`, `TURN`, `BOARD … DONE`, `INFO` (unknown keys silently ignored), `RESTART` (fresh `GomokuGame`, model retained), `END`, `ABOUT`. Wire `(X, Y)` ↔ board `(col, row)` translation centralized in the command handlers.
- [x] `adapters/zero/run.sh` launches via the existing `GomokuZero-player/.venv` interpreter; no second venv to maintain.
- [x] `adapters/zero/smoke_test.py` exercises START → ABOUT → INFO → BEGIN → TURN → RESTART → BEGIN → END and asserts legality. Passes end-to-end (TF model loads in ~3-8s, RESTART avoids reload).
- [x] `adapters/zero/calibrate_sims.py` benchmarks MCTS throughput on this machine. Result: throughput is affine and position-dependent (~0.4–2.7 sims/ms; setup ~30–80 ms per call). No single ratio is safe across all positions.

**Design note (refined during calibration):** Zero's strength is naturally controlled by sim count, not wall-clock — that's how its difficulty levels are defined (easy=250, medium=500, hard=2000). So `--sims` is the **primary** control and `INFO timeout_turn` is a **safety cap**: the adapter plays the configured sim count unless the timeout-derived cap is lower. Calibrated cap: `sims_cap = max(MIN_SIMS, (timeout_turn_ms − 80) × 1.0)`, conservative so it stays under budget for typical positions. This means the Q1 sweep should launch Zero with `--sims {250|500|2000}` and a comfortable `INFO timeout_turn`; the timeout knob is for minimax.

### M2 — gomoku-minimax adapter ✅
Built directly from the existing `Match` API; `gomoku_cli` is left untouched.
- [x] `gomoku-minimax/src/tools/gomoku_gomocup.cpp` — stdio Gomocup loop. (Named `gomoku_gomocup` to avoid collision with the existing `gomoku_engine` library target.) Constructs a `Match` with both seats set to the chosen AI controller; on `BEGIN`/`TURN` calls `stepAi()` and reports `state().lastPlacedMove()`; opponent moves applied via `Match::applyMove`. Handles `START`, `BEGIN`, `TURN`, `BOARD … DONE`, `INFO`, `RESTART`, `END`, `ABOUT`.
- [x] Wire `(X, Y)` ↔ `gomoku::Move{row=Y, col=X}` mapping centralized in `Engine::makeMove` and the reply path. Internal display is bottom-left, wire is top-left — no flip applied (gomoku is reflection-symmetric, only consistency matters).
- [x] `INFO timeout_turn <ms>` updates `MatchConfig::aiMoveTimeMs`. Controller level set at launch via `--controller {rookie|club|tactical|expert|analyst}`; `--time-ms` for the initial budget.
- [x] `RESTART` rebuilds the `Match` (cheap; also lets a config update from `INFO` take effect cleanly). `END` exits.
- [x] CMake target `gomoku_gomocup` added to `gomoku-minimax/CMakeLists.txt`, builds with the headless target set (no SFML dependency).
- [x] `adapters/minimax/run.sh` launches the built binary; `adapters/minimax/smoke_test.py` walks START → ABOUT → INFO → BEGIN → TURN → RESTART → TURN(reusing previously-occupied cell) → END and asserts both legality and that RESTART actually clears state. Confirmed `INFO timeout_turn` flows through to search time (107 ms at 100 ms budget vs 1017 ms at 1000 ms budget on the same position).

### M3 — Harness coordinator ✅
- [x] `harness/run_match.py` spawns both engines, sends `START 15`, per-engine `INFO timeout_turn` / `INFO timeout_match` (and optional `INFO max_memory`), picks who moves first, then loops `BEGIN`/`TURN` between sides with own-side legality refereeing. Forwards each move to the opponent.
- [x] Between games, sends `RESTART` (no respawn) so the TF model stays loaded.
- [x] Detects all five terminations: 5-in-a-row, board full, illegal move, engine crash (`EngineExited`), and timeout (per-turn or per-match budget exceeded with `TIMEOUT_GRACE_MS` slack). On fatal stop, exits non-zero and skips remaining games.
- [x] `harness/rules.py` is the own-side referee (15×15, 4-direction five-in-a-row check). `harness/rules_test.py` covers the win/illegal/draw paths.
- [x] CLI flags: `--engine-{a,b}-cmd/-name/-time-ms/-match-time-ms`, `--time-ms`, `--match-time-ms`, `--max-memory-bytes`, `--games`, `--swap-colors`, `--black {a|b|random}`, `--seed`, `--log-dir`.

### M4 — Tournament / reporting ✅
- [x] N games with color alternation (`--swap-colors` plus a per-game `black_slots` schedule), prints per-game one-liner and a final W/L/D + black/white split + average moves/duration.
- [x] Per-game persistence: `results/match_<timestamp>/game_NNN.json` carries move list, transcript, engine metadata, per-engine elapsed time, and a final ASCII board. The move list is the replay; no separate `.psq` written (the JSON is strictly more useful and any `.psq` rendering is a one-liner over `moves[]` if needed later).
- [x] Machine-readable per-game summary written to `games.jsonl` in the same directory (one line per game with engines, color assignment, result, termination, move count, timings). Tournament-level `summary.json` aggregates the totals.
- [x] Final board rendered as ASCII inside each `game_NNN.json` (`final_board_ascii`). PNG rendering deferred — not needed for the sweep driver.

### M5 — Calibration sweep (answers Q1) ⏳
- [x] `harness/sweep.py` driver: given `--difficulty {easy|medium|hard}` (drives Zero `--sims`) and `--times-ms 50,100,...`, runs `--games-per-cell N` games per cell with color alternation. Reuses the same `EngineProcess` pair across the entire sweep — both engines are spawned once, `RESTART` between games, TF model never reloaded.
- [x] Per-cell minimax budget pushed via `INFO timeout_turn` (Zero gets a generous safety cap; sims is the binding constraint, by design).
- [x] Output to `results/sweep_<difficulty>[_<label>]_<timestamp>/`: `cells.jsonl` (one line per cell), `games.jsonl` (one line per game), `cell_t<ms>ms/game_NNN.json` (full records with transcript), `summary.csv`, `summary.md`. Summary uses chess-scoring (win=1, draw=0.5, loss=0) with Wilson 95% CI.
- [x] Coarse-then-bisect: `--bisect` adds geometric-midpoint cells around the 50% crossing (`--bisect-rounds`, default 3). Stops early once any cell whose CI brackets 0.5 has width ≤ `--bisect-target-ci-width` (default 0.30).
- [x] Linear interpolation in `log10(time_ms)` reports the projected 50/50 minimax-expert time; degenerate cases (no crossing, or all wins/losses) get user-facing guidance ("widen --times-ms").
- [x] Monotonicity check on the assembled cells; non-monotone adjacent pairs flagged in the markdown summary and printed at end of sweep.
- [x] Bug fix found while validating: `harness/run_match.py` was DQ'ing engines that honored `INFO timeout_turn` but added a few ms of IPC/serialization overhead (e.g. minimax-expert reliably ~107 ms at a 100 ms budget). Aligned the wall-time check with the existing `TIMEOUT_GRACE_MS = 50` already applied to I/O wait.
- [ ] Run for `easy`, `medium`, `hard` and combine the three `summary.md` tables into a one-page summary: Zero difficulty → minimax `expert` ms at 50%. (Not started — requires three actual sweeps, each ~100s of games. Sweep driver is one invocation per difficulty; combining is a manual concat or a small follow-up script.)

### M6 — Stretch
- [ ] Add a third engine slot for a Gomocup reference engine (Pela, Rapfi) to benchmark both.
- [ ] Parallel game execution via multiple coordinator processes.

## Open questions / risks
- **Coordinate convention.** GomokuZero uses `(row, col)` with row 0 at the top; `gomoku-minimax`'s text UI puts row 1 at the bottom and uses letters for columns. Pin `(x=col, y=row)` with origin top-left at the protocol boundary and convert inside each adapter.
- **Legality divergence.** Freestyle15 is unambiguous (5+ in a row wins for either side), so both engines should agree. Still worth a unit test: feed a known win position, check both report the win.
- **Startup cost.** TensorFlow + model load is multi-second. Keep the Python engine process alive across games in a tournament (don't respawn per game).
- **Determinism.** Neither engine is deterministic by default (MCTS noise in Zero, time-based cutoffs in minimax). Accept that and rely on multi-game averaging, or expose a seed / fixed-depth mode on both sides if reproducibility is needed.
- **Time control.** GomokuZero is sims-based; minimax is time-based. Use Gomocup `INFO timeout_turn` as the harness-level knob; the Zero adapter translates it to a sim budget (calibrate once via a benchmark), the minimax adapter feeds it straight through. Allow per-engine override flags for cases where a fixed sim count or fixed depth is wanted.
