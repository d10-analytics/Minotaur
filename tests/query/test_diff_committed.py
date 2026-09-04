"""Behavioral coverage for committed-reference ``query diff`` mode."""

from __future__ import annotations

import subprocess
from pathlib import Path

from minotaur import cli
from minotaur.graph_model.loading import stamp_path


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


def _write_config(root: Path, *, targets: str = 'targets = ["app.py"]') -> None:
    (root / ".minotaur.toml").write_text(
        f'[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n{targets}\n',
        encoding="utf-8",
    )


def test_committed_mode_compares_head_graph_to_current_files_without_mutation(
    tmp_path: Path, monkeypatch: object, capsys: object
) -> None:
    root = _repo(tmp_path)
    _write_config(root)
    source = root / "app.py"
    source.write_text("def app():\n    return 1\n", encoding="utf-8")
    monkeypatch.chdir(root)  # type: ignore[attr-defined]
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")

    graph = root / "graph.json"
    sidecar = stamp_path(graph)
    before_graph = graph.read_bytes()
    before_sidecar = sidecar.read_bytes()
    source.write_text("def app():\n    return 1\n\ndef added():\n    pass\n", encoding="utf-8")
    status = cli.main(["query", "diff"])
    captured = capsys.readouterr()

    assert status == 1
    assert "relocated" in captured.out or "no changes" not in captured.out
    assert graph.read_bytes() == before_graph
    assert sidecar.read_bytes() == before_sidecar


def test_committed_mode_uses_head_when_dirty_disk_graph_is_mutated(
    tmp_path: Path, monkeypatch: object
) -> None:
    root = _repo(tmp_path)
    _write_config(root)
    source = root / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    monkeypatch.chdir(root)  # type: ignore[attr-defined]
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    graph = root / "graph.json"
    sidecar = stamp_path(graph)
    graph_before = graph.read_bytes()
    sidecar_before = sidecar.read_bytes()
    graph.write_bytes(b"not the committed graph")
    sidecar.write_bytes(b"0" * 64)
    source.write_text("value = 2\n\ndef added():\n    pass\n", encoding="utf-8")

    assert cli.main(["query", "diff"]) == 1
    assert graph.read_bytes() != graph_before
    assert sidecar.read_bytes() != sidecar_before


def test_committed_mode_scope_reads_graph_beside_definition_directory(
    tmp_path: Path, monkeypatch: object
) -> None:
    root = _repo(tmp_path)
    _write_config(root)
    source = root / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    definition_dir = root / "docs" / "systems" / "actual"
    definition_dir.mkdir(parents=True)
    (definition_dir / "system.toml").write_text(
        'schema_version = 1\nname = "declared"\nfiles = ["app.py"]\n', encoding="utf-8"
    )
    monkeypatch.chdir(root)  # type: ignore[attr-defined]
    assert cli.main(["analyze", "--scope", "declared"]) == 0
    assert (definition_dir / "graph.json").exists()
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    source.write_text("value = 2\n\ndef added():\n    pass\n", encoding="utf-8")

    assert cli.main(["query", "diff", "--scope", "declared"]) == 1


def test_committed_mode_outside_git_uses_disk_graph_without_stamping(
    tmp_path: Path, monkeypatch: object
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _write_config(root)
    source = root / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    monkeypatch.chdir(root)  # type: ignore[attr-defined]
    assert cli.main(["analyze"]) == 0
    graph = root / "graph.json"
    sidecar = stamp_path(graph)
    before = (graph.read_bytes(), sidecar.read_bytes())
    source.write_text("value = 2\n\ndef added():\n    pass\n", encoding="utf-8")

    assert cli.main(["query", "diff"]) == 1
    assert (graph.read_bytes(), sidecar.read_bytes()) == before
