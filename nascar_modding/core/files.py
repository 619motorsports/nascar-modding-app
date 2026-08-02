"""Crash-safe file writes shared by UI, editing, and verification layers."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path


def atomic_write_bytes(path: str | Path, data: bytes, tmp_suffix: str = '.tmp') -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(str(destination) + tmp_suffix)
    try:
        with temporary.open('wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def atomic_write_json(
    path: str | Path,
    value,
    *,
    indent: int | None = None,
    sort_keys: bool = False,
    separators: tuple[str, str] | None = None,
) -> None:
    buffer = io.StringIO()
    json.dump(value, buffer, indent=indent, sort_keys=sort_keys, separators=separators)
    atomic_write_bytes(path, buffer.getvalue().encode('utf-8'))
