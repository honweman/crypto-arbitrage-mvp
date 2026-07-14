from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .config import load_config
from .jsonl_rotation import (
    prune_rotated_jsonl_logs,
    rotate_jsonl_log_if_needed,
)
from .order_reliability import OrderIntentStore


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _compact_jsonl(
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


def optimize_sqlite(
    path: Path,
    *,
    order_intent_retention_days: float,
    order_intent_max_terminal_rows: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "before_bytes": _size(path),
        "after_bytes": _size(path),
        "optimized": False,
        "checkpoint_busy": 0,
        "checkpoint_log_pages": 0,
        "checkpointed_pages": 0,
        "page_count": 0,
        "free_page_count": 0,
        "order_intent_compaction": None,
    }
    if not path.is_file():
        return result

    if path.name == "order_intents.sqlite3":
        store = OrderIntentStore(path)
        result["order_intent_compaction"] = store.compact(
            terminal_retention_seconds=(
                max(0.0, order_intent_retention_days) * 24 * 60 * 60
            ),
            max_terminal_rows=max(1_000, order_intent_max_terminal_rows),
        )

    with sqlite3.connect(path, timeout=5.0) as connection:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA optimize")
        checkpoint = connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        result["page_count"] = int(
            connection.execute("PRAGMA page_count").fetchone()[0]
        )
        result["free_page_count"] = int(
            connection.execute("PRAGMA freelist_count").fetchone()[0]
        )
        connection.commit()

    if checkpoint:
        result["checkpoint_busy"] = int(checkpoint[0] or 0)
        result["checkpoint_log_pages"] = int(checkpoint[1] or 0)
        result["checkpointed_pages"] = int(checkpoint[2] or 0)
    result["optimized"] = True
    result["after_bytes"] = _size(path)
    return result


def prune_historical_files(
    directory: Path,
    *,
    patterns: tuple[str, ...],
    keep_files: int,
    min_age_days: float,
    apply: bool,
    now: float | None = None,
) -> dict[str, Any]:
    observed_at = time.time() if now is None else float(now)
    candidates: dict[Path, float] = {}
    if directory.is_dir():
        for pattern in patterns:
            for path in directory.glob(pattern):
                if not path.is_file() or path.is_symlink():
                    continue
                try:
                    candidates[path] = path.stat().st_mtime
                except OSError:
                    continue
    ordered = sorted(candidates, key=candidates.get, reverse=True)
    cutoff = observed_at - max(0.0, min_age_days) * 24 * 60 * 60
    eligible = [
        path
        for index, path in enumerate(ordered)
        if index >= max(0, keep_files) and candidates[path] <= cutoff
    ]
    removed: list[str] = []
    reclaimed_bytes = 0
    errors: list[str] = []
    if apply:
        for path in eligible:
            size = _size(path)
            try:
                path.unlink()
            except OSError as exc:
                errors.append(f"{path}: {exc}")
            else:
                removed.append(str(path))
                reclaimed_bytes += size
    return {
        "directory": str(directory),
        "patterns": list(patterns),
        "keep_files": max(0, keep_files),
        "min_age_days": max(0.0, min_age_days),
        "apply": apply,
        "candidate_count": len(eligible),
        "candidate_bytes": sum(_size(path) for path in eligible),
        "candidates": [str(path) for path in eligible],
        "removed_count": len(removed),
        "removed": removed,
        "reclaimed_bytes": reclaimed_bytes,
        "errors": errors,
    }


def run_maintenance(args: argparse.Namespace) -> dict[str, Any]:
    cfg = load_config(args.config)
    max_bytes = int(args.max_bytes or cfg.trade_log.rotate_max_bytes)
    keep_files = int(
        args.keep_files
        if args.keep_files is not None
        else cfg.trade_log.rotate_keep_files
    )
    compress = (
        cfg.trade_log.rotate_compress
        if args.compress is None
        else bool(args.compress)
    )
    log_paths = [
        Path(cfg.trade_log.path).expanduser(),
        Path(cfg.strategy_timeline.path).expanduser(),
        Path(cfg.trade_log.path).expanduser().with_name("web_audit_events.jsonl"),
    ]
    unique_log_paths = list(dict.fromkeys(log_paths))
    data_dir = Path(cfg.trade_log.path).expanduser().parent

    sqlite_results = []
    if args.sqlite:
        sqlite_results = [
            optimize_sqlite(
                path,
                order_intent_retention_days=args.order_intent_retention_days,
                order_intent_max_terminal_rows=args.order_intent_max_terminal_rows,
            )
            for path in sorted(data_dir.glob("*.sqlite3"))
            if path.is_file()
        ]

    backup_retention = prune_historical_files(
        data_dir,
        patterns=("deploy_backup_*.tgz",),
        keep_files=args.backup_keep_files,
        min_age_days=args.backup_min_age_days,
        apply=args.prune_backups,
    )
    config_retention = prune_historical_files(
        data_dir,
        patterns=("config_before_*.json", "config.acs.before_*.json"),
        keep_files=args.config_backup_keep_files,
        min_age_days=args.config_backup_min_age_days,
        apply=args.prune_backups,
    )
    errors = backup_retention["errors"] + config_retention["errors"]
    return {
        "ok": not errors,
        "config": args.config,
        "force": args.force,
        "settings": {
            "max_bytes": max_bytes,
            "keep_files": keep_files,
            "compress": compress,
            "sqlite": args.sqlite,
            "order_intent_retention_days": args.order_intent_retention_days,
            "order_intent_max_terminal_rows": args.order_intent_max_terminal_rows,
            "prune_backups": args.prune_backups,
        },
        "results": [
            _compact_jsonl(
                path,
                max_bytes=max_bytes,
                keep_files=keep_files,
                compress=compress,
                force=args.force,
            )
            for path in unique_log_paths
        ],
        "sqlite": sqlite_results,
        "backup_retention": backup_retention,
        "config_backup_retention": config_retention,
        "errors": errors,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Maintain logs, SQLite stores, and historical deployment files.",
    )
    parser.add_argument("--config", default="config.acs.json", help="Path to config JSON")
    parser.add_argument("--max-bytes", type=int, default=None)
    parser.add_argument("--keep-files", type=int, default=None)
    parser.add_argument(
        "--compress",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--sqlite",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--order-intent-retention-days", type=float, default=7.0)
    parser.add_argument(
        "--order-intent-max-terminal-rows",
        type=int,
        default=50_000,
    )
    parser.add_argument("--prune-backups", action="store_true")
    parser.add_argument("--backup-keep-files", type=int, default=5)
    parser.add_argument("--backup-min-age-days", type=float, default=14.0)
    parser.add_argument("--config-backup-keep-files", type=int, default=30)
    parser.add_argument("--config-backup-min-age-days", type=float, default=30.0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    payload = run_maintenance(args)
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    if not payload["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
