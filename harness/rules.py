#!/usr/bin/env python3
"""Minimal freestyle 15x15 rules used by the harness referee."""

from __future__ import annotations

from dataclasses import asdict, dataclass

BOARD_SIZE = 15
EMPTY = 0
BLACK = 1
WHITE = 2

COLOR_NAMES = {
    BLACK: "black",
    WHITE: "white",
}

WIN_DIRECTIONS = (
    (1, 0),
    (0, 1),
    (1, 1),
    (1, -1),
)


@dataclass(frozen=True)
class RefereeMove:
    ply: int
    color: int
    x: int
    y: int


@dataclass(frozen=True)
class PlaceResult:
    legal: bool
    reason: str | None = None
    move: RefereeMove | None = None
    winner: int | None = None
    draw: bool = False


class RefereeBoard:
    """Owns the referee state for a freestyle 15x15 game."""

    def __init__(self, size: int = BOARD_SIZE) -> None:
        if size != BOARD_SIZE:
            raise ValueError(f"only {BOARD_SIZE}x{BOARD_SIZE} is supported")
        self.size = size
        self.grid = [[EMPTY for _ in range(size)] for _ in range(size)]
        self.moves: list[RefereeMove] = []
        self.winner: int | None = None

    def next_color(self) -> int:
        return BLACK if len(self.moves) % 2 == 0 else WHITE

    def is_full(self) -> bool:
        return len(self.moves) == self.size * self.size

    def is_inside(self, x: int, y: int) -> bool:
        return 0 <= x < self.size and 0 <= y < self.size

    def cell(self, x: int, y: int) -> int:
        if not self.is_inside(x, y):
            raise IndexError(f"out of bounds: {x},{y}")
        return self.grid[y][x]

    def place(self, color: int, x: int, y: int) -> PlaceResult:
        if self.winner is not None:
            return PlaceResult(legal=False, reason="game is already over")
        if color not in COLOR_NAMES:
            return PlaceResult(legal=False, reason=f"invalid color: {color}")
        if color != self.next_color():
            expected = COLOR_NAMES[self.next_color()]
            return PlaceResult(legal=False, reason=f"wrong side to move, expected {expected}")
        if not self.is_inside(x, y):
            return PlaceResult(legal=False, reason=f"move out of bounds: {x},{y}")
        if self.grid[y][x] != EMPTY:
            return PlaceResult(legal=False, reason=f"cell is occupied: {x},{y}")

        self.grid[y][x] = color
        move = RefereeMove(ply=len(self.moves) + 1, color=color, x=x, y=y)
        self.moves.append(move)

        winner = color if self._has_five(x, y, color) else None
        if winner is not None:
            self.winner = winner
        draw = winner is None and self.is_full()
        return PlaceResult(legal=True, move=move, winner=winner, draw=draw)

    def move_dicts(self) -> list[dict[str, int]]:
        return [asdict(move) for move in self.moves]

    def final_board_rows(self) -> list[str]:
        symbols = {
            EMPTY: ".",
            BLACK: "X",
            WHITE: "O",
        }
        return ["".join(symbols[cell] for cell in row) for row in self.grid]

    def render_ascii(self) -> str:
        header = "   " + " ".join(f"{col:02d}" for col in range(self.size))
        rows = [header]
        for row_index, row in enumerate(self.grid):
            cells = " ".join(self._symbol(cell) for cell in row)
            rows.append(f"{row_index:02d} {cells}")
        return "\n".join(rows)

    def _symbol(self, cell: int) -> str:
        if cell == BLACK:
            return "X"
        if cell == WHITE:
            return "O"
        return "."

    def _has_five(self, x: int, y: int, color: int) -> bool:
        for dx, dy in WIN_DIRECTIONS:
            count = 1 + self._count_one_way(x, y, dx, dy, color) + self._count_one_way(x, y, -dx, -dy, color)
            if count >= 5:
                return True
        return False

    def _count_one_way(self, x: int, y: int, dx: int, dy: int, color: int) -> int:
        total = 0
        cx = x + dx
        cy = y + dy
        while self.is_inside(cx, cy) and self.grid[cy][cx] == color:
            total += 1
            cx += dx
            cy += dy
        return total
