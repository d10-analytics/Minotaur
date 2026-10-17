"""Decode Python source bytes as the Python compiler would (PEP 263).

A Python file may declare its encoding in a comment on one of its first two
lines, and a UTF-8 byte-order mark selects UTF-8. Every Minotaur component
that turns Python source bytes into text uses this module so analysis,
excerpts, and text searches agree on the decoded lines.

The module depends on the standard library only, so the analysis layer and
the presentation layer can both import it without depending on each other.
"""

from __future__ import annotations

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
    readline = iter(content.splitlines(keepends=True)).__next__
    try:
        encoding, _ = tokenize.detect_encoding(readline)
    except SyntaxError as error:
        raise UnicodeError(str(error)) from error
    _require_ascii_compatible(encoding)
    return content.decode(encoding)


def is_python_source_path(path: str) -> bool:
    """Return whether ``path`` names a Python source file by its suffix."""
    return PurePosixPath(path).suffix.lower() == ".py"


def _require_ascii_compatible(encoding: str) -> None:
    # Decoding (not encoding) the ASCII range keeps ``utf-8-sig`` acceptable:
    # encoding with it would prepend a byte-order mark and fail the comparison.
    try:
        decoded = _ASCII_BYTES.decode(encoding)
    except (LookupError, UnicodeError) as error:
        raise UnicodeError(f"unsupported source encoding {encoding!r}: {error}") from error
    if decoded != _ASCII_TEXT:
        raise UnicodeError(f"source encoding {encoding!r} is not ASCII-compatible")
