"""Exit-code behavior of the ``minotaur query`` argparse wiring itself.

These are not about any individual query's results; they guard the dispatch
mechanism in ``cli.py`` that turns ``query <name> ...`` into a parsed
namespace. F-05 found ``--help`` on a query subcommand exiting 2 because a
nested parser's ``SystemExit`` was caught and mapped to 2 unconditionally.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from minotaur import cli


def test_query_subcommand_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """``--help`` is not a failure: an agent or CI wrapper must see exit 0."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["query", "callers", "--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "usage: minotaur query callers" in out


def test_query_subcommand_usage_error_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    """A genuine usage error (missing required arguments) still exits 2."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["query", "callers"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "the following arguments are required" in err


# ---------------------------------------------------------------------------
# Grammar-toggle cases (T02 AC-03/AC-07): discovery-sensitive, so each test
# runs from a cwd inside a tmp_path Git repository whose top is the D-07
# discovery boundary; nothing above the temporary repo can bind.
# ---------------------------------------------------------------------------


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert (
        subprocess.run(["git", "init", "-q"], cwd=root, capture_output=True, check=False).returncode
        == 0
    )
    return root


def _write(root: Path, path: str, source: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")


def test_help_exits_zero_next_to_an_invalid_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-07: --help never locates or validates a config, so it exits 0."""
    root = _repo(tmp_path)
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 99\ntargets = ["src"]\n', encoding="utf-8"
    )
    monkeypatch.chdir(root)
    for argv in (["analyze", "--help"], ["query", "--help"], ["query", "callers", "--help"]):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(argv)
        assert excinfo.value.code == 0
        assert "usage:" in capsys.readouterr().out


def test_query_grammar_relaxes_only_when_a_config_is_located(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-03/AC-07: with a config present, --graph/--root are no longer required."""
    root = _repo(tmp_path)
    _write(root, "src/app.py", "def app():\n    return 1\n")
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "g.json"\ntargets = ["src"]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(root)

    status = cli.main(["query", "callers", "some_symbol"])

    assert status == 2
    err = capsys.readouterr().err
    assert "the following arguments are required" not in err


def test_analyze_without_flags_runs_from_config_next_to_a_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-03: a located config relaxes analyze's required declarations end-to-end."""
    root = _repo(tmp_path)
    _write(root, "src/app.py", "def app():\n    return 1\n")
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "g.json"\ntargets = ["src"]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(root)

    status = cli.main(["analyze"])

    assert status == 0
    assert (root / "g.json").exists()
