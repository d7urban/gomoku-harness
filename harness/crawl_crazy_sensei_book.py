#!/usr/bin/env python3
"""Polite crawler for the Crazy Sensei 15x15 Gomoku opening book.

The site exposes each continuation as a plain hyperlink, so this crawler walks the
move tree up to a requested ply limit and exports seed positions in harness
coordinates (`x`, `y`, zero-indexed, origin top-left).

Each exported opening is written as a standalone `gomokuzero-qt-save` file so the
Zero GUI can load it directly, and a manifest indexes the generated files.

To stay polite, the crawler:
- sleeps between requests,
- uses a single serial request stream,
- bounds the number of fetched pages,
- prefers larger book branches first when a full crawl would be too wide.
"""

from __future__ import annotations

import argparse
import heapq
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOK_ROOT = "https://www.crazy-sensei.com/book/gomoku_15x15/"
CRAZY_COLUMNS = "ABCDEFGHJKLMNOP"
PLAYER1 = 1
PLAYER2 = -1
BOOK_MOVE_RE = re.compile(r"^(?P<col>[A-HJ-P])(?P<row>1[0-5]|[1-9])$")
BOOK_ROW_RE = re.compile(
    r'<tr\s+id="(?P<move>[A-HJ-P](?:1[0-5]|[1-9]))_tr">\s*'
    r'<td[^>]*>\s*(?P<rank>\d+)\s*</td>\s*'
    r'<td[^>]*>\s*<a[^>]*href="(?P<href>[^"]+)">\s*(?P=move)\s*</a>\s*</td>\s*'
    r'<td[^>]*>\s*(?P<value>-?\d+(?:\.\d+)?)\s*</td>\s*'
    r'<td[^>]*>\s*(?P<size>\d+)\s*</td>\s*'
    r'</tr>',
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class ChildLink:
    move: str
    rank: int
    value: float
    size: int
    href: str


@dataclass(frozen=True)
class SeedMove:
    ply: int
    notation: str
    x: int
    y: int


@dataclass(frozen=True)
class SeedLine:
    path: str
    url: str
    ply: int
    terminal_reason: str
    last_move_value: float | None
    last_move_size: int | None
    path_min_size: int | None
    moves: list[SeedMove]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max-ply", type=int, default=8, help="Maximum opening ply to crawl and export.")
    parser.add_argument("--delay-s", type=float, default=0.5, help="Minimum delay between HTTP requests.")
    parser.add_argument("--max-pages", type=int, default=600, help="Maximum number of book pages to fetch, including the root page.")
    parser.add_argument("--min-size", type=int, default=12, help="Skip child links whose reported Size is below this threshold.")
    parser.add_argument("--timeout-s", type=float, default=30.0, help="Per-request timeout.")
    parser.add_argument("--output-dir", type=Path, default=None, help="Directory where manifest.json and per-seed save files are written.")
    args = parser.parse_args()
    if args.max_ply < 1:
        parser.error("--max-ply must be >= 1")
    if args.delay_s < 0:
        parser.error("--delay-s must be >= 0")
    if args.max_pages < 1:
        parser.error("--max-pages must be >= 1")
    if args.min_size < 0:
        parser.error("--min-size must be >= 0")
    if args.timeout_s <= 0:
        parser.error("--timeout-s must be > 0")
    if args.output_dir is None:
        args.output_dir = REPO_ROOT / "results" / f"crazy_sensei_gomoku_15x15_depth{args.max_ply}"
    return args


def move_to_xy(notation: str) -> tuple[int, int]:
    match = BOOK_MOVE_RE.match(notation)
    if match is None:
        raise ValueError(f"invalid Crazy Sensei move: {notation}")
    x = CRAZY_COLUMNS.index(match.group("col"))
    row_from_bottom = int(match.group("row"))
    y = 15 - row_from_bottom
    return (x, y)


def path_to_seed_moves(path_moves: tuple[str, ...]) -> list[SeedMove]:
    out: list[SeedMove] = []
    for index, notation in enumerate(path_moves, start=1):
        x, y = move_to_xy(notation)
        out.append(SeedMove(ply=index, notation=notation, x=x, y=y))
    return out


def book_path_from_url(url: str) -> str:
    parsed = urlparse(url)
    prefix = "/book/gomoku_15x15/"
    if not parsed.path.startswith(prefix):
        raise ValueError(f"unexpected book URL path: {url}")
    return parsed.path[len(prefix):].strip("/")


def parse_children(html: str) -> list[ChildLink]:
    out: list[ChildLink] = []
    for match in BOOK_ROW_RE.finditer(html):
        out.append(
            ChildLink(
                move=match.group("move"),
                rank=int(match.group("rank")),
                value=float(match.group("value")),
                size=int(match.group("size")),
                href=match.group("href"),
            )
        )
    return out


class PoliteFetcher:
    def __init__(self, *, delay_s: float, timeout_s: float) -> None:
        self.delay_s = delay_s
        self.timeout_s = timeout_s
        self._last_request_started = 0.0

    def fetch(self, url: str) -> str:
        for attempt in range(3):
            wait_s = self.delay_s - (time.monotonic() - self._last_request_started)
            if wait_s > 0:
                time.sleep(wait_s)

            self._last_request_started = time.monotonic()
            request = Request(
                url,
                headers={
                    "User-Agent": "gomoku-harness-crawler/0.1 (+https://www.crazy-sensei.com/book/gomoku_15x15/)"
                },
            )
            try:
                with urlopen(request, timeout=self.timeout_s) as response:
                    charset = response.headers.get_content_charset() or "utf-8"
                    return response.read().decode(charset, errors="replace")
            except HTTPError as exc:
                if exc.code in {429, 500, 502, 503, 504} and attempt < 2:
                    time.sleep(max(self.delay_s, 2.0 * (attempt + 1)))
                    continue
                raise
            except URLError:
                if attempt < 2:
                    time.sleep(max(self.delay_s, 2.0 * (attempt + 1)))
                    continue
                raise
        raise RuntimeError(f"failed to fetch {url}")


def make_output_payload(
    *,
    args: argparse.Namespace,
    pages_fetched: int,
    visited_paths: set[str],
    seeds: list[SeedLine],
    frontier_size: int,
    stopped_due_to_page_cap: bool,
) -> dict[str, Any]:
    return {
        "source": BOOK_ROOT,
        "crawled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "request_delay_s": args.delay_s,
        "max_ply": args.max_ply,
        "max_pages": args.max_pages,
        "min_size": args.min_size,
        "pages_fetched": pages_fetched,
        "unique_positions_visited": len(visited_paths),
        "seed_count": len(seeds),
        "frontier_remaining": frontier_size,
        "stopped_due_to_page_cap": stopped_due_to_page_cap,
        "seeds": [asdict(seed) for seed in sorted(seeds, key=lambda item: (item.ply, item.path))],
    }


def seed_to_gomokuzero_save(seed: SeedLine, *, saved_at: float) -> dict[str, Any]:
    move_history = []
    for move in seed.moves:
        player = PLAYER1 if move.ply % 2 == 1 else PLAYER2
        move_history.append([move.y, move.x, player])
    return {
        "format": "gomokuzero-qt-save",
        "version": 1,
        "board_size": 15,
        "saved_at": saved_at,
        "human_only_mode": True,
        "human_player": PLAYER1,
        "game_over": False,
        "move_history": move_history,
        "weight_mode": "best",
        "weight_path": "",
        "loaded_weight_file": "",
        "difficulty_mode": "medium",
        "custom_sims": 500,
        "side_combo_player": PLAYER1,
        "analysis_enabled": False,
    }


def seed_filename(seed: SeedLine, index: int) -> str:
    slug = seed.path.replace(",", "__")
    return f"seed_{index:04d}_{slug}.json"


def node_priority(depth: int, path_min_size: int, path: str) -> tuple[int, int, str]:
    # Under a page cap, depth-first expansion is more useful than breadth-first:
    # it produces complete opening lines instead of spending the whole budget on
    # early, very wide plies. Size remains the tiebreak so larger branches still
    # win among equally deep nodes.
    return (-depth, -path_min_size, path)


def crawl(args: argparse.Namespace) -> dict[str, Any]:
    fetcher = PoliteFetcher(delay_s=args.delay_s, timeout_s=args.timeout_s)
    root_html = fetcher.fetch(BOOK_ROOT)
    pages_fetched = 1

    frontier: list[tuple[tuple[int, int, str], int, dict[str, Any]]] = []
    serial = 0
    visited_paths: set[str] = {""}
    seeds: list[SeedLine] = []

    for child in parse_children(root_html):
        if child.size < args.min_size:
            continue
        path_moves = (child.move,)
        path = child.move
        node = {
            "path": path,
            "url": urljoin(BOOK_ROOT, child.href),
            "moves": path_moves,
            "path_min_size": child.size,
            "last_move_value": child.value,
            "last_move_size": child.size,
            "last_move_rank": child.rank,
        }
        priority = node_priority(len(path_moves), int(node["path_min_size"]), path)
        heapq.heappush(frontier, (priority, serial, node))
        serial += 1

    while frontier:
        _priority, _serial, node = heapq.heappop(frontier)
        path = str(node["path"])
        path_moves = tuple(node["moves"])
        depth = len(path_moves)

        if depth >= args.max_ply:
            seeds.append(
                SeedLine(
                    path=path,
                    url=str(node["url"]),
                    ply=depth,
                    terminal_reason="max_ply",
                    last_move_value=float(node["last_move_value"]),
                    last_move_size=int(node["last_move_size"]),
                    path_min_size=int(node["path_min_size"]),
                    moves=path_to_seed_moves(path_moves),
                )
            )
            continue

        if pages_fetched >= args.max_pages:
            break

        html = fetcher.fetch(str(node["url"]))
        pages_fetched += 1
        children = parse_children(html)
        if not children:
            seeds.append(
                SeedLine(
                    path=path,
                    url=str(node["url"]),
                    ply=depth,
                    terminal_reason="leaf",
                    last_move_value=float(node["last_move_value"]),
                    last_move_size=int(node["last_move_size"]),
                    path_min_size=int(node["path_min_size"]),
                    moves=path_to_seed_moves(path_moves),
                )
            )
            continue

        pushed_child = False
        for child in children:
            if child.size < args.min_size:
                continue
            child_url = urljoin(str(node["url"]), child.href)
            child_path = book_path_from_url(child_url)
            if child_path in visited_paths:
                continue
            visited_paths.add(child_path)
            child_moves = path_moves + (child.move,)
            child_node = {
                "path": child_path,
                "url": child_url,
                "moves": child_moves,
                "path_min_size": min(int(node["path_min_size"]), child.size),
                "last_move_value": child.value,
                "last_move_size": child.size,
                "last_move_rank": child.rank,
            }
            priority = node_priority(len(child_moves), int(child_node["path_min_size"]), child_path)
            heapq.heappush(frontier, (priority, serial, child_node))
            serial += 1
            pushed_child = True

        if not pushed_child:
            seeds.append(
                SeedLine(
                    path=path,
                    url=str(node["url"]),
                    ply=depth,
                    terminal_reason="min_size",
                    last_move_value=float(node["last_move_value"]),
                    last_move_size=int(node["last_move_size"]),
                    path_min_size=int(node["path_min_size"]),
                    moves=path_to_seed_moves(path_moves),
                )
            )

    stopped_due_to_page_cap = bool(frontier) and pages_fetched >= args.max_pages
    return make_output_payload(
        args=args,
        pages_fetched=pages_fetched,
        visited_paths=visited_paths,
        seeds=seeds,
        frontier_size=len(frontier),
        stopped_due_to_page_cap=stopped_due_to_page_cap,
    )


def main() -> int:
    args = parse_args()
    try:
        payload = crawl(args)
    except Exception as exc:  # noqa: BLE001
        print(f"crawl failed: {exc}", file=sys.stderr)
        return 1

    output_dir = args.output_dir
    saves_dir = output_dir / "saves"
    saves_dir.mkdir(parents=True, exist_ok=True)

    sorted_seeds = sorted(
        payload["seeds"],
        key=lambda item: (item["path_min_size"] is None, -(item["path_min_size"] or 0), item["ply"], item["path"]),
    )
    saved_at = time.time()
    manifest_entries: list[dict[str, Any]] = []

    for index, raw_seed in enumerate(sorted_seeds, start=1):
        seed = SeedLine(
            path=raw_seed["path"],
            url=raw_seed["url"],
            ply=raw_seed["ply"],
            terminal_reason=raw_seed["terminal_reason"],
            last_move_value=raw_seed["last_move_value"],
            last_move_size=raw_seed["last_move_size"],
            path_min_size=raw_seed["path_min_size"],
            moves=[SeedMove(**move) for move in raw_seed["moves"]],
        )
        filename = seed_filename(seed, index)
        save_payload = seed_to_gomokuzero_save(seed, saved_at=saved_at)
        (saves_dir / filename).write_text(json.dumps(save_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        manifest_entries.append(
            {
                **asdict(seed),
                "moves": [asdict(move) for move in seed.moves],
                "save_file": str(Path("saves") / filename),
            }
        )

    payload["seeds"] = manifest_entries
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"source={payload['source']}")
    print(f"pages_fetched={payload['pages_fetched']}")
    print(f"seed_count={payload['seed_count']}")
    print(f"frontier_remaining={payload['frontier_remaining']}")
    print(f"stopped_due_to_page_cap={payload['stopped_due_to_page_cap']}")
    print(f"output_dir={output_dir}")
    print(f"manifest={manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
