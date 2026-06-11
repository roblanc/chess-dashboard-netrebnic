#!/usr/bin/env python3
"""Download all public Chess.com games for a player via the PubAPI."""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

API_BASE = "https://api.chess.com/pub/player"
USER_AGENT = "chess-dashboard-netrebnic/1.0 (github pages; personal stats)"
DEFAULT_USERNAME = "netrebnic"


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def archive_month(url: str) -> str:
    match = re.search(r"/games/(\d{4}/\d{2})$", url)
    if not match:
        raise ValueError(f"Unexpected archive URL: {url}")
    return match.group(1)


def list_archives(username: str) -> list[str]:
    data = json.loads(fetch(f"{API_BASE}/{username}/games/archives").decode())
    return data.get("archives", [])


def download_month_pgn(username: str, year_month: str) -> str:
    year, month = year_month.split("/")
    url = f"{API_BASE}/{username}/games/{year}/{month}/pgn"
    return fetch(url).decode("utf-8", errors="replace")


def export_games(username: str, output: Path, delay_s: float = 0.25) -> dict:
    archives = list_archives(username)
    if not archives:
        raise SystemExit(f"No public game archives found for '{username}'.")

    chunks: list[str] = []
    months: list[str] = []

    for archive_url in archives:
        month = archive_month(archive_url)
        months.append(month)
        print(f"Downloading {username} {month}...", flush=True)
        chunks.append(download_month_pgn(username, month))
        if delay_s:
            time.sleep(delay_s)

    combined = "\n\n".join(part.strip() for part in chunks if part.strip()) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(combined, encoding="utf-8")

    game_count = combined.count("\n\n[Event ")
    if game_count == 0 and "[Event " in combined:
        game_count = 1

    return {
        "username": username,
        "months": len(months),
        "games": game_count,
        "output": str(output),
        "archive_months": months,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username", nargs="?", default=DEFAULT_USERNAME)
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output PGN path (default: data/chesscom-<username>-all-games.pgn)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.25,
        help="Seconds to wait between monthly downloads (default: 0.25)",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    output = args.output or repo_root / "data" / f"chesscom-{args.username}-all-games.pgn"

    try:
        result = export_games(args.username, output, delay_s=args.delay)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise SystemExit(f"Chess.com player '{args.username}' was not found.") from exc
        raise

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()