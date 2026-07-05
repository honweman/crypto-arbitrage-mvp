#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from arbitrage_bot.config import load_config
from arbitrage_bot.jsonl_rotation import (
    prune_rotated_jsonl_logs,
    rotate_jsonl_log_if_needed,
)


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _compact_one(
    path: Path,
    *,
    max_bytes: int,
    keep_files: int,
    compress: bool,
    force: bool,
) -> dict[str, Any]:
    before_bytes = _size(path)
    threshold = 1 if force and before_bytes > 0 else max_bytes
    rotated = rotate_jsonl_log_if_needed(
        path,
        max_bytes=threshold,
        keep_files=keep_files,
        compress=compress,
        background=False,
    )
    removed = prune_rotated_jsonl_logs(path, keep_files=keep_files)
    return {
        "path": str(path),
        "before_bytes": before_bytes,
        "after_bytes": _size(path),
        "rotated": str(rotated) if rotated else "",
        "removed_rotated_files": removed,
        "max_bytes": max_bytes,
        "keep_files": keep_files,
        "compress": compress,
    }


def compact_logs(args: argparse.Namespace) -> dict[str, Any]:
    cfg = load_config(args.config)
    max_bytes = int(args.max_bytes or cfg.trade_log.rotate_max_bytes)
    keep_files = int(args.keep_files if args.keep_files is not None else cfg.trade_log.rotate_keep_files)
    compress = cfg.trade_log.rotate_compress if args.compress is None else bool(args.compress)
    paths = [
        Path(cfg.trade_log.path),
        Path(cfg.strategy_timeline.path),
        Path(cfg.trade_log.path).with_name("web_audit_events.jsonl"),
    ]
    unique_paths = []
    seen: set[Path] = set()
    for path in paths:
        normalized = path.expanduser()
        if normalized not in seen:
            unique_paths.append(normalized)
            seen.add(normalized)

    results = [
        _compact_one(
            path,
            max_bytes=max_bytes,
            keep_files=keep_files,
            compress=compress,
            force=args.force,
        )
        for path in unique_paths
    ]
    return {
        "ok": True,
        "config": args.config,
        "force": args.force,
        "settings": {
            "max_bytes": max_bytes,
            "keep_files": keep_files,
            "compress": compress,
        },
        "results": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rotate and prune JSONL logs for the crypto arbitrage dashboard.",
    )
    parser.add_argument("--config", default="config.acs.json", help="Path to config JSON")
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=None,
        help="Override rotation threshold. Defaults to trade_log.rotate_max_bytes.",
    )
    parser.add_argument(
        "--keep-files",
        type=int,
        default=None,
        help="Override number of rotated files to keep. Defaults to trade_log.rotate_keep_files.",
    )
    parser.add_argument(
        "--compress",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Override gzip compression for rotated logs.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rotate non-empty active logs even if they are below the threshold.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    print(json.dumps(compact_logs(args), ensure_ascii=True, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
