#!/usr/bin/env python3
"""Helpers for harness opening-seed files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness.rules import BOARD_SIZE, RefereeBoard

OPENINGS_FORMAT = "gomoku-harness-openings"
OPENINGS_VERSION = 1


def _move_xy(raw: Any) -> tuple[int, int]:
    if isinstance(raw, dict):
        return (int(raw["x"]), int(raw["y"]))
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return (int(raw[0]), int(raw[1]))
    raise ValueError(f"invalid opening move: {raw!r}")


def normalize_opening_entry(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"invalid opening entry: {raw!r}")
    raw_moves = raw.get("moves")
    if not isinstance(raw_moves, list) or not raw_moves:
        raise ValueError("opening entry must contain a non-empty moves list")

    moves = [{"x": x, "y": y} for x, y in (_move_xy(move) for move in raw_moves)]
    validate_opening_moves(moves)

    normalized = dict(raw)
    normalized["moves"] = moves
    normalized["ply"] = len(moves)
    move_slug = "-".join(f"{move['x']}-{move['y']}" for move in moves)
    normalized.setdefault("id", f"opening-{normalized['ply']}-{move_slug}")
    normalized.setdefault("name", normalized["id"])
    return normalized


def load_openings_file(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("openings file must be a JSON object")
    if payload.get("format") != OPENINGS_FORMAT:
        raise ValueError(f"unsupported openings format: {payload.get('format')!r}")
    version = int(payload.get("version", 0))
    if version != OPENINGS_VERSION:
        raise ValueError(f"unsupported openings version: {version}")
    raw_openings = payload.get("openings")
    if not isinstance(raw_openings, list) or not raw_openings:
        raise ValueError("openings file must contain a non-empty openings list")
    return [normalize_opening_entry(raw) for raw in raw_openings]


def write_openings_file(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def validate_opening_moves(moves: list[dict[str, int]] | list[tuple[int, int]]) -> None:
    board = RefereeBoard()
    for index, raw_move in enumerate(moves, start=1):
        x, y = _move_xy(raw_move)
        placed = board.place(board.next_color(), x, y)
        if not placed.legal:
            raise ValueError(f"illegal opening move at ply {index}: {x},{y} ({placed.reason})")
        if placed.winner is not None:
            raise ValueError(f"opening move at ply {index} ends the game: {x},{y}")
        if placed.draw:
            raise ValueError(f"opening move at ply {index} fills the board: {x},{y}")


def transform_move(x: int, y: int, symmetry: str) -> tuple[int, int]:
    last = BOARD_SIZE - 1
    if symmetry == "identity":
        return (x, y)
    if symmetry == "rot90":
        return (last - y, x)
    if symmetry == "rot180":
        return (last - x, last - y)
    if symmetry == "rot270":
        return (y, last - x)
    if symmetry == "mirror_vertical":
        return (last - x, y)
    if symmetry == "mirror_horizontal":
        return (x, last - y)
    if symmetry == "mirror_main_diag":
        return (y, x)
    if symmetry == "mirror_anti_diag":
        return (last - y, last - x)
    raise ValueError(f"unknown symmetry: {symmetry}")


def all_symmetry_names() -> tuple[str, ...]:
    return (
        "identity",
        "rot90",
        "rot180",
        "rot270",
        "mirror_vertical",
        "mirror_horizontal",
        "mirror_main_diag",
        "mirror_anti_diag",
    )


def transform_moves(moves: list[dict[str, int]] | list[tuple[int, int]], symmetry: str) -> list[dict[str, int]]:
    return [{"x": tx, "y": ty} for tx, ty in (transform_move(*_move_xy(move), symmetry) for move in moves)]


def canonicalize_moves(moves: list[dict[str, int]] | list[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    variants = []
    for symmetry in all_symmetry_names():
        transformed = tuple(transform_move(*_move_xy(move), symmetry) for move in moves)
        variants.append(transformed)
    return min(variants)


def canonical_key(moves: list[dict[str, int]] | list[tuple[int, int]]) -> str:
    canonical = canonicalize_moves(moves)
    return ";".join(f"{x},{y}" for x, y in canonical)
