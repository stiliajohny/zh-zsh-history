"""Keep tests away from the developer's ZH config and journal."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_zh_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point config at an empty file so a real home directory cannot change tests."""
    monkeypatch.setenv("ZH_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.delenv("ZH_DB", raising=False)
