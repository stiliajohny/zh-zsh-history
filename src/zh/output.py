"""Compressed output files stored beside the journal."""

from __future__ import annotations

import gzip
import os
from pathlib import Path


def output_dir(db_path: Path) -> Path:
    """Return the directory that holds captured output."""
    return db_path.expanduser().parent / "outputs"


def write_output(directory: Path, name: str, payload: bytes) -> Path:
    """Gzip a capture and store it with mode 0600."""
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / name
    temporary = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temporary, "wb") as handle:
        handle.write(payload)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    return path


def read_output(path: Path) -> bytes:
    """Read one gzip capture."""
    with gzip.open(path, "rb") as handle:
        return handle.read()
