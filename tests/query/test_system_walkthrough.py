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

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from minotaur import cli

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


def test_public_systems_overview_walkthrough_matches_documented_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Generate the guide's synthetic tree and pin public compact/detail facts."""
    root = tmp_path / "repository"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)

    def write(relative: str, content: str) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    write(
        root.joinpath("billing/svc.py").relative_to(root).as_posix(),
        "def charge():\n    return 1\n",
    )
    write(
        "orders/api.py",
        "from billing.svc import charge\n\n"
        "def create_order():\n"
        "    charge()\n"
        "    return unresolved_reference\n",
    )
    write("loose.py", "def loose():\n    return 1\n")
    for name, files in (
        ("billing", ["billing/svc.py"]),
        ("orders", ["orders/api.py", "orders/legacy.py"]),
    ):
        directory = root / "docs" / "systems" / name
        directory.mkdir(parents=True)
        (directory / "system.toml").write_text(
            f'schema_version = 1\nname = "{name}"\nfiles = {json.dumps(files)}\n',
            encoding="utf-8",
        )

    graph = root / "graph.json"
    assert cli.main(["analyze", "--root", str(root), "--output", str(graph), str(root)]) == 0
    capsys.readouterr()

    def query(*extra: str) -> tuple[int, str, str]:
        status = cli.main(
            [
                "query",
                "systems",
                "--graph",
                str(graph),
                "--root",
                str(root),
                "--no-refresh",
                *extra,
            ]
        )
        captured = capsys.readouterr()
        return status, captured.out, captured.err

    status, compact_text, err = query()
    assert status == 0
    assert err == "minotaur: warning: orders/legacy.py (listed by system orders)\n"
    coverage = {
        "declared_files": {
            "absent": 1,
            "represented": 2,
            "scope": "all_declared_system_files",
            "total": 3,
        },
        "graph_files": {"count": 3, "scope": "final_graph_file_nodes"},
        "recorded_unresolved_references": {
            "count": 1,
            "scope": "all_declared_system_files",
        },
        "selection": {"status": "recorded", "targets": ["."]},
        "source_diagnostics": {"status": "unavailable"},
        "unassigned_files": {
            "count": 1,
            "scope": "final_graph_file_node_derived_paths",
        },
    }
    assert compact_text == (
        f"coverage {json.dumps(coverage, sort_keys=True, separators=(',', ':'))}\n"
        "billing  declared 1  represented 1  absent 0\n"
        "orders  declared 2  represented 1  absent 1\n"
    )

    status, compact_json, err = query("--json")
    assert status == 0
    assert err == "minotaur: warning: orders/legacy.py (listed by system orders)\n"
    compact = json.loads(compact_json)
    assert compact_json == json.dumps(compact, sort_keys=True, separators=(",", ":")) + "\n"
    assert set(compact) == {"query", "refreshed", "results", "stale", "coverage"}
    assert compact["results"] == [
        {
            "name": "billing",
            "declared_files": {
                "absent": 0,
                "represented": 1,
                "scope": "declared_system_files",
                "total": 1,
            },
        },
        {
            "name": "orders",
            "declared_files": {
                "absent": 1,
                "represented": 1,
                "scope": "declared_system_files",
                "total": 2,
            },
        },
    ]
    assert compact["coverage"] == coverage
    assert all("paths" not in result["declared_files"] for result in compact["results"])
    assert "connections" not in compact

    status, details_json, err = query("--details", "--json")
    assert status == 0
    assert err == "minotaur: warning: orders/legacy.py (listed by system orders)\n"
    details = json.loads(details_json)
    assert details_json == json.dumps(details, sort_keys=True, separators=(",", ":")) + "\n"
    assert details["results"][0]["declared_files"]["paths"] == ["billing/svc.py"]
    assert details["results"][1]["declared_files"]["paths"] == [
        "orders/api.py",
        "orders/legacy.py",
    ]
    assert details["coverage"]["unassigned_files"]["paths"] == ["loose.py"]
    assert [
        (connection["source_category"], connection["target_category"])
        for connection in details["connections"]
    ] == [("system: orders", "system: billing")]
    connection = details["connections"][0]
    assert connection["kinds"] == ["calls", "imports"]
    assert connection["relationships"]
    assert all(
        set(relationship) == {"source", "target", "kind", "relationship_extensions", "evidence"}
        for relationship in connection["relationships"]
    )

    status, details_text, err = query("--details")
    assert status == 0
    assert err == "minotaur: warning: orders/legacy.py (listed by system orders)\n"
    lines = details_text.splitlines()
    assert lines[0] == (
        f"coverage {json.dumps(details['coverage'], sort_keys=True, separators=(',', ':'))}"
    )
    assert lines[1:] == [
        "billing  declared 1  represented 1  absent 0",
        "orders  declared 2  represented 1  absent 1",
        'declared_files {"billing":["billing/svc.py"],'
        '"orders":["orders/api.py","orders/legacy.py"]}',
        f"connections {json.dumps(details['connections'], sort_keys=True, separators=(',', ':'))}",
    ]


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
