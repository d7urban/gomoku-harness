# gomoku-harness

A Python coordinator for running head-to-head matches between gomoku engines that speak the
[Gomocup protocol](https://plastovicka.github.io/protocl2en.htm). Built to answer concrete
strength questions — calibration sweeps, time-control round-robins, ablation matches — with
machine-readable per-game logs and reproducible openings.

Scope: **freestyle 15×15** only. No swap rules, forbidden moves, or overlines. See
[`PLAN.md`](PLAN.md) for the design rationale and [`docs/protocol.md`](docs/protocol.md) for
the wire format.

## Engines under test

The harness ships adapters for two local engines, both expected as sibling repos:

| Adapter | Engine | Sibling path |
|---|---|---|
| `adapters/zero/` | `GomokuZero-player` (TF AlphaZero-style MCTS) | `../GomokuZero-player` |
| `adapters/minimax/` | `gomoku-minimax` (C++20 alpha-beta / PVS) | `../gomoku-minimax` |

`adapters/minimax-v0/` and `adapters/minimax-v1/` are pinned older builds for regression work.
Any third-party Gomocup binary will also work — supply the launch command directly via
`--engine-*-cmd`.

## Layout

```
adapters/                 # Adapter shims (run.sh + optional engine.py wrapper)
docs/protocol.md          # Gomocup subset every adapter must implement
harness/
  run_match.py            # Single head-to-head match driver
  sweep.py                # Calibration sweep (Zero difficulty × minimax time-budget)
  time_control_round_robin.py  # Round-robin between time-control presets
  ab_sweep.py             # A/B sweep utility
  openings.py             # Opening-seed file format + helpers
  rules.py                # Freestyle-15 referee
  build_*.py / evaluate_*.py / filter_*.py / extract_*.py
                          # Opening-set construction & curation tools
  *_test.py               # Unit / smoke tests
results/                  # All run outputs land here (per-run subdirectory)
main.py                   # Placeholder entrypoint (unused)
```

## Setup

Requires Python ≥ 3.13 and [uv](https://docs.astral.sh/uv/). The harness itself has no
runtime dependencies; the Zero adapter reuses `../GomokuZero-player/.venv`, and the minimax
adapter expects a pre-built `gomoku_gomocup` binary in
`../gomoku-minimax/build-release/` (or `build/`).

```sh
uv sync
```

Build the minimax engine in its own repo before running anything that uses it:

```sh
cmake -S ../gomoku-minimax -B ../gomoku-minimax/build-release \
      -DCMAKE_BUILD_TYPE=Release -DGOMOKU_BUILD_SFML_UI=OFF
cmake --build ../gomoku-minimax/build-release --target gomoku_gomocup
```

## Quickstart

### One head-to-head match

```sh
uv run python -m harness.run_match \
    --engine-a-cmd "./adapters/minimax/run.sh --controller expert --time-ms 1000 --threads 4" \
    --engine-a-name minimax \
    --engine-b-cmd "./adapters/zero/run.sh --sims 500" \
    --engine-b-name zero \
    --games 4 --swap-colors \
    --openings-file results/crazy_sensei_openings_253.json
```

Outputs land under `results/match_<timestamp>/` (one JSON per game plus a
`tournament.json` summary).

### Calibration sweep (Q1 from PLAN.md)

Find the `minimax_time_ms` setting that ties Zero at a given difficulty:

```sh
uv run python -m harness.sweep \
    --difficulty easy \
    --times-ms 50,100,250,500,1000,2500,5000 \
    --games-per-cell 20 \
    --bisect --bisect-rounds 3
```

Or with v2 period clocks:

```sh
uv run python -m harness.sweep \
    --difficulty easy \
    --protocol-version v2 \
    --preset-cells blitz,fast,slow \
    --games-per-cell 20
```

### Time-control round-robin

Pit the same minimax build against itself across multiple period clocks:

```sh
uv run python -m harness.time_control_round_robin \
    --presets blitz,fast,slow \
    --games-per-pair 80 \
    --openings-file results/crazy_sensei_openings_253.json \
    --label no_book
```

For the current gomoku-minimax quiescence/time-governor comparison, run the
same script against a direct `gomoku_gomocup` binary and the balanced
round-robin opening set:

```sh
python3 harness/time_control_round_robin.py \
    --engine-cmd "../gomoku-minimax/build/gomoku_gomocup --threads 16" \
    --presets "blitz,fast" \
    --games-per-pair 20 \
    --openings-file results/crazy_sensei_openings_balanced_rr_20260424.json \
    --label semanticfix_blitz_vs_fast_Qfix-TMfix-20g
```

Built-in presets (in `time_control_round_robin.py` and `sweep.py`):

| preset | period clock | avg per move |
|---|---|---:|
| `blitz` | 5:00 / 40 moves | 7.5 s |
| `fast` | 15:00 / 60 moves | 15 s |
| `slow` | 60:00 / 60 moves | 60 s |

## Protocol versions

- **v1** (`--protocol-version v1`, default for `run_match`): legacy fixed-turn — the harness
  pushes `INFO timeout_turn` once per game, then plays.
- **v2**: live period clock — the harness tracks each side's clock independently and sends
  `INFO time_left` / `INFO moves_to_reset` before every move, so the engine can budget its
  own per-move thinking. Required for the time-control round-robin and the preset sweep.

Both modes use the same Gomocup command set documented in `docs/protocol.md`.

## Output format

Every run writes a self-contained directory under `results/`. A typical layout
(time-control round-robin):

```
results/time_control_round_robin_<label>_<timestamp>/
  Results.md                # Human-readable standings + per-pair breakdown
  tournament_config.json    # Engine command, presets, openings, games-per-pair, …
  standings.{json,csv}
  pair_results.{json,csv}
  games.jsonl               # One line per game (winner, durations, opening, …)
  <pair>/
    game_001.json           # Per-game record:
                            #   - black/white engine specs + ABOUT line
                            #   - full transcript (commands & responses, with timestamps)
                            #   - parsed engine_search_records (one per move) + summary
                            #   - moves, opening, final board, termination
    game_002.json …
```

`engine_search_records` are parsed from the `[minimax]` stderr lines emitted by the minimax
engine — one structured record per played move with depth, nodes, NPS, soft/hard caps,
`stop_reason`, panic flag, defensive-filter outcome, PV, etc. This is what every analysis
script in the repo reads.

## Openings

Openings are JSON files in the `gomoku-harness-openings` v1 format (see
`harness/openings.py`). The harness cycles through them deterministically and (in the
round-robin) plays each opening twice per pairing with colors swapped.

Curation pipeline:

```
build_openings_from_crazy_sensei.py   # Crawl Crazy Sensei book → raw opening manifest
evaluate_openings_with_zero.py        # Score openings with Zero MCTS (color balance)
build_balanced_openings.py            # Mirror each opening into adjacent slots
filter_openings_by_results.py         # Keep only openings whose results stayed balanced
```

Two reusable sets ship in `results/`:
`crazy_sensei_openings_253.json` (the default) and
`crazy_sensei_openings_balanced_rr_20260424.json`.

## Tests

```sh
uv run python -m unittest discover -s harness -p '*_test.py'
uv run python harness/smoke_test.py            # Integration smoke (uses test_engine.py)
uv run python adapters/zero/smoke_test.py      # Adapter smoke (boots TF, ~10 s)
uv run python adapters/minimax/smoke_test.py
```

`harness/test_engine.py` is a deterministic stub Gomocup engine used by the harness smoke
tests — it doesn't exercise either real engine.

## Adding an engine

1. Implement the Gomocup subset in `docs/protocol.md` (mandatory commands plus `RESTART`).
2. Drop a launch script at `adapters/<name>/run.sh` that execs the binary.
3. Pass it via `--engine-a-cmd` / `--engine-b-cmd`. No code changes in the harness needed.
