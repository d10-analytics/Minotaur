"""The documented walkthroughs must still produce their pasted output.

F-13 replaced placeholder examples in the query guide and README with real
commands run against the checked-in example graph. Pasted output rots the
moment a renderer changes, so this test re-runs every documented transcript
and compares byte-for-byte instead of trusting the paste.

Commands run through ``python -m minotaur`` from the repository root, exactly
as the documentation shows them, so argument parsing and ``--root src``
resolution are exercised the way a reader would experience them.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
EXAMPLE_GRAPH = ROOT / "examples" / "python-workflow" / "minotaur-graph.json"
DOCUMENTS = (
    ROOT / "docs" / "guides" / "query-for-agents.md",
    ROOT / "README.md",
)
CONSOLE_BLOCK = re.compile(r"^```console\n(.*?)^```$", re.DOTALL | re.MULTILINE)

# The diff walkthrough writes a fresh analysis to a scratch path. Tests must
# not depend on (or pollute) a real /tmp/current.json, so the documented path
# is redirected into pytest's tmp_path.
SCRATCH_PLACEHOLDER = "/tmp/current.json"


def _transcripts() -> list[tuple[str, str, str]]:
    """Yield ``(document, command, expected stdout)`` for every pasted block."""
    found: list[tuple[str, str, str]] = []
    for document in DOCUMENTS:
        text = document.read_text(encoding="utf-8")
        for block in CONSOLE_BLOCK.findall(text):
            command: str | None = None
            expected: list[str] = []
            for line in block.splitlines():
                if line.startswith("$ "):
                    if command is not None:
                        found.append((document.name, command, "".join(expected)))
                        expected = []
                    command = line[2:]
                elif command is not None and command.endswith("\\"):
                    command = command[:-1] + line
                else:
                    expected.append(line + "\n")
            assert command is not None, f"{document.name}: console block without a command"
            found.append((document.name, command, "".join(expected)))
    return found


TRANSCRIPTS = _transcripts()


def test_documentation_contains_the_expected_walkthrough_commands() -> None:
    """Guard the parser itself: a silently empty scan would pass every case."""
    commands = [command for _, command, _ in TRANSCRIPTS]
    assert len(commands) == 10
    assert sum(command.startswith("minotaur query ") for command in commands) == 9


@pytest.mark.parametrize(
    ("document", "command", "expected"),
    TRANSCRIPTS,
    ids=[f"{document}:{index}" for index, (document, _, _) in enumerate(TRANSCRIPTS)],
)
def test_documented_command_still_prints_its_pasted_output(
    document: str, command: str, expected: str, tmp_path: Path
) -> None:
    """Re-run a documented transcript and compare stdout exactly."""
    before = EXAMPLE_GRAPH.read_bytes()
    arguments = shlex.split(command)
    assert arguments[0] == "minotaur"
    scratch = str(tmp_path / "current.json")
    arguments = [
        scratch if argument == SCRATCH_PLACEHOLDER else argument for argument in arguments[1:]
    ]
    if "diff" in arguments:
        # The diff transcript compares against the analysis its own previous
        # command produced; run that prerequisite into the same scratch path.
        subprocess.run(
            [
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
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
    completed = subprocess.run(
        [sys.executable, "-m", "minotaur", *arguments],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == expected
    assert EXAMPLE_GRAPH.read_bytes() == before, f"{document} rewrote the checked-in example"
