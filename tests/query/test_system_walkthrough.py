"""The system walkthrough must continue to match the installed CLI output.

``examples/system-walkthrough/`` is a committed, runnable example of the
``surface``, ``consumers``, and ``system-deps`` queries over a fabricated
storefront package with two declared systems. Pasted output rots the moment a
renderer changes, so this test re-runs every documented console command
exactly as a reader would and compares standard output byte-for-byte.

Commands run through ``python -m minotaur`` from the repository root, exactly
as the documentation shows them. Scratch outputs are redirected into pytest's
``tmp_path`` so a walkthrough never rewrites a checked-in file, and every
committed example artifact is byte-compared before and after each command.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
EXAMPLE = ROOT / "examples" / "system-walkthrough"
WALKTHROUGH = EXAMPLE / "README.md"
CONSOLE_BLOCK = re.compile(r"^```console\n(.*?)^```$", re.DOTALL | re.MULTILINE)

SCRATCH_GRAPH = "/tmp/system-walkthrough-graph.json"

#: Every committed artifact under the example directory, keyed by relative path.
COMMITTED_FILES = {
    path.relative_to(EXAMPLE): path for path in sorted(EXAMPLE.rglob("*")) if path.is_file()
}


def _committed_bytes() -> dict[Path, bytes]:
    """Snapshot every committed example artifact's bytes."""
    return {relative: path.read_bytes() for relative, path in COMMITTED_FILES.items()}


def _transcripts() -> list[tuple[str, str, str]]:
    """Return ``(command, expected output)`` for each documented command."""
    found: list[tuple[str, str, str]] = []
    for block in CONSOLE_BLOCK.findall(WALKTHROUGH.read_text(encoding="utf-8")):
        command: str | None = None
        expected: list[str] = []
        for line in block.splitlines():
            if line.startswith("$ "):
                if command is not None:
                    found.append((command, "".join(expected)))
                    expected = []
                command = line[2:]
            elif command is not None and command.endswith("\\"):
                command = command[:-1] + line
            else:
                expected.append(line + "\n")
        assert command is not None, "console block without a command"
        found.append((command, "".join(expected)))
    return found


TRANSCRIPTS = _transcripts()


def test_walkthrough_has_all_expected_console_commands() -> None:
    """Guard the parser itself: a silently empty scan would pass every case."""
    commands = [command for command, _ in TRANSCRIPTS]
    assert len(commands) == 12
    assert sum(command.startswith("minotaur analyze ") for command in commands) == 1
    assert sum(command.startswith("minotaur query diff ") for command in commands) == 1
    assert sum(command.startswith("minotaur query surface ") for command in commands) == 4
    assert sum(command.startswith("minotaur query consumers ") for command in commands) == 3
    assert sum(command.startswith("minotaur query system-deps ") for command in commands) == 3
    assert sum(" --json" in command for command in commands) == 3
    assert sum(" --details" in command for command in commands) == 1


@pytest.mark.parametrize(
    ("command", "expected"),
    TRANSCRIPTS,
    ids=[f"{index}: {command}" for index, (command, _) in enumerate(TRANSCRIPTS)],
)
def test_documented_command_still_prints_its_pasted_output(
    command: str, expected: str, tmp_path: Path
) -> None:
    """Re-run one documented transcript and compare stdout byte-for-byte."""
    before = _committed_bytes()
    arguments = shlex.split(command)
    assert arguments[0] == "minotaur"
    scratch = str(tmp_path / "system-walkthrough-graph.json")
    arguments = [scratch if argument == SCRATCH_GRAPH else argument for argument in arguments[1:]]
    if "diff" in arguments:
        # The diff transcript compares the committed graph against the fresh
        # analysis its own console block produced; run that prerequisite into
        # the same scratch path first.
        prerequisite = [
            sys.executable,
            "-m",
            "minotaur",
            "analyze",
            "--root",
            "examples/system-walkthrough",
            "--output",
            scratch,
            "--force",
            "examples/system-walkthrough/shop",
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
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, f"{command}\n{completed.stderr}"
    assert completed.stdout == expected, command
    assert _committed_bytes() == before, f"{command} rewrote a checked-in example artifact"
