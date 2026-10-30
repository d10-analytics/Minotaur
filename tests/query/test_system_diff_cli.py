"""Public composition tests for configured ``query diff --systems``."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from minotaur import cli, comparison
from minotaur.graph_model.evidence import Evidence, Rule
from minotaur.graph_model.loading import graph_digest, stamp_path
from minotaur.graph_model.provenance import NodeClass, Provenance
from minotaur.language_interpreter.contract import AnalysisResult
from minotaur.language_interpreter.sql import interpreter as sql_interpreter

if TYPE_CHECKING:
    from conftest import InterpreterFault


def _git(root: Path, *args: str) -> None:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.parent.mkdir(parents=True, exist_ok=True)
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


def _configured_sql_repo(tmp_path: Path) -> Path:
    root = _repo(tmp_path)
    (root / "src").mkdir()
    (root / "src" / "schema.sql").write_text("CREATE TABLE base (id int)\n", encoding="utf-8")
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\ntargets = ["src"]\n',
        encoding="utf-8",
    )
    definition = root / "docs" / "systems" / "sql"
    definition.mkdir(parents=True)
    (definition / "system.toml").write_text(
        'schema_version = 1\nname = "sql"\nfiles = ["src/schema.sql"]\n',
        encoding="utf-8",
    )
    return root


def _sub_configured_repo(tmp_path: Path) -> Path:
    """A Git repository whose config sits below the worktree root in ``sub``."""
    root = _repo(tmp_path)
    app = root / "sub" / "app"
    app.mkdir(parents=True)
    (app / "__init__.py").write_text("", encoding="utf-8")
    (app / "api.py").write_text("def receive():\n    return 1\n", encoding="utf-8")
    definition = root / "sub" / "systems" / "core"
    definition.mkdir(parents=True)
    (definition / "system.toml").write_text(
        'schema_version = 1\nname = "core"\nfiles = ["app/api.py"]\n', encoding="utf-8"
    )
    (root / "sub" / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        'targets = ["app/api.py"]\nsystems_dir = "systems"\n',
        encoding="utf-8",
    )
    return root


def _invalid_systems_dir(directory: Path) -> None:
    """Create one child system folder holding an unparseable definition."""
    definition = directory / "core"
    definition.mkdir(parents=True, exist_ok=True)
    (definition / "system.toml").write_text("not valid [[[\n", encoding="utf-8")


def _counting_producer(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Install a producer that delegates to the real one and records each call."""
    original = cli._produce_selection
    calls: list[object] = []

    def counting_producer(*args: object, **kwargs: object) -> object:
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(cli, "_produce_selection", counting_producer)
    return calls


def _state(root: Path) -> dict[str, object]:
    """Capture every comparison input plus Git index and porcelain state."""
    tracked = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.splitlines()
    files: dict[str, object] = {}
    for value in sorted(set(tracked)):
        path = root / value
        if not os.path.lexists(path):
            files[value] = None
        elif path.is_symlink():
            files[value] = ("symlink", os.readlink(path))
        elif path.is_file():
            files[value] = path.read_bytes()
        else:
            files[value] = "directory"
    return {
        "files": files,
        "index": subprocess.run(
            ["git", "ls-files", "--stage"], cwd=root, text=True, capture_output=True, check=True
        ).stdout,
        "status": subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        ).stdout,
    }


def _assert_state(root: Path, before: dict[str, object]) -> None:
    assert _state(root) == before


def _serialized_file_boundary_fixture(payload: dict[str, object], label: str) -> dict[str, object]:
    """Keep one valid source-to-file boundary while changing its file label."""
    nodes = payload["nodes"]
    relationships = payload["relationships"]
    assert isinstance(nodes, list)
    assert isinstance(relationships, list)
    file_node = next(
        node
        for node in nodes
        if isinstance(node, dict)
        and node.get("node_class") == NodeClass.FILE.value
        and node.get("path") == "app/api.py"
    )
    source_id = next(
        node["id"]
        for node in nodes
        if isinstance(node, dict) and node.get("label") == "consumer.consume"
    )
    target_id = next(
        node["id"]
        for node in nodes
        if isinstance(node, dict) and node.get("label") == "app.api.receive"
    )
    assert isinstance(file_node, dict)
    file_id = file_node["id"]
    relation = next(
        relationship
        for relationship in relationships
        if isinstance(relationship, dict)
        and relationship.get("kind") == "calls"
        and relationship.get("source") == source_id
        and relationship.get("target") == target_id
    )
    assert isinstance(relation, dict)
    result = dict(payload)
    result["nodes"] = [
        {**node, "label": label} if node.get("id") == file_id else node for node in nodes
    ]
    result["relationships"] = [{**relation, "target": file_id}]
    return result


def _controlled_file_boundary_result(
    result: AnalysisResult, *, label: str, evidence_only: bool = False
) -> AnalysisResult:
    """Supply one valid current graph for the approved FILE_PATH proof row."""
    nodes = result.document.nodes
    file_node = next(
        node for node in nodes if node.node_class is NodeClass.FILE and node.path == "app/api.py"
    )
    source_id = next(node.id for node in nodes if node.label == "consumer.consume")
    target_id = next(node.id for node in nodes if node.label == "app.api.receive")
    relationship = next(
        relationship
        for relationship in result.document.relationships
        if relationship.kind == "calls"
        and relationship.source == source_id
        and relationship.target == target_id
    )
    if evidence_only:
        evidence = relationship.evidence + (
            Evidence(Provenance.CURATED_RULE, rule=Rule("controlled-proof")),
        )
        relationship = replace(relationship, evidence=evidence)
    file_node = replace(file_node, label=label)
    updated_nodes = tuple(
        replace(node, label=label) if node.id == file_node.id else node for node in nodes
    )
    document = replace(
        result.document,
        nodes=updated_nodes,
        relationships=(replace(relationship, target=file_node.id),),
    )
    return replace(result, document=document)


def _commit_file_boundary_graph(root: Path, *, label: str) -> None:
    graph = root / "graph.json"
    payload = json.loads(graph.read_text(encoding="utf-8"))
    transformed = _serialized_file_boundary_fixture(payload, label)
    content = json.dumps(transformed, sort_keys=True, separators=(",", ":")).encode("utf-8")
    graph.write_bytes(content)
    stamp_path(graph).write_bytes((graph_digest(content) + "\n").encode("ascii"))
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "file boundary fixture")


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


def test_systems_grammar_requires_zero_or_two_revisions_and_rejects_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")

    with pytest.raises(SystemExit) as scope_error:
        cli.main(["query", "diff", "--systems", "--scope", "App"])
    assert scope_error.value.code == 2
    assert "unrecognized arguments: --scope" in capsys.readouterr().err

    assert cli.main(["query", "diff", "--systems", "HEAD"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "zero or exactly two revisions" in captured.err

    assert cli.main(["query", "diff", "--systems", "missing-a", "missing-b"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "before" in captured.err
    assert "missing-a" in captured.err


@pytest.mark.parametrize("override", ("--graph", "--root"))
def test_systems_route_rejects_unused_graph_and_root_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    override: str,
) -> None:
    """AC-07: the systems route never silently accepts an unused graph/root."""
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["query", "diff", "--systems", override, "unused.json"])

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"unrecognized arguments: {override}" in captured.err


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
    before_state = _state(root)

    status = cli.main(["query", "diff", "--systems", "--system=App", "--details", "--validate"])
    captured = capsys.readouterr()

    assert status == 1
    assert captured.out.startswith("surface added: App app/api.py.app.api.send\n")
    assert "consumer changed: App <- consumer.py" in captured.out
    assert "old evidence: unavailable" in captured.out
    assert "new evidence: [" in captured.out
    assert graph.read_bytes() == before[0]
    assert sidecar.read_bytes() == before[1]
    _assert_state(root, before_state)


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
    before = _state(root)

    status = cli.main(["query", "diff", "--systems", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert status == 0
    assert '"changed":false' in captured.out
    assert '"coverage":{"new":' in captured.out
    comparison = payload["comparison"]
    assert comparison["schema_version"] == 1
    assert comparison["selected_system"] is None
    assert comparison["before"]["kind"] == "commit"
    assert comparison["after"]["kind"] == "working-tree"
    assert captured.err == ""
    _assert_state(root, before)


def test_systems_combined_json_details_and_validation_keep_typed_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / "app" / "api.py").write_text(
        "def receive():\n    return 1\n\ndef send():\n    return 2\n", encoding="utf-8"
    )
    (root / "consumer.py").write_text(
        "from app.api import receive, send\n\ndef consume():\n    receive()\n    return send()\n",
        encoding="utf-8",
    )
    before = _state(root)

    status = cli.main(
        ["query", "diff", "--systems", "--system=App", "--json", "--details", "--validate"]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert status == 1
    assert payload["changed"] is True
    assert payload["exit_code"] == 1
    assert payload["boundary_changes"]
    assert payload["boundary_changes"][0]["new"]["relationships"]
    _assert_state(root, before)
    assert captured.err == ""


@pytest.mark.parametrize("flag", ("--system", "--details"))
def test_systems_only_flags_are_rejected_without_systems_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flag: str,
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            ["query", "diff", "--system=App"] if flag == "--system" else ["query", "diff", flag]
        )

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unrecognized arguments" in captured.err


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


def test_systems_stale_committed_graph_does_not_change_source_baseline(
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
    committed = (graph.read_bytes(), stamp_path(graph).read_bytes())

    # The saved graph is inert: unchanged source still reports equality even
    # though the committed graph is unreadable.
    assert cli.main(["query", "diff", "--systems", "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["changed"] is False
    assert captured.err == ""

    # A source edit is detected from source rather than from the saved graph.
    (root / "app" / "unrelated.py").write_text("value = 1\n", encoding="utf-8")
    assert cli.main(["query", "diff", "--systems", "--json"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["changed"] is True
    assert any(node["status"] == "added" for node in payload["comparison"]["nodes"])
    assert captured.err == ""
    assert (graph.read_bytes(), stamp_path(graph).read_bytes()) == committed


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

    # The scoped status is zero while the retained context still reports the
    # complete comparison as changed.
    assert cli.main(["query", "diff", "--systems", "--system", "Other", "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["changed"] is False
    assert payload["selected_system"] == "Other"
    assert payload["comparison"]["changed"] is True


@pytest.mark.parametrize(
    "config_args",
    [
        ("--config", ".minotaur.toml"),
        ("--config=.minotaur.toml",),
        ("--config", "missing.toml", "--config", ".minotaur.toml"),
    ],
)
def test_systems_config_forms_preserve_last_value_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config_args: tuple[str, ...],
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / "app" / "api.py").write_text(
        "def receive():\n    return 1\n\ndef send():\n    return 2\n", encoding="utf-8"
    )
    (root / "consumer.py").write_text(
        "from app.api import receive, send\n\ndef consume():\n    receive()\n    return send()\n",
        encoding="utf-8",
    )

    assert cli.main(["query", "diff", "--systems", *config_args]) == 1
    captured = capsys.readouterr()
    assert captured.out.startswith("surface added: App")
    assert captured.err == ""


def test_systems_nested_dotdot_config_route_normalizes_before_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / "nested").mkdir()

    assert cli.main(["query", "diff", "--systems", "--config", "nested/../.minotaur.toml"]) == 0
    captured = capsys.readouterr()
    assert "no system differences" in captured.out
    assert captured.err == ""


def test_plain_diff_accepts_linked_explicit_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    alias = root / "config-alias.toml"
    alias.symlink_to(root / ".minotaur.toml")

    assert cli.main(["query", "diff", "--config", str(alias)]) == 0
    captured = capsys.readouterr()
    assert captured.out == "no changes\n"
    assert captured.err == ""


def test_systems_without_git_fails_as_current_input_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "nongit"
    root.mkdir()
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\ntargets = ["app.py"]\n',
        encoding="utf-8",
    )
    (root / "app.py").write_text("def app():\n    return 1\n", encoding="utf-8")
    monkeypatch.chdir(root)

    assert cli.main(["query", "diff", "--systems"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "current input" in captured.err
    assert "Git" in captured.err


def test_systems_unborn_git_fails_as_historical_input_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    (root / "app.py").write_text("def app():\n    return 1\n", encoding="utf-8")
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\ntargets = ["app.py"]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(root)

    assert cli.main(["query", "diff", "--systems"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "before Git input" in captured.err
    assert "HEAD" in captured.err


def test_invalid_unselected_current_definition_fails_before_filtering(
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
    (definition / "system.toml").write_text(
        "schema_version = 1\nname = [invalid\n", encoding="utf-8"
    )
    before = _state(root)

    assert cli.main(["query", "diff", "--systems", "--system", "App"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "other/system.toml" in captured.err
    assert "Invalid value" in captured.err
    _assert_state(root, before)


def test_current_source_diagnostic_outside_selected_system_is_global_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / "consumer.py").write_text("def broken(:\n", encoding="utf-8")
    before = _state(root)

    assert cli.main(["query", "diff", "--systems", "--system", "App"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "consumer.py" in captured.err
    assert "captured source analysis produced diagnostics" in captured.err
    _assert_state(root, before)


def test_systems_diff_renders_sql_warning_and_rejects_sql_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_sql_repo(tmp_path)
    monkeypatch.chdir(root)
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        'targets = ["src"]\n[minotaur.sql]\nview_depth_threshold = 3\n',
        encoding="utf-8",
    )
    warning_source = (
        "CREATE TABLE base (id int)\nGO\n"
        "CREATE VIEW v1 AS SELECT * FROM base\nGO\n"
        "CREATE VIEW v2 AS SELECT * FROM v1\n"
    )
    (root / "src" / "schema.sql").write_text(warning_source, encoding="utf-8")
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        'targets = ["src"]\n[minotaur.sql]\nview_depth_threshold = 1\n',
        encoding="utf-8",
    )

    status = cli.main(["query", "diff", "--systems", "--json"])
    captured = capsys.readouterr()
    assert status == 0
    assert "view-depth-warning" in captured.err
    assert json.loads(captured.out)["changed"] is False

    (root / "src" / "schema.sql").write_text(
        warning_source + "\nGO\nCREATE TABLE broken (\n", encoding="utf-8"
    )
    status = cli.main(["query", "diff", "--systems", "--json"])
    captured = capsys.readouterr()
    assert status == 2
    assert captured.out == ""
    assert "captured source analysis produced diagnostics" in captured.err


def test_systems_diff_preserves_each_captured_sql_migration_pattern(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    (root / "migrations" / "nested").mkdir(parents=True)
    (root / "migrations" / "001.sql").write_text("CREATE TABLE base (id int)\n", encoding="utf-8")
    (root / "migrations" / "nested" / "002.sql").write_text(
        "CREATE TABLE nested (id int)\n", encoding="utf-8"
    )
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        'targets = ["migrations"]\n[minotaur.sql]\n'
        'migration_patterns = ["migrations/**/*.sql"]\n'
        'foreign_key_target_files = { "Parent" = "schema/parent.sql" }\n',
        encoding="utf-8",
    )
    definition = root / "docs" / "systems" / "sql"
    definition.mkdir(parents=True)
    (definition / "system.toml").write_text(
        'schema_version = 1\nname = "sql"\n'
        'files = ["migrations/001.sql", "migrations/nested/002.sql"]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        'targets = ["migrations"]\n[minotaur.sql]\n'
        'migration_patterns = ["migrations/*.sql"]\n'
        'foreign_key_target_files = { "Parent" = "schema/parent-after.sql" }\n',
        encoding="utf-8",
    )

    observed = []
    original = sql_interpreter.analyze_view_warnings

    def observe(document, settings):
        observed.append(settings)
        return original(document, settings)

    monkeypatch.setattr(sql_interpreter, "analyze_view_warnings", observe)
    assert cli.main(["query", "diff", "--systems", "--json"]) == 0
    capsys.readouterr()
    assert [settings.migration_patterns for settings in observed] == [
        ("migrations/**/*.sql",),
        ("migrations/*.sql",),
    ]
    assert [dict(settings.foreign_key_target_files) for settings in observed] == [
        {"parent": "schema/parent.sql"},
        {"parent": "schema/parent-after.sql"},
    ]
    recursive, direct_only = observed
    assert recursive.matches_migration("migrations/nested/002.sql")
    assert not direct_only.matches_migration("migrations/nested/002.sql")


@pytest.mark.parametrize("route", ("root", "target", "systems"))
@pytest.mark.parametrize("nested", (False, True))
def test_systems_current_link_and_nested_routes_fail_before_production(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    route: str,
    nested: bool,
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    graph = root / "graph.json"
    if nested:
        (root / "nested").symlink_to(root / "app")
        route_value = "nested/.."
    else:
        (root / "route-link").symlink_to(root / "app")
        route_value = "route-link"
    config = (root / ".minotaur.toml").read_text(encoding="utf-8")
    if route == "root":
        config = config.replace('root = "."', f'root = "{route_value}"')
    elif route == "target":
        config = config.replace('"app"', f'"{route_value}/app"')
    else:
        config += f'systems_dir = "{route_value}/docs/systems"\n'
    (root / ".minotaur.toml").write_text(config, encoding="utf-8")
    before = _state(root)

    assert cli.main(["query", "diff", "--systems"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "current input" in captured.err
    assert graph.read_bytes() == before["files"]["graph.json"]
    _assert_state(root, before)


def test_systems_historical_link_is_rejected_after_current_route_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    config_bytes = (root / ".minotaur.toml").read_bytes()
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / "historical.toml").write_bytes(config_bytes)
    (root / ".minotaur.toml").unlink()
    (root / ".minotaur.toml").symlink_to("historical.toml")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "historical link")
    (root / ".minotaur.toml").unlink()
    (root / ".minotaur.toml").write_bytes(config_bytes)
    before = _state(root)

    assert cli.main(["query", "diff", "--systems"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "before revision" in captured.err
    assert "symbolic link" in captured.err
    _assert_state(root, before)


def test_systems_historical_gitlink_is_rejected_after_current_route_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    config_bytes = (root / ".minotaur.toml").read_bytes()
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True
    ).stdout.strip()
    _git(root, "rm", "--cached", "-q", ".minotaur.toml")
    result = subprocess.run(
        ["git", "update-index", "--add", "--cacheinfo", f"160000,{commit},.minotaur.toml"],
        cwd=root,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    _git(root, "commit", "-qm", "historical gitlink")
    (root / ".minotaur.toml").write_bytes(config_bytes)
    before = _state(root)

    assert cli.main(["query", "diff", "--systems"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "before revision" in captured.err
    assert "Gitlink" in captured.err
    _assert_state(root, before)


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("traversal", "unresolved traversal in systems root"),
        ("absolute", "systems root escapes the selected worktree"),
        (
            "config-folder",
            "configured systems_dir escapes the config folder (allowed folder is sub)",
        ),
    ],
)
def test_systems_diff_route_inspection_precedes_the_config_folder_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
    expected: str,
) -> None:
    """Route escapes report first; only an in-worktree escape names the folder."""
    if case == "config-folder":
        root = _sub_configured_repo(tmp_path)
        _invalid_systems_dir(root / "systems")
        config_path = root / "sub" / ".minotaur.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                'systems_dir = "systems"', 'systems_dir = "../systems"'
            ),
            encoding="utf-8",
        )
        cwd = root / "sub"
    else:
        root = _configured_repo(tmp_path)
        if case == "traversal":
            outside = tmp_path / "outside-systems"
            value = "../outside-systems"
        else:
            outside = tmp_path / "absolute-systems"
            value = str(outside)
        _invalid_systems_dir(outside)
        config_path = root / ".minotaur.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8") + f'systems_dir = "{value}"\n',
            encoding="utf-8",
        )
        cwd = root
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "escaping systems root")
    monkeypatch.chdir(cwd)

    assert cli.main(["query", "diff", "--systems"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert expected in captured.err
    assert "invalid TOML" not in captured.err


def test_systems_diff_refuses_a_config_below_root_outside_its_folder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The comparison route applies the config-folder rule to a nested config."""
    root = _sub_configured_repo(tmp_path)
    config_path = root / "sub" / ".minotaur.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace('root = "."', 'root = ".."'),
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "escaping root")
    monkeypatch.chdir(root / "sub")

    assert cli.main(["query", "diff", "--systems"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "configured root escapes the config folder (allowed folder is sub)" in captured.err


def test_systems_diff_live_side_refuses_out_of_folder_graph_before_production(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A live working-tree escape is attributed to ``current input`` before production."""
    root = _sub_configured_repo(tmp_path)
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    config_path = root / "sub" / ".minotaur.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'graph = "graph.json"', 'graph = "../g.json"'
        ),
        encoding="utf-8",
    )
    calls = _counting_producer(monkeypatch)
    monkeypatch.chdir(root / "sub")
    before = _state(root)

    assert cli.main(["query", "diff", "--systems"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "current input" in captured.err
    assert "after input" not in captured.err
    assert "configured graph escapes the config folder" in captured.err
    assert calls == []
    _assert_state(root, before)


def test_systems_diff_before_revision_refuses_out_of_folder_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A captured ``HEAD`` config is refused on the before side before production."""
    root = _sub_configured_repo(tmp_path)
    config_path = root / "sub" / ".minotaur.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace('root = "."', 'root = ".."'),
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "escaping head")
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace('root = ".."', 'root = "."'),
        encoding="utf-8",
    )
    calls = _counting_producer(monkeypatch)
    monkeypatch.chdir(root / "sub")
    before = _state(root)

    assert cli.main(["query", "diff", "--systems"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "before input revision 'HEAD'" in captured.err
    assert "configured root escapes the config folder" in captured.err
    assert calls == []
    _assert_state(root, before)


def test_systems_diff_after_revision_refuses_out_of_folder_systems_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A named revision's escape is refused after the in-folder side is analyzed."""
    root = _sub_configured_repo(tmp_path)
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "in-folder old")
    old = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True
    ).stdout.strip()
    config_path = root / "sub" / ".minotaur.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'systems_dir = "systems"', 'systems_dir = "../systems"'
        ),
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "escaping new")
    new = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True
    ).stdout.strip()
    calls = _counting_producer(monkeypatch)
    monkeypatch.chdir(root / "sub")
    before = _state(root)

    assert cli.main(["query", "diff", "--systems", old, new]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "after input revision" in captured.err
    assert new in captured.err
    assert "configured systems_dir escapes the config folder" in captured.err
    assert len(calls) == 1
    _assert_state(root, before)


def test_systems_diff_allows_a_config_below_root_with_in_folder_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A confined nested config still compares and leaves the tree unchanged."""
    root = _sub_configured_repo(tmp_path)
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    monkeypatch.chdir(root / "sub")
    before = _state(root)

    assert cli.main(["query", "diff", "--systems"]) == 0
    captured = capsys.readouterr()
    assert "no system differences" in captured.out
    assert captured.err == ""
    _assert_state(root, before)


def test_systems_diff_refuses_a_linked_child_under_systems_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Systems mode still refuses every linked child folder as a symbolic link."""
    root = _sub_configured_repo(tmp_path)
    (root / "sub" / "systems" / "alias").symlink_to(
        root / "sub" / "systems" / "core", target_is_directory=True
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "linked system folder")
    monkeypatch.chdir(root / "sub")

    assert cli.main(["query", "diff", "--systems"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "symbolic link" in captured.err


def test_systems_pinned_read_failure_is_attributed_before_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A selected blob that cannot be read fails the before side before any output."""
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    blob = subprocess.run(
        ["git", "rev-parse", "HEAD:app/api.py"],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    loose = root / ".git" / "objects" / blob[:2] / blob[2:]
    assert loose.is_file()
    loose.chmod(0o644)
    loose.unlink()
    before = _state(root)

    assert cli.main(["query", "diff", "--systems"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "before" in captured.err
    assert "app/api.py" in captured.err
    assert "missing" in captured.err
    _assert_state(root, before)


def test_systems_target_absent_on_both_sides_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    config = (root / ".minotaur.toml").read_text(encoding="utf-8")
    (root / ".minotaur.toml").write_text(config.replace('"app"', '"missing.py"'), encoding="utf-8")
    before = _state(root)

    assert cli.main(["query", "diff", "--systems"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "minotaur: error: configured target is absent on both comparison sides: missing.py\n"
    )
    _assert_state(root, before)


def test_systems_absent_target_is_rejected_before_any_side_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_sql_repo(tmp_path)
    monkeypatch.chdir(root)
    (root / "src" / "schema.sql").write_text(
        "CREATE TABLE base (id int)\nGO\n"
        "CREATE VIEW v1 AS SELECT * FROM base\nGO\n"
        "CREATE VIEW v2 AS SELECT * FROM v1\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        'targets = ["src", "untracked.sql"]\n[minotaur.sql]\nview_depth_threshold = 1\n',
        encoding="utf-8",
    )

    status = cli.main(["query", "diff", "--systems", "--json"])
    captured = capsys.readouterr()
    assert status == 2
    assert captured.out == ""
    assert captured.err == (
        "minotaur: error: configured target is absent on both comparison sides: untracked.sql\n"
    )
    assert "view-depth-warning" not in captured.err


def _file_target_repo(tmp_path: Path) -> Path:
    root = _configured_repo(tmp_path)
    config = (root / ".minotaur.toml").read_text(encoding="utf-8")
    (root / ".minotaur.toml").write_text(
        config.replace(
            'targets = ["app", "consumer.py"]', 'targets = ["app/api.py", "consumer.py"]'
        ),
        encoding="utf-8",
    )
    return root


@pytest.mark.parametrize("delete_all", (False, True))
def test_systems_deleted_committed_targets_keep_selection_context_and_no_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    delete_all: bool,
) -> None:
    root = _file_target_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / "app" / "api.py").unlink()
    if delete_all:
        (root / "consumer.py").unlink()
    before = _state(root)

    status = cli.main(["query", "diff", "--systems", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert status == 1
    assert payload["changed"] is True
    assert payload["selection"]["old"]["targets"] == ["app/api.py", "consumer.py"]
    assert payload["selection"]["new"]["targets"] == ["app/api.py", "consumer.py"]
    _assert_state(root, before)


def test_systems_added_and_deleted_systems_are_public_structural_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    added_root = _configured_repo(tmp_path / "added")
    monkeypatch.chdir(added_root)
    assert cli.main(["analyze"]) == 0
    _git(added_root, "add", ".")
    _git(added_root, "commit", "-qm", "baseline")
    (added_root / "docs" / "systems" / "other").mkdir(parents=True)
    (added_root / "docs" / "systems" / "other" / "system.toml").write_text(
        'schema_version = 1\nname = "Other"\nfiles = ["missing.py"]\n', encoding="utf-8"
    )
    before_added = _state(added_root)
    assert cli.main(["query", "diff", "--systems", "--json"]) == 1
    added_output = capsys.readouterr()
    added_payload = json.loads(added_output.out)
    assert added_payload["added_systems"] == ["Other"]
    assert added_payload["changed"] is True
    _assert_state(added_root, before_added)

    deleted_root = _configured_repo(tmp_path / "deleted")
    monkeypatch.chdir(deleted_root)
    assert cli.main(["analyze"]) == 0
    _git(deleted_root, "add", ".")
    _git(deleted_root, "commit", "-qm", "baseline")
    definition = deleted_root / "docs" / "systems" / "app" / "system.toml"
    definition.unlink()
    before_deleted = _state(deleted_root)
    assert cli.main(["query", "diff", "--systems", "--json"]) == 1
    deleted_output = capsys.readouterr()
    deleted_payload = json.loads(deleted_output.out)
    assert deleted_payload["removed_systems"] == ["App"]
    assert deleted_payload["changed"] is True
    _assert_state(deleted_root, before_deleted)


def test_systems_public_route_composes_matched_file_endpoint_label_and_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Compose both sides' stored endpoint records for the FILE_PATH row.

    The analyzer cannot naturally produce two labels for one file path, so a
    controlled producer supplies the Before and After graph shapes in analysis
    order. Both sides still come from captured source; the committed graph is
    inert.
    """
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _commit_file_boundary_graph(root, label="legacy-api.py")

    original_producer = cli._produce_selection
    labels = ["legacy-api.py", "current-api.py"]

    def controlled_producer(*args: object, **kwargs: object) -> object:
        workspace, selection, result = original_producer(*args, **kwargs)  # type: ignore[arg-type]
        if not labels:
            labels.extend(["legacy-api.py", "current-api.py"])
        label = labels.pop(0)
        return (
            workspace,
            selection,
            _controlled_file_boundary_result(result, label=label),
        )

    monkeypatch.setattr(cli, "_produce_selection", controlled_producer)
    before = _state(root)

    assert cli.main(["query", "diff", "--systems", "--details"]) == 1
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    boundary = "boundary endpoint: no_system.consumer.consume -> App.current-api.py (calls)"
    assert lines.count(boundary) == 1
    assert "boundary added:" not in captured.out
    assert "boundary removed:" not in captured.out
    index = lines.index(boundary)
    old = json.loads(lines[index + 1].removeprefix("old: "))
    new = json.loads(lines[index + 2].removeprefix("new: "))
    old_evidence = json.loads(lines[index + 3].removeprefix("old evidence: "))
    new_evidence = json.loads(lines[index + 4].removeprefix("new evidence: "))
    assert old["target_endpoint"]["label"] == "legacy-api.py"
    assert new["target_endpoint"]["label"] == "current-api.py"
    assert old["target_endpoint"]["node_class"] == new["target_endpoint"]["node_class"] == "file"
    assert (
        old["target_endpoint"]["path"]
        == new["target_endpoint"]["path"]
        == {
            "status": "recorded",
            "value": "app/api.py",
        }
    )
    assert old["source_membership"] == new["source_membership"] == "no_system"
    assert old["target_membership"] == new["target_membership"] == "system: App"
    assert old_evidence[0]["evidence"] == new_evidence[0]["evidence"]
    assert old_evidence[0]["evidence"][0]["provenance"] == "static-analysis"
    assert old_evidence[0]["evidence"][0]["sites"][0]["path"] == "consumer.py"
    _assert_state(root, before)

    assert cli.main(["query", "diff", "--systems", "--json", "--details"]) == 1
    json_output = capsys.readouterr()
    payload = json.loads(json_output.out)
    assert payload["changed"] is True
    assert payload["exit_code"] == 1
    assert payload["boundary_changes"]
    assert [change["kind"] for change in payload["boundary_changes"]] == ["endpoint"]
    change = payload["boundary_changes"][0]
    assert change["old"]["target_endpoint"]["label"] == "legacy-api.py"
    assert change["new"]["target_endpoint"]["label"] == "current-api.py"
    assert (
        change["old"]["target_endpoint"]["node_class"]
        == change["new"]["target_endpoint"]["node_class"]
        == "file"
    )
    assert (
        change["old"]["target_endpoint"]["path"]
        == change["new"]["target_endpoint"]["path"]
        == {"status": "recorded", "value": "app/api.py"}
    )
    assert (
        change["old"]["relationships"][0]["target"]["id"]
        == change["new"]["relationships"][0]["target"]["id"]
    )
    assert change["old"]["source_membership"] == change["new"]["source_membership"] == "no_system"
    assert change["old"]["target_membership"] == change["new"]["target_membership"] == "system: App"
    assert (
        change["old"]["relationships"][0]["evidence"]
        == change["new"]["relationships"][0]["evidence"]
    )
    assert change["old"]["relationships"][0]["evidence"][0]["provenance"] == "static-analysis"
    assert change["old"]["relationships"][0]["evidence"][0]["sites"][0]["path"] == "consumer.py"
    _assert_state(root, before)


def test_systems_public_route_composes_file_endpoint_evidence_only_as_status_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The After side keeps the matched endpoint while adding evidence only."""
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _commit_file_boundary_graph(root, label="legacy-api.py")

    original_producer = cli._produce_selection
    calls = 0
    observed_evidence: list[tuple[str, ...]] = []

    def controlled_producer(*args: object, **kwargs: object) -> object:
        nonlocal calls
        workspace, selection, result = original_producer(*args, **kwargs)  # type: ignore[arg-type]
        calls += 1
        controlled = _controlled_file_boundary_result(
            result, label="legacy-api.py", evidence_only=calls > 1
        )
        observed_evidence.append(
            tuple(
                evidence.provenance.value
                for evidence in controlled.document.relationships[0].evidence
            )
        )
        return (
            workspace,
            selection,
            controlled,
        )

    monkeypatch.setattr(cli, "_produce_selection", controlled_producer)
    before = _state(root)

    assert cli.main(["query", "diff", "--systems", "--json", "--details"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["changed"] is False
    assert payload["exit_code"] == 0
    assert payload["boundary_changes"] == []
    assert payload["surface_changes"] == []
    assert payload["consumer_changes"] == []
    assert payload["dependency_changes"] == []
    assert captured.err == ""
    assert observed_evidence == [("static-analysis",), ("static-analysis", "curated-rule")]
    _assert_state(root, before)


def test_systems_untracked_source_addition_is_a_detected_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / "app" / "unrelated.py").write_text("value = 1\n", encoding="utf-8")
    before = _state(root)

    assert cli.main(["query", "diff", "--systems", "--json"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["changed"] is True
    assert (
        payload["coverage"]["new"]["graph_files"]["count"]
        > payload["coverage"]["old"]["graph_files"]["count"]
    )
    added = [
        node
        for node in payload["comparison"]["nodes"]
        if node["status"] == "added" and isinstance(node["after"], dict)
    ]
    assert any(node["after"]["node"].get("path") == "app/unrelated.py" for node in added)
    # Genuinely changed captured content still changes the logical digest.
    assert (
        payload["comparison"]["before"]["source_digest"]
        != payload["comparison"]["after"]["source_digest"]
    )
    _assert_state(root, before)


def test_systems_keeps_absent_current_graph_and_sidecar_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    graph = root / "graph.json"
    graph.unlink()
    stamp_path(graph).unlink()
    before = _state(root)

    assert cli.main(["query", "diff", "--systems", "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["changed"] is False
    assert captured.err == ""
    assert before["files"]["graph.json"] is None  # type: ignore[index]
    assert before["files"]["graph.json.sha256"] is None  # type: ignore[index]
    _assert_state(root, before)


def test_systems_evidence_only_graph_change_is_status_zero_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    graph = root / "graph.json"
    payload = json.loads(graph.read_text(encoding="utf-8"))
    for relationship in payload["relationships"]:
        if relationship["kind"] == "calls":
            alternate = dict(relationship["evidence"][0])
            alternate["provenance"] = "curated-rule"
            alternate["rule"] = {"id": "alternate"}
            relationship["evidence"].append(alternate)
            break
    content = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    graph.write_bytes(content)
    stamp_path(graph).write_bytes((graph_digest(content) + "\n").encode("ascii"))
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "evidence-only historical change")
    before = _state(root)

    assert cli.main(["query", "diff", "--systems", "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["changed"] is False
    assert captured.err == ""
    _assert_state(root, before)


def test_systems_status_comes_from_typed_result_when_renderer_is_neutral(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / "app" / "api.py").write_text(
        "def receive():\n    return 1\n\ndef send():\n    return 2\n", encoding="utf-8"
    )
    (root / "consumer.py").write_text(
        "from app.api import receive, send\n\ndef consume():\n    receive()\n    return send()\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.system_diff_view, "render_text", lambda *_args, **_kwargs: "neutral\n")

    assert cli.main(["query", "diff", "--systems", "--system=App"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "neutral\n"
    assert captured.err == ""


def test_systems_public_route_accepts_supported_unresolved_origin_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    (root / "app").mkdir()
    (root / "app" / "api.py").write_text(
        "from missing import receive\n\ndef caller():\n    return receive()\n", encoding="utf-8"
    )
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\ntargets = ["app"]\n',
        encoding="utf-8",
    )
    definition = root / "docs" / "systems" / "app"
    definition.mkdir(parents=True)
    (definition / "system.toml").write_text(
        'schema_version = 1\nname = "App"\nfiles = ["app/api.py"]\n', encoding="utf-8"
    )
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "unresolved chain")

    assert cli.main(["query", "diff", "--systems", "--system=App", "--validate"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("no system differences\n")
    assert captured.err == ""


@pytest.mark.parametrize(
    ("before", "after"),
    (
        (None, "system: App"),
        ("system: App", None),
        ("system: App", "Ordinary"),
        ("Ordinary", "system: App"),
    ),
)
def test_literal_owner_graph_absent_membership(tmp_path, monkeypatch, capsys, before, after):
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    names = ("system: App", "Ordinary", "Unrelated")

    def definitions(owner):
        for index, name in enumerate(names):
            directory = root / "docs" / "systems" / ("app" if index == 0 else str(index))
            directory.mkdir(exist_ok=True)
            files = ["app/api.py"] if index == 0 else [f"placeholder{index}.py"]
            if name == owner:
                files.append("absent.py")
            (directory / "system.toml").write_text(
                f"schema_version = 1\nname = {json.dumps(name)}\nfiles = {json.dumps(files)}\n"
            )

    definitions(before)
    assert cli.main(["analyze"]) == 0
    capsys.readouterr()
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    definitions(after)
    for selected in (None, *names):
        arguments = ["query", "diff", "--systems"]
        if selected is not None:
            arguments += ["--system", selected]
        expected = int(selected is None or selected in (before, after))
        assert cli.main([*arguments, "--json"]) == expected
        payload = json.loads(capsys.readouterr().out)
        changes = payload["membership_changes"]
        assert changes == (
            [
                {
                    "domain": "membership",
                    "kind": "changed",
                    "key": ["absent.py"],
                    "old": {"file": "absent.py", "system": before},
                    "new": {"file": "absent.py", "system": after},
                    "involved_systems": sorted(
                        {name for name in (before, after) if name is not None}
                    ),
                }
            ]
            if expected
            else []
        )
        for flags in ([], ["--details"]):
            assert cli.main([*arguments, *flags]) == expected
            capsys.readouterr()


def _embedded_comparison_payload(html: str) -> dict[str, object]:
    """Extract the inert comparison payload from a rendered report."""
    prefix = '<script id="minotaur-presentation" type="application/json">'
    payload = json.loads(html.split(prefix, 1)[1].split("</script>", 1)[0])
    comparison = payload["comparison"]
    assert isinstance(comparison, dict)
    return comparison


def _record_identity(record: dict[str, object]) -> object:
    return record.get("id") or record.get("relationship_id")


def _comparison_fingerprint(records: object) -> list[dict[str, object]]:
    """Project the fields that both public outputs must store identically."""
    assert isinstance(records, list)
    return [
        {
            "id": _record_identity(record),
            "status": record.get("status"),
            "reasons": list(record.get("reasons") or []),
            "involved_systems": list(record.get("involved_systems") or []),
            "before": record.get("before"),
            "after": record.get("after"),
        }
        for record in records
        if isinstance(record, dict)
    ]


def _head_sha(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True
    ).stdout.strip()


_SIDE_RECORD_KEYS = frozenset(
    {"kind", "requested_revision", "commit", "config_path", "root", "targets", "source_digest"}
)


def _assert_side_record(record: object) -> dict[str, object]:
    """Require the exact captured-side shape and a hex content digest."""
    assert isinstance(record, dict)
    assert set(record) == set(_SIDE_RECORD_KEYS)
    assert isinstance(record["source_digest"], str)
    assert len(record["source_digest"]) == 64
    assert all(character in "0123456789abcdef" for character in record["source_digest"])
    assert isinstance(record["targets"], list)
    return record


def test_systems_two_revision_grammar_compares_pinned_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    base = _head_sha(root)
    (root / "app" / "api.py").write_text(
        "def receive():\n    return 1\n\ndef send():\n    return 2\n", encoding="utf-8"
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "changed")
    head = _head_sha(root)

    assert cli.main(["query", "diff", "--systems", base, "HEAD", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["changed"] is True
    assert payload["revisions"] == {"old": f"{base} · {base[:7]}", "new": f"HEAD · {head[:7]}"}
    assert payload["comparison"]["changed"] is True

    assert cli.main(["query", "diff", "--systems", "HEAD", "HEAD", "--json"]) == 0
    identical = json.loads(capsys.readouterr().out)
    assert identical["changed"] is False
    assert identical["comparison"]["changed"] is False

    # Captured-side records identify both pinned revisions and are stable for
    # the same repository state.
    assert cli.main(["query", "diff", "--systems", base, "HEAD", "--json"]) == 1
    context = json.loads(capsys.readouterr().out)["comparison"]
    before_record = _assert_side_record(context["before"])
    after_record = _assert_side_record(context["after"])
    assert context["schema_version"] == 1
    assert before_record["kind"] == "commit"
    assert before_record["requested_revision"] == base
    assert before_record["commit"] == base
    assert before_record["config_path"] == ".minotaur.toml"
    assert before_record["root"] == "."
    assert before_record["targets"] == ["app", "consumer.py"]
    assert after_record["kind"] == "commit"
    assert after_record["requested_revision"] == "HEAD"
    assert after_record["commit"] == head
    assert after_record["targets"] == ["app", "consumer.py"]
    assert before_record["source_digest"] != after_record["source_digest"]
    assert context["before"] == payload["comparison"]["before"]
    assert context["after"] == payload["comparison"]["after"]


def test_systems_working_tree_after_has_no_commit_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    head = _head_sha(root)

    assert cli.main(["query", "diff", "--systems", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["revisions"]["new"] == "Working tree at report generation"
    assert payload["revisions"]["old"].startswith("HEAD · ")
    assert head[:7] in payload["revisions"]["old"]
    assert payload["selected_system"] is None

    # The Before side is a real commit; the working-tree After side carries no
    # invented commit identity, so a fabricated SHA or a dropped field fails.
    comparison = payload["comparison"]
    assert comparison["schema_version"] == 1
    assert comparison["before"]["kind"] == "commit"
    assert comparison["before"]["requested_revision"] == "HEAD"
    assert comparison["before"]["commit"] == head
    assert comparison["after"]["kind"] == "working-tree"
    assert comparison["after"]["requested_revision"] is None
    assert comparison["after"]["commit"] is None
    # A clean working tree is logically identical to the captured commit, so
    # the logical manifest digest is identical. The manifest normalizes modes
    # to the exec/non-exec model, so this no longer varies with the process
    # umask; a difference here would leak filesystem permission bits.
    assert comparison["before"]["source_digest"] == comparison["after"]["source_digest"]


def test_systems_json_and_html_describe_the_same_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One changed invocation proves JSON and the embedded payload agree."""
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    head = _head_sha(root)
    (root / "app" / "api.py").write_text(
        "def receive():\n    return 1\n\ndef send():\n    return 2\n", encoding="utf-8"
    )
    (root / "consumer.py").write_text(
        "from app.api import receive, send\n\ndef consume():\n    receive()\n    return send()\n",
        encoding="utf-8",
    )
    report = root / "comparison.html"

    # A selected system scopes the top-level status while the complete context
    # stays in both outputs, so a viewer-side or divergent projection fails.
    status = cli.main(
        ["query", "diff", "--systems", "--system", "App", "--json", "--html", str(report)]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    embedded = _embedded_comparison_payload(report.read_text(encoding="utf-8"))
    context = payload["comparison"]

    assert status == 1
    assert captured.err == ""
    assert payload["selected_system"] == "App"
    assert context["selected_system"] == "App"
    assert context["changed"] is True
    assert embedded["changed"] is True
    assert context["revisions"] == embedded["revisions"]
    for key in ("nodes", "relationships", "call_changes"):
        assert _comparison_fingerprint(context[key]) == _comparison_fingerprint(embedded[key])
    assert context["limitations"] == embedded["limitations"]
    assert any(
        record["status"] != "unchanged"
        for record in _comparison_fingerprint(context["nodes"])
        if record["status"] is not None
    )

    # The schema-versioned captured-side records are present and identical in
    # both public outputs, so a removed or divergent field fails this proof.
    assert context["schema_version"] == 1
    assert embedded["schema_version"] == 1
    assert context["before"] == embedded["before"]
    assert context["after"] == embedded["after"]
    before_record = _assert_side_record(context["before"])
    after_record = _assert_side_record(context["after"])
    assert before_record["kind"] == "commit"
    assert before_record["requested_revision"] == "HEAD"
    assert before_record["commit"] == head
    assert before_record["config_path"] == ".minotaur.toml"
    assert before_record["root"] == "."
    assert before_record["targets"] == ["app", "consumer.py"]
    assert after_record["kind"] == "working-tree"
    assert after_record["requested_revision"] is None
    assert after_record["commit"] is None
    assert after_record["config_path"] == ".minotaur.toml"
    assert after_record["root"] == "."
    assert after_record["targets"] == ["app", "consumer.py"]
    assert before_record["source_digest"] != after_record["source_digest"]


def test_systems_html_unchanged_pair_reports_zero_without_touching_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    graph = root / "graph.json"
    sidecar = stamp_path(graph)
    report = root / "comparison.html"
    inputs = {
        ".minotaur.toml": (root / ".minotaur.toml").read_bytes(),
        "graph.json": graph.read_bytes(),
        "graph.json.sha256": sidecar.read_bytes(),
        "app/api.py": (root / "app" / "api.py").read_bytes(),
        "docs/systems/app/system.toml": (
            root / "docs" / "systems" / "app" / "system.toml"
        ).read_bytes(),
    }

    assert cli.main(["query", "diff", "--systems", "--html", str(report)]) == 0
    captured = capsys.readouterr()
    assert "no system differences" in captured.out
    assert captured.err == ""
    assert report.exists()
    for relative, content in inputs.items():
        assert (root / relative).read_bytes() == content


def test_systems_html_refuses_existing_report_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    report = root / "comparison.html"
    report.write_text("existing report", encoding="utf-8")

    assert cli.main(["query", "diff", "--systems", "--html", str(report)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "already exists" in captured.err
    assert "--force" in captured.err
    assert report.read_text(encoding="utf-8") == "existing report"


def test_systems_force_replaces_report_and_requires_html(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")

    assert cli.main(["query", "diff", "--systems", "--force"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--force requires --html" in captured.err

    report = root / "comparison.html"
    report.write_text("existing report", encoding="utf-8")
    assert cli.main(["query", "diff", "--systems", "--html", str(report), "--force"]) == 0
    assert "existing report" not in report.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "alias",
    ("source", "config", "graph", "definition", "git-metadata"),
)
def test_systems_force_refuses_protected_output_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    alias: str,
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    target = {
        "source": root / "app" / "api.py",
        "config": root / ".minotaur.toml",
        "graph": root / "graph.json",
        "definition": root / "docs" / "systems" / "app" / "system.toml",
        "git-metadata": root / ".git" / "config",
    }[alias]
    original = target.read_bytes()

    assert cli.main(["query", "diff", "--systems", "--html", str(target), "--force"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "comparison input" in captured.err or "protected" in captured.err
    assert target.read_bytes() == original


def test_systems_force_refuses_hardlink_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    source = root / "app" / "api.py"
    link = root / "comparison.html"
    os.link(source, link)
    original = source.read_bytes()

    assert cli.main(["query", "diff", "--systems", "--html", str(link), "--force"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "hard-links" in captured.err
    assert source.read_bytes() == original


def test_systems_output_directory_and_missing_parent_are_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")

    assert cli.main(["query", "diff", "--systems", "--html", str(root)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "directory" in captured.err

    assert cli.main(["query", "diff", "--systems", "--html", str(root / "missing" / "r.html")]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "parent directory does not exist" in captured.err


def test_systems_html_dangling_link_is_refused_before_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    resolved = tmp_path / "missing" / "report.html"
    link = tmp_path / "link.html"
    link.symlink_to(resolved)

    def unexpected_acquisition(*args: object, **kwargs: object) -> None:
        raise AssertionError("acquisition must not run for an invalid output parent")

    monkeypatch.setattr(cli, "prepare_comparison", unexpected_acquisition)
    capsys.readouterr()
    assert cli.main(["query", "diff", "--systems", "--html", str(link)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert str(resolved) in captured.err
    assert not resolved.parent.exists()


def test_systems_html_link_target_directory_replaced_during_comparison_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    given_parent = tmp_path / "A"
    given_parent.mkdir()
    resolved_parent = tmp_path / "B"
    resolved_parent.mkdir()
    old_parent = tmp_path / "B.old"
    link = given_parent / "report.html"
    link.symlink_to(resolved_parent / "report.html")
    original_acquire = cli.prepare_comparison

    def replace_parent(*args: object, **kwargs: object) -> object:
        resolved_parent.rename(old_parent)
        resolved_parent.mkdir()
        return original_acquire(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "prepare_comparison", replace_parent)
    capsys.readouterr()
    assert cli.main(["query", "diff", "--systems", "--html", str(link)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"output parent directory changed during comparison: {link}" in captured.err
    assert list(resolved_parent.iterdir()) == []
    assert list(old_parent.iterdir()) == []


def test_systems_html_link_target_directory_removed_during_comparison_preserves_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    given_parent = tmp_path / "A"
    given_parent.mkdir()
    resolved_parent = tmp_path / "B"
    resolved_parent.mkdir()
    old_parent = tmp_path / "B.old"
    (resolved_parent / "report.html").write_bytes(b"previous report")
    link = given_parent / "report.html"
    link.symlink_to(resolved_parent / "report.html")
    original_acquire = cli.prepare_comparison

    def remove_parent(*args: object, **kwargs: object) -> object:
        resolved_parent.rename(old_parent)
        return original_acquire(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "prepare_comparison", remove_parent)
    capsys.readouterr()
    assert cli.main(["query", "diff", "--systems", "--html", str(link), "--force"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"output parent directory is unavailable: {link}" in captured.err
    assert (old_parent / "report.html").read_bytes() == b"previous report"
    assert sorted(path.name for path in old_parent.iterdir()) == ["report.html"]
    assert not resolved_parent.exists()
    assert link.is_symlink()


def test_systems_html_intermediate_directory_link_retargeted_during_comparison_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    before = tmp_path / "B"
    before.mkdir()
    after = tmp_path / "C"
    after.mkdir()
    directory_link = tmp_path / "out"
    directory_link.symlink_to(before, target_is_directory=True)
    report = directory_link / "report.html"
    original_acquire = cli.prepare_comparison

    def retarget_parent(*args: object, **kwargs: object) -> object:
        directory_link.unlink()
        directory_link.symlink_to(after, target_is_directory=True)
        return original_acquire(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "prepare_comparison", retarget_parent)
    capsys.readouterr()
    assert cli.main(["query", "diff", "--systems", "--html", str(report)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"output parent directory changed during comparison: {report}" in captured.err
    assert list(before.iterdir()) == []
    assert list(after.iterdir()) == []


def test_systems_output_write_failure_preserves_existing_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    report = root / "comparison.html"
    report.write_text("existing report", encoding="utf-8")

    def fail_write(output: Path, content: bytes) -> None:
        raise OSError("simulated report write failure")

    monkeypatch.setattr(cli, "_write_atomically", fail_write)
    assert cli.main(["query", "diff", "--systems", "--html", str(report), "--force", "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "simulated report write failure" in captured.err
    assert report.read_text(encoding="utf-8") == "existing report"


def test_systems_output_created_during_comparison_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    report = root / "comparison.html"
    original_producer = cli._produce_selection

    def racing_producer(*args: object, **kwargs: object) -> object:
        result = original_producer(*args, **kwargs)  # type: ignore[arg-type]
        report.write_text("concurrent report", encoding="utf-8")
        return result

    monkeypatch.setattr(cli, "_produce_selection", racing_producer)
    assert cli.main(["query", "diff", "--systems", "--html", str(report)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "created during comparison" in captured.err
    assert report.read_text(encoding="utf-8") == "concurrent report"


def test_systems_side_config_overrides_select_each_side_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    (root / "alt.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        'targets = ["app", "consumer.py"]\nsystems_dir = "alt-systems"\n',
        encoding="utf-8",
    )
    alternative = root / "alt-systems" / "other"
    alternative.mkdir(parents=True)
    (alternative / "system.toml").write_text(
        'schema_version = 1\nname = "Other"\nfiles = ["app/api.py"]\n', encoding="utf-8"
    )
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    before = _state(root)

    assert cli.main(["query", "diff", "--systems", "--after-config", "alt.toml", "--json"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["old_system_names"] == ["App"]
    assert payload["new_system_names"] == ["Other"]
    assert captured.err == ""
    _assert_state(root, before)


def test_systems_config_route_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")

    assert cli.main(["query", "diff", "--systems", "--config", str(root / ".minotaur.toml")]) == 0
    capsys.readouterr()

    outside = tmp_path / "outside.toml"
    outside.write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "g.json"\ntargets = ["app"]\n',
        encoding="utf-8",
    )
    assert cli.main(["query", "diff", "--systems", "--config", str(outside)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "outside the invoking repository" in captured.err

    assert (
        cli.main(["query", "diff", "--systems", "--before-config", str(root / ".minotaur.toml")])
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "repository-relative" in captured.err


def test_systems_call_expression_only_change_is_status_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    base = _head_sha(root)
    (root / "consumer.py").write_text(
        "from app.api import receive\n\ndef consume():\n    return receive(2)\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "call change")

    assert cli.main(["query", "diff", "--systems", base, "HEAD", "--json"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["changed"] is True
    assert all(node["status"] == "unchanged" for node in payload["comparison"]["nodes"])
    assert all(
        relationship["status"] == "unchanged"
        for relationship in payload["comparison"]["relationships"]
    )
    assert any(item["status"] == "changed" for item in payload["comparison"]["call_changes"])


def test_systems_moved_call_is_not_a_detected_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D-10/V-06: a call that only shifts line is not a structural change."""
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    base = _head_sha(root)
    # Insert a blank line before the same call: the normalized expression is
    # byte-identical, only its evidence location moves.
    (root / "consumer.py").write_text(
        "from app.api import receive\n\ndef consume():\n\n    return receive()\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "shift only")

    assert cli.main(["query", "diff", "--systems", base, "HEAD", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["changed"] is False
    assert payload["exit_code"] == 0
    assert all(node["status"] == "unchanged" for node in payload["comparison"]["nodes"])
    assert all(
        relationship["status"] == "unchanged"
        for relationship in payload["comparison"]["relationships"]
    )
    assert payload["comparison"]["call_changes"]
    assert all(item["status"] == "unchanged" for item in payload["comparison"]["call_changes"])


def test_systems_duplicate_call_adds_multiplicity_without_expression_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pure duplicate add is a multiplicity change, not an expression edit."""
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    base = _head_sha(root)
    (root / "consumer.py").write_text(
        "from app.api import receive\n\ndef consume():\n    receive()\n    return receive()\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "duplicate call")

    assert cli.main(["query", "diff", "--systems", base, "HEAD", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    changed = [
        item for item in payload["comparison"]["call_changes"] if item["status"] != "unchanged"
    ]
    assert len(changed) == 1
    assert changed[0]["reasons"] == ["multiplicity_changed"]


def test_systems_large_report_warning_keeps_json_stdout_parseable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    report = root / "comparison.html"
    monkeypatch.setattr(cli, "_LARGE_ARTIFACT_BYTES", 10)

    assert cli.main(["query", "diff", "--systems", "--json", "--html", str(report)]) == 0
    captured = capsys.readouterr()
    assert "exceeds 10 MiB" in captured.err
    assert json.loads(captured.out)["changed"] is False


def test_systems_config_route_keeps_working_directory_meaning_from_nested_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    nested = root / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")

    assert cli.main(["query", "diff", "--systems", "--config", "../.minotaur.toml"]) == 0
    captured = capsys.readouterr()
    assert "no system differences" in captured.out
    assert captured.err == ""


def _embedded_presentation(html: str) -> dict[str, object]:
    """Extract the complete inert presentation payload from a rendered report."""
    prefix = '<script id="minotaur-presentation" type="application/json">'
    payload = json.loads(html.split(prefix, 1)[1].split("</script>", 1)[0])
    assert isinstance(payload, dict)
    return payload


def test_systems_per_side_target_present_on_opposite_side_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """C-01: a target absent on its own side is allowed when the coordinate
    exists on the opposite captured side, even if only one side declares it.

    ``b.py`` is committed at HEAD but selected only by the After config and
    deleted from the working tree, so it is a deletion representable from the
    Before tree rather than an absent-on-both-sides error.
    """
    root = _repo(tmp_path)
    (root / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (root / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\ntargets = ["a.py"]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / "b.py").unlink()
    (root / "alt.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\ntargets = ["b.py"]\n',
        encoding="utf-8",
    )

    assert cli.main(["query", "diff", "--systems", "--after-config", "alt.toml", "--json"]) == 1
    captured = capsys.readouterr()
    assert "absent on both comparison sides" not in captured.err
    assert json.loads(captured.out)["changed"] is True


def test_systems_captured_call_excerpts_resolve_under_the_side_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """C-07: captured excerpt bytes resolve under each side's configured root.

    With ``root = "src"`` the graph stores root-relative locations, so captured
    bytes must be read below ``<capture>/src`` or every call excerpt degrades to
    unavailable even though the exact captured source exists.
    """
    root = _repo(tmp_path)
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "pkg" / "api.py").write_text("def receive():\n    return 1\n", encoding="utf-8")
    (root / "src" / "consumer.py").write_text(
        "from pkg.api import receive\n\ndef consume():\n    return receive()\n",
        encoding="utf-8",
    )
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "src"\ngraph = "graph.json"\n'
        'targets = ["pkg", "consumer.py"]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / "src" / "consumer.py").write_text(
        "from pkg.api import receive\n\ndef consume():\n    return receive(2)\n",
        encoding="utf-8",
    )
    report = root / "comparison.html"

    status = cli.main(["query", "diff", "--systems", "--json", "--html", str(report)])
    capsys.readouterr()
    assert status == 1
    excerpts = _embedded_presentation(report.read_text(encoding="utf-8"))["excerpts"]
    assert isinstance(excerpts, dict)
    for side in ("before", "after"):
        rendered = excerpts[side]["paths"]["consumer.py"]
        assert rendered["status"] == "available"
        assert "receive" in json.dumps(rendered["spans"])


def test_systems_json_and_html_agree_under_a_configured_source_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-02/C-07: one sub-root invocation proves both public outputs describe
    the same captured sides and that captured excerpts resolve below each
    side's configured root rather than the snapshot root."""
    root = _repo(tmp_path)
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "pkg" / "api.py").write_text("def receive():\n    return 1\n", encoding="utf-8")
    (root / "src" / "consumer.py").write_text(
        "from pkg.api import receive\n\ndef consume():\n    return receive()\n",
        encoding="utf-8",
    )
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "src"\ngraph = "graph.json"\n'
        'targets = ["pkg", "consumer.py"]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    head = _head_sha(root)
    (root / "src" / "consumer.py").write_text(
        "from pkg.api import receive\n\ndef consume():\n    return receive(2)\n",
        encoding="utf-8",
    )
    report = root / "comparison.html"

    assert cli.main(["query", "diff", "--systems", "--json", "--html", str(report)]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    presentation = _embedded_presentation(report.read_text(encoding="utf-8"))
    context = payload["comparison"]

    assert context["changed"] is True
    assert context["before"] == presentation["comparison"]["before"]
    assert context["after"] == presentation["comparison"]["after"]
    assert context["before"]["root"] == "src"
    assert context["before"]["config_path"] == ".minotaur.toml"
    assert context["before"]["targets"] == ["src/consumer.py", "src/pkg"]
    assert context["before"]["commit"] == head
    assert context["after"]["kind"] == "working-tree"
    assert context["after"]["commit"] is None
    assert context["before"]["source_digest"] != context["after"]["source_digest"]
    for key in ("nodes", "relationships", "call_changes"):
        assert _comparison_fingerprint(context[key]) == _comparison_fingerprint(
            presentation["comparison"][key]
        )
    excerpts = presentation["excerpts"]
    assert isinstance(excerpts, dict)
    for side in ("before", "after"):
        rendered = excerpts[side]["paths"]["consumer.py"]
        assert rendered["status"] == "available"
        assert "receive" in json.dumps(rendered["spans"])


def test_systems_json_and_html_agree_for_one_side_target_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-02/C-01: a target declared by only one side is admitted when its
    coordinate exists on the opposite captured tree, and both public outputs
    describe the same admitted sides."""
    root = _repo(tmp_path)
    (root / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (root / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\ntargets = ["a.py"]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / "b.py").unlink()
    (root / "alt.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\ntargets = ["b.py"]\n',
        encoding="utf-8",
    )
    report = root / "comparison.html"

    assert (
        cli.main(
            [
                "query",
                "diff",
                "--systems",
                "--after-config",
                "alt.toml",
                "--json",
                "--html",
                str(report),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert "absent on both comparison sides" not in captured.err
    payload = json.loads(captured.out)
    presentation = _embedded_presentation(report.read_text(encoding="utf-8"))
    context = payload["comparison"]

    assert context["before"]["targets"] == ["a.py"]
    assert context["after"]["targets"] == ["b.py"]
    assert context["before"]["config_path"] == ".minotaur.toml"
    assert context["after"]["config_path"] == "alt.toml"
    assert context["before"] == presentation["comparison"]["before"]
    assert context["after"] == presentation["comparison"]["after"]
    assert context["changed"] is True


def test_capture_present_admits_only_regular_files_and_directories(tmp_path: Path) -> None:
    """C-01: a non-regular opposite entry is not evidence of a deletion."""
    root = tmp_path / "capture"
    root.mkdir()
    (root / "file.py").write_text("value = 1\n", encoding="utf-8")
    (root / "package").mkdir()
    (root / "link").symlink_to("file.py")
    (root / "dirlink").symlink_to("package")

    assert comparison._capture_present(root, "file.py") is True
    assert comparison._capture_present(root, "package") is True
    assert comparison._capture_present(root, ".") is True
    assert comparison._capture_present(root, "missing.py") is False
    assert comparison._capture_present(root, "link") is False
    assert comparison._capture_present(root, "dirlink") is False

    if hasattr(os, "mkfifo"):
        os.mkfifo(root / "pipe")
        assert comparison._capture_present(root, "pipe") is False


_ACCESSOR_LIB = """class Box:
    @property
    def v(self):
        return 1

    @v.setter
    def v(self, value):
        pass
"""

_ACCESSOR_CONSUMER = """from app.lib import Box

def use():
    box = Box()
    box.v = 2
    return box.v
"""

_OVERLOAD_MODULE = """from typing import overload


@overload
def f(x: int) -> int: ...
@overload
def f(x: str) -> str: ...
@overload
def f(x: bytes) -> bytes: ...
def f(x):
    return x
"""

_LAST_OVERLOAD_STUB = "@overload\ndef f(x: bytes) -> bytes: ...\n"

_JS_ACCESSOR_LIB = """export class Gauge {
  get level() {
    return 1;
  }

  set level(value) {
    this.value = value;
  }
}
"""

_JS_ACCESSOR_CONSUMER = """import { Gauge } from "./lib.js";

export function read() {
  const gauge = new Gauge();
  gauge.level = 2;
  return gauge.level;
}
"""


def _systems_repo(
    tmp_path: Path, files: dict[str, str], systems: dict[str, list[str]], targets: list[str]
) -> Path:
    """Commit sources, config and one system definition per named system."""
    root = _repo(tmp_path)
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        f"targets = {json.dumps(targets)}\n",
        encoding="utf-8",
    )
    for name, members in systems.items():
        definition = root / "docs" / "systems" / name.lower()
        definition.mkdir(parents=True)
        (definition / "system.toml").write_text(
            f"schema_version = 1\nname = {json.dumps(name)}\nfiles = {json.dumps(members)}\n",
            encoding="utf-8",
        )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    return root


def _accessor_repo(tmp_path: Path) -> Path:
    return _systems_repo(
        tmp_path,
        {"app/__init__.py": "", "app/lib.py": _ACCESSOR_LIB, "consumer.py": _ACCESSOR_CONSUMER},
        {"App": ["app/lib.py"], "Client": ["consumer.py"]},
        ["app", "consumer.py"],
    )


def _overload_repo(tmp_path: Path, source: str = _OVERLOAD_MODULE) -> Path:
    return _systems_repo(
        tmp_path,
        {"ov/__init__.py": "", "ov/mod.py": source},
        {"Ov": ["ov/mod.py"]},
        ["ov"],
    )


def _node_rows(payload: dict[str, object], status: str) -> list[dict[str, object]]:
    comparison = payload["comparison"]
    assert isinstance(comparison, dict)
    return [node for node in comparison["nodes"] if node["status"] == status]


def _row_node(row: dict[str, object]) -> dict[str, object]:
    side = row["after"] if row["status"] != "removed" else row["before"]
    # A list here means several occurrences merged under one correspondence key.
    assert isinstance(side, dict), (row["id"], row["reasons"])
    node = side["node"]
    assert isinstance(node, dict)
    return node


def _declaration_role(node: dict[str, object]) -> object:
    extensions = node.get("extensions") or {}
    assert isinstance(extensions, dict)
    namespace = node["identity"]["namespace"]  # type: ignore[index]
    return (extensions.get(namespace) or {}).get("declaration_role")


def _symbol_rows(payload: dict[str, object], label: str) -> list[tuple[str, object]]:
    """Return (status, role) for every comparison node row carrying ``label``."""
    comparison = payload["comparison"]
    assert isinstance(comparison, dict)
    return sorted(
        (
            (row["status"], _declaration_role(_row_node(row)))
            for row in comparison["nodes"]
            if _row_node(row).get("label") == label
        ),
        key=repr,
    )


def test_systems_python_property_accessors_compare_across_systems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A getter and setter consumed from another system correspond one-to-one."""
    root = _accessor_repo(tmp_path)
    monkeypatch.chdir(root)

    assert cli.main(["query", "diff", "--systems"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("no system differences\n")
    assert captured.err == ""

    (root / "consumer.py").write_text(
        _ACCESSOR_CONSUMER + "\n\ndef extra():\n    return Box()\n", encoding="utf-8"
    )
    assert cli.main(["query", "diff", "--systems"]) == 1
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0] == (
        "boundary added: Client.consumer.extra -> App.app.lib.Box (calls)"
    )
    assert captured.err == ""

    assert cli.main(["query", "diff", "--systems", "--json"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert _symbol_rows(payload, "app.lib.Box.v") == [
        ("unchanged", "property"),
        ("unchanged", "setter"),
    ]
    assert [_row_node(row)["label"] for row in _node_rows(payload, "added")] == ["consumer.extra"]
    assert captured.err == ""


def test_systems_python_setter_removal_is_one_removed_node_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Removing only the setter removes its own key and leaves the getter matched."""
    root = _accessor_repo(tmp_path)
    monkeypatch.chdir(root)
    getter_only = _ACCESSOR_LIB.split("\n    @v.setter")[0] + "\n"
    (root / "app" / "lib.py").write_text(getter_only, encoding="utf-8")

    assert cli.main(["query", "diff", "--systems", "--json"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    removed = _node_rows(payload, "removed")
    assert len(removed) == 1
    node = _row_node(removed[0])
    assert node["label"] == "app.lib.Box.v"
    assert _declaration_role(node) == "setter"
    assert node["location"]["range"]["start"]["line"] == 6  # type: ignore[index]
    assert _node_rows(payload, "added") == []
    assert _symbol_rows(payload, "app.lib.Box.v") == [
        ("removed", "setter"),
        ("unchanged", "property"),
    ]
    assert captured.err == ""


def test_systems_line_shift_above_accessors_keeps_every_node_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Accessor identity does not depend on source position."""
    root = _accessor_repo(tmp_path)
    monkeypatch.chdir(root)
    (root / "app" / "lib.py").write_text("\n\n\n" + _ACCESSOR_LIB, encoding="utf-8")

    assert cli.main(["query", "diff", "--systems", "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    nodes = payload["comparison"]["nodes"]
    assert nodes
    assert all(node["status"] == "unchanged" for node in nodes)
    assert _symbol_rows(payload, "app.lib.Box.v") == [
        ("unchanged", "property"),
        ("unchanged", "setter"),
    ]
    assert captured.err == ""


@pytest.mark.parametrize(
    "duplicate",
    (
        "\n    @property\n    def v(self):\n        return 2\n",
        "\n    @v.setter\n    def v(self, value):\n        pass\n",
    ),
    ids=("two-properties", "two-setters"),
)
def test_systems_duplicate_property_or_setter_definitions_stay_ambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    duplicate: str,
) -> None:
    """Accessor roles never number genuine duplicates, so refusal stands."""
    root = _accessor_repo(tmp_path)
    monkeypatch.chdir(root)
    (root / "app" / "lib.py").write_text(_ACCESSOR_LIB + duplicate, encoding="utf-8")

    assert cli.main(["query", "diff", "--systems"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("minotaur: error: ambiguous")


def test_systems_overload_stubs_compare_within_named_system(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Overload stubs and their implementation each keep a distinct identity."""
    root = _overload_repo(tmp_path)
    monkeypatch.chdir(root)

    assert cli.main(["query", "diff", "--systems", "--system", "Ov"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("no system differences\n")
    assert captured.err == ""

    (root / "ov" / "mod.py").write_text(
        _OVERLOAD_MODULE + "\n\ndef g():\n    return f(1)\n", encoding="utf-8"
    )
    assert cli.main(["query", "diff", "--systems", "--system", "Ov", "--json"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert [_row_node(row)["label"] for row in _node_rows(payload, "added")] == ["ov.mod.g"]
    assert _node_rows(payload, "removed") == []
    assert _node_rows(payload, "changed") == []
    assert _symbol_rows(payload, "ov.mod.f") == [
        ("unchanged", "overload"),
        ("unchanged", "overload"),
        ("unchanged", "overload"),
        ("unchanged", None),
    ]
    assert captured.err == ""


def test_systems_last_overload_stub_removal_removes_its_symbol_and_reference_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The removed stub's unresolved ``overload`` reference keeps its own origin key."""
    root = _overload_repo(tmp_path)
    monkeypatch.chdir(root)
    assert _LAST_OVERLOAD_STUB in _OVERLOAD_MODULE
    (root / "ov" / "mod.py").write_text(
        _OVERLOAD_MODULE.replace(_LAST_OVERLOAD_STUB, ""), encoding="utf-8"
    )

    assert cli.main(["query", "diff", "--systems", "--system", "Ov", "--json"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert [row["reasons"] for row in _node_rows(payload, "changed")] == []
    assert _node_rows(payload, "added") == []
    removed = sorted(
        (
            (
                _row_node(row)["node_class"],
                _row_node(row)["label"],
                _declaration_role(_row_node(row)),
                _row_node(row)["location"]["range"]["start"]["line"],  # type: ignore[index]
            )
            for row in _node_rows(payload, "removed")
        ),
        key=repr,
    )
    assert removed == [
        ("symbol", "ov.mod.f", "overload", 8),
        ("unresolved-reference", "overload", None, 7),
    ]
    assert captured.err == ""


def test_systems_aliased_overload_spelling_stays_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An aliased ``overload`` decorator has no role, so the stubs stay refused."""
    aliased = _OVERLOAD_MODULE.replace(
        "from typing import overload", "from typing import overload as ov"
    ).replace("@overload", "@ov")
    root = _overload_repo(tmp_path, aliased)
    monkeypatch.chdir(root)

    assert cli.main(["query", "diff", "--systems"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("minotaur: error: ambiguous")


def test_systems_javascript_class_accessors_compare_across_systems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A JavaScript get/set pair consumed from another system corresponds one-to-one."""
    root = _systems_repo(
        tmp_path,
        {"web/lib.js": _JS_ACCESSOR_LIB, "web/consumer.js": _JS_ACCESSOR_CONSUMER},
        {"Lib": ["web/lib.js"], "Client": ["web/consumer.js"]},
        ["web"],
    )
    monkeypatch.chdir(root)

    assert cli.main(["query", "diff", "--systems"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("no system differences\n")
    assert captured.err == ""

    (root / "web" / "lib.js").write_text(
        _JS_ACCESSOR_LIB + "\nexport function create() {\n  return new Gauge();\n}\n",
        encoding="utf-8",
    )
    (root / "web" / "consumer.js").write_text(
        _JS_ACCESSOR_CONSUMER.replace("{ Gauge }", "{ Gauge, create }")
        + "\nexport function make() {\n  return create();\n}\n",
        encoding="utf-8",
    )
    assert cli.main(["query", "diff", "--systems"]) == 1
    captured = capsys.readouterr()
    assert (
        "boundary added: Client.web/consumer.make -> Lib.web/lib.create (calls)"
        in captured.out.splitlines()
    )
    assert captured.err == ""

    assert cli.main(["query", "diff", "--systems", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert _symbol_rows(payload, "web/lib.Gauge.level") == [
        ("unchanged", "get"),
        ("unchanged", "set"),
    ]


def test_isolated_current_source_failure_names_the_file_in_the_capture_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interpreter_fault: InterpreterFault,
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    # consumer.py stays uncommitted so only the current side analyzes it.
    _git(root, "rm", "-q", "--cached", "consumer.py")
    _git(root, "commit", "-qm", "baseline")
    capsys.readouterr()
    before = _state(root)
    interpreter_fault.install("python", "consumer.py", RuntimeError("injected fault"))

    assert cli.main(["query", "diff", "--systems", "--system", "App"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "consumer.py" in captured.err
    assert "captured source analysis produced diagnostics" in captured.err
    assert "captured source production failed" not in captured.err
    assert "Traceback" not in captured.err
    _assert_state(root, before)


# --- Scoped capture through the public command -------------------------------


def _commit_all(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)


def _add_gitlink(root: Path, path: str) -> None:
    commit = _head_sha(root)
    result = subprocess.run(
        ["git", "update-index", "--add", "--cacheinfo", f"160000,{commit},{path}"],
        cwd=root,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def _edit_api(root: Path, value: int) -> None:
    (root / "app" / "api.py").write_text(f"def receive():\n    return {value}\n", encoding="utf-8")


def _diff(capsys: pytest.CaptureFixture[str], *arguments: str) -> tuple[int, str, str]:
    status = cli.main(["query", "diff", "--systems", *arguments])
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def _two_revision_repo(tmp_path: Path) -> Path:
    """A configured repository with one commit, ready for a second edit."""
    root = _configured_repo(tmp_path)
    (root / "notes.txt").write_text("notes\n", encoding="utf-8")
    _commit_all(root, "baseline")
    return root


@pytest.mark.parametrize("form", ("working-tree", "two-revision"))
def test_systems_diff_ignores_links_and_gitlinks_outside_the_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    form: str,
) -> None:
    """A tracked link or gitlink the analysis never reads does not refuse the run."""
    root = _two_revision_repo(tmp_path)
    (root / "schemas").mkdir()
    (root / "schemas" / "target.json").write_text("{}\n", encoding="utf-8")
    (root / "schemas" / "x.json").symlink_to("target.json")
    (root / "app" / "__pycache__").mkdir()
    (root / "app" / "__pycache__" / "real.py").write_text("x = 1\n", encoding="utf-8")
    (root / "app" / "__pycache__" / "cached.py").symlink_to("real.py")
    _git(root, "add", "-A")
    _git(root, "add", "-f", "app/__pycache__/cached.py", "app/__pycache__/real.py")
    _add_gitlink(root, "vendor")
    _git(root, "commit", "-qm", "links outside the selection")
    monkeypatch.chdir(root)
    arguments: tuple[str, ...] = ("--json",)
    if form == "two-revision":
        _edit_api(root, 2)
        _commit_all(root, "edit")
        arguments = ("HEAD~1", "HEAD", "--json")

    status, out, err = _diff(capsys, *arguments)

    assert status in (0, 1), err
    assert "unsafe entry" not in err
    assert json.loads(out)["comparison"]["before"]["kind"] == "commit"


def test_systems_diff_refuses_an_in_selection_symlink_in_the_before_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _two_revision_repo(tmp_path)
    (root / "app" / "alias.py").symlink_to("api.py")
    _commit_all(root, "link inside the target")
    (root / "app" / "alias.py").unlink()
    _commit_all(root, "remove link")
    monkeypatch.chdir(root)

    status, out, err = _diff(capsys, "HEAD~1", "HEAD")

    assert status == 2
    assert out == ""
    assert "before revision" in err
    assert "unsafe entry 'app/alias.py': symbolic links are not captured" in err


def test_systems_diff_refuses_an_in_selection_gitlink_in_the_before_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _two_revision_repo(tmp_path)
    _add_gitlink(root, "app/inner")
    _git(root, "commit", "-qm", "gitlink inside the target")
    _git(root, "rm", "--cached", "-q", "app/inner")
    _git(root, "commit", "-qm", "remove gitlink")
    monkeypatch.chdir(root)

    status, out, err = _diff(capsys, "HEAD~1", "HEAD")

    assert status == 2
    assert out == ""
    assert "before revision" in err
    assert "unsafe entry 'app/inner': Gitlink is not a source directory" in err


def test_systems_diff_refuses_a_linked_route_component_of_systems_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _two_revision_repo(tmp_path)
    _git(root, "mv", "docs", "realdocs")
    (root / "docs").symlink_to("realdocs")
    _commit_all(root, "docs is a link")
    _git(root, "rm", "-q", "--cached", "docs")
    (root / "docs").unlink()
    _git(root, "mv", "realdocs", "docs")
    _commit_all(root, "docs is a directory")
    monkeypatch.chdir(root)

    status, out, err = _diff(capsys, "HEAD~1", "HEAD")

    assert status == 2
    assert out == ""
    assert "symbolic links are not captured" in err
    assert "'docs'" in err


def test_systems_diff_refuses_a_linked_discovery_candidate_below_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _two_revision_repo(tmp_path)
    (root / "sub").mkdir()
    (root / "sub" / "keep.txt").write_text("keep\n", encoding="utf-8")
    _commit_all(root, "sub directory")
    (root / "sub" / ".minotaur.toml").symlink_to("../.minotaur.toml")
    _commit_all(root, "linked candidate")
    (root / "sub" / ".minotaur.toml").unlink()
    _commit_all(root, "remove linked candidate")
    monkeypatch.chdir(root / "sub")

    status, out, err = _diff(capsys, "HEAD~1", "HEAD")

    assert status == 2
    assert out == ""
    assert "sub/.minotaur.toml" in err
    assert "symbolic link" in err


def _minotaur_shaped_repo(tmp_path: Path) -> Path:
    """Config at the root with ``root = "src"`` and systems outside the root."""
    root = _repo(tmp_path)
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "pkg" / "mod.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "src"\ngraph = "../graph.json"\n'
        'systems_dir = "../docs/systems"\ntargets = ["pkg"]\n',
        encoding="utf-8",
    )
    definition = root / "docs" / "systems" / "core"
    definition.mkdir(parents=True)
    (definition / "system.toml").write_text(
        'schema_version = 1\nname = "core"\nfiles = ["pkg/mod.py"]\n',
        encoding="utf-8",
    )
    (root / "schemas").mkdir()
    (root / "schemas" / "v1.json").write_text("{}\n", encoding="utf-8")
    (root / "schemas" / "x.json").symlink_to("v1.json")
    _commit_all(root, "baseline")
    return root


def test_systems_diff_completes_on_a_repository_shaped_like_minotaur(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _minotaur_shaped_repo(tmp_path)
    monkeypatch.chdir(root)

    status, out, err = _diff(capsys, "--json")
    assert status == 0, err
    assert "unsafe entry" not in err
    assert json.loads(out)["comparison"]["after"]["kind"] == "working-tree"

    (root / "src" / "pkg" / "mod.py").write_text("def run():\n    return 2\n", encoding="utf-8")
    _commit_all(root, "edit")
    status, out, err = _diff(capsys, "HEAD~1", "HEAD", "--json")
    assert status in (0, 1), err
    assert "unsafe entry" not in err
    assert json.loads(out)["comparison"]["before"]["kind"] == "commit"


def test_systems_diff_refuses_a_regular_file_at_the_analysis_root_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-directory root entry is captured as listed, so analysis reports it."""
    root = _repo(tmp_path)
    (root / "src").write_text("not a directory\n", encoding="utf-8")
    config = (
        '[minotaur]\nschema_version = 1\nroot = "src"\ngraph = "../graph.json"\n'
        'systems_dir = "../docs/systems"\ntargets = ["pkg"]\n'
    )
    (root / ".minotaur.toml").write_text(config, encoding="utf-8")
    (root / "docs" / "systems" / "core").mkdir(parents=True)
    (root / "docs" / "systems" / "core" / "system.toml").write_text(
        'schema_version = 1\nname = "core"\nfiles = ["pkg/mod.py"]\n', encoding="utf-8"
    )
    _commit_all(root, "src is a file")
    (root / "src").unlink()
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "mod.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    _commit_all(root, "src is a directory")
    monkeypatch.chdir(root)

    status, out, err = _diff(capsys, "HEAD~1", "HEAD")

    assert status == 2
    assert out == ""
    assert "before" in err
    assert "analysis root is not an ordinary directory" in err


def test_systems_diff_captures_a_hidden_system_folder_below_a_dot_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unpruned systems tree wins over the enclosing pruned target."""
    root = _repo(tmp_path)
    (root / "app").mkdir()
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    _edit_api(root, 1)
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\ntargets = ["."]\n',
        encoding="utf-8",
    )
    hidden = root / "docs" / "systems" / ".experimental"
    hidden.mkdir(parents=True)
    (hidden / "system.toml").write_text(
        'schema_version = 1\nname = "Experimental"\nfiles = ["app/api.py"]\n', encoding="utf-8"
    )
    _commit_all(root, "baseline")
    (root / "app" / "api.py").write_text(
        "def receive():\n    return 1\n\ndef extra():\n    return receive()\n", encoding="utf-8"
    )
    _commit_all(root, "edit")
    monkeypatch.chdir(root)

    status, out, err = _diff(capsys, "HEAD~1", "HEAD", "--json")

    assert status == 1, err
    payload = json.loads(out)
    assert payload["new_system_names"] == ["Experimental"]
    assert [item["involved_systems"] for item in payload["call_changes"]] == [["Experimental"]]


def test_systems_digest_ignores_files_outside_the_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The digest covers exactly the captured selection, not the whole tree."""
    root = _two_revision_repo(tmp_path)
    monkeypatch.chdir(root)

    status, out, err = _diff(capsys, "--json")
    assert status == 0, err
    clean = json.loads(out)["comparison"]
    assert clean["before"]["source_digest"] == clean["after"]["source_digest"]

    (root / "extra").mkdir()
    (root / "extra" / "untracked.txt").write_text("new\n", encoding="utf-8")
    (root / "notes.txt").write_text("edited\n", encoding="utf-8")
    status, out, err = _diff(capsys, "--json")
    assert status == 0, err
    dirty = json.loads(out)["comparison"]
    assert dirty["after"]["source_digest"] == clean["after"]["source_digest"]
    assert dirty["after"]["source_digest"] == dirty["before"]["source_digest"]


class _GitRecorder:
    """Record the Git processes started while ``capture_pair`` runs, by side."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from minotaur import git
        from minotaur.git import PinnedCommit

        self.calls: list[tuple[str | None, tuple[str, ...]]] = []
        self.pair_calls = 0
        self.manifests: list[dict[str, tuple[str, ...]]] = []
        self._active = False
        self._side: str | None = None
        original_run = git.run_git
        original_blobs = PinnedCommit.stream_blobs
        original_listing = PinnedCommit.full_listing
        original_resolve = git.resolve_commit
        original_pair = comparison.capture_pair
        recorder = self

        def run_git(root: Path, arguments: object, **kwargs: object) -> object:
            if recorder._active:
                recorder.calls.append((recorder._side, tuple(arguments)))  # type: ignore[call-overload]
            return original_run(root, arguments, **kwargs)  # type: ignore[arg-type]

        def stream_blobs(pin: PinnedCommit, requested: object, consume: object) -> object:
            previous, recorder._side = recorder._side, pin.side
            try:
                return original_blobs(pin, requested, consume)  # type: ignore[arg-type]
            finally:
                recorder._side = previous

        def full_listing(pin: PinnedCommit) -> object:
            previous, recorder._side = recorder._side, pin.side
            try:
                return original_listing(pin)
            finally:
                recorder._side = previous

        def resolve_commit(root: Path, revision: str, *, side: str = "historical") -> str:
            previous, recorder._side = recorder._side, side
            try:
                return original_resolve(root, revision, side=side)
            finally:
                recorder._side = previous

        def capture_pair(*args: object, **kwargs: object) -> object:
            recorder.pair_calls += 1
            recorder._active = True
            try:
                before, after, result = original_pair(*args, **kwargs)  # type: ignore[arg-type]
            finally:
                recorder._active = False
            recorder.manifests.append(
                {
                    "before": tuple(entry.path for entry in before.manifest.entries),
                    "after": tuple(entry.path for entry in after.manifest.entries),
                }
            )
            return before, after, result

        monkeypatch.setattr(git, "run_git", run_git)
        monkeypatch.setattr(git, "resolve_commit", resolve_commit)
        monkeypatch.setattr(PinnedCommit, "stream_blobs", stream_blobs)
        monkeypatch.setattr(PinnedCommit, "full_listing", full_listing)
        monkeypatch.setattr(comparison, "capture_pair", capture_pair)

    def subcommands(self, side: str | None) -> list[str]:
        return [arguments[0] for owner, arguments in self.calls if owner == side]


def _selection_repo(tmp_path: Path) -> Path:
    root = _configured_repo(tmp_path)
    (root / "sub").mkdir()
    (root / "sub" / "keep.txt").write_text("keep\n", encoding="utf-8")
    (root / "other").mkdir()
    (root / "other" / "data.txt").write_text("unrelated\n", encoding="utf-8")
    (root / "alt.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        'targets = ["app", "consumer.py"]\n',
        encoding="utf-8",
    )
    _commit_all(root, "baseline")
    _edit_api(root, 2)
    _commit_all(root, "edit")
    return root


_SELECTED_PATHS = (
    ".minotaur.toml",
    "app",
    "app/__init__.py",
    "app/api.py",
    "consumer.py",
    "docs",
    "docs/systems",
    "docs/systems/app",
    "docs/systems/app/system.toml",
    # The start directory is the route directory of the absent discovery
    # candidate ``sub/.minotaur.toml``; the unrelated ``other`` is not selected.
    "sub",
)


@pytest.mark.parametrize("explicit_after_config", (False, True))
def test_systems_two_revision_capture_is_exactly_the_union_selection_in_bounded_git_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    explicit_after_config: bool,
) -> None:
    root = _selection_repo(tmp_path)
    monkeypatch.chdir(root / "sub")
    recorder = _GitRecorder(monkeypatch)
    arguments = ["HEAD~1", "HEAD", "--json"]
    expected = _SELECTED_PATHS
    if explicit_after_config:
        arguments = ["--after-config", "alt.toml", *arguments]
        expected = tuple(sorted((*_SELECTED_PATHS, "alt.toml")))

    status, _out, err = _diff(capsys, *arguments)

    assert status in (0, 1), err
    assert recorder.pair_calls == 1
    manifest = recorder.manifests[0]
    assert manifest["before"] == expected
    assert manifest["after"] == expected
    assert "other/data.txt" not in manifest["before"]
    for side in ("before", "after"):
        commands = recorder.subcommands(side)
        assert commands.count("rev-parse") == 1
        assert commands.count("ls-tree") == 1
        assert 1 <= commands.count("cat-file") <= 2
        assert "show" not in commands
        assert len(commands) <= 4
    listing = [args for owner, args in recorder.calls if args[0] == "ls-tree"]
    assert all(args[:4] == ("ls-tree", "-r", "-t", "-z") for args in listing)


def test_systems_working_tree_capture_issues_only_the_head_probe_on_the_after_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _selection_repo(tmp_path)
    monkeypatch.chdir(root / "sub")
    recorder = _GitRecorder(monkeypatch)

    status, _out, err = _diff(capsys, "--json")

    assert status in (0, 1), err
    assert recorder.pair_calls == 1
    assert recorder.manifests[0]["after"] == _SELECTED_PATHS
    assert recorder.subcommands("after") == ["rev-parse"]
    before = recorder.subcommands("before")
    assert before.count("rev-parse") == 1 and before.count("ls-tree") == 1
    assert 1 <= before.count("cat-file") <= 2 and "show" not in before


def _normalized_git_calls(recorder: _GitRecorder) -> list[tuple[str | None, tuple[str, ...]]]:
    import re

    return [
        (side, tuple(re.sub(r"[0-9a-f]{40}", "<sha>", item) for item in arguments))
        for side, arguments in recorder.calls
    ]


def test_systems_capture_git_work_does_not_grow_with_files_outside_the_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sequences = []
    for name, extra in (("small", 0), ("large", 2000)):
        base = tmp_path / name
        base.mkdir()
        root = _configured_repo(base)
        bulk = root / "bulk"
        bulk.mkdir()
        for index in range(extra):
            (bulk / f"file{index}.txt").write_text(f"{index}\n", encoding="utf-8")
        (root / "notes.txt").write_text("notes\n", encoding="utf-8")
        _commit_all(root, "baseline")
        _edit_api(root, 2)
        _commit_all(root, "edit")
        monkeypatch.chdir(root)
        with monkeypatch.context() as scoped:
            recorder = _GitRecorder(scoped)
            status, _out, err = _diff(capsys, "HEAD~1", "HEAD", "--json")
        assert status in (0, 1), err
        sequences.append(_normalized_git_calls(recorder))

    assert sequences[0]
    assert sequences[0] == sequences[1]


def _capture_tempdirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import tempfile

    temporary = tmp_path / "tmpdir"
    temporary.mkdir()
    monkeypatch.setenv("TMPDIR", str(temporary))
    monkeypatch.setattr(tempfile, "tempdir", None)
    return temporary


def _leftovers(temporary: Path) -> list[str]:
    return sorted(
        item.name
        for item in temporary.iterdir()
        if item.name.startswith(("minotaur-revision-", "minotaur-working-"))
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_systems_working_tree_capture_reports_an_unreadable_directory_inside_a_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    (root / "app" / "inner").mkdir()
    (root / "app" / "inner" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (root / "other").mkdir()
    (root / "other" / "data.txt").write_text("unrelated\n", encoding="utf-8")
    _commit_all(root, "baseline")
    (root / "app" / "__pycache__").mkdir()
    (root / "app" / "__pycache__" / "cached.pyc").write_text("x", encoding="utf-8")
    temporary = _capture_tempdirs(tmp_path, monkeypatch)
    monkeypatch.chdir(root)

    inside = root / "app" / "inner"
    inside.chmod(0)
    try:
        status, out, err = _diff(capsys)
    finally:
        inside.chmod(0o755)
    assert status == 2
    assert out == ""
    assert "app/inner" in err
    assert _leftovers(temporary) == []

    for harmless in (root / "other", root / "app" / "__pycache__"):
        harmless.chmod(0)
        try:
            status, out, err = _diff(capsys)
        finally:
            harmless.chmod(0o755)
        assert status in (0, 1), err
        assert out != ""
        assert _leftovers(temporary) == []


def _commit_config(root: Path, text: str) -> None:
    (root / ".minotaur.toml").write_text(text, encoding="utf-8")
    _commit_all(root, "committed config")
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        'targets = ["app", "consumer.py"]\n',
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("committed", "message"),
    (
        (
            '[minotaur]\nroot = "."\ngraph = "graph.json"\ntargets = ["app"]\n',
            "at '.minotaur.toml' (ConfigError): "
            "missing required field: schema_version (in .minotaur.toml)",
        ),
        (
            "x = 1\n",
            "at '.minotaur.toml' (ConfigError): missing [minotaur] section in .minotaur.toml",
        ),
        (
            '[minotaur]\nschema_version = 1\nroot = "../.."\ngraph = "graph.json"\n'
            'targets = ["app"]\n',
            "at '../..': unresolved traversal in analysis root",
        ),
    ),
)
def test_systems_working_tree_form_attributes_a_bad_head_config_without_temporary_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    committed: str,
    message: str,
) -> None:
    root = _configured_repo(tmp_path)
    _commit_config(root, committed)
    temporary = _capture_tempdirs(tmp_path, monkeypatch)
    monkeypatch.chdir(root)

    status, out, err = _diff(capsys)

    assert status == 2
    assert out == ""
    assert err == f"minotaur: error: before input revision 'HEAD' {message}\n"
    assert str(temporary) not in err
    assert _leftovers(temporary) == []


_NO_SCHEMA = '[minotaur]\nroot = "."\ngraph = "graph.json"\ntargets = ["app", "consumer.py"]\n'
_NO_SCHEMA_MESSAGE = "missing required field: schema_version (in .minotaur.toml)"


def _commit_config_at(root: Path, text: str, message: str) -> str:
    (root / ".minotaur.toml").write_text(text, encoding="utf-8")
    _commit_all(root, message)
    return _head_sha(root)


def test_systems_two_revision_form_reports_the_underlying_config_error_for_a_bad_before_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    good = (root / ".minotaur.toml").read_text(encoding="utf-8")
    before = _commit_config_at(root, _NO_SCHEMA, "bad before")
    after = _commit_config_at(root, good, "good after")
    temporary = _capture_tempdirs(tmp_path, monkeypatch)
    monkeypatch.chdir(root)

    status, out, err = _diff(capsys, before, after)

    assert status == 2
    assert out == ""
    assert err == (
        f"minotaur: error: before input revision '{before}' at '.minotaur.toml' "
        f"(ConfigError): {_NO_SCHEMA_MESSAGE}\n"
    )
    assert str(temporary) not in err
    assert _leftovers(temporary) == []


def test_systems_two_revision_form_reports_the_underlying_config_error_for_a_bad_after_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    good = (root / ".minotaur.toml").read_text(encoding="utf-8")
    before = _commit_config_at(root, good, "good before")
    after = _commit_config_at(root, _NO_SCHEMA, "bad after")
    temporary = _capture_tempdirs(tmp_path, monkeypatch)
    monkeypatch.chdir(root)

    status, out, err = _diff(capsys, before, after)

    assert status == 2
    assert out == ""
    assert err == (
        f"minotaur: error: after input revision '{after}' at '.minotaur.toml' "
        f"(ConfigError): {_NO_SCHEMA_MESSAGE}\n"
    )
    assert str(temporary) not in err
    assert _leftovers(temporary) == []


def test_systems_two_revision_form_keeps_a_non_config_read_failure_free_of_capture_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A config coordinate that is a directory on one side must not leak the capture path."""
    root = _configured_repo(tmp_path)
    (root / ".minotaur.toml").unlink()
    nested = root / "sub" / ".minotaur.toml"
    nested.mkdir(parents=True)
    (nested / "placeholder.txt").write_text("x\n", encoding="utf-8")
    before = _commit_config_at_path(root, "directory config")
    for item in nested.iterdir():
        item.unlink()
    nested.rmdir()
    nested.write_text(
        '[minotaur]\nschema_version = 1\nroot = ".."\ngraph = "graph.json"\n'
        'targets = ["app", "consumer.py"]\n',
        encoding="utf-8",
    )
    after = _commit_config_at_path(root, "file config")
    temporary = _capture_tempdirs(tmp_path, monkeypatch)
    monkeypatch.chdir(root / "sub")

    status, out, err = _diff(capsys, before, after)

    assert status == 2
    assert out == ""
    assert err == (
        f"minotaur: error: before input revision '{before}' at 'sub/.minotaur.toml' "
        "(IsADirectoryError): invalid captured configuration\n"
    )
    assert str(temporary) not in err
    assert _leftovers(temporary) == []


def _commit_config_at_path(root: Path, message: str) -> str:
    _commit_all(root, message)
    return _head_sha(root)


def test_systems_working_tree_form_reports_the_analyze_message_for_a_bad_live_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    _commit_all(root, "baseline")
    (root / ".minotaur.toml").write_text(_NO_SCHEMA, encoding="utf-8")
    temporary = _capture_tempdirs(tmp_path, monkeypatch)
    monkeypatch.chdir(root)

    assert cli.main(["analyze"]) == 2
    analyze_err = capsys.readouterr().err
    prefix = "minotaur: error: "
    assert analyze_err.startswith(prefix) and analyze_err.endswith("\n")
    message = analyze_err[len(prefix) : -1]
    assert message  # measured, not hard-coded

    status, out, err = _diff(capsys)

    assert status == 2
    assert out == ""
    assert err == (
        f"minotaur: error: current input at '{root / '.minotaur.toml'}' (ConfigError): {message}\n"
    )
    assert str(temporary) not in err
    assert _leftovers(temporary) == []


def test_systems_working_tree_form_reports_an_untracked_live_config_as_absent_on_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    (root / ".minotaur.toml").rename(root / "live.toml")
    _commit_all(root, "no committed config")
    (root / "live.toml").rename(root / ".minotaur.toml")
    temporary = _capture_tempdirs(tmp_path, monkeypatch)
    monkeypatch.chdir(root)

    status, out, err = _diff(capsys)

    assert status == 2
    assert out == ""
    assert "before input revision 'HEAD'" in err
    assert "required config is absent" in err
    assert str(temporary) not in err
    assert _leftovers(temporary) == []


def test_systems_two_revision_form_captures_a_target_declared_only_by_the_after_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The union selection keeps the before copy of a target only the after config names."""
    root = _repo(tmp_path)
    (root / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (root / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\ntargets = ["a.py"]\n',
        encoding="utf-8",
    )
    _commit_all(root, "baseline")
    (root / "b.py").unlink()
    (root / "alt.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\ntargets = ["b.py"]\n',
        encoding="utf-8",
    )
    _commit_all(root, "drop b")
    monkeypatch.chdir(root)
    recorder = _GitRecorder(monkeypatch)

    status, _out, err = _diff(capsys, "--after-config", "alt.toml", "HEAD~1", "HEAD", "--json")

    assert status in (0, 1), err
    assert "absent on both comparison sides" not in err
    assert "b.py" in recorder.manifests[0]["before"]
    assert "b.py" not in recorder.manifests[0]["after"]


def test_the_single_side_capture_functions_are_gone() -> None:
    from minotaur import comparison_snapshot

    for name in (
        "capture_revision",
        "capture_local_revision",
        "capture_working_tree",
        "_tree_files",
        "_materialize_tree",
    ):
        assert not hasattr(comparison_snapshot, name)
        assert name not in comparison_snapshot.__all__


def _rev_sha(root: Path, revision: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", f"{revision}^{{commit}}"],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


@pytest.mark.parametrize("form", ("discovery", "explicit-after", "working-tree"))
def test_systems_capture_starts_only_side_owned_listing_and_batch_git_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    form: str,
) -> None:
    """No Git process outside the per-side probe, listing and batches runs during capture.

    Counting only side-attributed calls would miss a per-path ``git show`` (or
    any other Git process) started outside the pinned listing and batch
    readers, so every recorded call must belong to a side and have the bounded
    shape.
    """
    root = _selection_repo(tmp_path)
    shas = {"before": _rev_sha(root, "HEAD~1"), "after": _rev_sha(root, "HEAD")}
    monkeypatch.chdir(root / "sub")
    recorder = _GitRecorder(monkeypatch)
    arguments = ["HEAD~1", "HEAD", "--json"]
    if form == "explicit-after":
        arguments = ["--after-config", "alt.toml", *arguments]
    elif form == "working-tree":
        arguments = ["--json"]
        shas["before"] = shas["after"]

    status, _out, err = _diff(capsys, *arguments)

    assert status in (0, 1), err
    assert recorder.pair_calls == 1
    assert [arguments for owner, arguments in recorder.calls if owner is None] == []
    for side in ("before", "after"):
        calls = [arguments for owner, arguments in recorder.calls if owner == side]
        assert [call[:2] for call in calls if call[0] == "rev-parse"] == [("rev-parse", "--verify")]
        if side == "after" and form == "working-tree":
            assert len(calls) == 1
            continue
        assert [call for call in calls if call[0] == "ls-tree"] == [
            ("ls-tree", "-r", "-t", "-z", shas[side])
        ]
        batches = [call for call in calls if call[0] == "cat-file"]
        assert 1 <= len(batches) <= 2
        assert all(call == ("cat-file", "--batch") for call in batches)
        assert len(calls) == 2 + len(batches)


def _alternative_config_repo(tmp_path: Path) -> Path:
    """A committed repository whose ``alt.toml`` names a different systems folder."""
    root = _configured_repo(tmp_path)
    (root / "alt.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        'targets = ["app", "consumer.py"]\nsystems_dir = "alt-systems"\n',
        encoding="utf-8",
    )
    alternative = root / "alt-systems" / "other"
    alternative.mkdir(parents=True)
    (alternative / "system.toml").write_text(
        'schema_version = 1\nname = "Other"\nfiles = ["app/api.py"]\n', encoding="utf-8"
    )
    _commit_all(root, "baseline")
    return root


@pytest.mark.parametrize("form", ("working-tree", "two-revision"))
@pytest.mark.parametrize(
    ("option", "expected_before", "expected_after"),
    (
        ("--before-config", ["Other"], ["App"]),
        ("--config", ["Other"], ["Other"]),
    ),
)
def test_systems_before_and_shared_config_options_select_the_captured_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    form: str,
    option: str,
    expected_before: list[str],
    expected_after: list[str],
) -> None:
    """``--before-config`` and ``--config`` decide which configuration each side analyzes.

    The discoverable ``.minotaur.toml`` declares the ``App`` system, so a side
    that fell back to discovery instead of the requested ``alt.toml`` would
    report ``App`` there.
    """
    root = _alternative_config_repo(tmp_path)
    monkeypatch.chdir(root)
    revisions = ("HEAD", "HEAD") if form == "two-revision" else ()

    status, out, err = _diff(capsys, option, "alt.toml", *revisions, "--json")

    assert status == (0 if expected_before == expected_after else 1), err
    payload = json.loads(out)
    assert payload["old_system_names"] == expected_before
    assert payload["new_system_names"] == expected_after
    assert payload["comparison"]["before"]["config_path"] == "alt.toml"
    expected_after_path = "alt.toml" if option == "--config" else ".minotaur.toml"
    assert payload["comparison"]["after"]["config_path"] == expected_after_path


_ACQUISITION_FORMS: tuple[tuple[str, tuple[str, ...], dict[str, object]], ...] = (
    ("working-tree", (), {}),
    ("revisions", ("v1", "v2"), {"before": "v1", "after": "v2"}),
    (
        "side-configs",
        ("--before-config", "a.toml", "--after-config", "./b.toml"),
        {"before_config": "a.toml", "after_config": "b.toml"},
    ),
    (
        "side-configs-with-revisions",
        ("v1", "v2", "--before-config", "a.toml", "--after-config", "sub/../b.toml"),
        {"before": "v1", "after": "v2", "before_config": "a.toml", "after_config": "b.toml"},
    ),
    ("config", ("--config", ".minotaur.toml"), {"raw_config_path": ".minotaur.toml"}),
    ("json", ("--json",), {}),
    ("details", ("--details",), {}),
    ("system", ("--system", "App"), {}),
    ("validate", ("--validate",), {"validate": True}),
    ("html", ("--html", "{html}"), {}),
)


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        pytest.param(arguments, expected, id=name)
        for name, arguments, expected in _ACQUISITION_FORMS
    ],
)
def test_systems_diff_forms_acquire_through_prepare_comparison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: tuple[str, ...],
    expected: dict[str, object],
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    received: list[dict[str, object]] = []

    def stub(
        start: Path,
        raw_config_path: Path | None,
        producer: object,
        validate: bool = False,
        before: str | None = None,
        after: str | None = None,
        before_config: str | None = None,
        after_config: str | None = None,
    ) -> object:
        received.append(
            {
                "raw_config_path": raw_config_path,
                "validate": validate,
                "before": before,
                "after": after,
                "before_config": before_config,
                "after_config": after_config,
            }
        )
        raise ValueError("acquisition stub")

    monkeypatch.setattr(cli, "prepare_comparison", stub)
    html = tmp_path / "fresh-report.html"
    argv = [argument.format(html=html) for argument in arguments]

    assert cli.main(["query", "diff", "--systems", *argv]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "minotaur: error: acquisition stub\n"
    defaults: dict[str, object] = {
        "raw_config_path": None,
        "validate": False,
        "before": None,
        "after": None,
        "before_config": None,
        "after_config": None,
    }
    assert received == [{**defaults, **expected}]
    assert not html.exists()


def test_systems_diff_cli_imports_no_acquisition_internals() -> None:
    source = Path(cli.__file__).read_text(encoding="utf-8")
    assert "_acquire_systems_pair" not in source
    for name in (
        "_captured_analysis",
        "_comparison_plan",
        "_current_config_route",
        "_select_worktree",
        "_validate_live_current_contract",
        "capture_pair",
    ):
        assert not hasattr(cli, name)


def test_systems_force_refuses_a_root_level_definition_in_a_revision_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    (root / "app").mkdir()
    (root / "app" / "api.py").write_text("def receive():\n    return 1\n", encoding="utf-8")
    (root / "core").mkdir()
    definition = root / "core" / "system.toml"
    definition.write_text(
        'schema_version = 1\nname = "core"\nfiles = ["app/api.py"]\n', encoding="utf-8"
    )
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        'targets = ["app"]\nsystems_dir = "."\n',
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    monkeypatch.chdir(root)
    original = definition.read_bytes()

    status = cli.main(
        ["query", "diff", "--systems", "HEAD", "HEAD", "--html", str(definition), "--force"]
    )
    captured = capsys.readouterr()
    assert status == 2
    assert captured.out == ""
    assert captured.err == (
        f"minotaur: error: output path is also a comparison input: {definition.resolve()}\n"
    )
    assert definition.read_bytes() == original


def test_systems_force_refuses_the_graph_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    sidecar = root / "graph.json.sha256"
    original = sidecar.read_bytes()
    capsys.readouterr()

    status = cli.main(["query", "diff", "--systems", "--html", str(sidecar), "--force"])
    captured = capsys.readouterr()
    assert status == 2
    assert captured.out == ""
    assert captured.err == (
        f"minotaur: error: output path is also a comparison input: {sidecar.resolve()}\n"
    )
    assert sidecar.read_bytes() == original


def test_systems_refuses_a_new_report_inside_the_systems_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    report = root / "docs" / "systems" / "report.html"
    capsys.readouterr()

    status = cli.main(["query", "diff", "--systems", "--html", str(report)])
    captured = capsys.readouterr()
    assert status == 2
    assert captured.out == ""
    assert captured.err == (
        f"minotaur: error: output path is inside a protected comparison input: {report.resolve()}\n"
    )
    assert not report.exists()


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (
            "/elsewhere//x.toml/",
            "--config path is outside the invoking repository: /elsewhere//x.toml/",
        ),
        ("", "--config requires a non-empty file path"),
    ],
)
def test_systems_invalid_config_spelling_is_reported_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raw: str,
    message: str,
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")

    assert cli.main(["query", "diff", "--systems", "--config", raw]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"minotaur: error: {message}\n"


_WALKTHROUGH = Path(__file__).resolve().parents[2] / "examples" / "system-walkthrough"
_ROW_PREFIXES = ("node ", "relationship ", "call ")
_FEE_ROW = "relationship added: shop.orders._fee -> shop.billing.charge (calls)"


def _walkthrough_repo(tmp_path: Path) -> Path:
    """The shop walkthrough tree and definitions with two private helpers added."""
    root = _repo(tmp_path)
    for name in ("shop", "docs"):
        shutil.copytree(_WALKTHROUGH / name, root / name)
    shutil.copy(_WALKTHROUGH / ".minotaur.toml", root / ".minotaur.toml")
    _commit_all(root, "baseline")
    with (root / "shop" / "orders.py").open("a", encoding="utf-8") as handle:
        handle.write(
            "\n\ndef _fee(order):\n    return charge(order)\n\n\ndef _settle(order):\n"
            "    return _fee(order)\n"
        )
    return root


def _difference_rows(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith(_ROW_PREFIXES)]


def _evidence_json(output: str, row: str, label: str) -> object:
    lines = output.splitlines()
    index = lines.index(row)
    for line in lines[index + 1 : index + 5]:
        if line.startswith(f"{label}: "):
            value = line[len(label) + 2 :]
            return value if value == "unavailable" else json.loads(value)
    raise AssertionError(f"{label} missing after {row}")


def test_systems_diff_walkthrough_rows_evidence_and_system_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _walkthrough_repo(tmp_path)
    monkeypatch.chdir(root)
    call_row = "call added: shop.orders._fee -> shop.billing.charge"

    default_status, default, default_err = _diff(capsys)
    details_status, details, details_err = _diff(capsys, "--details")
    billing_status, billing, _ = _diff(capsys, "--system", "billing")

    # Every row prints by default and with --details, and the status is 1.
    for status, out, err in (
        (default_status, default, default_err),
        (details_status, details, details_err),
    ):
        assert (status, err) == (1, "")
        assert _difference_rows(out) == [
            "node added: shop.orders._fee (shop/orders.py, function)",
            "node added: shop.orders._settle (shop/orders.py, function)",
            "relationship added: shop.orders -> shop.orders._fee (contains)",
            "relationship added: shop.orders -> shop.orders._settle (contains)",
            _FEE_ROW,
            "relationship added: shop.orders._settle -> shop.orders._fee (calls)",
            call_row,
            "call added: shop.orders._settle -> shop.orders._fee",
        ]
        assert "no system differences" not in out
        assert "node:comparison:" not in out
        # The status word is the change status, not the edge kind.
        assert "relationship calls:" not in out
        assert out.index("boundary added:") < out.index("node added:") < out.index("old coverage:")

    # --details prints stored evidence for each row class.
    relationship = _evidence_json(details, _FEE_ROW, "new evidence")
    assert isinstance(relationship, list) and len(relationship) == 1
    assert relationship[0]["provenance"] == "static-analysis"
    assert relationship[0]["locations"][0]["path"] == "shop/orders.py"
    assert _evidence_json(details, _FEE_ROW, "old evidence") == "unavailable"
    assert _evidence_json(details, call_row, "new evidence") == [relationship[0]["locations"][0]]
    node_row = "node added: shop.orders._fee (shop/orders.py, function)"
    assert _evidence_json(details, node_row, "new evidence") == "unavailable"
    boundary = "boundary added: orders.shop.orders._fee -> billing.shop.billing.charge (calls)"
    assert isinstance(_evidence_json(details, boundary, "new evidence"), list)

    # --system billing: the crossing relationship's source node is outside the
    # selection, yet its readable name resolves, byte-identically to the
    # unfiltered row; rows only the unfiltered run has are absent.
    assert billing_status == 1
    assert _difference_rows(billing) == [_FEE_ROW, call_row]
    assert set(_difference_rows(billing)) < set(_difference_rows(default))
    assert "node added:" not in billing
    assert "node:comparison:" not in billing
    for row in _difference_rows(billing):
        assert default.splitlines().count(row) == 1


def test_systems_diff_system_filter_with_no_involvement_is_empty_and_exits_zero(
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
    _commit_all(root, "baseline")
    (root / "app" / "api.py").write_text(
        "def receive():\n    return 1\n\ndef send():\n    return 2\n", encoding="utf-8"
    )
    monkeypatch.chdir(root)

    unfiltered_status, unfiltered, _ = _diff(capsys)
    status, out, _ = _diff(capsys, "--system", "Other")

    assert unfiltered_status == 1
    assert _difference_rows(unfiltered)
    assert status == 0
    assert out.startswith("no system differences\n")
    assert _difference_rows(out) == []
    assert not any(
        line.startswith(("system ", "surface ", "boundary ")) for line in out.splitlines()
    )


def test_systems_diff_without_declared_systems_prints_rows_and_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    (root / "mod.py").write_text("def helper():\n    return 1\n\ndef main():\n    return 0\n")
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\ntargets = ["mod.py"]\n',
        encoding="utf-8",
    )
    _commit_all(root, "baseline")
    monkeypatch.chdir(root)

    unchanged_status, unchanged, _ = _diff(capsys)
    assert unchanged_status == 0
    assert unchanged.startswith("no system differences\n")
    assert _difference_rows(unchanged) == []

    (root / "mod.py").write_text(
        "def helper():\n    return 1\n\ndef main():\n    return helper()\n"
        "\ndef extra():\n    return 2\n",
        encoding="utf-8",
    )
    status, out, _ = _diff(capsys)

    assert status == 1
    assert "no system differences" not in out
    assert _difference_rows(out) == [
        "node added: mod.extra (mod.py, function)",
        "relationship added: mod -> mod.extra (contains)",
        "relationship added: mod.main -> mod.helper (calls)",
        "call added: mod.main -> mod.helper",
    ]


def _overlapping_definition(root: Path) -> None:
    """Add a second definition that lists a file the first one already lists."""
    definition = root / "docs" / "systems" / "twin"
    definition.mkdir(parents=True)
    (definition / "system.toml").write_text(
        'schema_version = 1\nname = "Twin"\nfiles = ["app/api.py"]\n', encoding="utf-8"
    )


def test_overlapping_current_definitions_report_the_loader_sentence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    _overlapping_definition(root)

    assert cli.main(["query", "surface", "App"]) == 2
    surface = capsys.readouterr().err.removeprefix("minotaur: error: ").strip()
    assert surface.startswith("file listed in two systems: app/api.py (declared in ")

    assert cli.main(["query", "diff", "--systems"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert surface in captured.err
    assert "app/system.toml" in captured.err
    assert "twin/system.toml" in captured.err


def test_overlapping_saved_definitions_name_worktree_relative_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _configured_repo(tmp_path)
    monkeypatch.chdir(root)
    _overlapping_definition(root)
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "overlap")
    first = _head_sha(root)
    shutil.rmtree(root / "docs" / "systems" / "twin")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "repaired")
    second = _head_sha(root)

    assert cli.main(["query", "diff", "--systems", first, second]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        "file listed in two systems: app/api.py "
        "(declared in docs/systems/app/system.toml and docs/systems/twin/system.toml)"
    ) in captured.err
    assert str(root) not in captured.err
    assert str(tmp_path) not in captured.err
    assert "minotaur-revision-" not in captured.err
