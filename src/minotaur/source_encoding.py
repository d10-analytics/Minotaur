"""Decode Python source bytes as the Python compiler would (PEP 263).

A Python file may declare its encoding in a comment on one of its first two
lines, and a UTF-8 byte-order mark selects UTF-8. As in the compiler, the
declaration is found in the raw bytes of those lines, so bytes on them that
are not UTF-8 do not hide it. A file with neither must be UTF-8 throughout.
Every Minotaur component that turns Python source bytes into text uses this
module so analysis, excerpts, and text searches agree on the decoded lines.

The module depends on the standard library only, so the analysis layer and
the presentation layer can both import it without depending on each other.
"""

from __future__ import annotations

import codecs
import tokenize
from pathlib import PurePosixPath

_ASCII_BYTES = bytes(range(128))
_ASCII_TEXT = "".join(chr(code_point) for code_point in range(128))


def decode_python_source(content: bytes) -> str:
    """Return ``content`` decoded with its declared or default encoding.

    The encoding comes from :func:`tokenize.detect_encoding`: a PEP 263
    declaration, ``utf-8-sig`` for a byte-order mark, or ``utf-8`` otherwise.
    A declaration that cannot be honoured, a codec that is not a text codec or
    does not decode ASCII bytes to the same characters, and bytes that the
    codec cannot decode all raise :class:`UnicodeError`, so callers handle
    every decoding failure through one exception type.
    """
    # ``splitlines`` breaks on LF, CRLF and CR alike, so a file with CR-only
    # line endings is read as separate lines and later text is never mistaken
    # for a declaration on the first two lines.
    lines = content.splitlines(keepends=True)
    try:
        encoding = _detect_encoding(lines)
    except SyntaxError as error:
        encoding = _declared_beside_non_utf8_bytes(lines, error)
    _require_ascii_compatible(encoding)
    return content.decode(encoding)


def is_python_source_path(path: str) -> bool:
    """Return whether ``path`` names a Python source file by its suffix."""
    return PurePosixPath(path).suffix.lower() == ".py"


def _detect_encoding(lines: list[bytes]) -> str:
    encoding, _ = tokenize.detect_encoding(iter(lines).__next__)
    return encoding


def _declared_beside_non_utf8_bytes(lines: list[bytes], error: SyntaxError) -> str:
    """Return the declaration that ``error`` hid behind non-UTF-8 bytes.

    :func:`tokenize.detect_encoding` decodes each examined line as UTF-8
    before matching a declaration, while the compiler matches the ASCII
    declaration in the raw bytes. Mapping every byte to one character keeps
    the ASCII text of the first two lines byte-for-byte and makes the other
    bytes decodable, so the retry applies the same declaration, blank-line and
    codec rules to the bytes the compiler examines. A retry that finds no
    declaration, an unusable one, or a UTF-8 one keeps the original failure,
    because the bytes that caused it cannot be UTF-8. A byte-order mark maps
    to characters that are neither blank nor a comment, so a file that starts
    with one is always given UTF-8 by the retry and keeps its failure too.
    """
    try:
        encoding = _detect_encoding([line.decode("latin-1").encode("utf-8") for line in lines[:2]])
    except SyntaxError:
        raise UnicodeError(str(error)) from error
    if codecs.lookup(encoding).name == "utf-8":
        raise UnicodeError(str(error)) from error
    return encoding


def _require_ascii_compatible(encoding: str) -> None:
    # Decoding (not encoding) the ASCII range keeps ``utf-8-sig`` acceptable:
    # encoding with it would prepend a byte-order mark and fail the comparison.
    try:
        decoded = _ASCII_BYTES.decode(encoding)
    except (LookupError, UnicodeError) as error:
        raise UnicodeError(f"unsupported source encoding {encoding!r}: {error}") from error
    if decoded != _ASCII_TEXT:
        raise UnicodeError(f"source encoding {encoding!r} is not ASCII-compatible")
