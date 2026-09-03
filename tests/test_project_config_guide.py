"""The project-configuration guide must document every shipped behavior.

The guide is prose, and prose cannot be executed, so this module proves
content presence the same way the repository's other text-assertion tests do
(``tests/test_graph_model_loading.py`` reads source and ``pyproject.toml``
text rather than importing a parser): each named test reads
``docs/guides/project-configuration.md`` and asserts the exact claim the
guide must keep.  One named test guards each behavior the configuration
slice ships, and every test fails if the guide file is absent or that
behavior is removed from it.
"""

from __future__ import annotations

import re
from pathlib import Path

GUIDE = Path(__file__).parents[1] / "docs/guides/project-configuration.md"


def _guide_text() -> str:
    """Read the guide, collapsing prose wraps so reflow cannot false-fail.

    Markdown soft-wraps prose across lines, so an asserted phrase may straddle
    a newline today or after a future reflow.  Whitespace is collapsed to a
    single space so a test still fails only when the documented behavior
    disappears, not when the paragraph is re-wrapped.
    """
    return re.sub(r"\s+", " ", GUIDE.read_text(encoding="utf-8")).strip()


def test_guide_file_exists() -> None:
    assert GUIDE.is_file(), f"guide file missing: {GUIDE}"


def test_guide_documents_the_minotaur_toml_format_and_schema_version() -> None:
    text = _guide_text()
    assert "`.minotaur.toml`" in text
    assert re.search(r"\[minotaur\]", text)
    assert re.search(r"integer `schema_version = 1`", text)
    assert "schema_version = 1" in text
    assert "schema_version" in text and "integer" in text


def test_guide_documents_walk_up_discovery_from_the_current_directory() -> None:
    text = _guide_text()
    assert "current working directory" in text
    assert "parent directories" in text
    assert "nearest" in text
    assert "filesystem root" in text


def test_guide_documents_discovery_stops_at_the_git_work_tree_root() -> None:
    text = _guide_text()
    assert "inside a Git work tree" in text
    assert "stops at the work-tree root" in text
    assert "above the work-tree root" in text
    assert "never binds" in text


def test_guide_documents_discovery_continues_outside_a_git_work_tree() -> None:
    text = _guide_text()
    assert "Outside a Git work tree" in text
    assert "continues all the way to the filesystem root" in text
    assert "preferring the nearest file" in text


def test_guide_documents_explicit_config_selection() -> None:
    text = _guide_text()
    assert "`--config CONFIG`" in text
    assert "disables walk-up discovery" in text
    assert "never merges" in text


def test_guide_documents_field_by_field_precedence_with_explicit_cli_wins() -> None:
    text = _guide_text()
    assert "field by field" in text
    assert "an explicit CLI value always wins for its own field" in text


def test_guide_documents_root_anchoring_at_the_configuration_file_directory() -> None:
    text = _guide_text()
    assert "resolved relative to the directory that contains the configuration file" in text
    assert "defaults to the configuration file's directory" in text


def test_guide_documents_targets_and_graph_anchor_at_the_declared_root() -> None:
    text = _guide_text()
    assert "declared project `root`" in text
    assert "`minotaur-graph.json` inside the project root" in text
    assert "resolves outside the root is rejected" in text


def test_guide_documents_the_python_310_tomli_fallback_mechanism() -> None:
    text = _guide_text()
    assert "effective on Python 3.10 only" in text
    assert 'tomli>=2.0; python_version < "3.11"' in text
    assert "import tomllib" in text
    assert "import tomli as tomllib" in text
    assert "no mandatory third-party TOML dependency on 3.11+" in text
