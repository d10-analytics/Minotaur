from __future__ import annotations

from minotaur.language_interpreter.javascript.interpreter import _relative_target
from minotaur.language_interpreter.paths import resolve_relative


def test_resolve_relative_returns_prefix_or_none_on_escape() -> None:
    assert resolve_relative(("a", "b", "c"), 2) == ("a",)
    assert resolve_relative(("a",), 2) is None
    assert resolve_relative((), 0) == ()


def test_relative_target_preserves_stepwise_parent_navigation() -> None:
    assert _relative_target("pkg/mod.js", "../x/../leaf.js") == "leaf.js"
