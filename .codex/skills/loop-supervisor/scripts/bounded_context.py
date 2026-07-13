#!/usr/bin/env python3
"""Print bounded tails of text logs for loop supervisor context."""

from __future__ import annotations

import argparse
from pathlib import Path


def tail_lines(text: str, line_limit: int) -> list[str]:
    lines = text.splitlines()
    return lines[-line_limit:] if line_limit > 0 else []


def clamp_chars(text: str, char_limit: int) -> str:
    if char_limit <= 0 or len(text) <= char_limit:
        return text
    return "[truncated]\n" + text[-char_limit:]


def read_bounded(path: Path, line_limit: int, char_limit: int) -> str:
    text = path.read_text(errors="replace")
    bounded = "\n".join(tail_lines(text, line_limit))
    return clamp_chars(bounded, char_limit)


def main() -> int:
    parser = argparse.ArgumentParser(description="Print bounded log context.")
    parser.add_argument("paths", nargs="+", help="Log or transcript paths to read")
    parser.add_argument("--tail", type=int, default=80, help="Lines per file")
    parser.add_argument("--max-chars", type=int, default=12000, help="Chars per file")
    args = parser.parse_args()

    for raw_path in args.paths:
        path = Path(raw_path).expanduser()
        print(f"## {path}")
        if not path.exists():
            print("[missing]")
            continue
        if not path.is_file():
            print("[not a file]")
            continue
        print(read_bounded(path, args.tail, args.max_chars))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
