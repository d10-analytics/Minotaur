from __future__ import annotations

from minotaur.language_interpreter.paths import resolve_relative


def test_resolve_relative_returns_prefix_or_none_on_escape() -> None:
    assert resolve_relative(("a", "b", "c"), 2) == ("a",)
    assert resolve_relative(("a",), 2) is None
    assert resolve_relative((), 0) == ()
