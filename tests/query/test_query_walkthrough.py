"""The query walkthrough must continue to match the installed CLI output."""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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
