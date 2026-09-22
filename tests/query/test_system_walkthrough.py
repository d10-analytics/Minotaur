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
import runpy
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from minotaur import cli
from minotaur.graph_model.loading import graph_digest, load_graph_bytes, stamp_path

ROOT = Path(__file__).parents[2]
EXAMPLE = ROOT / "examples" / "system-walkthrough"
WALKTHROUGH = EXAMPLE / "README.md"
RUNNER = ROOT / "examples" / "run_walkthrough.py"
COMPARISON_REPORT = EXAMPLE / "minotaur-comparison.html"
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
    sandbox = tmp_path / "config-free-sandbox"
    sandbox.mkdir()
    (sandbox / "examples").symlink_to(ROOT / "examples", target_is_directory=True)
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
            cwd=sandbox,
            text=True,
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 0, f"{command}\n{completed.stderr}"
    completed = subprocess.run(
        [sys.executable, "-m", "minotaur", *arguments],
        cwd=sandbox,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, f"{command}\n{completed.stderr}"
    assert completed.stdout == expected, command
    assert _committed_bytes() == before, f"{command} rewrote a checked-in example artifact"


def test_public_systems_membership_change_keeps_graph_bytes_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A definition-only change is reported while the analyzed graph is unchanged."""
    root = tmp_path / "repository"
    root.mkdir()

    def write(relative: str, content: str) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def git(*args: str) -> None:
        completed = subprocess.run(
            ["git", *args], cwd=root, text=True, capture_output=True, check=False
        )
        assert completed.returncode == 0, completed.stderr

    write("a.py", "def a():\n    return 1\n")
    write("b.py", "def b():\n    return 1\n")
    write("outside.py", "def outside():\n    return 1\n")
    write(
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        'targets = ["a.py", "b.py", "outside.py"]\n',
    )
    write("docs/systems/a/system.toml", 'schema_version = 1\nname = "A"\nfiles = ["a.py"]\n')
    write("docs/systems/b/system.toml", 'schema_version = 1\nname = "B"\nfiles = ["b.py"]\n')

    monkeypatch.chdir(root)
    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Minotaur Tests")
    assert cli.main(["analyze"]) == 0
    capsys.readouterr()
    git("add", ".")
    git("commit", "-qm", "baseline")
    graph = root / "graph.json"
    sidecar = stamp_path(graph)
    before_graph, before_sidecar = graph.read_bytes(), sidecar.read_bytes()
    config = root / ".minotaur.toml"
    definition_a = root / "docs/systems/a/system.toml"
    definition_b = root / "docs/systems/b/system.toml"
    before_config = config.read_bytes()
    before_definition_b = definition_b.read_bytes()
    definition_a.write_text(
        'schema_version = 1\nname = "A"\nfiles = ["a.py", "outside.py"]\n',
        encoding="utf-8",
    )
    before_definition_a = definition_a.read_bytes()
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

    status = cli.main(["query", "diff", "--systems", "--system", "A"])
    captured = capsys.readouterr()
    assert status == 1
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert lines[0] == "membership changed: outside.py — unassigned -> A"
    assert len(lines) == 5
    expected_old_coverage = {
        "declared_files": {
            "absent": 0,
            "represented": 2,
            "scope": "all_declared_system_files",
            "total": 2,
        },
        "graph_files": {"count": 3, "scope": "final_graph_file_nodes"},
        "recorded_unresolved_references": {
            "count": 0,
            "scope": "all_declared_system_files",
        },
        "selection": {
            "status": "recorded",
            "targets": ["a.py", "b.py", "outside.py"],
        },
        "source_diagnostics": {"status": "unavailable"},
        "unassigned_files": {
            "count": 1,
            "paths": ["outside.py"],
            "scope": "final_graph_file_node_derived_paths",
        },
    }
    expected_new_coverage = {
        "declared_files": {
            "absent": 0,
            "represented": 3,
            "scope": "all_declared_system_files",
            "total": 3,
        },
        "graph_files": {"count": 3, "scope": "final_graph_file_nodes"},
        "recorded_unresolved_references": {
            "count": 0,
            "scope": "all_declared_system_files",
        },
        "selection": expected_old_coverage["selection"],
        "source_diagnostics": {"status": "unavailable"},
        "unassigned_files": {
            "count": 0,
            "paths": [],
            "scope": "final_graph_file_node_derived_paths",
        },
    }
    assert json.loads(lines[1].removeprefix("old coverage: ")) == expected_old_coverage
    assert json.loads(lines[2].removeprefix("new coverage: ")) == expected_new_coverage
    assert lines[3:] == [
        'old selection: {"status":"recorded","targets":["a.py","b.py","outside.py"]}',
        'new selection: {"status":"recorded","targets":["a.py","b.py","outside.py"]}',
    ]
    assert graph.read_bytes() == before_graph
    assert sidecar.read_bytes() == before_sidecar
    assert config.read_bytes() == before_config
    assert definition_a.read_bytes() == before_definition_a
    assert definition_b.read_bytes() == before_definition_b
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


def test_public_systems_outside_consumer_is_visible_from_both_involved_systems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A cross-system consumer remains visible from either selected system."""
    root = tmp_path / "repository"
    root.mkdir()

    def write(relative: str, content: str) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def git(*args: str) -> None:
        completed = subprocess.run(
            ["git", *args], cwd=root, text=True, capture_output=True, check=False
        )
        assert completed.returncode == 0, completed.stderr

    write("a.py", "def receive():\n    return 1\n")
    write("b.py", "def consume():\n    return 0\n")
    write(
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        'targets = ["a.py", "b.py"]\n',
    )
    write("docs/systems/a/system.toml", 'schema_version = 1\nname = "A"\nfiles = ["a.py"]\n')
    write("docs/systems/b/system.toml", 'schema_version = 1\nname = "B"\nfiles = ["b.py"]\n')

    monkeypatch.chdir(root)
    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Minotaur Tests")
    assert cli.main(["analyze"]) == 0
    capsys.readouterr()
    git("add", ".")
    git("commit", "-qm", "baseline")
    graph = root / "graph.json"
    sidecar = stamp_path(graph)
    before_graph, before_sidecar = graph.read_bytes(), sidecar.read_bytes()
    config = root / ".minotaur.toml"
    definition_a = root / "docs/systems/a/system.toml"
    definition_b = root / "docs/systems/b/system.toml"
    before_config = config.read_bytes()
    before_definition_a = definition_a.read_bytes()
    before_definition_b = definition_b.read_bytes()
    write("b.py", "from a import receive\n\ndef consume():\n    return receive()\n")
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

    status_a = cli.main(["query", "diff", "--systems", "--system", "A"])
    output_a = capsys.readouterr()
    assert status_a == 1
    assert output_a.err == ""
    expected_lines = [
        "surface added: A a.py.a.receive",
        "consumer added: A <- b.py",
        "dependency added: B -> A",
        "boundary added: B.b -> A.a.receive (imports)",
        "boundary added: B.b.consume -> A.a.receive (calls)",
    ]
    expected_coverage = {
        "declared_files": {
            "absent": 0,
            "represented": 2,
            "scope": "all_declared_system_files",
            "total": 2,
        },
        "graph_files": {"count": 2, "scope": "final_graph_file_nodes"},
        "recorded_unresolved_references": {
            "count": 0,
            "scope": "all_declared_system_files",
        },
        "selection": {"status": "recorded", "targets": ["a.py", "b.py"]},
        "source_diagnostics": {"status": "unavailable"},
        "unassigned_files": {
            "count": 0,
            "paths": [],
            "scope": "final_graph_file_node_derived_paths",
        },
    }
    output_lines_a = output_a.out.splitlines()
    assert output_lines_a[:5] == expected_lines
    assert json.loads(output_lines_a[5].removeprefix("old coverage: ")) == expected_coverage
    assert json.loads(output_lines_a[6].removeprefix("new coverage: ")) == expected_coverage
    assert output_lines_a[7:] == [
        'old selection: {"status":"recorded","targets":["a.py","b.py"]}',
        'new selection: {"status":"recorded","targets":["a.py","b.py"]}',
    ]

    status_b = cli.main(["query", "diff", "--systems", "--system", "B"])
    output_b = capsys.readouterr()
    assert status_b == 1
    assert output_b.err == ""
    assert output_b.out.splitlines() == output_lines_a

    status_details = cli.main(["query", "diff", "--systems", "--system", "A", "--details"])
    details = capsys.readouterr()
    assert status_details == 1
    assert details.err == ""
    call_evidence = []
    for line in details.out.splitlines():
        if line.startswith("new evidence: "):
            record = json.loads(line.removeprefix("new evidence: "))
            if any(item["kind"] == "calls" for item in record):
                call_evidence.extend(record)
    call_record = next(item for item in call_evidence if item["kind"] == "calls")
    assert call_record["source"]["path"] == {"status": "recorded", "value": "b.py"}
    assert call_record["target"]["path"] == {"status": "recorded", "value": "a.py"}
    assert call_record["evidence"][0]["sites"][0]["path"] == "b.py"

    assert graph.read_bytes() == before_graph
    assert sidecar.read_bytes() == before_sidecar
    assert config.read_bytes() == before_config
    assert definition_a.read_bytes() == before_definition_a
    assert definition_b.read_bytes() == before_definition_b
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


def test_public_systems_ambiguity_is_attributed_before_unrelated_filter_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A valid historical graph still rejects an ambiguous current identity."""
    root = tmp_path / "repository"
    root.mkdir()

    def write(relative: str, content: str) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def git(*args: str) -> None:
        completed = subprocess.run(
            ["git", *args], cwd=root, text=True, capture_output=True, check=False
        )
        assert completed.returncode == 0, completed.stderr

    write("a.py", "def receive():\n    return 1\n")
    write("b.py", "from a import receive\n\ndef consume():\n    return receive()\n")
    write(
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\n'
        'targets = ["a.py", "b.py"]\n',
    )
    write("docs/systems/a/system.toml", 'schema_version = 1\nname = "A"\nfiles = ["a.py"]\n')
    write(
        "docs/systems/other/system.toml", 'schema_version = 1\nname = "Other"\nfiles = ["b.py"]\n'
    )

    monkeypatch.chdir(root)
    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Minotaur Tests")
    assert cli.main(["analyze"]) == 0
    capsys.readouterr()
    git("add", ".")
    git("commit", "-qm", "baseline")
    graph = root / "graph.json"
    sidecar = stamp_path(graph)
    historical = graph.read_bytes()
    assert load_graph_bytes(historical).document.nodes
    assert sidecar.read_text(encoding="ascii").strip() == graph_digest(historical)
    before_graph, before_sidecar = historical, sidecar.read_bytes()
    config = root / ".minotaur.toml"
    definition_a = root / "docs/systems/a/system.toml"
    definition_other = root / "docs/systems/other/system.toml"
    before_config = config.read_bytes()
    before_definition_a = definition_a.read_bytes()
    before_definition_other = definition_other.read_bytes()

    write(
        "a.py",
        "def receive():\n    return 1\n\ndef receive():\n    return 2\n",
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

    status = cli.main(["query", "diff", "--systems", "--system", "Other"])
    captured = capsys.readouterr()
    assert status == 2
    assert captured.out == ""
    assert captured.err.startswith("minotaur: error: ambiguous new target")
    assert "a.py:0" in captured.err
    assert "a.py:3" in captured.err
    assert "system: Other" not in captured.out
    assert graph.read_bytes() == before_graph
    assert sidecar.read_bytes() == before_sidecar
    assert config.read_bytes() == before_config
    assert definition_a.read_bytes() == before_definition_a
    assert definition_other.read_bytes() == before_definition_other
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


# ---------------------------------------------------------------------------
# Historical and working-tree comparison workflows
# ---------------------------------------------------------------------------


def _example_runner() -> dict[str, object]:
    """Load the example runner that owns the fixed-identity comparison fixture."""
    return runpy.run_path(str(RUNNER))


def _baseline_repository(root: Path) -> dict[str, object]:
    """Create the tagged ``v1.0`` fixture in ``root`` without touching the checkout."""
    runner = _example_runner()
    runner["create_systems_baseline"](root)
    return runner


def _cli(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run one public Minotaur command in the fixture repository."""
    return subprocess.run(
        [sys.executable, "-m", "minotaur", *arguments],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )


def _rev_parse(root: Path, revision: str) -> str:
    """Resolve one revision to its full commit ID in the fixture repository."""
    return subprocess.run(
        ["git", "rev-parse", revision],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def _embedded_presentation(report: Path) -> dict[str, object]:
    """Parse the inert presentation payload an offline report embeds."""
    _, rest = report.read_text(encoding="utf-8").split(
        '<script id="minotaur-presentation" type="application/json">', 1
    )
    embedded, _ = rest.split("</script>", 1)
    return json.loads(embedded)


def _committed_historical_pair(root: Path) -> None:
    """Stage, commit, and tag the documented second revision."""
    runner = _baseline_repository(root)
    runner["stage_systems_revision"](root)
    runner["commit_systems_revision"](root)


def test_public_historical_comparison_retains_revisions_and_representative_changes(
    tmp_path: Path,
) -> None:
    """Explicit tags keep their requested names and resolved IDs, with real facts."""
    root = tmp_path / "repository"
    root.mkdir()
    _committed_historical_pair(root)

    completed = _cli(root, "query", "diff", "--systems", "v1.0", "v2.0", "--json")

    assert completed.returncode == 1
    assert completed.stderr == ""
    payload = json.loads(completed.stdout)
    before_commit = _rev_parse(root, "v1.0")
    after_commit = _rev_parse(root, "v2.0")
    assert payload["revisions"] == {
        "old": f"v1.0 · {before_commit[:7]}",
        "new": f"v2.0 · {after_commit[:7]}",
    }
    assert payload["changed"] is True
    assert payload["exit_code"] == 1
    assert payload["limitations"] == []

    comparison = payload["comparison"]
    assert comparison["revisions"] == payload["revisions"]
    for side, revision, commit in (
        ("before", "v1.0", before_commit),
        ("after", "v2.0", after_commit),
    ):
        assert comparison[side]["kind"] == "commit"
        assert comparison[side]["requested_revision"] == revision
        assert comparison[side]["commit"] == commit

    # A boundary change: the new Orders call into Billing crosses the boundary.
    boundary = [
        change
        for change in payload["boundary_changes"]
        if change["kind"] == "added"
        and change["new"] is not None
        and change["new"]["kind"] == "calls"
        and "shop.orders.cancel_order" in json.dumps(change["new"]["projections"])
        and "shop.billing.refund" in json.dumps(change["new"]["projections"])
    ]
    assert boundary, "expected the added cancel_order -> refund boundary call"

    # An internal call-expression change: Billing's charge still calls record,
    # but the recorded call expression differs and stays inside Billing.
    expression_changes = [
        change
        for change in payload["call_changes"]
        if change["status"] == "changed" and change["reasons"] == ["expression_changed"]
    ]
    assert len(expression_changes) == 1
    expression = expression_changes[0]
    assert expression["involved_systems"] == ["billing"]
    assert expression["before"][0]["expression"]["path"] == "shop/billing.py"
    assert expression["after"][0]["expression"]["path"] == "shop/billing.py"
    assert expression["before"][0]["fingerprint"] != expression["after"][0]["fingerprint"]

    # A move: the same symbol leaves shop/orders.py and appears in shop/order_ops.py.
    node_labels = {
        (change["status"], side, node["label"])
        for change in payload["nodes"]
        for side in ("before", "after")
        if isinstance(change.get(side), dict)
        for node in [change[side]]
    }
    assert ("removed", "before", "shop.orders.complete_order") in node_labels
    assert ("added", "after", "shop.order_ops.complete_order") in node_labels

    # A removed item: the unassigned checkout file disappears with its module
    # and symbol, and the Orders surface it reached is removed.
    removed_labels = {
        change["before"]["label"]
        for change in payload["nodes"]
        if change["status"] == "removed" and isinstance(change.get("before"), dict)
    }
    assert {"shop/checkout.py", "shop.checkout", "shop.checkout.checkout"} <= removed_labels
    assert any(
        change["kind"] == "removed"
        and change["key"] == ["orders", "shop/orders.py", "shop.orders.create_order"]
        for change in payload["surface_changes"]
    )
    assert any(
        change["kind"] == "changed"
        and change["key"] == ["shop/order_ops.py"]
        and change["old"]["system"] is None
        and change["new"]["system"] == "orders"
        for change in payload["membership_changes"]
    )


def test_public_working_tree_comparison_labels_after_without_commit_identity(
    tmp_path: Path,
) -> None:
    """HEAD-versus-working-tree keeps the approved After label and no commit ID."""
    root = tmp_path / "repository"
    root.mkdir()
    runner = _baseline_repository(root)
    runner["stage_systems_revision"](root)

    completed = _cli(root, "query", "diff", "--systems", "--json")

    assert completed.returncode == 1
    assert completed.stderr == ""
    payload = json.loads(completed.stdout)
    head_commit = _rev_parse(root, "HEAD")
    assert payload["revisions"] == {
        "old": f"HEAD · {head_commit[:7]}",
        "new": "Working tree at report generation",
    }
    comparison = payload["comparison"]
    assert comparison["before"]["kind"] == "commit"
    assert comparison["before"]["commit"] == head_commit
    assert comparison["before"]["requested_revision"] == "HEAD"
    assert comparison["after"] == {
        "kind": "working-tree",
        "requested_revision": None,
        "commit": None,
        "config_path": ".minotaur.toml",
        "root": ".",
        "targets": ["shop"],
        "source_digest": comparison["after"]["source_digest"],
    }
    # The approved label must not smuggle in a commit ID for the working tree.
    assert head_commit[:7] not in payload["revisions"]["new"]


def test_public_comparison_report_is_offline_static_and_keeps_identities(
    tmp_path: Path,
) -> None:
    """A written report has no remote assets and does not track later edits."""
    root = tmp_path / "repository"
    root.mkdir()
    _committed_historical_pair(root)
    report = tmp_path / "comparison.html"
    completed = _cli(
        root,
        "query",
        "diff",
        "--systems",
        "v1.0",
        "v2.0",
        "--html",
        str(report),
    )
    assert completed.returncode == 1
    assert report.is_file()

    presentation = _embedded_presentation(report)
    comparison = presentation["comparison"]
    before_commit = _rev_parse(root, "v1.0")[:7]
    after_commit = _rev_parse(root, "v2.0")[:7]
    assert comparison["revisions"] == {
        "old": f"v1.0 · {before_commit}",
        "new": f"v2.0 · {after_commit}",
    }
    assert comparison["changed"] is True

    content = report.read_text(encoding="utf-8")
    assert str(root) not in content, "the report must not embed temporary paths"
    remote = re.findall(r"""(?:src|href)\s*=\s*["']https?://""", content)
    assert remote == [], "the report must be self-contained and offline"

    before_bytes = report.read_bytes()
    (root / "shop" / "billing.py").write_text(
        "def charge(order):\n    return order\n", encoding="utf-8"
    )
    (root / "shop" / "orders.py").write_text(
        "def create_order(cart):\n    return cart\n", encoding="utf-8"
    )
    assert report.read_bytes() == before_bytes, "a captured report must not re-read source"


def test_checked_in_comparison_report_matches_fixed_identity_regeneration(
    tmp_path: Path,
) -> None:
    """The committed report comes from the public commands and is reproducible."""
    assert COMPARISON_REPORT.is_file(), "checked-in comparison report is missing"
    subprocess.run(
        [
            sys.executable,
            str(EXAMPLE / "regenerate_system_walkthrough.py"),
            "--comparison-only",
            "--output-directory",
            str(tmp_path),
        ],
        cwd=ROOT,
        check=True,
    )
    generated = tmp_path / "minotaur-comparison.html"
    assert generated.read_bytes() == COMPARISON_REPORT.read_bytes()
    assert _embedded_presentation(generated)["comparison"]["changed"] is True
