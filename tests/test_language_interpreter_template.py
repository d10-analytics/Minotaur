"""Behavioral proof for the collision-safe language template scaffold."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
TEMPLATE_ROOT = ROOT / "templates" / "language_interpreter"
TEMPLATE_README = TEMPLATE_ROOT / "README.md"
CONTRIBUTOR_GUIDE = ROOT / "docs" / "guides" / "create-a-language-interpreter.md"
QUALIFIED_TEMPLATE = TEMPLATE_ROOT / "test_example_language_interpreter.py.tmpl"
OLD_TEMPLATE = TEMPLATE_ROOT / "test_interpreter.py.tmpl"
QUALIFIED_DESTINATION = (
    "tests/language_interpreter/example_language/test_example_language_interpreter.py"
)


def test_language_template_uses_one_language_qualified_behavioral_test_name() -> None:
    """The copied test has one stable basename that cannot collide by language."""
    assert not OLD_TEMPLATE.exists()
    assert QUALIFIED_TEMPLATE.is_file()
    assert sum(path.name == QUALIFIED_TEMPLATE.name for path in TEMPLATE_ROOT.iterdir()) == 1

    readme = TEMPLATE_README.read_text(encoding="utf-8")
    guide = CONTRIBUTOR_GUIDE.read_text(encoding="utf-8")
    assert QUALIFIED_TEMPLATE.name in readme
    assert QUALIFIED_DESTINATION in readme
    assert QUALIFIED_DESTINATION in guide
    assert "test_interpreter.py.tmpl" not in readme
    assert "minotaur-sql" not in readme
    assert "--import-mode" not in readme
