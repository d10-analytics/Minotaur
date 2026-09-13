"""Public composition tests for configured ``query diff --systems``."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from minotaur import cli
from minotaur.graph_model.loading import graph_digest, stamp_path


def _git(root: Path, *args: str) -> None:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Minotaur Tests")
    return root


def _configured_repo(tmp_path: Path) -> Path:
    root = _repo(tmp_path)
    (root / "app").mkdir()
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "api.py").write_text("def receive():\n    return 1\n", encoding="utf-8")
    (root / "consumer.py").write_text(
        "from app.api import receive\n\ndef consume():\n    return receive()\n", encoding="utf-8"
    )
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        'targets = ["app", "consumer.py"]\n',
        encoding="utf-8",
    )
    definition = root / "docs" / "systems" / "app"
    definition.mkdir(parents=True)
    (definition / "system.toml").write_text(
        'schema_version = 1\nname = "App"\nfiles = ["app/api.py"]\n', encoding="utf-8"
    )
    return root


def test_systems_help_does_not_parse_malformed_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    (root / ".minotaur.toml").write_text("not = [valid", encoding="utf-8")
    monkeypatch.chdir(root)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["query", "diff", "--systems", "--help"])

    assert excinfo.value.code == 0
    assert "usage: minotaur query diff" in capsys.readouterr().out


def test_systems_mode_requires_config_and_keeps_stdout_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    monkeypatch.chdir(root)

    assert cli.main(["query", "diff", "--systems"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "current input" in captured.err
    assert "config" in captured.err


def test_systems_grammar_rejects_scope_and_explicit_graphs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)

    with pytest.raises(SystemExit) as scope_error:
        cli.main(["query", "diff", "--systems", "--scope", "App"])
    assert scope_error.value.code == 2
    assert "unrecognized arguments: --scope" in capsys.readouterr().err

    assert cli.main(["query", "diff", "old.json", "new.json", "--systems"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "cannot be combined" in captured.err


def test_systems_composes_real_acquisition_comparison_and_rendering_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")

    graph = root / "graph.json"
    sidecar = stamp_path(graph)
    before = (graph.read_bytes(), sidecar.read_bytes())
    (root / "app" / "api.py").write_text(
        "def receive():\n    return 1\n\ndef send():\n    return 2\n", encoding="utf-8"
    )
    (root / "consumer.py").write_text(
        "from app.api import receive, send\n\ndef consume():\n    receive()\n    return send()\n",
        encoding="utf-8",
    )

    status = cli.main(["query", "diff", "--systems", "--system", "App", "--details"])
    captured = capsys.readouterr()

    assert status == 1
    assert captured.out.startswith("surface added: App app/api.py.app.api.send\n")
    assert "consumer changed: App <- consumer.py" in captured.out
    assert "old evidence: unavailable" in captured.out
    assert "new evidence: [" in captured.out
    assert graph.read_bytes() == before[0]
    assert sidecar.read_bytes() == before[1]


def test_system_filter_unknown_name_is_error_before_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")

    assert cli.main(["query", "diff", "--systems", "--system", "Ap"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unknown system: Ap" in captured.err


def test_systems_json_status_is_typed_and_context_is_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")

    status = cli.main(["query", "diff", "--systems", "--json"])
    captured = capsys.readouterr()

    assert status == 0
    assert '"changed":false' in captured.out
    assert '"coverage":{"new":' in captured.out
    assert captured.err == ""


def test_systems_linked_config_fails_before_historical_read_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    graph = root / "graph.json"
    sidecar = stamp_path(graph)
    before = (graph.read_bytes(), sidecar.read_bytes())
    alias = root / "config-alias.toml"
    alias.symlink_to(root / ".minotaur.toml")
    before_index = subprocess.run(
        ["git", "ls-files", "--stage"], cwd=root, text=True, capture_output=True, check=True
    ).stdout

    assert cli.main(["query", "diff", "--systems", "--config", str(alias)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "config-alias.toml" in captured.err
    assert "symbolic link" in captured.err
    assert (graph.read_bytes(), sidecar.read_bytes()) == before
    assert (
        subprocess.run(
            ["git", "ls-files", "--stage"],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        == before_index
    )
    assert (
        subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        == "?? config-alias.toml\n"
    )


def test_systems_invalid_historical_graph_cannot_be_hidden_by_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    graph = root / "graph.json"
    corrupt = b'{"schema_version":1}'
    graph.write_bytes(corrupt)
    stamp_path(graph).write_bytes((graph_digest(corrupt) + "\n").encode("ascii"))
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "invalid historical graph")

    assert cli.main(["query", "diff", "--systems", "--system", "App"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "historical Git input" in captured.err
    assert "graph.json" in captured.err


def test_unaffected_system_filter_returns_zero_when_complete_result_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    (root / "other.py").write_text("def other():\n    return 1\n", encoding="utf-8")
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        'targets = ["app", "consumer.py", "other.py"]\n',
        encoding="utf-8",
    )
    definition = root / "docs" / "systems" / "other"
    definition.mkdir(parents=True)
    (definition / "system.toml").write_text(
        'schema_version = 1\nname = "Other"\nfiles = ["other.py"]\n', encoding="utf-8"
    )
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / "app" / "api.py").write_text(
        "def receive():\n    return 1\n\ndef send():\n    return 2\n", encoding="utf-8"
    )

    assert cli.main(["query", "diff", "--systems", "--system", "Other"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("no system differences\n")
    assert "old coverage:" in captured.out
    assert "new coverage:" in captured.out
    assert captured.err == ""
