"""Exit-code behavior of the ``minotaur query`` argparse wiring itself.

These are not about any individual query's results; they guard the dispatch
mechanism in ``cli.py`` that turns ``query <name> ...`` into a parsed
namespace. F-05 found ``--help`` on a query subcommand exiting 2 because a
nested parser's ``SystemExit`` was caught and mapped to 2 unconditionally.
"""

from __future__ import annotations

import pytest

from minotaur import cli


def test_query_subcommand_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """``--help`` is not a failure: an agent or CI wrapper must see exit 0."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["query", "callers", "--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "usage: minotaur query callers" in out


def test_query_subcommand_usage_error_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    """A genuine usage error (missing required arguments) still exits 2."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["query", "callers"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "the following arguments are required" in err
