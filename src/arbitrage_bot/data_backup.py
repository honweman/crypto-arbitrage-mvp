"""Scheduled snapshots of the data directory.

User accounts, encrypted exchange credentials, strategies, and P/L history
all live in local SQLite/JSON files; before this module the only copies were
the ad-hoc archives made during deploys. The backup task periodically writes
a consistent snapshot (SQLite files are copied through the sqlite3 backup
API so a write in progress cannot corrupt the archive) into a timestamped
tar.gz and prunes old archives.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path

from .config import BotConfig

logger = logging.getLogger(__name__)

BACKUP_PREFIX = "data_backup_"


def _data_dir(cfg: BotConfig) -> Path:
    return Path(cfg.trade_log.path).parent


def _backup_dir(cfg: BotConfig) -> Path:
    configured = str(cfg.backup.path or "").strip()
    if configured:
        return Path(configured)
    return _data_dir(cfg) / "backups"


def _snapshot_sqlite(source: Path, destination: Path) -> None:
    source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        destination_conn = sqlite3.connect(destination)
        try:
            source_conn.backup(destination_conn)
        finally:
            destination_conn.close()
    finally:
        source_conn.close()


def create_backup_archive(cfg: BotConfig) -> Path | None:
    """Write one snapshot archive; returns its path or None when idle.

    Everything under the data directory is included except the backup
    directory itself and transient SQLite WAL/SHM side files (their content
    is captured by the sqlite3 backup API copy of the main database).
    """
    data_dir = _data_dir(cfg)
    if not data_dir.exists():
        return None
    backup_dir = _backup_dir(cfg)
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    archive_path = backup_dir / f"{BACKUP_PREFIX}{timestamp}.tar.gz"

    backup_root = backup_dir.resolve()
    with tempfile.TemporaryDirectory(prefix="crypto-arb-backup-") as staging_text:
        staging = Path(staging_text)
        copied = 0
        for source in sorted(data_dir.rglob("*")):
            if not source.is_file():
                continue
            resolved = source.resolve()
            if backup_root == resolved or backup_root in resolved.parents:
                continue
            if source.suffix in {".sqlite3-wal", ".sqlite3-shm"} or source.name.endswith(
                ("-wal", "-shm")
            ):
                continue
            relative = source.relative_to(data_dir)
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                if source.suffix == ".sqlite3":
                    _snapshot_sqlite(source, target)
                else:
                    shutil.copy2(source, target)
                copied += 1
            except (OSError, sqlite3.Error) as exc:
                logger.warning("backup: skipping %s (%s)", source, exc)
        if copied == 0:
            return None
        partial_path = archive_path.with_suffix(".tar.gz.partial")
        with tarfile.open(partial_path, "w:gz") as archive:
            archive.add(staging, arcname="data")
        partial_path.rename(archive_path)
    return archive_path


def prune_backup_archives(cfg: BotConfig) -> int:
    """Delete oldest archives beyond backup.keep; returns number removed."""
    keep = max(1, int(cfg.backup.keep))
    backup_dir = _backup_dir(cfg)
    if not backup_dir.exists():
        return 0
    archives = sorted(
        (
            item
            for item in backup_dir.iterdir()
            if item.is_file()
            and item.name.startswith(BACKUP_PREFIX)
            and item.name.endswith(".tar.gz")
        ),
        key=lambda item: item.name,
    )
    removed = 0
    for stale in archives[:-keep] if len(archives) > keep else []:
        try:
            stale.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("backup: could not prune %s (%s)", stale, exc)
    return removed


def run_backup_cycle(cfg: BotConfig) -> Path | None:
    archive = create_backup_archive(cfg)
    if archive is not None:
        pruned = prune_backup_archives(cfg)
        logger.info(
            "backup: wrote %s (pruned %d old archive(s))",
            archive,
            pruned,
        )
    return archive


async def backup_task_loop(cfg: BotConfig) -> None:
    """Background loop: snapshot on start, then every interval_hours."""
    interval_seconds = max(0.25, float(cfg.backup.interval_hours)) * 3600.0
    while True:
        try:
            await asyncio.to_thread(run_backup_cycle, cfg)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("backup: cycle failed; retrying next interval")
        await asyncio.sleep(interval_seconds)
