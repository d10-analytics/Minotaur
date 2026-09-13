"""The query walkthrough must continue to match the installed CLI output."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from minotaur import cli
from minotaur.graph_model.loading import stamp_path

ROOT = Path(__file__).parents[2]
EXAMPLE_GRAPH = ROOT / "examples" / "python-workflow" / "minotaur-graph.json"
EXAMPLE_SOURCE = ROOT / "src" / "minotaur" / "language_interpreter" / "selection.py"
WALKTHROUGH = ROOT / "examples" / "query-walkthrough" / "README.md"
CONSOLE_BLOCK = re.compile(r"^```console\n(.*?)^```$", re.DOTALL | re.MULTILINE)

SCRATCH_GRAPH = "/tmp/query-walkthrough-graph.json"
SCRATCH_ROOT = "/tmp/query-walkthrough-src"
SCRATCH_OUTPUT = "/tmp/current.json"


def _transcripts() -> list[tuple[str, str, str]]:
    """Return ``(block, command, expected output)`` for each console command."""
    found: list[tuple[str, str, str]] = []
    for block_number, block in enumerate(
        CONSOLE_BLOCK.findall(WALKTHROUGH.read_text(encoding="utf-8")), start=1
    ):
        command: str | None = None
        expected: list[str] = []
        for line in block.splitlines():
            if line.startswith("$ "):
                if command is not None:
                    found.append((f"block {block_number}", command, "".join(expected)))
                    expected = []
                command = line[2:]
            elif command is not None and command.endswith("\\"):
                command = command[:-1] + line
            else:
                expected.append(line + "\n")
        assert command is not None, f"block {block_number}: console block without a command"
        found.append((f"block {block_number}", command, "".join(expected)))
    return found


TRANSCRIPTS = _transcripts()


def _prepare_scratch_freshness(tmp_path: Path, *, second_edit: bool) -> dict[str, str]:
    """Copy the walkthrough's graph and source, then introduce content drift."""
    graph = tmp_path / "query-walkthrough-graph.json"
    source_root = tmp_path / "query-walkthrough-src"
    source = source_root / "minotaur" / "language_interpreter" / "selection.py"
    source.parent.mkdir(parents=True)
    shutil.copy2(EXAMPLE_GRAPH, graph)
    shutil.copy2(EXAMPLE_SOURCE, source)
    source.write_bytes(source.read_bytes() + b"\n# scratch edit 1\n")
    if second_edit:
        source.write_bytes(source.read_bytes() + b"\n# scratch edit 2\n")
    return {
        SCRATCH_GRAPH: str(graph),
        SCRATCH_ROOT: str(source_root),
    }


def _replace_scratch_paths(
    arguments: list[str], replacements: dict[str, str], tmp_path: Path
) -> list[str]:
    """Redirect fixed paths shown in the document into this test's temp dir."""
    output = [replacements.get(argument, argument) for argument in arguments]
    return [
        str(tmp_path / "current.json") if argument == SCRATCH_OUTPUT else argument
        for argument in output
    ]


def test_walkthrough_has_all_expected_console_commands() -> None:
    """Guard parsing so a missing or malformed block cannot pass silently."""
    assert len({block for block, _, _ in TRANSCRIPTS}) == 12
    commands = [command for _, command, _ in TRANSCRIPTS]
    assert len(commands) == 13
    assert sum(command.startswith("minotaur query ") for command in commands) == 11


@pytest.mark.parametrize(
    ("block", "command", "expected"),
    TRANSCRIPTS,
    ids=[f"{block}: {command}" for block, command, _ in TRANSCRIPTS],
)
def test_walkthrough_command_matches_pasted_output(
    block: str, command: str, expected: str, tmp_path: Path
) -> None:
    """Run each documented command and compare its documented terminal stream."""
    before = EXAMPLE_GRAPH.read_bytes()
    arguments = shlex.split(command)
    assert arguments[0] == "minotaur"

    replacements: dict[str, str] = {}
    combined_stream = SCRATCH_GRAPH in arguments
    if combined_stream:
        replacements = _prepare_scratch_freshness(tmp_path, second_edit="--no-refresh" in arguments)
    arguments = _replace_scratch_paths(arguments[1:], replacements, tmp_path)

    if "diff" in arguments:
        scratch = str(tmp_path / "current.json")
        prerequisite = [
            sys.executable,
            "-m",
            "minotaur",
            "analyze",
            "--root",
            "src",
            "--output",
            scratch,
            "--force",
            "src/minotaur/language_interpreter/selection.py",
        ]
        completed = subprocess.run(
            prerequisite,
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 0, f"{command}\n{completed.stderr}"

    completed = subprocess.run(
        [sys.executable, "-m", "minotaur", *arguments],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if combined_stream else subprocess.PIPE,
        check=False,
    )
    assert completed.returncode == 0, f"{command}\n{completed.stderr or ''}"
    actual = completed.stdout
    assert actual == expected, f"{block}: {command}"
    assert EXAMPLE_GRAPH.read_bytes() == before, f"{block}: rewrote the checked-in example"


def test_public_systems_source_change_walkthrough_has_exact_status_evidence_and_no_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run the public systems command for a source-only change."""
    root = tmp_path / "repository"
    root.mkdir()

    def git(*args: str) -> None:
        completed = subprocess.run(
            ["git", *args], cwd=root, text=True, capture_output=True, check=False
        )
        assert completed.returncode == 0, completed.stderr

    def write(relative: str, content: str) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Minotaur Tests")
    write("app/api.py", "def receive():\n    return 1\n")
    write("consumer.py", "from app.api import receive\n\ndef consume():\n    return receive()\n")
    write(
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        'targets = ["app", "consumer.py"]\n',
    )
    write(
        "docs/systems/app/system.toml",
        'schema_version = 1\nname = "App"\nfiles = ["app/api.py"]\n',
    )

    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0
    capsys.readouterr()
    git("add", ".")
    git("commit", "-qm", "baseline")
    graph = root / "graph.json"
    sidecar = stamp_path(graph)
    definition = root / "docs/systems/app/system.toml"
    before_graph, before_sidecar = graph.read_bytes(), sidecar.read_bytes()
    before_config = (root / ".minotaur.toml").read_bytes()
    before_definition = definition.read_bytes()

    write(
        "app/api.py",
        "def receive():\n    return 1\n\ndef send():\n    return 2\n",
    )
    write(
        "consumer.py",
        "from app.api import receive, send\n\ndef consume():\n    receive()\n    return send()\n",
    )
    before_status = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    before_index = subprocess.run(
        ["git", "ls-files", "--stage"], cwd=root, text=True, capture_output=True, check=True
    ).stdout

    status = cli.main(["query", "diff", "--systems", "--system", "App"])
    captured = capsys.readouterr()
    assert status == 1
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert lines[:4] == [
        "surface added: App app/api.py.app.api.send",
        "consumer changed: App <- consumer.py",
        "boundary added: no_system.consumer -> App.app.api.send (imports)",
        "boundary added: no_system.consumer.consume -> App.app.api.send (calls)",
    ]
    assert len(lines) == 8
    expected_coverage = {
        "declared_files": {
            "absent": 0,
            "represented": 1,
            "scope": "all_declared_system_files",
            "total": 1,
        },
        "graph_files": {"count": 2, "scope": "final_graph_file_nodes"},
        "recorded_unresolved_references": {
            "count": 0,
            "scope": "all_declared_system_files",
        },
        "selection": {"status": "recorded", "targets": ["app", "consumer.py"]},
        "source_diagnostics": {"status": "unavailable"},
        "unassigned_files": {
            "count": 1,
            "paths": ["consumer.py"],
            "scope": "final_graph_file_node_derived_paths",
        },
    }
    assert json.loads(lines[4].removeprefix("old coverage: ")) == expected_coverage
    assert json.loads(lines[5].removeprefix("new coverage: ")) == expected_coverage
    assert lines[6:] == [
        'old selection: {"status":"recorded","targets":["app","consumer.py"]}',
        'new selection: {"status":"recorded","targets":["app","consumer.py"]}',
    ]

    status = cli.main(["query", "diff", "--systems", "--system", "App", "--details"])
    details = capsys.readouterr()
    assert status == 1
    assert details.err == ""
    detail_lines = details.out.splitlines()
    assert "old evidence: unavailable" in detail_lines
    evidence = next(
        line.removeprefix("new evidence: ")
        for line in detail_lines
        if line.startswith("new evidence: ")
    )
    evidence_record = json.loads(evidence)[0]
    assert evidence_record["kind"] == "calls"
    assert evidence_record["source"]["path"] == {"status": "recorded", "value": "consumer.py"}
    assert evidence_record["target"]["path"] == {"status": "recorded", "value": "app/api.py"}
    assert evidence_record["evidence"][0]["provenance"] == "static-analysis"
    assert evidence_record["evidence"][0]["producer"] == {
        "name": "minotaur-python",
        "status": "recorded",
        "version": {"status": "unavailable"},
    }
    assert evidence_record["evidence"][0]["sites"] == [
        {
            "coordinate_encoding": "utf-8",
            "path": "consumer.py",
            "range": {
                "end": {"column": 16, "line": 5},
                "end_exclusive": True,
                "start": {"column": 12, "line": 5},
            },
        }
    ]
    assert graph.read_bytes() == before_graph
    assert sidecar.read_bytes() == before_sidecar
    assert (root / ".minotaur.toml").read_bytes() == before_config
    assert definition.read_bytes() == before_definition
    assert (
        subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        == before_status
    )
    assert (
        subprocess.run(
            ["git", "ls-files", "--stage"], cwd=root, text=True, capture_output=True, check=True
        ).stdout
        == before_index
    )
