from __future__ import annotations

import gzip
import os
import shutil
import threading
import time
from pathlib import Path


def _rotated_name(path: Path, now: float | None = None) -> Path:
    timestamp = time.strftime(
        "%Y%m%d_%H%M%S",
        time.localtime(time.time() if now is None else now),
    )
    candidate = path.with_name(f"{path.name}.{timestamp}")
    counter = 1
    while candidate.exists() or candidate.with_name(f"{candidate.name}.gz").exists():
        candidate = path.with_name(f"{path.name}.{timestamp}.{counter}")
        counter += 1
    return candidate


def _rotated_candidates(path: Path) -> list[Path]:
    if not path.parent.exists():
        return []
    candidates = [
        item
        for item in path.parent.glob(f"{path.name}.*")
        if item.is_file() and not item.name.endswith(".tmp")
    ]
    return sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True)


def _cleanup_rotated_logs(path: Path, keep_files: int) -> None:
    if keep_files <= 0:
        return
    for candidate in _rotated_candidates(path)[keep_files:]:
        try:
            candidate.unlink()
        except OSError:
            continue


def prune_rotated_jsonl_logs(path: Path, *, keep_files: int) -> int:
    if keep_files < 0:
        keep_files = 0
    candidates = _rotated_candidates(path)
    removed = 0
    for candidate in candidates[keep_files:]:
        try:
            candidate.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _compress_rotated_log(path: Path, base_path: Path, keep_files: int) -> None:
    if not path.exists() or path.name.endswith(".gz"):
        return
    gz_path = path.with_name(f"{path.name}.gz")
    tmp_path = path.with_name(f"{path.name}.gz.tmp")
    try:
        with path.open("rb") as source, gzip.open(
            tmp_path,
            "wb",
            compresslevel=6,
        ) as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        os.replace(tmp_path, gz_path)
        path.unlink()
        _cleanup_rotated_logs(base_path, keep_files)
    except OSError:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def rotate_jsonl_log_if_needed(
    path: Path,
    *,
    max_bytes: int,
    keep_files: int,
    compress: bool,
    background: bool = True,
    now: float | None = None,
) -> Path | None:
    if max_bytes <= 0 or not path.exists():
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    if stat.st_size < max_bytes:
        return None

    rotated_path = _rotated_name(path, now=now)
    try:
        os.replace(path, rotated_path)
        path.touch(mode=stat.st_mode & 0o777)
    except OSError:
        return None

    if compress:
        if background:
            thread = threading.Thread(
                target=_compress_rotated_log,
                args=(rotated_path, path, keep_files),
                daemon=True,
            )
            thread.start()
        else:
            _compress_rotated_log(rotated_path, path, keep_files)
    else:
        _cleanup_rotated_logs(path, keep_files)
    return rotated_path
