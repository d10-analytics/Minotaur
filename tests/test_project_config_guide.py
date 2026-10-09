"""The configuration and graph-trust documentation must state every claim.

The documents are prose, and prose cannot be executed, so this module proves
content presence the same way the repository's other text-assertion tests do
(``tests/test_graph_model_loading.py`` reads source and ``pyproject.toml``
text rather than importing a parser): each named test reads a document under
``docs/`` and asserts the exact claim it must keep.  One named test guards each
behavior the configuration and trust slices ship, and every test fails if the
documented claim is removed from its document.
"""

from __future__ import annotations

import re
from pathlib import Path

GUIDE = Path(__file__).parents[1] / "docs/guides/project-configuration.md"
FRESHNESS = Path(__file__).parents[1] / "docs/concepts/freshness.md"
GRAPH_FORMAT = Path(__file__).parents[1] / "docs/formats/minotaur-graph-v1.md"
KNOWN_FIELDS_START = "The known fields inside `[minotaur]` are:"
KNOWN_FIELDS_END = "Any other field is unknown"


def _guide_text() -> str:
    """Read the guide, collapsing prose wraps so reflow cannot false-fail.

    Markdown soft-wraps prose across lines, so an asserted phrase may straddle
    a newline today or after a future reflow.  Whitespace is collapsed to a
    single space so a test still fails only when the documented behavior
    disappears, not when the paragraph is re-wrapped.
    """
    return re.sub(r"\s+", " ", GUIDE.read_text(encoding="utf-8")).strip()


def _document_text(path: Path) -> str:
    """Read one document with the same whitespace collapse as the guide."""
    return re.sub(r"\s+", " ", path.read_text(encoding="utf-8")).strip()


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


def test_guide_documents_git_probe_failure_and_unavailable_fallback() -> None:
    text = _guide_text()
    assert (
        "Outside a Git work tree — or when git is not installed or cannot be launched — "
        "the walk continues all the way to the filesystem root" in text
    )
    assert (
        "If a Git probe runs and fails, every config-capable command started inside "
        "the checkout without `--config` stops with exit `2`, quoting git's message, "
        "before writing a graph" in text
    )
    sentences = re.split(r"(?<=\.)\s+", text)
    warnings = [sentence for sentence in sentences if "source_control omitted" in sentence]
    assert len(warnings) == 1
    assert (
        "Given `--config` (or started outside the checkout), `analyze` and a graph "
        "query that refreshes a stale graph warn `source_control omitted` and continue "
        "with their normal exit status" in warnings[0]
    )
    assert "visualize" not in warnings[0]
    assert "Committed `query diff` still exits `2`, even with `--config`" in text
    assert "With discovery bypassed, `visualize` is unaffected because it never probes git" in text
    assert "The explicit `query diff OLD NEW` form never probes git either" in text
    assert (
        "When git is not installed or cannot be launched, provenance is omitted without "
        "a warning and committed `query diff` falls back to reading the disk graph" in text
    )


def test_guide_documents_hook_environment_isolation_without_discarding_policy() -> None:
    text = _guide_text()
    sentences = re.split(r"(?<=\.)\s+", text)
    ignored = [sentence for sentence in sentences if "Git probes ignore" in sentence]
    honoured = [sentence for sentence in sentences if "are still honoured" in sentence]
    assert len(ignored) == len(honoured) == 1
    assert "Minotaur can run from a pre-commit hook in any worktree" in ignored[0]
    assert "inherited repository-location variables" in ignored[0]
    assert set(re.findall(r"\bGIT_[A-Z_]+\b", ignored[0])) == {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG",
        "GIT_OBJECT_DIRECTORY",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_GRAFT_FILE",
        "GIT_INDEX_FILE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_REPLACE_REF_BASE",
        "GIT_PREFIX",
        "GIT_SHALLOW_FILE",
        "GIT_COMMON_DIR",
    }
    assert "Inherited `git -c`/`GIT_CONFIG_*` settings" in honoured[0]
    for name in (
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_NAMESPACE",
        "GIT_TEST_",
    ):
        assert name in honoured[0]
        assert all(name not in sentence for sentence in sentences if sentence != honoured[0])
    assert "except that probes set `GIT_NO_LAZY_FETCH=1` and `LC_ALL=C`" in honoured[0]


def test_guide_documents_field_by_field_precedence_with_explicit_cli_wins() -> None:
    text = _guide_text()
    assert "field by field" in text
    assert "an explicit CLI value always wins for its own field" in text


def test_guide_documents_scoped_analysis_precedence_and_conflicts() -> None:
    text = _guide_text()
    assert (
        "`analyze --scope NAME` — replaces the configured `targets` with the named "
        "system's declared files" in text
    )
    assert (
        "writes `graph.json` and its `graph.json.sha256` sidecar into that system's "
        "definition directory instead of the configured `graph`" in text
    )
    assert (
        "It requires a located configuration and cannot be combined with positional "
        "`TARGET` arguments or `--output`" in text
    )


def test_guide_documents_root_anchoring_at_the_configuration_file_directory() -> None:
    text = _guide_text()
    assert "resolved relative to the directory that contains the configuration file" in text
    assert "defaults to the configuration file's directory" in text


def test_guide_documents_targets_and_graph_anchor_at_the_declared_root() -> None:
    text = _guide_text()
    assert "declared project `root`" in text
    assert "`minotaur-graph.json` inside the project root" in text
    assert "resolves outside the root is rejected" in text


def test_guide_documents_systems_dir_as_an_optional_known_field() -> None:
    text = _guide_text()
    assert "`systems_dir`" in text
    assert "`systems_dir` — optional" in text
    assert "defaults to `docs/systems`" in text
    assert "inside the declared project root" in text


def test_guide_documents_systems_dir_root_anchoring_and_default() -> None:
    text = _guide_text()
    assert (
        "A relative `systems_dir` is likewise resolved relative to the declared project `root`"
        in text
    )
    assert "When `systems_dir` is omitted it defaults to `docs/systems` inside that root" in text


def test_guide_documents_sql_view_depth_setting_and_default() -> None:
    text = _guide_text()
    assert "`view_depth_threshold`" in text
    assert "defaults to `3`" in text
    assert "must be positive" in text


def test_guide_documents_diff_mode_configuration_boundary() -> None:
    text = _guide_text()
    assert (
        "The explicit two-file form, `query diff OLD NEW`, never locates, parses, or validates "
        "a configuration file"
    ) in text
    assert "The committed-reference form, `query diff` with no positional graphs" in text
    assert "requires a located configuration" in text
    assert (
        "For `analyze`, `visualize`, the config-consuming `query` subcommands, and "
        "committed `query diff`, `--help` only locates the configuration file to show "
        "the configured grammar; it never parses or validates that configuration."
    ) in text
    assert (
        "If locating fails because of a missing or empty `--config` or a failed Git "
        "probe, help still shows the configured grammar and exits `0` with no stderr "
        "output, while a real run exits `2`."
    ) in text
    assert "help skips discovery" not in text


def test_guide_documents_the_python_310_tomli_fallback_mechanism() -> None:
    text = _guide_text()
    assert "effective on Python 3.10 only" in text
    assert 'tomli>=2.0; python_version < "3.11"' in text
    assert "import tomllib" in text
    assert "import tomli as tomllib" in text
    assert "no mandatory third-party TOML dependency on 3.11+" in text


def test_guide_documents_foreign_key_target_file_mapping_contract() -> None:
    text = _guide_text()
    assert (
        "SQL foreign-key ownership can also be recorded with an exact target-to-file mapping"
        in text
    )
    assert 'foreign_key_target_files = { "dbo.Parent" = "schema/parent.sql"' in text
    assert "Mapping keys must be quoted SQL target names with one or two non-empty" in text
    assert "Keys are matched case-insensitively" in text
    assert "Mapping values are literal, root-relative POSIX paths ending in `.sql`" in text
    assert "cannot be absolute" in text
    assert "contain `.` or `..` path components" in text
    assert "use backslashes" in text
    assert "contain glob characters" in text
    assert "A mapping is not a glob and does not infer ownership from a filename" in text


def test_guide_documents_foreign_key_mapping_rejection_and_warning_boundaries() -> None:
    text = _guide_text()
    assert (
        "Invalid `foreign_key_target_files` syntax or values are rejected before source analysis"
        in text
    )
    assert "graph loading, or graph writing with status `2`" in text
    assert "No graph or stamp sidecar is produced by that failure" in text
    assert "only when its path is selected and readable" in text
    assert "Multiple declarations take precedence and produce `ambiguous`" in text
    assert "an absent, unreadable, or unselected mapped path produces `undeclared` instead" in text
    assert "stable warning code `orphaned-foreign-key`" in text
    assert (
        "ordered `minotaur-sql` payload fields `source_table`, `constraint_name`, `target`, "
        "and `reason`"
    ) in text
    assert "do not alter the existing generic unresolved graph identity" in text
    assert (
        "repeated observations for one source table and target text remain one coalesced generic"
        in text
    )


def test_guide_documents_config_folder_confinement() -> None:
    text = _guide_text()
    assert "`root`, `graph`, and `systems_dir` must each resolve" in text
    assert "with links followed" in text
    assert "inside the folder that contains the configuration file" in text
    assert "refused before any read or write" in text
    assert "`configured <field> escapes the config folder`" in text


def test_guide_documents_config_symlink_anchoring_at_the_link_folder() -> None:
    text = _guide_text()
    assert "anchors at the folder that holds the link, not" in text
    assert "the folder that holds the link's target" in text


def test_guide_documents_systems_mode_config_folder_confinement() -> None:
    text = _guide_text()
    assert "`query diff --systems` confines" in text
    assert "for the working copy and for the committed configuration captured" in text
    assert "every compared revision" in text
    assert "cannot be compared on either side" in text
    assert "systems mode does not follow links on its other routes" in text
    assert "a link that stays inside the configuration folder, is refused there" in text
    assert "even though the other config-sourced paths follow links" not in text


def test_guide_documents_systems_folder_confinement() -> None:
    text = _guide_text()
    assert "system's child folder and its `system.toml` must each resolve inside" in text
    assert "`system folder escapes the systems folder`" in text
    assert "`system definition escapes the systems folder`" in text
    assert "both before the definition is read" in text


def test_guide_documents_every_quoted_target_file_key_spelling() -> None:
    text = _guide_text()
    assert "must be quoted, in every spelling TOML accepts" in text
    assert "`[minotaur.sql.foreign_key_target_files]` table" in text
    assert "quoted-segment or whitespace-padded header" in text
    assert "dotted keys under `[minotaur.sql]`, `[minotaur]`, or no header at all" in text
    assert "inline table nested at any enclosing level" in text
    assert "`invalid minotaur.sql.foreign_key_target_files: target keys must be quoted`" in text


def test_guide_lists_foreign_key_target_files_in_the_sql_known_fields() -> None:
    text = _guide_text()
    fields = text.split(KNOWN_FIELDS_START, 1)[1].split(KNOWN_FIELDS_END, 1)[0]
    assert "`foreign_key_target_files`" in fields


def test_guide_states_root_alone_does_not_reanchor_configured_targets() -> None:
    text = _guide_text()
    assert "`--root` alone, however, does not re-anchor the configured `targets`" in text
    assert "pass the targets explicitly" in text
    assert "or keep the configured graph while analyzing a different root" not in text


TRUST_CLAIMS = (
    "trusted without re-analysis",
    "Anyone who can write the graph's directory, or commit to the clone, controls",
    "`--validate` checks structure only",
    "`analyze --force` to regenerate the graph from current source",
)


def test_guide_documents_committed_graph_trust() -> None:
    text = _guide_text()
    for claim in TRUST_CLAIMS:
        assert claim in text


def test_freshness_documents_committed_graph_trust() -> None:
    text = _document_text(FRESHNESS)
    for claim in TRUST_CLAIMS:
        assert claim in text


def test_graph_format_documents_committed_graph_trust() -> None:
    text = _document_text(GRAPH_FORMAT)
    for claim in TRUST_CLAIMS:
        assert claim in text
