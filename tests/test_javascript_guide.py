"""The JavaScript guide keeps the reference-resolution contract visible to users."""

from __future__ import annotations

import re
from pathlib import Path

GUIDE = Path(__file__).parents[1] / "docs/guides/analyze-javascript.md"
FIXTURE_ROOT = Path(__file__).parents[1] / "tests/fixtures/analyzer_semantics/javascript"


def _text() -> str:
    return re.sub(r"\s+", " ", GUIDE.read_text(encoding="utf-8")).strip()


def test_javascript_guide_documents_non_use_identifier_forms_emit_no_facts() -> None:
    text = _text()
    assert (
        "Declaration names, object-property keys, `this`, labels, class-expression names, "
        "class method keys, `MetaProperty`, and IIFE callee forms do not produce these "
        "expression facts" in text
    )
    # ``new`` left the no-fact list when its callee became an expression position.
    assert "`this`, `new`, labels" not in text


def test_javascript_guide_documents_new_callee_references() -> None:
    text = _text()
    assert (
        "The callee of `new X()` is an ordinary expression position: `new helper()` produces "
        "a `references` edge to the resolved declaration, and `new unres()` produces an "
        "unresolved-reference node, never a `calls` relationship" in text
    )


def test_javascript_guide_documents_computed_method_key_walking() -> None:
    text = _text()
    assert (
        "A class method whose key is computed, such as `[pick()]() {}`, emits no method "
        "symbol, and never a method labelled with the key's identifier text" in text
    )
    assert (
        "Its key and body are walked as expressions under the nearest emitted owner: the "
        "class symbol for a class declaration, and the enclosing owner for a class "
        "expression" in text
    )
    assert "a computed key such as `[pick()]` is an expression position and is walked as one" in text


def test_javascript_guide_documents_class_member_field_exclusion() -> None:
    text = _text()
    assert (
        "Field and static-field definitions (`field = ...` and `static field = ...`, "
        "computed or not) are the class-member exception: they emit no key fact and no "
        "initializer fact for either class form" in text
    )
    assert (
        "that exclusion covers both class declarations and class expressions, whose field "
        "and static-field definitions emit no key or initializer facts" in text
    )


def test_javascript_guide_documents_unsupported_import_locals_are_unbound() -> None:
    text = _text()
    assert (
        "The local name that binds an unsupported import is neither a binding nor a shadow, "
        "so a later use is an ordinary unbound name that produces its own unresolved "
        "reference instead of being suppressed" in text
    )


def test_javascript_guide_documents_commonjs_boundary_as_unsupported() -> None:
    text = _text()
    assert (
        "CommonJS is not resolved: `require` and `module` have no special handling, so "
        "`require('./lib.js')` and `module.exports` produce unresolved references and never "
        "an `imports` relationship" in text
    )


def test_javascript_guide_references_the_semantics_sample_fixtures() -> None:
    text = _text()
    assert "tests/fixtures/analyzer_semantics/javascript/expressions.js" in text
    assert "accessors.js" in text
    assert (FIXTURE_ROOT / "expressions.js").is_file()
    assert (FIXTURE_ROOT / "accessors.js").is_file()
