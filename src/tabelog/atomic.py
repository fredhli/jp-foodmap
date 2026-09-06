"""Atomic file writes (M-020).

Every data / publish artefact in this repo used to be written with
``open(path, 'w')`` or ``Path.write_text`` — both truncate first, so an
interruption (Ctrl-C, crash, full disk, WSL hiccup) leaves a half-written
file on disk. For ``data/tabelog/tabelog.csv`` that is unrecoverable: the
file is gitignored, so there is no copy to roll back to.

The correct pattern already existed in ``scrape/translate_policies.py``
(``save_atomic``); this module generalises it and adds ``flush`` + ``fsync``
plus an optional ``.prev`` backup:

    tmp = path.with_name(path.name + '.tmp')   # same directory => same fs
    write tmp; flush(); os.fsync()
    (optional) copy the current path -> path.prev
    tmp.replace(path)                          # atomic rename

``Path.replace`` maps to ``os.replace``, which is atomic on POSIX and on
Windows (ReplaceFile semantics). A reader therefore only ever sees the old
complete file or the new complete file — never a truncated one.

Import from ``tabelog.atomic`` or from ``tabelog.paths`` (re-exported there
so scripts have a single import site).
"""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "backup_file",
    "atomic_write_bytes",
    "atomic_write_text",
    "atomic_write_json",
    "atomic_write_csv",
]

TMP_SUFFIX = ".tmp"
PREV_SUFFIX = ".prev"


def _tmp_path(path: Path) -> Path:
    """Sibling temp file — must live on the same filesystem as `path`,
    otherwise `replace` degrades to a non-atomic copy."""
    return path.with_name(path.name + TMP_SUFFIX)


def _replace_with_retry(tmp: Path, path: Path) -> None:
    """`tmp.replace(path)`, retried on Windows sharing violations.

    On Windows `os.replace` fails with PermissionError (WinError 5 / 32) when
    another process has the target open — Dropbox indexing a file that was
    written a second ago, or Defender scanning it. Seen 2026-09-06 on the
    google_places.csv ledger: the last write of a two-hour run failed after
    every API call had already been paid for. The window is short, so a few
    retries with backoff (about 10 s in total) cover it; after that the
    original error is re-raised and the .tmp is left in place, complete, for
    a hand copy."""
    delays = (0.1, 0.3, 0.7, 1.5, 3.0, 5.0)
    for delay in delays + (None,):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if delay is None:
                raise
            time.sleep(delay)


def _fsync_dir(directory: Path) -> None:
    """Best-effort durability for the rename itself. Not supported on
    Windows (and unnecessary there), so failures are swallowed."""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def backup_file(path: Path, suffix: str = PREV_SUFFIX) -> Path | None:
    """Copy `path` to `path.<suffix>` before it gets overwritten. Returns the
    backup path, or None when there was nothing to back up."""
    path = Path(path)
    if not path.exists():
        return None
    prev = path.with_name(path.name + suffix)
    shutil.copy2(path, prev)
    return prev


def atomic_write_bytes(
    path: Path | str,
    data: bytes,
    *,
    keep_prev: bool = False,
) -> None:
    """Write `data` to `path` atomically. With keep_prev, the file being
    replaced is first copied to `<path>.prev`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    if keep_prev:
        backup_file(path)
    _replace_with_retry(tmp, path)
    _fsync_dir(path.parent)


def atomic_write_text(
    path: Path | str,
    text: str,
    *,
    encoding: str = "utf-8",
    newline: str | None = None,
    keep_prev: bool = False,
) -> None:
    """Text flavour of `atomic_write_bytes`. `newline` follows the `open()`
    convention ('' to stop csv double-spacing on Windows)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    with tmp.open("w", encoding=encoding, newline=newline) as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    if keep_prev:
        backup_file(path)
    _replace_with_retry(tmp, path)
    _fsync_dir(path.parent)


def atomic_write_json(
    path: Path | str,
    obj: Any,
    *,
    keep_prev: bool = False,
    **dumps_kwargs: Any,
) -> None:
    """json.dumps + atomic write. Defaults match what the callers used
    before (ensure_ascii=False)."""
    dumps_kwargs.setdefault("ensure_ascii", False)
    atomic_write_text(
        path, json.dumps(obj, **dumps_kwargs), keep_prev=keep_prev
    )


def atomic_write_csv(
    path: Path | str,
    rows: Iterable[Mapping[str, Any]],
    fieldnames: Sequence[str],
    *,
    encoding: str = "utf-8-sig",
    extrasaction: str = "raise",
    keep_prev: bool = False,
) -> None:
    """DictWriter into an in-memory buffer, then one atomic write.

    Serialising first means a mid-serialisation exception (a row with an
    unexpected key, an unencodable character) leaves the on-disk file
    completely untouched instead of truncated at that row.
    """
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=list(fieldnames), extrasaction=extrasaction)
    w.writeheader()
    for r in rows:
        w.writerow(r)
    atomic_write_text(
        path, buf.getvalue(), encoding=encoding, newline="", keep_prev=keep_prev
    )
