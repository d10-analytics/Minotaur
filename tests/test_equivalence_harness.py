"""Behavioral proofs for the baseline/branch equivalence instrument."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "check_equivalence.py"
BASELINE_SRC = Path("/home/onyx/Programming/Code/Minotaur-baseline/src")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )


def _query_file(tmp_path: Path) -> Path:
    queries = tmp_path / "queries.json"
    queries.write_text("[]\n", encoding="utf-8")
    return queries


def _source_root(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    (root / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    return root


def test_identical_tree_guard_runs_before_import_or_provenance() -> None:
    result = _run(
        "--baseline-src",
        str(BASELINE_SRC),
        "--branch-src",
        str(BASELINE_SRC),
    )
    assert result.returncode == 1
    assert "identical trees" in result.stderr


def test_import_provenance_guard_rejects_a_tree_that_shadows_nothing(tmp_path: Path) -> None:
    empty = tmp_path / "empty-src"
    empty.mkdir()
    result = _run(
        "--baseline-src",
        str(BASELINE_SRC),
        "--branch-src",
        str(empty),
    )
    assert result.returncode == 1
    assert "minotaur imported from" in result.stderr
    assert f"outside {empty.resolve()}" in result.stderr
    assert "not a repository checkout" not in result.stderr


def test_self_test_rejects_byte_injection_and_accepts_clean_copy(tmp_path: Path) -> None:
    root = _source_root(tmp_path)
    result = _run(
        "--baseline-src",
        str(BASELINE_SRC),
        "--branch-src",
        str(ROOT / "src"),
        "--queries",
        str(_query_file(tmp_path)),
        "--root",
        str(root),
        "--self-test",
    )
    assert result.returncode == 0, result.stderr
    assert "analyze graph SHA-256: DIFFERENT" in result.stdout
    assert "self-test: PASS" in result.stdout
    assert "self-test mode: provenance guard skipped" in result.stderr


def test_clean_equal_heads_in_distinct_worktrees_are_refused(tmp_path: Path) -> None:
    worktree = tmp_path / "same-head"
    added = subprocess.run(
        [
            "git",
            "-C",
            str(BASELINE_SRC.parent),
            "worktree",
            "add",
            "--detach",
            str(worktree),
            "HEAD",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if added.returncode:
        pytest.skip(f"git worktree unavailable: {added.stderr}")
    try:
        result = _run(
            "--baseline-src",
            str(BASELINE_SRC),
            "--branch-src",
            str(worktree / "src"),
        )
        assert result.returncode == 1
        assert "same clean HEAD" in result.stderr
        assert str(BASELINE_SRC) in result.stderr
        assert str(worktree / "src") in result.stderr
    finally:
        subprocess.run(
            ["git", "-C", str(BASELINE_SRC.parent), "worktree", "remove", "--force", str(worktree)],
            capture_output=True,
            text=True,
            check=False,
        )


def test_plain_source_copy_is_rejected_by_provenance_guard(tmp_path: Path) -> None:
    copied = tmp_path / "copy"
    shutil.copytree(BASELINE_SRC, copied)
    result = _run(
        "--baseline-src",
        str(BASELINE_SRC),
        "--branch-src",
        str(copied),
    )
    assert result.returncode == 1
    assert "not a repository checkout" in result.stderr
