from __future__ import annotations

from pathlib import Path

from minotaur.graph_model.location import Location, Position, Range
from minotaur.language_interpreter.contract import DiagnosticCode
from minotaur.language_interpreter.reading import ParsedSource, ParseFailure, read_and_parse
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


def test_parsed_source_is_frozen() -> None:
    source = ParsedSource("file.py", b"pass", "pass", object())

    try:
        source.relative = "other.py"  # type: ignore[misc]
    except AttributeError:
        pass
    else:
        raise AssertionError("ParsedSource must be immutable")
