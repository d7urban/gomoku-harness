# Engine ↔ Harness Protocol

The harness drives both engines using the **Gomocup protocol** ([spec](https://plastovicka.github.io/protocl2en.htm)). This document records the exact subset every adapter in this repo must implement.

## Coordinate convention

- `(X, Y)` pairs, **zero-indexed**.
- `X` = column (0..14), `Y` = row (0..14).
- Origin at the **top-left** corner.
- Board is fixed at **15×15** (freestyle).

Both engines internally use different conventions (GomokuZero: `(row, col)` top-left; gomoku-minimax text UI: letter columns, 1-indexed rows from the bottom). Translation lives **inside each adapter** so the wire format stays uniform.

## Mandatory commands (manager → brain)

| Command | Args | Brain response |
|---|---|---|
| `START [size]` | `size` is always `15` here; brain replies `ERROR` for any other value. | `OK` or `ERROR <message>` |
| `BEGIN` | (none) — brain plays the opening move | `X,Y` |
| `TURN X,Y` | Opponent just played at `(X,Y)` | `X,Y` (brain's reply move) |
| `BOARD … DONE` | Sequence of `X,Y,field` lines (`field` ∈ {1 = own, 2 = opponent, 3 = our blocking move in continuous game — unused here}). Then `DONE`. Replays a position; brain returns its move. | `X,Y` |
| `INFO key value` | See INFO keys below. No response required. | (silent; may emit `MESSAGE`/`DEBUG`) |
| `END` | Manager is done | (brain exits cleanly) |
| `ABOUT` | Identity query | Single line: `name="...", version="...", author="...", country="..."` |

## Optional commands we use

| Command | Args | Brain response | Why |
|---|---|---|---|
| `RESTART` | (none) | `OK` | Reset to an empty 15×15 board **without** tearing down the process. Critical for the TF-backed engine where model load is multi-second. The harness uses this between games in a tournament. |

## INFO keys we send

Per Gomocup, `INFO` is fire-and-forget. The harness has two protocol profiles:

- `v1` (legacy): only static `timeout_turn` / `timeout_match` settings are sent between games.
- `v2` (clocked): the same static settings are sent between games, and live clock facts are sent before each `BEGIN` / `TURN` / `BOARD`.

Static keys sent between games (after `START` / `RESTART`, before the first move command):

| Key | Units | Meaning |
|---|---|---|
| `timeout_turn` | ms | Max think time per move. Adapters must respect this. The Zero adapter translates to a sim budget; the minimax adapter passes it to `MatchConfig::aiMoveTimeMs`. |
| `timeout_match` | ms | Max think time for the whole game. Adapters may ignore if they only enforce per-turn. |
| `max_memory` | bytes | Soft cap. Both adapters may currently ignore. |

Live keys sent in `v2` immediately before the side-to-move is asked for a reply:

| Key | Units | Meaning |
|---|---|---|
| `time_left` | ms | Remaining time for the side to move in the current period. |
| `moves_to_reset` | moves | Number of moves remaining before the current period resets. |

Unknown `INFO` keys are silently ignored by the adapter (per spec). Other Gomocup keys (`game_type`, `rule`, `evaluate`, `folder`) are not sent by this harness.

## Brain → manager messages

| Line | Meaning |
|---|---|
| `X,Y` | Move reply |
| `OK` | Acknowledge `START` / `RESTART` |
| `ERROR <msg>` | Cannot execute a known command (e.g. `START 19`) |
| `UNKNOWN <msg>` | Unknown command received |
| `MESSAGE <text>` | Human-readable info (logged by harness, not parsed) |
| `DEBUG <text>` | Debug info (logged by harness, not parsed) |

## Out of scope

These exist in the Gomocup spec but the harness will **never** send them and adapters need not implement them:

- `RECTSTART` — board is fixed at 15×15.
- `SWAP2BOARD` — no swap rules supported.
- `TAKEBACK`, `PLAY` — not needed for the tournament workflow.
- Any forbidden-move / overline logic — freestyle only.

## Lifecycle: one tournament

```
manager                                 brain
  ──► START 15
                                          ◄── OK
  ──► INFO timeout_turn 500
  ──► ABOUT
                                          ◄── name="...", version="..."
  -- game 1 --
  ──► BEGIN              (or TURN x,y if brain plays second)
                                          ◄── X,Y
  ──► TURN X,Y
                                          ◄── X,Y
  ...                                     (until 5-in-a-row or board full)
  -- game 2 --
  ──► RESTART
                                          ◄── OK
  ──► INFO timeout_turn 500              (re-sent if it changed)
  ──► BEGIN / TURN
  ...
  ──► END                                (brain exits)
```

The harness — not either engine — is the referee: it tracks the board independently, validates each reply move for legality, declares the result on five-in-a-row or full board, and times out engines that exceed `timeout_turn`.

In `v2`, the harness additionally tracks each side's remaining period clock and sends:

```
  ──► INFO time_left 300000
  ──► INFO moves_to_reset 40
```

immediately before every move request. That lets clock-aware engines reproduce chess-like controls such as Blitz (`5:00 / 40`), Fast (`15:00 / 60`), and Slow (`30:00 / 80`) while leaving the original `v1` wire behavior available for older sweeps and adapters.
