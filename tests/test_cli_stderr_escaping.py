"""Behavioral proof that every stderr line escapes through one CLI writer.

Hostile characters enter through real fixture repositories (control-character
file names, graph labels, recorded paths, system definitions, and recorded
analyzer versions), and every claim is answered through the real ``cli.main``
with ``capsys``. The unknown-symbol kind is exercised through ``query
callers`` because that is the real resolution seam where
``query/index.py``'s ``UnknownSymbol`` reaches ``_error`` with its
``nearest labels`` suggestion list; ``query definitions`` answers a bare-name
miss with ``no definitions`` and exit ``0`` without raising.
"""

from __future__ import annotations

import ast
import json
import linecache
import os
import sys
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest

from minotaur import cli
from minotaur.language_interpreter import ANALYZER_SEMANTICS_VERSION

pytestmark = pytest.mark.skipif(os.name != "posix", reason="hostile file names are POSIX only")

#: Characters whose raw appearance inside a stderr message the escape rule
#: forbids. A raw ``\n`` may only terminate a message, never occur within one.
HOSTILE = "\n\x1b\x9b\u202e\t\r\x0b\x0c\x7f\x07"

ESC_FILE = "esc\x1bx.py"

#: A file name that would forge a command error if its newlines printed raw.
FORGED_FILE = "nl\nminotaur: error: forged line\nz.py"
ESCAPED_FORGED_FILE = "nl\\nminotaur: error: forged line\\nz.py"


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _assert_no_hostile(text: str) -> None:
    for character in HOSTILE:
        assert character not in text


def _stderr_lines(err: str) -> list[str]:
    """Split on real newlines only, so ``\\r`` and friends stay visible to the check."""
    assert err == "" or err.endswith("\n")
    lines = err.split("\n")[:-1]
    for line in lines:
        _assert_no_hostile(line)
    return lines


def _analyze(root: Path) -> list[str]:
    return ["analyze", "--root", str(root), "--output", str(root / "g.json"), str(root)]


@pytest.fixture
def visible_warnings() -> Iterator[None]:
    """Show every warning, since the invalid-escape category differs by Python version.

    Python's display reads source lines through ``linecache``, which keeps them
    by file name across this process, so each test starts from an empty cache.
    """
    linecache.clearcache()
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        yield
    linecache.clearcache()


@pytest.fixture(scope="module")
def stderr_repo(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """An analyzed repository whose labels and file names carry hostile text."""
    root = tmp_path_factory.mktemp("stderr_repo")
    _write(root, ESC_FILE, "def escfn():\n    return 1\n")
    _write(root, "app.py", "def alpha():\n    return 1\n")
    graph = root / "g.json"
    assert cli.main(["analyze", "--root", str(root), "--output", str(graph), str(root)]) == 0
    return {"root": root, "graph": graph}


@pytest.fixture(scope="module")
def version_repo(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """A separate analyzed repository so the version edit never sees drift."""
    root = tmp_path_factory.mktemp("version_repo")
    _write(root, ESC_FILE, "def escfn():\n    return 1\n")
    graph = root / "g.json"
    assert cli.main(["analyze", "--root", str(root), "--output", str(graph), str(root)]) == 0
    return {"root": root, "graph": graph}


@pytest.fixture(scope="module")
def stale_repo(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """A separate analyzed repository so the stale test's file edit cannot drift
    any other test's graph: the edit lives inside this fixture only."""
    root = tmp_path_factory.mktemp("stale_repo")
    _write(root, ESC_FILE, "def escfn():\n    return 1\n")
    _write(root, "app.py", "def alpha():\n    return 1\n")
    graph = root / "g.json"
    assert cli.main(["analyze", "--root", str(root), "--output", str(graph), str(root)]) == 0
    return {"root": root, "graph": graph}


def test_unknown_symbol_error_escapes_the_hostile_nearest_label(
    stderr_repo: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """A near-miss qualified name reports the hostile label in its suggestion list."""
    stderr = stderr_repo
    status = cli.main(
        [
            "query",
            "callers",
            "esc-x.escfn",
            "--graph",
            str(stderr["graph"]),
            "--root",
            str(stderr["root"]),
        ]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert captured.out == ""
    assert _stderr_lines(captured.err) == [
        "minotaur: error: unknown symbol: esc-x.escfn; nearest labels: esc\\x1bx.escfn"
    ]


def test_stale_lines_escape_the_drifted_hostile_path(
    stale_repo: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """Editing the hostile-named file leaves one stale line for that path.

    The mutation happens inside this test's own analyzed repository, so no
    assertion elsewhere reads a drifted or rewritten graph and the suite may
    run in any order.
    """
    stderr = stale_repo
    path = stderr["root"] / ESC_FILE
    path.write_text("def escfn():\n    return 2\n", encoding="utf-8")

    status = cli.main(
        [
            "query",
            "definitions",
            "escfn",
            "--graph",
            str(stderr["graph"]),
            "--root",
            str(stderr["root"]),
            "--no-refresh",
        ]
    )
    captured = capsys.readouterr()

    assert status == 0
    assert captured.out != ""
    assert _stderr_lines(captured.err) == ["minotaur: stale: esc\\x1bx.py"]


def test_analyzer_change_line_escapes_the_recorded_version(
    version_repo: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """A committed graph whose producer version carries ESC reports the change escaped."""
    stderr = version_repo
    raw = json.loads(stderr["graph"].read_text(encoding="utf-8"))
    raw["generated_by"]["version"] = "esc\x1bversion"
    edited = stderr["root"] / "graph_edited_version.json"
    edited.write_text(json.dumps(raw), encoding="utf-8")

    status = cli.main(
        [
            "query",
            "definitions",
            "escfn",
            "--graph",
            str(edited),
            "--root",
            str(stderr["root"]),
            "--no-refresh",
        ]
    )
    captured = capsys.readouterr()

    assert status == 0
    assert _stderr_lines(captured.err) == [
        "minotaur: analyzer version changed "
        f"(esc\\x1bversion -> {ANALYZER_SEMANTICS_VERSION}), not refreshing"
    ]


def test_absent_file_warning_escapes_the_listed_file_and_system(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A system whose name carries U+202E lists an ESC-named file that is absent."""
    root = tmp_path
    _write(root, "app/one.py", "def alpha():\n    return 1\n")
    _write(
        root,
        "docs/systems/rtl\u202esys/system.toml",
        'schema_version = 1\nname = "rtl\u202esys"\nfiles = ["absent\\u001bfile.py"]\n',
    )
    _write(
        root,
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "g.json"\n'
        'targets = ["app"]\nsystems_dir = "docs/systems"\n',
    )
    monkeypatch.chdir(root)
    assert cli.main(["analyze"]) == 0

    status = cli.main(["query", "systems"])
    captured = capsys.readouterr()

    assert status == 0
    assert captured.out != ""
    assert _stderr_lines(captured.err) == [
        "minotaur: warning: absent\\x1bfile.py (listed by system rtl\\u202esys)"
    ]


def test_module_name_warning_escapes_the_hostile_path_and_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``analyze`` warns about a dotted-plus-ESC directory with the escape spellings."""
    root = tmp_path
    _write(root, "a.b\x1bc/inner.py", "x = 1\n")

    status = cli.main(["analyze", "--root", str(root), "--output", str(root / "g.json"), str(root)])
    captured = capsys.readouterr()

    assert status == 0
    assert captured.out == ""
    assert _stderr_lines(captured.err) == [
        "a.b\\x1bc/inner.py: module-name-unavailable: module name unavailable: "
        "path component 'a.b\\x1bc' contains a dot [severity=warning]"
    ]


def test_root_mismatch_warning_escapes_the_import_root_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A package under an ESC-named directory hints at that directory escaped."""
    root = tmp_path
    _write(root, "esc\x1bdir/pkg/__init__.py", "")
    _write(root, "esc\x1bdir/pkg/a.py", "def helper():\n    return 1\n")
    _write(root, "esc\x1bdir/pkg/b.py", "from pkg.a import helper\nimport numpy\nhelper()\n")

    status = cli.main(
        [
            "analyze",
            "--root",
            str(root),
            "--output",
            str(root / "g.json"),
            str(root / "esc\x1bdir/pkg"),
        ]
    )
    captured = capsys.readouterr()

    assert status == 0
    assert captured.out == ""
    assert _stderr_lines(captured.err) == [
        "minotaur: warning: 50% of imports (1 of 2) only resolve "
        f"with a different root; pass --root {root}/esc\\x1bdir "
        "so module names match import names"
    ]


def test_stale_line_prints_a_recorded_path_with_a_real_newline_as_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A drifted file whose name holds real newlines cannot forge further stderr lines."""
    root = tmp_path
    _write(root, FORGED_FILE, "def nlfn():\n    return 1\n")
    assert cli.main(_analyze(root)) == 0
    capsys.readouterr()
    (root / FORGED_FILE).write_text("def nlfn():\n    return 2\n", encoding="utf-8")

    status = cli.main(
        [
            "query",
            "definitions",
            "nlfn",
            "--graph",
            str(root / "g.json"),
            "--root",
            str(root),
            "--no-refresh",
        ]
    )
    captured = capsys.readouterr()

    assert status == 0
    assert captured.out != ""
    assert len(captured.err.splitlines()) == 1
    assert _stderr_lines(captured.err) == [f"minotaur: stale: {ESCAPED_FORGED_FILE}"]


def test_diagnostic_metadata_prints_json_ascii_escapes_for_a_non_ascii_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A printed JSON fragment spells ``é`` as ``\\u00e9``, not as the raw character."""
    root = tmp_path
    _write(root, "child.sql", "CREATE TABLE Café (parent_id int REFERENCES Parent(id))\n")

    status = cli.main(_analyze(root))
    captured = capsys.readouterr()

    assert status == 0
    assert "é" not in captured.err
    lines = _stderr_lines(captured.err)
    assert len(lines) == 1
    assert lines[0].startswith("child.sql:")
    assert lines[0].endswith(
        ": orphaned-foreign-key: orphaned foreign key [severity=warning "
        'metadata={"minotaur-sql":{"source_table":"Caf\\u00e9","constraint_name":"unnamed",'
        '"target":"Parent","reason":"undeclared"}}]'
    )


def test_python_warning_display_escapes_the_file_name_and_source_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    visible_warnings: None,
) -> None:
    """Parsing invalid escapes warns through the writer, then the display is restored."""
    root = tmp_path
    _write(root, FORGED_FILE, 'y = "\\q"\n')
    _write(root, "plain.py", 'x = "\\d \x1b[2J\x1b]0;owned\x07"\n')
    monkeypatch.chdir(root)
    display = warnings.showwarning

    status = cli.main(_analyze(root))
    captured = capsys.readouterr()

    assert status == 0
    assert warnings.showwarning is display
    lines = _stderr_lines(captured.err)
    assert len(lines) == 2
    assert lines[0].startswith(f"{ESCAPED_FORGED_FILE}:1: ")
    assert lines[0].endswith('\\n  y = "\\q"')
    assert lines[1].startswith("plain.py:1: ")
    assert lines[1].endswith('\\n  x = "\\d \\x1b[2J\\x1b]0;owned\\x07"')


def test_query_auto_refresh_warning_display_escapes_the_source_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    visible_warnings: None,
) -> None:
    """A query re-analyzing an edited file shows the same warning escaped."""
    root = tmp_path
    _write(root, "plain.py", "def f():\n    return 1\n")
    monkeypatch.chdir(root)
    assert cli.main(_analyze(root)) == 0
    capsys.readouterr()
    (root / "plain.py").write_text('x = "\\d \x1b[2J"\ndef f():\n    return 1\n', encoding="utf-8")

    status = cli.main(
        ["query", "definitions", "f", "--graph", str(root / "g.json"), "--root", str(root)]
    )
    captured = capsys.readouterr()

    assert status == 0
    assert captured.out == "plain.py:2  plain.f  function\n"
    lines = _stderr_lines(captured.err)
    assert lines[:2] == [
        "minotaur: refreshing graph (1 drifted paths)",
        "minotaur: stale: plain.py",
    ]
    assert len(lines) == 3
    assert lines[2].startswith("plain.py:1: ")
    assert lines[2].endswith('\\n  x = "\\d \\x1b[2J"')


def test_warning_filters_still_decide_whether_a_warning_shows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI routes the display only; a filter that hides a warning keeps it hidden."""
    root = tmp_path
    _write(root, "plain.py", 'x = "\\d"\n')

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        status = cli.main(_analyze(root))
    captured = capsys.readouterr()

    assert status == 0
    assert captured.err == ""


class _FailingStream:
    """A stderr whose descriptor has gone away, as after its reader closes."""

    def write(self, text: str) -> int:
        raise OSError("stderr is gone")

    def flush(self) -> None:
        raise OSError("stderr is gone")


def test_failed_warning_write_keeps_the_file_in_the_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, visible_warnings: None
) -> None:
    """A warning that cannot print is lost; it must not fail the parse of its file."""
    root = tmp_path
    _write(root, "plain.py", 'x = "\\d"\ndef f():\n    return 1\n')

    with monkeypatch.context() as patch:
        patch.setattr(sys, "stderr", _FailingStream())
        status = cli.main(_analyze(root))

    assert status == 0
    graph = json.loads((root / "g.json").read_text(encoding="utf-8"))
    assert "plain.f" in {node["label"] for node in graph["nodes"]}


def test_closed_stderr_drops_a_warning_instead_of_writing_it_to_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    visible_warnings: None,
) -> None:
    """Python sets ``sys.stderr`` to ``None`` when descriptor 2 is closed at startup."""
    root = tmp_path
    _write(root, "plain.py", 'x = "\\d"\n')

    with monkeypatch.context() as patch:
        patch.setattr(sys, "stderr", None)
        status = cli.main(_analyze(root))
    captured = capsys.readouterr()

    assert status == 0
    assert captured.out == ""


def _function(name: str, tree: ast.Module) -> ast.FunctionDef:
    return next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _enclosing_function(node: ast.AST) -> ast.FunctionDef | None:
    """Return the nearest function definition enclosing ``node``, if any."""
    candidate: ast.AST | None = node
    while getattr(candidate, "parent", None) is not None:
        candidate = candidate.parent  # type: ignore[attr-defined]
        if isinstance(candidate, ast.FunctionDef):
            return candidate
    return None


def _writer_calls(tree: ast.Module, function: ast.FunctionDef) -> dict[str, ast.Call]:
    """Collect the explicitly named plain calls inside ``function``."""
    return {
        call.func.id: call
        for call in ast.walk(function)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }


def test_cli_writes_stderr_only_through_the_escaping_writer() -> None:
    """Fail when any stderr write bypasses ``_write_stderr`` in ``cli.py``.

    Every ``sys.stderr`` reference in the CLI source must sit inside the one
    private writer, whose body must pass the finished line through
    ``escape_text`` and print it to ``sys.stderr`` with the default
    one-trailing-newline behavior, and ``_error`` must route through the
    writer instead of printing to stderr itself.
    """
    tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child.parent = parent  # type: ignore[attr-defined]

    writer = _function("_write_stderr", tree)
    error = _function("_error", tree)

    references = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
        and node.attr == "stderr"
    ]
    writer_calls = _writer_calls(tree, writer)
    error_calls = _writer_calls(tree, error)

    assert references, "the escaping writer must write to sys.stderr"
    assert all(_enclosing_function(reference) is writer for reference in references)
    assert "escape_text" in writer_calls, "the writer must escape the finished line"
    print_call = writer_calls.get("print")
    assert print_call is not None, "the writer must print the escaped line"
    # ``print`` ends with exactly one newline only when no ``end`` is given.
    assert all(keyword.arg != "end" for keyword in print_call.keywords)
    assert "_write_stderr" in error_calls, "command errors must route through the writer"
