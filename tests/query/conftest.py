"""Shared isolation for query tests that create synthetic projects."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_query_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep synthetic query tests outside the checkout's discovered config."""
    monkeypatch.chdir(tmp_path)
