"""Durable file primitives shared by every store in the app.

Both patterns here exist because the naive versions lose data: a plain
json.dump truncates the file before writing, so a crash mid-write leaves an
empty config, and an un-flushed append can lose the last trade of the day.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

_append_lock = threading.Lock()


def atomic_json_dump(path: Path, data: Any) -> None:
    """Write via tmp file + fsync + atomic rename, so the target is either the
    old complete file or the new complete file — never a truncated one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_json(path: Path, default: Any = None) -> Any:
    """Never raises — a corrupt file returns the default so the app still boots."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def append_jsonl(path: Path, record: dict) -> None:
    """Append one record, fsync'd — a closed trade must survive a crash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, default=str)
    with _append_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())


def read_jsonl(path: Path, limit: int | None = None) -> list[dict]:
    """Malformed lines are skipped rather than killing the whole read."""
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []
    if limit is not None:
        lines = lines[-limit:]
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out
