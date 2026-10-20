from __future__ import annotations

import os
from pathlib import Path

import pytest

from minotaur.graph_model.location import Location, Position, Range
from minotaur.graph_model.provenance import NodeClass
from minotaur.graph_model.serialization import serialize
from minotaur.language_interpreter.contract import Diagnostic, DiagnosticCode, DiagnosticSeverity
from minotaur.language_interpreter.python.interpreter import analyze_python_workspace
from minotaur.language_interpreter.reading import (
    ParsedSource,
    ParseFailure,
    RawSource,
    read_and_parse,
    read_sources,
)
from minotaur.language_interpreter.registry import default_registry
from minotaur.language_interpreter.selection import select_sources
from minotaur.language_interpreter.workspace import Workspace


def test_read_and_parse_sorts_root_relative_paths_and_preserves_raw_bytes(tmp_path: Path) -> None:
    root = Workspace(tmp_path)
    first = tmp_path / "z" / "source.py"
    second = tmp_path / "a" / "source.py"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"z = 'caf\xc3\xa9'\n")
    second.write_bytes(b"a = 1\n")
    seen: list[str] = []

    def parse(source: str, relative: str) -> str:
        seen.append(relative)
        return source.split(" = ", 1)[0]

    sources, diagnostics = read_and_parse(root, [first, second], parse)

    assert diagnostics == []
    assert seen == ["a/source.py", "z/source.py"]
    assert [source.relative for source in sources] == ["a/source.py", "z/source.py"]
    assert sources[1] == ParsedSource("z/source.py", b"z = 'caf\xc3\xa9'\n", "z = 'café'\n", "z")


def test_read_and_parse_reports_parse_failure_and_continues(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    broken = tmp_path / "a-broken.py"
    valid = tmp_path / "b-valid.py"
    broken.write_text("broken", encoding="utf-8")
    valid.write_text("valid", encoding="utf-8")
    location = Location(
        "a-broken.py",
        Range(Position(0, 2), Position(0, 2)),
    )

    def parse(source: str, relative: str) -> str:
        if relative == "a-broken.py":
            raise ParseFailure("unexpected token", location)
        return source

    sources, diagnostics = read_and_parse(workspace, (broken, valid), parse)

    assert sources == [ParsedSource("b-valid.py", b"valid", "valid", "valid")]
    assert diagnostics[0].code is DiagnosticCode.PARSE_ERROR
    assert diagnostics[0].path == "a-broken.py"
    assert diagnostics[0].message == "unexpected token"
    assert diagnostics[0].location == location


def test_read_and_parse_reports_decode_error_and_continues(tmp_path: Path) -> None:
    broken = tmp_path / "a-broken.py"
    valid = tmp_path / "b-valid.py"
    broken.write_bytes(b"\xff")
    valid.write_bytes(b"valid")
    parsed_relatives: list[str] = []

    def parse(source: str, relative: str) -> str:
        parsed_relatives.append(relative)
        return source

    sources, diagnostics = read_and_parse(Workspace(tmp_path), [broken, valid], parse)

    assert sources == [ParsedSource("b-valid.py", b"valid", "valid", "valid")]
    assert parsed_relatives == ["b-valid.py"]
    assert len(diagnostics) == 1
    assert diagnostics[0].code is DiagnosticCode.SOURCE_READ_ERROR
    assert diagnostics[0].path == "a-broken.py"
    assert diagnostics[0].location is None


def test_read_sources_preserves_raw_bytes_decodes_bom_and_isolates_siblings(
    tmp_path: Path,
) -> None:
    ordinary = tmp_path / "z" / "ordinary.py"
    bom = tmp_path / "a" / "bom.py"
    invalid = tmp_path / "b" / "invalid.py"
    missing = tmp_path / "c" / "missing.py"
    ordinary.parent.mkdir()
    bom.parent.mkdir()
    invalid.parent.mkdir()
    missing.parent.mkdir()
    ordinary_bytes = b"value = 'caf\xc3\xa9'\n"
    bom_bytes = b"\xef\xbb\xbfvalue = 1\r\n"
    ordinary.write_bytes(ordinary_bytes)
    bom.write_bytes(bom_bytes)
    invalid.write_bytes(b"\xff")

    sources, diagnostics = read_sources(
        Workspace(tmp_path),
        [ordinary, missing, invalid, bom],
    )

    assert sources == [
        RawSource("a/bom.py", bom_bytes, "value = 1\r\n"),
        RawSource("z/ordinary.py", ordinary_bytes, "value = 'café'\n"),
    ]
    assert [diagnostic.path for diagnostic in diagnostics] == ["b/invalid.py", "c/missing.py"]
    assert all(diagnostic.location is None for diagnostic in diagnostics)


def test_read_and_parse_calls_parser_once_for_each_surviving_raw_source(tmp_path: Path) -> None:
    valid = tmp_path / "valid.py"
    invalid = tmp_path / "invalid.py"
    missing = tmp_path / "missing.py"
    valid.write_bytes(b"value = 1\n")
    invalid.write_bytes(b"\xff")
    calls: list[str] = []

    def parse(source: str, relative: str) -> str:
        calls.append(relative)
        return source

    sources, diagnostics = read_and_parse(Workspace(tmp_path), [missing, invalid, valid], parse)

    assert calls == ["valid.py"]
    assert sources == [ParsedSource("valid.py", b"value = 1\n", "value = 1\n", "value = 1\n")]
    assert [diagnostic.path for diagnostic in diagnostics] == ["invalid.py", "missing.py"]


def test_read_and_parse_keeps_mixed_failure_diagnostics_in_path_order(tmp_path: Path) -> None:
    first = tmp_path / "a-parse.py"
    missing = tmp_path / "b-missing.py"
    last = tmp_path / "c-parse.py"
    first.write_text("bad\n", encoding="utf-8")
    last.write_text("bad\n", encoding="utf-8")

    def parse(source: str, relative: str) -> str:
        raise ParseFailure("unexpected token")

    _, diagnostics = read_and_parse(Workspace(tmp_path), [last, missing, first], parse)

    assert [(diagnostic.code, diagnostic.path) for diagnostic in diagnostics] == [
        (DiagnosticCode.PARSE_ERROR, "a-parse.py"),
        (DiagnosticCode.SOURCE_READ_ERROR, "b-missing.py"),
        (DiagnosticCode.PARSE_ERROR, "c-parse.py"),
    ]


def test_read_sources_decodes_with_the_supplied_hook_and_keeps_raw_bytes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "module.py"
    raw = b"value = 'caf\xe9'\n"
    source.write_bytes(raw)
    received: list[bytes] = []

    def decode(content: bytes) -> str:
        received.append(content)
        return content.decode("latin-1").upper()

    sources, diagnostics = read_sources(Workspace(tmp_path), [source], decode=decode)

    assert diagnostics == []
    assert received == [raw]
    assert sources == [RawSource("module.py", raw, "VALUE = 'CAFÉ'\n")]


def test_read_and_parse_forwards_the_hook_and_isolates_its_unicode_error(
    tmp_path: Path,
) -> None:
    broken = tmp_path / "a-broken.py"
    valid = tmp_path / "b-valid.py"
    broken.write_bytes(b"reject me\n")
    valid.write_bytes(b"accept me\n")
    failure = UnicodeError("declared encoding cannot be honoured")
    parsed_relatives: list[str] = []

    def decode(content: bytes) -> str:
        if content.startswith(b"reject"):
            raise failure
        return content.decode("ascii").upper()

    def parse(source: str, relative: str) -> str:
        parsed_relatives.append(relative)
        return source

    sources, diagnostics = read_and_parse(
        Workspace(tmp_path), [broken, valid], parse, decode=decode
    )

    assert parsed_relatives == ["b-valid.py"]
    assert sources == [ParsedSource("b-valid.py", b"accept me\n", "ACCEPT ME\n", "ACCEPT ME\n")]
    assert diagnostics == [
        Diagnostic(DiagnosticCode.SOURCE_READ_ERROR, "a-broken.py", str(failure))
    ]


def test_parsed_source_is_frozen() -> None:
    source = ParsedSource("file.py", b"pass", "pass", object())

    try:
        source.relative = "other.py"  # type: ignore[misc]
    except AttributeError:
        pass
    else:
        raise AssertionError("ParsedSource must be immutable")


_BAD_NAME = "bad\udcff"
_BACKSLASH_NAME = "we\\ird"
_MINIMAL_SOURCE = {
    ".py": "value = 1\n",
    ".js": "function ok() {}\n",
    ".sql": "CREATE TABLE t (id int);\n",
}


def _write_stray_names(root: Path, extension: str) -> None:
    """Create ``ok``, a backslash name and a non-UTF-8 name under ``root``."""
    content = _MINIMAL_SOURCE[extension].encode("utf-8")
    (root / f"ok{extension}").write_bytes(content)
    if os.name != "posix":
        pytest.skip("backslash and raw-byte names are POSIX filename features")
    (root / f"{_BACKSLASH_NAME}{extension}").write_bytes(content)
    try:
        (root / f"{_BAD_NAME}{extension}").write_bytes(content)
    except OSError:
        pytest.skip("filesystem refuses non-UTF-8 file names")


def _file_paths(document: object) -> set[str]:
    return {
        node.path
        for node in document.nodes  # type: ignore[attr-defined]
        if node.node_class is NodeClass.FILE
    }


@pytest.mark.skipif(os.name != "posix", reason="raw-byte and backslash names are POSIX only")
def test_read_sources_skips_unrepresentable_paths_and_keeps_control_characters(
    tmp_path: Path,
) -> None:
    names = ["ok.py", "we\\ird.py", "nl\nname.py", "ctl\x1b[31mred.py"]
    for name in names:
        (tmp_path / name).write_text("x = 1\n", encoding="utf-8")
    try:
        (tmp_path / os.fsdecode(b"bad\xff.py")).write_text("x = 1\n", encoding="utf-8")
    except OSError:
        pytest.skip("filesystem refuses non-UTF-8 file names")
    files = sorted(tmp_path.iterdir())

    sources, diagnostics = read_sources(Workspace(tmp_path), files)

    assert [source.relative for source in sources] == [
        "ctl\x1b[31mred.py",
        "nl\nname.py",
        "ok.py",
    ]
    assert [(item.code, item.severity, item.path) for item in diagnostics] == [
        (DiagnosticCode.SOURCE_READ_ERROR, DiagnosticSeverity.ERROR, "bad\udcff.py"),
        (DiagnosticCode.SOURCE_READ_ERROR, DiagnosticSeverity.ERROR, "we\\ird.py"),
    ]
    for item in diagnostics:
        assert "ird" not in item.message
        assert "bad" not in item.message
        assert "graph path" in item.message


@pytest.mark.parametrize("extension", [".py", ".js", ".sql"])
def test_selected_analysis_reports_unrepresentable_names_per_file(
    tmp_path: Path, extension: str
) -> None:
    _write_stray_names(tmp_path, extension)
    registry = default_registry()

    workspace, selection = select_sources(tmp_path, (tmp_path,), registry)
    registration = registry.registration_for(tmp_path / f"ok{extension}")
    assert registration is not None
    result = registration.analyze(workspace, selection.files)

    assert _file_paths(result.document) == {f"ok{extension}"}
    assert [(item.code, item.severity, item.path) for item in result.diagnostics] == [
        (DiagnosticCode.SOURCE_READ_ERROR, DiagnosticSeverity.ERROR, f"{_BAD_NAME}{extension}"),
        (
            DiagnosticCode.SOURCE_READ_ERROR,
            DiagnosticSeverity.ERROR,
            f"{_BACKSLASH_NAME}{extension}",
        ),
    ]
    assert serialize(result.document)

    if extension == ".py":
        public = analyze_python_workspace(tmp_path)
        assert _file_paths(public.document) == _file_paths(result.document)
        assert public.diagnostics == result.diagnostics
