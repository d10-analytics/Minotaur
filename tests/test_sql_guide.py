"""The SQL guide keeps the view warning contract visible to users."""

from __future__ import annotations

import re
from pathlib import Path

GUIDE = Path(__file__).parents[1] / "docs/guides/analyze-sql.md"


def _text() -> str:
    return re.sub(r"\s+", " ", GUIDE.read_text(encoding="utf-8")).strip()


def test_sql_guide_documents_view_warning_codes_and_nonfatal_severity() -> None:
    text = _text()
    assert "`circular-dependency`" in text
    assert "`view-depth-warning`" in text
    assert "namespaced `minotaur-sql` metadata" in text
    assert "Warnings are printed with severity and do not fail `analyze`" in text
    assert "error diagnostics still produce the existing nonzero status" in text


def test_sql_guide_documents_depth_scope_and_exclusions() -> None:
    text = _text()
    assert "`view_depth_threshold`" in text
    assert "default is `3`" in text
    assert "Procedure and function reads" in text
    assert "ordinary diamonds" in text
    assert "unrelated generic references" in text


def test_sql_guide_documents_duplicate_categories_payload_and_exclusions() -> None:
    text = _text()
    assert "`canonical-and-migration`" in text
    assert "`multi-migration`" in text
    assert "`multi-canonical`" in text
    assert '`{"minotaur-sql":{"category":<category>}}`' in text
    assert "root-relative POSIX" in text
    assert "Git history" in text
    assert "live catalog" in text
    assert "execute SQL" in text
    assert "replay migrations" in text
