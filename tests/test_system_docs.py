"""The system-definitions documentation must keep every shipped claim.

Prose cannot be executed, so this module proves content presence the same way
``tests/test_project_config_guide.py`` does: each named test reads a shipped
document — the system-definitions query guide, the system-definition-v1 format
reference, the purpose-and-boundary concept page, the query reference, or the
README — and asserts the exact claim that document must keep. One named test
guards each behavior the system-definitions slices ship, and every test fails
if the file is absent, a documented claim is removed, or a banned
shipped-v1 phrase returns to the purpose page.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
QUERY_GUIDE = ROOT / "docs/guides/system-definitions.md"
FORMAT_REFERENCE = ROOT / "docs/formats/system-definition-v1.md"
PURPOSE = ROOT / "docs/concepts/purpose.md"
QUERY_REFERENCE = ROOT / "docs/guides/query-reference.md"
README = ROOT / "README.md"
SYSTEM_WALKTHROUGH = ROOT / "examples/system-walkthrough/README.md"
GRAPH_FORMAT_REFERENCE = ROOT / "docs/formats/minotaur-graph-v1.md"


def _collapsed(path: Path) -> str:
    """Read one document, collapsing prose wraps so reflow cannot false-fail.

    Markdown soft-wraps prose across lines, so an asserted phrase may straddle
    a newline today or after a future reflow. Whitespace is collapsed to a
    single space so a test still fails only when the documented behavior
    disappears, not when a paragraph is re-wrapped.
    """
    return re.sub(r"\s+", " ", path.read_text(encoding="utf-8")).strip()


# ---------------------------------------------------------------------------
# AC-14: docs/guides/system-definitions.md
# ---------------------------------------------------------------------------


def test_query_guide_file_exists() -> None:
    assert QUERY_GUIDE.is_file(), f"query guide file missing: {QUERY_GUIDE}"


def test_query_guide_documents_exact_file_membership_and_categories() -> None:
    text = _collapsed(QUERY_GUIDE)
    assert (
        "With committed definitions, `systems` inventories every declared boundary and its "
        "graph coverage, while `surface`, `consumers`, and `system-deps` answer who reaches "
        "across a named boundary" in text
    )
    assert 'exact-file test "is this file listed — Y/N"' in text
    assert "membership" in text
    assert "`no_system`" in text
    assert "`external`" in text
    assert "the endpoint's file is listed by the named system" in text
    assert "carries a path that no system lists" in text
    assert "carries no path at all" in text


def test_query_guide_documents_the_two_consumption_layers_with_kinds() -> None:
    text = _collapsed(QUERY_GUIDE)
    assert "**Symbol layer** — `calls` and `references`" in text
    assert "**Module layer** — `imports`" in text
    assert "links against the system's modules" in text
    assert "even when no call into the system resolves" in text


def test_query_guide_documents_imports_are_never_surface() -> None:
    text = _collapsed(QUERY_GUIDE)
    assert "is a *consumer fact* and never an exposed boundary" in text
    assert "reported by `consumers` and `system-deps` but are never `surface`" in text
    assert "The module is not an implicit callable boundary" in text


def test_query_guide_documents_surface_record_semantics() -> None:
    text = _collapsed(QUERY_GUIDE)
    assert "returns one record per *exposed in-scope symbol*" in text
    assert "inbound `calls` or `references` edge whose source sits outside the system" in text
    assert "A file that merely imports the system's module exposes nothing" in text
    assert "same-file edge — is internal and exposes nothing" in text
    assert "Records key on the exposed symbol, never on the call site" in text
    assert "prints `no exposed symbols` at exit `0`" in text


def test_query_guide_documents_consumers_record_semantics() -> None:
    text = _collapsed(QUERY_GUIDE)
    assert "returns one record per *outside file*" in text
    assert "distinct relationship kinds that file contributes" in text
    assert "is a consumer through `imports` even when its calls never resolve" in text
    assert "a system may legitimately have no determined consumers" in text
    assert "prints `no consumers` at exit `0`" in text


def test_query_guide_documents_system_deps_record_semantics() -> None:
    text = _collapsed(QUERY_GUIDE)
    assert "returns one record per *target category*" in text
    assert "explicit `no_system` and `external` rows" in text
    assert "Same-system and same-file edges are internal and never a dependency" in text
    assert "No target is silently attributed to a system" in text
    assert "prints `no dependencies` at exit `0`" in text


def test_query_guide_documents_deterministic_records_and_rendering() -> None:
    text = _collapsed(QUERY_GUIDE)
    assert "key records on the semantic participant" in text
    assert "preserve call sites as payload only" in text
    assert "Records are returned in stable sorted order" in text
    assert "never node IDs" in text
    assert "shared JSON envelope" in text


def test_query_guide_documents_strict_loading_and_absent_file_warnings() -> None:
    text = _collapsed(QUERY_GUIDE)
    assert "strict-loads the whole committed systems tree" in text
    assert "exits `2`" in text
    assert "five nearest declared systems" in text
    assert (
        "A valid system name is a non-empty string with no Unicode General Category `Cc` control "
        "characters" in text
    )
    assert "listed by system" in text
    assert "the repository overview reports them across all declared systems" in text
    assert "never changes the answer or its exit status" in text


# ---------------------------------------------------------------------------
# The re-export boundary statement
# ---------------------------------------------------------------------------


REEXPORT_BOUNDARY = (
    "Re-exports are not resolved: `from pkg import f` records an unresolved "
    "`pkg.f` reference with no module-layer `imports` consumer, so "
    "`pkg/__init__.py` never links a name that another module actually defines."
)


def test_system_guide_documents_the_reexport_boundary_statement() -> None:
    text = _collapsed(QUERY_GUIDE)
    assert REEXPORT_BOUNDARY in text


def test_query_reference_documents_the_reexport_boundary_statement() -> None:
    text = _collapsed(QUERY_REFERENCE)
    assert REEXPORT_BOUNDARY in text


def test_reexport_boundary_statement_does_not_use_the_readme_connection_phrase() -> None:
    # The system README parser reads "points to" as an interaction claim, so the
    # pinned sentence must never contain it.
    assert "points to" not in REEXPORT_BOUNDARY


def test_analysis_python_readme_documents_the_reexport_boundary_statement() -> None:
    # Checks only that the pinned sentence is present in the README, not its
    # position, and must never require the phrase "points to".
    readme = ROOT / "docs/systems/analysis-python/README.md"
    text = _collapsed(readme)
    assert REEXPORT_BOUNDARY in text


def test_system_guide_names_text_fallback_exactly_once() -> None:
    text = _collapsed(QUERY_GUIDE)
    raw = QUERY_GUIDE.read_text(encoding="utf-8")
    assert raw.count("--text-fallback") == 1, (
        "`query unreferenced --text-fallback` must be named exactly once in "
        "the system-definitions guide as the text-level cross-check"
    )
    assert "query unreferenced --text-fallback" in text
    assert "retains a suspect whenever its bare name appears elsewhere" in text


# ---------------------------------------------------------------------------
# AC-15: docs/formats/system-definition-v1.md
# ---------------------------------------------------------------------------


def test_format_reference_file_exists() -> None:
    assert FORMAT_REFERENCE.is_file(), f"format reference file missing: {FORMAT_REFERENCE}"


def test_format_reference_documents_location_and_the_flat_tree() -> None:
    text = _collapsed(FORMAT_REFERENCE)
    assert "one directory per system" in text
    assert "`systems_dir`" in text
    assert "defaults to `docs/systems` inside the declared project root" in text
    assert (
        "the repository overview and three named-boundary queries work with just `--graph` "
        "and `--root`" in text
    )
    assert "**immediate child directory** of `systems_dir` that contains a `system.toml`" in text
    assert "never nested" in text
    assert "no directory is contained in another system" in text


def test_format_reference_documents_the_definition_fields() -> None:
    text = _collapsed(FORMAT_REFERENCE)
    assert "`schema_version` — **required**; the integer `1`" in text
    assert (
        "`name` — **required**; a non-empty string unique across all definitions that contains "
        "no Unicode General Category `Cc` control character" in text
    )
    assert "`files` — **required**; a non-empty list" in text
    assert "root-relative *individual repository file paths*" in text


def test_format_reference_documents_the_files_entry_vocabulary() -> None:
    text = _collapsed(FORMAT_REFERENCE)
    assert "Never directories, globs or patterns, or node IDs" in text
    assert "Never absolute paths, and never paths escaping the repository root" in text
    assert "deduplicated, keeping its first position" in text
    assert "references no qualified symbols and never node IDs" in text


def test_format_reference_documents_narrative_files_are_ignored() -> None:
    text = _collapsed(FORMAT_REFERENCE)
    assert "Only the `system.toml` file is read and validated" in text
    assert "No prose is ever parsed" in text


def test_format_reference_documents_system_graph_writes_and_committed_reads() -> None:
    text = _collapsed(FORMAT_REFERENCE)
    assert (
        "`analyze --scope NAME` writes the system's graph, `graph.json`, and its "
        "`graph.json.sha256` sidecar into the same directory" in text
    )
    assert (
        "committed `query diff --scope NAME` reads that `graph.json` and its "
        "`graph.json.sha256` sidecar at `HEAD`" in text
    )
    assert "the system loader never reads `graph.json` or its sidecar" in text
    assert "reads nothing else in the system directory" not in text


def test_format_reference_documents_strict_all_or_nothing_failure() -> None:
    text = _collapsed(FORMAT_REFERENCE)
    assert "deterministic and strict" in text
    assert "fails the whole load" in text
    assert "exits `2` with a file-attributed error" in text
    assert "no partial system set is ever returned" in text
    assert "names the offending definition file" in text


def test_format_reference_documents_each_rejection_class() -> None:
    text = _collapsed(FORMAT_REFERENCE)
    assert "missing, mistyped (non-integer or boolean), or unsupported `schema_version`" in text
    assert "any unknown field" in text
    assert "`depends_on`" in text
    assert "a missing, mistyped, or empty `name`" in text
    assert "a name containing any Unicode General Category `Cc` control character" in text
    assert "two definitions declaring the same system `name`" in text


def test_format_reference_documents_overlap_and_absent_files() -> None:
    text = _collapsed(FORMAT_REFERENCE)
    assert "one file listed in two systems" in text
    assert "a file belongs to at most one system" in text
    assert "names both defining files" in text
    assert "surfaced as a `minotaur: warning:` line" in text
    assert "never silently dropped" in text


# ---------------------------------------------------------------------------
# AC-16: docs/concepts/purpose.md
# ---------------------------------------------------------------------------


def test_purpose_file_exists() -> None:
    assert PURPOSE.is_file(), f"purpose page file missing: {PURPOSE}"


def test_purpose_section_2_marks_scopes_shipped_and_expectations_archived() -> None:
    text = _collapsed(PURPOSE)
    assert "shipped — committed system definitions" in text
    assert "*archived/deferred* — no implementation is planned" in text
    assert "declared-answer concept is archived/deferred" in text
    assert "planned expectations package" not in text
    assert "*planned* — system definitions" not in text
    assert (
        "Fixed queries that answer one structural question each from a graph: `callers`, "
        "`definitions`, `impact`, `unreferenced`, `systems`, `surface`, `consumers`, "
        "`system-deps`, `context`." in text
    )


def test_purpose_section_4_describes_a_definition_as_a_computed_scope_only() -> None:
    text = _collapsed(PURPOSE)
    assert "committed file naming a boundary by listing individual repository files" in text
    assert "Relationships are computed from the analyzed graph only" in text
    assert "declares no dependencies and no expectations" in text
    assert "no hand-recorded relationship data" in text
    assert "references no qualified names, and never node ids" in text
    assert "A scope is a lens, not a claim" in text


def test_purpose_section_4_does_not_promise_numbered_future_packages() -> None:
    text = _collapsed(PURPOSE)
    assert "No separate declared-answer or curated-edge package is part of this contract" in text
    assert "Expectations (03)" not in text
    assert "Curated relationships (04)" not in text
    assert "curated-rule edges carrying a rule id (04)" not in text


def test_purpose_never_claims_shipped_v1_expectation_or_curated_behavior() -> None:
    text = _collapsed(PURPOSE)
    assert "declares dependencies on" not in text
    assert "curated-rule edges static analysis cannot see" not in text
    assert "in a system definition *are* expectations" not in text
    assert "Scopes without expectations are documentation with nothing to check" not in text
    assert "They are one design" not in text
    assert "references qualified names and paths, never node ids" not in text


def test_purpose_section_3_principles_do_not_read_scopes_as_checked() -> None:
    text = _collapsed(PURPOSE)
    assert "A shipped scope is a read-only lens that nothing checks" in text
    assert "The declared-answer concept is archived/deferred and has no implementation" in text
    assert "checked, not trusted" not in text


def test_purpose_section_3_documents_diff_exit_facts_without_expectation_policy() -> None:
    text = _collapsed(PURPOSE)
    assert "A `diff` whose compared structures differ exits `1`" in text
    assert "an identical comparison exits `0`" in text
    assert "no shipped query compares a declared answer" in text
    assert "When the later expectations package (03) ships" not in text


def test_purpose_documents_committed_diff_and_content_keyed_reproducibility() -> None:
    text = _collapsed(PURPOSE)
    assert (
        "the committed structure at `HEAD` against the current working tree, or the same "
        "question across two explicit snapshots" in text
    )
    assert (
        "records the `source_control` commit/branch of its last real generation plus "
        "per-file content digests" in text
    )
    assert "identical content never rewrites" in text
    assert "committed bytes stay stable across commit advances and branch switches" in text
    assert "content digests, not the stamp, are the freshness authority" in text
    assert "stamp may legitimately lag `HEAD`" in text


# ---------------------------------------------------------------------------
# AC-18: docs/guides/query-reference.md and README.md
# ---------------------------------------------------------------------------


def test_query_reference_file_exists() -> None:
    assert QUERY_REFERENCE.is_file(), f"query reference file missing: {QUERY_REFERENCE}"


def test_query_reference_documents_scoped_analysis_and_committed_graph_origin() -> None:
    text = _collapsed(QUERY_REFERENCE)
    assert "minotaur analyze --scope NAME" in text
    assert "`analyze --scope` requires a located `.minotaur.toml`" in text
    assert "cannot be combined with positional targets or `--output`" in text
    assert (
        "writes the system's graph to `graph.json` and its `graph.json.sha256` "
        "sidecar in the system's definition directory" in text
    )
    assert (
        "that system's committed graph, the `graph.json` that `analyze --scope NAME` "
        "writes in its definition directory" in text
    )


def test_query_reference_enumerates_the_system_queries_with_shared_options() -> None:
    text = _collapsed(QUERY_REFERENCE)
    assert (
        "`callers`, `definitions`, `impact`, `unreferenced`, `systems`, `surface`, "
        "`consumers`, "
        "and `system-deps` accept the following common options" in text
    )
    assert (
        "`callers`, `definitions`, `impact`, `unreferenced`, `systems`, `surface`, "
        "`consumers`, "
        "and `system-deps` also report the freshness of the answer alongside it" in text
    )
    assert "minotaur query surface orders --graph GRAPH.json --root ROOT" in text
    assert "minotaur query consumers orders --graph GRAPH.json --root ROOT" in text
    assert "minotaur query system-deps orders --graph GRAPH.json --root ROOT" in text


def test_query_reference_documents_system_query_shared_option_behavior() -> None:
    text = _collapsed(QUERY_REFERENCE)
    assert "run in the shared graph-query loop" in text
    assert (
        "`--no-refresh` and `--json` behave exactly as they do for the other graph queries" in text
    )
    assert "records carry semantic labels and root-relative paths, never node IDs" in text
    assert "the four system queries also accept `--details`" in text
    assert "Text output starts with a compact, key-sorted `coverage ` JSON line" in text
    assert "`relationships` present only for `--details`" in text


def test_query_reference_documents_system_comparison_capture_scope() -> None:
    text = _collapsed(QUERY_REFERENCE)
    assert (
        "Each side captures only the comparison selection: the configuration file and, "
        "when it is discovered rather than given, its `.minotaur.toml` discovery "
        "candidates, the analysis root as a directory, the `systems_dir` subtree, and "
        "the targets."
    ) in text
    assert (
        "Inside the targets, hidden, cache and virtual-environment directories are "
        "pruned as analysis prunes them, while untracked or Git-ignored files are "
        "captured."
    ) in text
    assert (
        "Content elsewhere, such as `.venv` or data directories, is neither read nor copied."
    ) in text
    assert (
        "A committed symbolic link, gitlink or unsupported entry inside the selection "
        "is refused with status `2`, while one elsewhere is ignored."
    ) in text
    assert (
        "Links are handled differently on the two sides: inside targets and "
        "`systems_dir` the working-tree side omits links instead of refusing them; "
        "a link on a configured route is refused as described in "
        "[project configuration](project-configuration.md#path-confinement)."
    ) in text
    assert (
        'With the default `root`, setting `systems_dir = "."` makes the whole '
        "configuration folder part of the selection, and `source_digest` covers "
        "exactly the captured selection."
    ) in text


def test_query_reference_documents_system_coverage_and_exit_semantics() -> None:
    text = _collapsed(QUERY_REFERENCE)
    assert "Coverage distinguishes the saved selection, final graph file count" in text
    assert "an observed refresh reports separate `warnings` and `errors` counts" in text
    assert "`relationships` text line containing the same deterministic evidence array" in text
    assert "For system queries, `coverage` is the composed coverage of the final graph" in text


def test_query_reference_keeps_the_system_query_model_claims() -> None:
    text = _collapsed(QUERY_REFERENCE)
    assert 'exact-file test "is this file listed"' in text
    assert "import of the system's module is never surface" in text
    assert "only imports a system module is a consumer through `imports`" in text
    assert "explicit `no_system` and `external` rows" in text
    assert "an empty result prints `no exposed symbols`" in text
    assert "An empty result prints `no consumers`" in text
    assert "An empty result prints `no dependencies`" in text


def test_query_reference_documents_sql_call_consumers_and_function_candidates() -> None:
    text = _collapsed(QUERY_REFERENCE)
    assert (
        "the current SQL dependency kinds `sql:reads-from`, `sql:foreign-key-to`, and `sql:calls`"
        in text
    )
    assert "A SQL function-call result names the declaring owner and called `sql:function`" in text
    assert "`impact` follows inbound `calls`, `imports`, and the current SQL dependency" in text
    assert "SQL table, view, and function candidates use the same explicit" in text
    assert "core functions, methods, and classes, plus SQL tables, views, and functions" in text
    assert (
        "SQL functions are reported when they have no inbound `sql:calls` relationship from a"
        in text
    )
    assert "a recursive self-call does not count as inbound use" in text
    assert (
        "For SQL tables, views, and functions, it counts the bare name case-insensitively"
    ) in text
    assert "case-insensitive SQL table, view, and function declarations" in text
    assert (
        "through `calls`, `references`, `sql:reads-from`, `sql:foreign-key-to`, or `sql:calls`"
        in text
    )
    assert "including `sql:reads-from`, `sql:foreign-key-to`, and `sql:calls`" in text


def test_system_guide_documents_sql_call_boundary_kind() -> None:
    text = _collapsed(QUERY_GUIDE)
    assert (
        "rows include the current SQL relationship kinds `sql:reads-from`, "
        "`sql:foreign-key-to`, and `sql:calls`" in text
    )
    assert (
        "SQL `sql:reads-from`, `sql:foreign-key-to`, and `sql:calls` edges participate in the same"
        in text
    )
    assert (
        "A `sql:calls` edge specifically records a call from one SQL declaration "
        "to a declared `sql:function`" in text
    )
    assert (
        "Every outgoing `calls`, `references`, `imports`, `sql:reads-from`, "
        "`sql:foreign-key-to`, or `sql:calls` edge" in text
    )


def test_query_reference_documents_both_diff_modes_and_deliberate_exit_semantics() -> None:
    text = _collapsed(QUERY_REFERENCE)
    assert "Compare the committed graph at `HEAD` with the current working tree" in text
    assert "minotaur query diff --scope NAME" in text
    assert "requires a located `.minotaur.toml`" in text
    assert "Compare two analyzed graph files explicitly" in text
    assert "`diff` found structures differing in any recorded way" in text
    assert "including an added, removed, or relocated symbol or a relationship change" in text
    assert (
        "For the other graph queries, `1` means a graph refresh completed with source diagnostics"
        in text
    )


def test_query_reference_documents_committed_diff_git_failure_boundary() -> None:
    text = _collapsed(QUERY_REFERENCE)
    assert (
        "If the Git probe runs and fails, committed `query diff` exits `2`, quoting "
        "git's message instead of reading the disk graph, even with `--config`" in text
    )
    assert (
        "Only when outside a Git work tree, or when git is not installed or cannot be "
        "launched, does this mode fall back to reading the disk graph" in text
    )


def test_readme_documents_optional_graph_workflow_and_system_comparison() -> None:
    text = _collapsed(README)
    assert "optional repository workflow" in text
    assert "configuration does not commit files for you" in text
    assert "last-generation Git provenance" in text
    assert "Ordinary `query diff` compares symbols and relationships in graph snapshots" in text
    assert "`query diff --systems` compares subsystem connections" in text
    assert "examples/system-walkthrough/README.md#3-see-what-changed" in text
    assert "docs/concepts/freshness.md" in text


def test_readme_names_the_system_queries_in_prose() -> None:
    text = _collapsed(README)
    assert "Declare which files belong to each system" in text
    assert "`consumers` shows outside files that use it" in text
    assert "`surface` shows symbols they reach" in text
    assert "`system-deps` shows its outgoing dependencies" in text
    assert "`systems` summarizes the declarations" in text
    assert "docs/guides/system-definitions.md" in text


def test_system_walkthrough_leads_with_visual_questions_and_plain_language() -> None:
    text = _collapsed(SYSTEM_WALKTHROUGH)
    assert "What parts make up this project?" in text
    assert "Where do those parts connect?" in text
    assert "What changed?" in text
    assert "Open [the interactive system map](minotaur-graph.html)" in text
    assert "The red lines cross a system boundary" in text
    assert "Orders gains a new reason to depend on Billing" in text
    assert "A difference returns status `1`" in text


def test_system_walkthrough_keeps_exact_commands_in_an_optional_reference() -> None:
    text = _collapsed(SYSTEM_WALKTHROUGH)
    introduction = text.index("## 1. See the current architecture")
    comparison = text.index("## 3. See what changed")
    command_reference = text.index("## Command reference: inspect the current boundary")
    assert introduction < comparison < command_reference
    assert "optional reference" in text
    assert "[comparison reference](comparison.md)" in text


def test_readme_uses_the_greeting_walkthrough_transcript() -> None:
    text = README.read_text(encoding="utf-8")
    assert text.count("```text") == 1
    assert "app.py:11:12  app.welcome" in text
    assert "python examples/run_walkthrough.py python" in text
    assert "examples/system-walkthrough/minotaur-graph.json" not in text


# ---------------------------------------------------------------------------
# AC-08 / AC-14: historical and working-tree comparison documentation
# ---------------------------------------------------------------------------


COMPARISON_REFERENCE = ROOT / "examples/system-walkthrough/comparison.md"
CUSTOMIZE = ROOT / "docs/guides/customize-html-visualization.md"


def test_comparison_reference_documents_both_workflows_and_identities() -> None:
    assert COMPARISON_REFERENCE.is_file(), f"comparison reference missing: {COMPARISON_REFERENCE}"
    text = _collapsed(COMPARISON_REFERENCE)
    assert "minotaur query diff --systems BEFORE AFTER" in text
    assert "minotaur query diff --systems --html working-tree-comparison.html" in text
    assert (
        "using each revision's own configuration, source selection, and system definitions" in text
    )
    # The historical command's real stdout is the same change rows and context
    # lines as the working-tree command; it prints no revision header. The
    # identities belong to the saved report's compact header.
    assert "byte-for-byte the same as the working-tree report above" in text
    assert "The command line prints no revision header" in text
    assert "Writing the report with `--html` renders a compact header above the graph" in text
    assert "Before: v1.0 · 134b138" in text
    assert "After: v2.0 · a2b78dd" in text
    assert "Working tree at report generation" in text
    assert "does not change when your working files change" in text


def test_comparison_reference_explains_changes_exit_and_evidence_limits() -> None:
    text = _collapsed(COMPARISON_REFERENCE)
    assert "A boundary change" in text
    assert "A call-expression change" in text
    assert "A move" in text
    assert "A removed item" in text
    assert "means the comparison succeeded and found changes" in text
    assert "Writing a requested `--html` report does not change this" in text
    assert (
        "prints a limitation notice for that side instead of claiming the call is unchanged" in text
    )
    assert "a revision that cannot be acquired or interpreted is an error at exit `2`" in text


def test_comparison_reference_distinguishes_graph_views_from_details_source_switch() -> None:
    text = _collapsed(COMPARISON_REFERENCE)
    assert "This is a graph-level view" in text
    assert "changes only which side's captured source excerpt is shown" in text
    assert "`Captured After revision: v2.0 · a2b78dd`" in text
    assert "makes no network requests" in text
    assert "The saved report is static" in text


def test_query_reference_documents_systems_html_and_historical_revisions() -> None:
    text = _collapsed(QUERY_REFERENCE)
    assert "minotaur query diff --systems BEFORE AFTER --html comparison.html" in text
    assert "minotaur query diff --systems --html comparison.html" in text
    assert "using that revision's configuration, source selection, and system definitions" in text
    assert "identifies Before as `HEAD` with its resolved commit ID" in text
    assert "`Working tree at report generation` without inventing a commit identity" in text
    assert "retains each requested revision name and its resolved short commit ID" in text
    assert "including a successful comparison that also wrote a requested" in text
    assert "Missing call-expression evidence on a side is a limitation notice" in text


def test_comparison_guides_document_same_name_disambiguation() -> None:
    for guide in (QUERY_REFERENCE, QUERY_GUIDE):
        text = _collapsed(guide)
        assert (
            "such as a property's getter, setter and deleter, are named with their declaration role"
            in text
        )
        assert (
            "`node added: m.C.x (m.py, method, setter)` and the endpoint `m.C.x (setter)`" in text
        )
        assert (
            "When roles do not tell them apart, as for `@overload` stubs and their implementation, "
            "each is named with its 1-based declaration line, such as `m.f:5`."
        ) in text
        assert "A name that no other node shares keeps its plain form." in text


def test_comparison_guides_document_selected_view_limitation_notices() -> None:
    for guide in (QUERY_REFERENCE, QUERY_GUIDE):
        text = _collapsed(guide)
        assert (
            "view keeps only the limitation notices for the call changes it retains, in the text "
            "and in the top-level JSON `limitations`; JSON `comparison.limitations` and the "
            "`--html` report keep every notice."
        ) in text


def test_customize_html_documents_comparison_controls_and_limits() -> None:
    text = _collapsed(CUSTOMIZE)
    assert "minotaur query diff --systems BEFORE AFTER --html comparison.html" in text
    assert "Graph view" in text
    assert "**Combined**, **Before**, and **After**" in text
    assert "**Emphasize changes**" in text
    assert "**Source revision** switch" in text
    assert "it never changes the graph view, layout, zoom, pan, selection" in text
    assert "`Captured Before revision: v1.0 · 134b138`" in text
    assert (
        "`Working tree at report generation` without presenting the working "
        "tree as a commit" in text
    )
    assert "shown in a notice region and is never drawn as a detected change" in text
    assert "shows them in a compact header above the graph" in text
    assert "`Before: v1.0 · 134b138`" in text
    assert "`After: v2.0 · a2b78dd`" in text


def test_customize_html_documents_boundaries_provenance_and_shortcut_focus() -> None:
    text = _collapsed(CUSTOMIZE)
    assert (
        "With All Systems selected, an edge is marked cross-system when both endpoints "
        "belong to declared systems that differ on the viewed revision, and in Combined "
        "only when it crosses on at least one revision and is internal on neither." in text
    )
    assert (
        "Edge details list every supporting provenance and each location with its "
        "provenance, except SQL foreign-key edges that carry column mappings, which "
        "list each evidence record separately." in text
    )
    assert (
        "Shortcuts are ignored while a form field (text box, checkbox or menu) has "
        "focus, while Escape in search clears it." in text
    )


def test_system_definitions_guide_documents_per_revision_source_selection() -> None:
    text = _collapsed(QUERY_GUIDE)
    assert "minotaur query diff --systems BEFORE AFTER" in text
    assert (
        "configuration, source selection, and system definitions that belong "
        "to that revision" in text
    )
    assert "A committed graph or sidecar is not required" in text
    assert "`Working tree at report generation` without inventing a commit identity" in text
    assert (
        "A successful comparison that also writes a requested `--html` report "
        "still exits `1`" in text
    )


def test_readme_documents_historical_comparison_and_offline_report() -> None:
    text = _collapsed(README)
    assert "either `HEAD` versus the working tree or an explicit historical pair" in text
    assert "`--html comparison.html`" in text
    assert "retains the revision identities it was generated from" in text
    assert "examples/system-walkthrough/minotaur-comparison.html" in text
    assert "resolved commit IDs" in text


# ---------------------------------------------------------------------------
# Markdown link checker, walkthrough kind lists, freshness first-read list,
# query-reference systems example and empty-tree behavior, format-reference
# expected_symbol_kind, and invalid fixture enumeration.
# ---------------------------------------------------------------------------

LINK_PATTERN = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_HEADING_PATTERN = re.compile(r"^(#+)\s+(.*)$")
_SKIP_DIRECTORIES = {".git", ".venv", "node_modules", "__pycache__", "dist", "build"}


def _github_slug(heading: str) -> str:
    """The common GitHub-style anchor slugger for the checker's anchor compare."""
    text = heading.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _markdown_files() -> list[Path]:
    return sorted(path for path in ROOT.rglob("*.md") if not (set(path.parts) & _SKIP_DIRECTORIES))


def _same_repository_md_link_problems() -> list[str]:
    """Every broken same-repository ``.md`` link, with target or anchor slug.

    Same-repository relative ``.md`` links only: each target is resolved from
    the linking file's directory and must exist, and each ``#anchor`` slug is
    compared against the target file's ``#`` headings with the GitHub-style
    slugger. No network access; external links and pure-fragment links are out
    of scope.
    """
    problems: list[str] = []
    for source in _markdown_files():
        for match in LINK_PATTERN.finditer(source.read_text(encoding="utf-8")):
            target = match.group(1)
            if "://" in target or target.startswith("#"):
                continue
            path_part, _, anchor = target.partition("#")
            if not path_part.endswith(".md"):
                continue
            resolved = (source.parent / path_part).resolve()
            if not resolved.is_file():
                problems.append(f"{source}: broken target {target}")
                continue
            if not anchor:
                continue
            headings = {
                _github_slug(m.group(2))
                for line in resolved.read_text(encoding="utf-8").splitlines()
                if (m := _HEADING_PATTERN.match(line))
            }
            if anchor not in headings:
                problems.append(f"{source}: broken anchor {target}")
    return problems


def test_same_repository_markdown_link_targets_and_anchors_resolve() -> None:
    problems = _same_repository_md_link_problems()
    assert problems == [], "broken same-repository .md links: " + "; ".join(problems)


def test_system_walkthrough_kind_lists_include_sql_calls_in_canonical_order() -> None:
    text = _collapsed(SYSTEM_WALKTHROUGH)
    # Canonical SQL dependency-kind order (CURRENT_SQL_DEPENDENCY_KINDS,
    # src/minotaur/query/sql.py): sql:reads-from, sql:foreign-key-to, sql:calls.
    assert "`sql:reads-from`, `sql:foreign-key-to`, or `sql:calls`?" in text, (
        "the surface kind sentence must list sql:calls in canonical order"
    )
    assert (
        "`sql:reads-from`, `sql:foreign-key-to`, and `sql:calls`) and the concrete "
        "in-scope targets" in text
    ), "the consumers kind sentence must list sql:calls in canonical order"
    assert (
        "outgoing `calls`, `references`, `imports`, `sql:reads-from`, "
        "`sql:foreign-key-to`, and `sql:calls`" in text
    ), "the system-deps kind sentence must list sql:calls in canonical order"


def test_freshness_concept_names_functions_and_the_system_queries_as_first_reads() -> None:
    text = _collapsed(ROOT / "docs/concepts/freshness.md")
    # Function-name citations, not line numbers that go stale.
    assert (
        "`stale_graph = query.no_refresh and not observed.is_clean` gates "
        "`text_fallback=query.text_fallback and not stale_graph` in `_run_unreferenced`"
    ) in text
    assert "in `_run_unreferenced` (`minotaur/cli.py`)" in text
    assert "cli.py:360" not in text and "cli.py:380" not in text
    # The system queries share the first-read validation cost.
    for command in (
        "`query systems`",
        "`query surface`",
        "`query consumers`",
        "`query system-deps`",
    ):
        assert command in text
    assert "shared loader `_load_and_refresh_graph` in `minotaur/cli.py`" in text


def test_query_reference_states_the_real_source_of_the_systems_example() -> None:
    text = _collapsed(QUERY_REFERENCE)
    assert "the overview tests' synthetic three-file tree with two declared systems" in text, (
        "the systems example must state its real synthetic source"
    )
    assert "the synthetic fixture used by the public walkthrough" not in text


def test_query_reference_documents_the_zero_system_overview_behavior() -> None:
    text = _collapsed(QUERY_REFERENCE)
    assert "With zero declared systems, text output prints `no declared systems`" in text
    assert "`--details` appends the empty `declared_files {}` and `connections []`" in text
    assert "exits `0`" in text


def test_format_reference_documents_expected_symbol_kind_and_no_current_producer() -> None:
    text = _collapsed(GRAPH_FORMAT_REFERENCE)
    assert "optional `expected_symbol_kind` field" in text
    assert "same symbol-kind vocabulary as `symbol_kind`" in text
    assert "validated at construction" in text
    assert "no identity role" in text
    assert (
        "no bundled interpreter currently emits it, and no query or rendering path reads it"
    ) in text


def test_format_reference_lists_the_invalid_generated_at_fixture() -> None:
    text = _collapsed(GRAPH_FORMAT_REFERENCE)
    assert "- `invalid-generated-at.json` fails JSON Schema validation;" in text
    fixture = ROOT / "tests/fixtures/minotaur-graph-v1/invalid/invalid-generated-at.json"
    assert fixture.is_file()


# ---------------------------------------------------------------------------
# Isolated interpreter failures: the analyze guides and the recovery guide
# ---------------------------------------------------------------------------

ANALYZE_PYTHON_GUIDE = ROOT / "docs/guides/analyze-python.md"
SOURCE_ERROR_RECOVERY_GUIDE = ROOT / "docs/guides/source-error-recovery.md"
INTERPRETER_ERROR_GUIDES = (
    ANALYZE_PYTHON_GUIDE,
    ROOT / "docs/guides/analyze-javascript.md",
    ROOT / "docs/guides/analyze-sql.md",
    SOURCE_ERROR_RECOVERY_GUIDE,
)


def test_analyze_guides_name_the_interpreter_error_diagnostic() -> None:
    for guide in INTERPRETER_ERROR_GUIDES:
        text = _collapsed(guide)
        assert "`interpreter-error`" in text, f"{guide.name} must name `interpreter-error`"
        assert "error-severity `interpreter-error`" in text, (
            f"{guide.name} must state that `interpreter-error` is an error"
        )
        assert "with no location" in text, f"{guide.name} must state it carries no location"


def test_analyze_python_exit_statuses_include_the_interpreter_error_and_no_blame_exit() -> None:
    text = _collapsed(ANALYZE_PYTHON_GUIDE)
    assert (
        "`1` after writing a valid partial graph with parse, source-read, or "
        "`interpreter-error` diagnostics"
    ) in text
    assert "no single selected file explains" in text
    assert (
        "An analysis failure that no single selected file explains is also a dispatch "
        "error: it exits `2`, writes no graph or sidecar"
    ) in text


def test_source_error_recovery_states_the_interpreter_error_and_no_blame_outcomes() -> None:
    text = _collapsed(SOURCE_ERROR_RECOVERY_GUIDE)
    assert "no single selected file explains" in text
    assert (
        "exit `2` means an argument or preflight failure, or an analysis failure that "
        "no single selected file explains, and writes no graph"
    ) in text
    assert "writes the partial graph from the remaining files, and exits `1`" in text


# ---------------------------------------------------------------------------
# Module entry guard: console-script-first launcher documentation
# ---------------------------------------------------------------------------


PYTHON_GUIDE = ROOT / "docs/guides/analyze-python.md"
CONTRIBUTING = ROOT / "docs/guides/contributing.md"

RESIDUAL_LIMIT = (
    "CPython loads a few modules before that guard runs, and a project that "
    "defines its own `minotaur` module still shadows the module form"
)
SAFE_FORM = "For an untrusted checkout, use the "
PYTHONPATH_CAVEAT = (
    "A `PYTHONPATH` that includes the project applies to every launcher, "
    "console script included, so that "
)


def _hardening_paragraphs(path: Path) -> list[str]:
    """Return every paragraph of ``path`` that mentions ``python -P -m minotaur``."""
    paragraphs = re.split(r"\n\s*\n", path.read_text(encoding="utf-8"))
    return [re.sub(r"\s+", " ", p) for p in paragraphs if "python -P -m" in re.sub(r"\s+", " ", p)]


def test_readme_quick_start_runs_the_console_script_with_both_windows_fallbacks() -> None:
    raw = README.read_text(encoding="utf-8")
    text = _collapsed(README)
    assert "python -m pip install -e .\nminotaur --help\n" in raw
    assert "python -m minotaur --help" not in text
    assert "`.\\.venv\\Scripts\\python.exe` instead of `python`" in text
    assert "`.\\.venv\\Scripts\\minotaur.exe` instead of `minotaur`" in text


def test_readme_calls_the_module_form_equivalent_and_scopes_p_to_python_311() -> None:
    text = _collapsed(README)
    assert "`python -m minotaur` is equivalent for ordinary projects" in text
    paragraphs = _hardening_paragraphs(README)
    assert paragraphs
    for paragraph in paragraphs:
        assert "3.11" in paragraph, paragraph


def test_readme_limitations_state_the_residual_limit_and_pythonpath_caveat() -> None:
    text = _collapsed(README)
    limitations = text[text.index("## Supported behavior and limitations") :]
    assert RESIDUAL_LIMIT in limitations
    assert SAFE_FORM + "installed `minotaur` console script" in limitations
    assert "or `python -P -m minotaur` on Python 3.11 or newer" in limitations
    assert PYTHONPATH_CAVEAT in limitations
    assert "only when your environment does not put the project on the import path" in limitations


def test_python_guide_lists_the_console_script_before_the_module_form() -> None:
    text = _collapsed(PYTHON_GUIDE)
    console = text.index("minotaur analyze --root ROOT")
    module = text.index("python -m minotaur analyze --root ROOT")
    assert console < module
    assert text.index("Run the installed console script") < text.index("`python -m minotaur`")


def test_python_guide_non_execution_passage_carries_residual_limit_and_caveat() -> None:
    text = _collapsed(PYTHON_GUIDE)
    boundary = text[text.index("## Boundaries of the current slice") :]
    assert RESIDUAL_LIMIT in boundary
    assert (
        SAFE_FORM + "console script, or `python -P -m minotaur` on Python 3.11 or newer" in boundary
    )
    assert PYTHONPATH_CAVEAT in boundary
    assert "only when the environment does not put the project on the import path" in boundary


def test_python_guide_scopes_every_p_mention_to_python_311() -> None:
    paragraphs = _hardening_paragraphs(PYTHON_GUIDE)
    assert paragraphs
    for paragraph in paragraphs:
        assert "3.11" in paragraph, paragraph


def test_contributing_describes_the_entry_as_removing_the_working_directory() -> None:
    text = _collapsed(CONTRIBUTING)
    assert (
        "which removes the working directory from the import path and then invokes "
        "the same CLI as the installed command" in text
    )


def test_contributing_pins_the_comparison_capture_route_and_its_proving_test() -> None:
    text = _collapsed(CONTRIBUTING)
    assert (
        "it captures each side's analyzed selection and analyzes that side's own "
        "configuration, source, and system definitions without a saved graph or sidecar"
    ) in text
    assert (
        "For system comparisons, follow `prepare_comparison` in "
        "[comparison.py](../../src/minotaur/comparison.py): it captures each side's "
        "analyzed selection and analyzes that side's own configuration, source, and "
        "system definitions without a saved graph or sidecar, then prepares reporting "
        "snapshots. `minotaur query diff --systems` runs it"
    ) in text
    assert "`minotaur query diff --systems` runs it" in text
    assert "obtains the graph committed" not in text
    name = "test_systems_diff_forms_acquire_through_prepare_comparison"
    assert (f"`{name}` in `tests/query/test_system_diff_cli.py` proves the call") in text
    proving = (ROOT / "tests/query/test_system_diff_cli.py").read_text(encoding="utf-8")
    module = ast.parse(proving)
    assert any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
        for node in module.body
    )


def test_query_reference_documents_role_aware_diff_key_and_systems_contrast() -> None:
    text = _collapsed(QUERY_REFERENCE)
    assert (
        "Symbols are keyed by `(symbol kind, qualified label, recorded declaration role, "
        "ordinal)`, where the ordinal counts same-key definitions in source order."
    ) in text
    assert "`(symbol kind, qualified label)`" not in text
    assert (
        "Plain `diff` appends a recorded role to every such symbol in text, as in "
        "`- mod.C.x (setter)`, and adds a `role` field to its JSON record."
    ) in text
    assert (
        "which names a role only when same-label nodes collide and has no separate "
        "JSON `role` field"
    ) in text


def test_query_reference_documents_diff_unresolved_marker_and_relocation_projection() -> None:
    text = _collapsed(QUERY_REFERENCE)
    assert (
        'ends its text line with `[unresolved]` and carries `"unresolved": true` in JSON, '
        "as in `callers`; absence of the marker means resolved."
    ) in text
    assert (
        "Each location is a flat object with `path`, one-based `line`, and one-based "
        "`column`, or `null` for an unknown endpoint."
    ) in text
    assert (
        "A relocation is reported only when the start `(path, line)` changed; a body "
        "edit or a column-only shift at the same start is not one"
    ) in text
    assert "A relocation-only difference still exits `1`." in text


def test_query_reference_documents_committed_diff_error_handling() -> None:
    text = _collapsed(QUERY_REFERENCE)
    assert (
        "Committed-reference `diff` prints its diagnostics and exits `2` when the current "
        "analysis has any error-severity diagnostic"
    ) in text
    assert (
        "A group is affected when one of its old definitions is in an error file and its "
        "per-file definition counts changed."
    ) in text
    assert (
        "An affected group pairs its definitions within each file, hides unmatched old "
        "definitions and moves inside error files, and still reports that group's "
        "clean-file additions, removals, and relocations."
    ) in text
    assert (
        "A removed relationship is suppressed when any old-side instance's endpoint or "
        "recorded evidence location is in an error file."
    ) in text
    assert (
        "Every addition, every row of an unaffected group (including relocations inside "
        "an error file whose counts did not change), every warning-only row, and every "
        "clean-source edge that became unresolved because its target's file failed is kept."
    ) in text


def test_query_reference_documents_definitions_matching_and_unreferenced_refusal() -> None:
    text = _collapsed(QUERY_REFERENCE)
    assert (
        "A symbol matches when the query equals its exact label or its bare name, which "
        "is the recorded SQL name for SQL and otherwise the final label segment."
    ) in text
    assert "A root module `main` therefore does not hide `app.cli.main`" in text
    assert (
        "A table declared as `dbo.[order.lines]` has the recorded name `order.lines`, so "
        "the query `order.lines` finds it and `lines` does not, while `lines` finds "
        "`dbo.lines`"
    ) in text
    assert "but not `dbo.order.lines`" not in text
    assert (
        "If more than one definition matches the query, by exact label or by bare name, "
        "every matching line is marked `[duplicate-name]`."
    ) in text
    assert "If more than one definition has that bare name" not in text
    assert (
        "For SQL, both the exact-label and the bare-name comparison are case-insensitive, "
        "so `DBO.LINES` finds `dbo.lines`; non-SQL names retain exact matching."
    ) in text
    assert "every symbol whose qualified label ends with it" not in text
    assert (
        "exits `2` with `path has no analyzed source: PATH` instead of printing an empty result."
    ) in text


def test_graph_format_reference_states_plain_diff_reads_declaration_role() -> None:
    text = _collapsed(GRAPH_FORMAT_REFERENCE)
    assert "does not change node labels or symbol kinds." in text
    assert "or plain `query diff` results" not in text
    assert (
        "Plain `query diff` reads the same recognised role, keys each symbol by kind, "
        "label, role and source order, and appends the role to its text label and JSON record."
    ) in text


# ---------------------------------------------------------------------------
# AC-12: the safe text output contract and its system-README invariant
# ---------------------------------------------------------------------------

QUERY_AND_SYSTEM_REPORTING_README = ROOT / "docs/systems/query-and-system-reporting/README.md"
COMMAND_INTERFACE_README = ROOT / "docs/systems/command-interface/README.md"
SOURCE_PRESENTATION_README = ROOT / "docs/systems/source-presentation/README.md"
PROJECT_ACQUISITION_README = ROOT / "docs/systems/project-acquisition/README.md"

SAFE_TEXT_RULE = (
    "Text output never prints a raw control or other non-printable character taken "
    "from the analyzed repository or a graph: in result lines and in messages on "
    "standard error, each such character prints as its Python escape (a newline as "
    "`\\n`, ESC as `\\x1b`, U+202E as `\\u202e`), and printable text, including "
    "non-ASCII, prints unchanged."
)

SAFE_TEXT_EXCEPTIONS = (
    "`query context` source lines keep a real tab, `query diff --systems` prints a "
    "value containing whitespace or such a character as a JSON string, and `--json` "
    "output carries the exact strings."
)

GUIDE_JSON_FRAGMENT_ESCAPES = (
    "A JSON-encoded fragment embedded in any printed line — a result line on standard "
    "output or a diagnostic or message line on standard error — is escaped by the JSON "
    "serializer instead: a value there uses JSON's ASCII escapes, such as `\\u001b` for "
    "ESC and `\\u00e9` for `é`, so the same name can appear in two spellings on adjacent "
    "lines, and no raw character is ever printed."
)

GUIDE_JSON_FRAGMENT_SITES = (
    "Examples include `coverage` and `relationships` in system-query text, "
    "`declared_files` and `connections` in `query systems --details`, the "
    "`old coverage`, `new coverage`, `old selection`, and `new selection` lines of "
    "`query diff --systems` with its `old`, `new`, `old evidence`, and `new evidence` "
    "values, and the `metadata=` object of an analysis diagnostic on standard error."
)

ESCAPE_HELPER_SENTENCE = (
    "`escape_text` in `src/minotaur/query/render.py` is the single definition of an "
    "unsafe character: each character whose `str` form is not printable is replaced by "
    "Python's own escape, printable characters — including every non-ASCII letter — pass "
    "through unchanged, and no quotes are added."
)

PER_FIELD_STDOUT_SENTENCE = (
    "Record renderers escape the values they print field by field, at the point where "
    "each is interpolated, so no symbol label, path, kind, or role reaches a result line "
    "raw."
)

SYSTEMS_DIFF_QUOTING_SENTENCE = (
    "`query diff --systems` prints a value as a JSON string when it contains whitespace "
    "or any character the helper changes, so one value stays one token."
)

STDERR_WRITER_SENTENCE = (
    "The `command-interface` whole-line stderr writer `_write_stderr` in "
    "`src/minotaur/cli.py` escapes each finished line once before printing it, so graph, "
    "configuration, system-definition, and Git-probe text cannot carry a raw control "
    "character to the terminal."
)

UNESCAPED_JSON_SENTENCE = (
    "`--json` output bypasses the helper and carries the exact strings, encoded only by "
    "the JSON serializer."
)

README_JSON_FRAGMENT_ESCAPES = (
    "A JSON-encoded fragment embedded in any printed line — a result line on standard "
    "output or a diagnostic or message line on standard error — is escaped by the JSON "
    "serializer instead, so its values use JSON's ASCII escapes (`\\u001b` for ESC, "
    "`\\u00e9` for `é`) and a value there can differ in spelling from an escaped field on "
    "the next line; no raw character is printed."
)

README_JSON_FRAGMENT_SITES = (
    "Examples include the `coverage` and `relationships` lines of system-query text, the "
    "`declared_files` and `connections` lines of `query systems --details`, the old/new "
    "coverage and selection lines with the detail values of `query diff --systems`, and "
    "the `metadata=` object of an analysis diagnostic on standard error."
)

INVARIANT_LINK = "../query-and-system-reporting/README.md#cross-system-invariants"

COMMAND_INTERFACE_INVARIANT_POINTER = (
    "Safe text output, including this system's whole-line stderr escaping, follows the "
    f"[query and system reporting cross-system invariants]({INVARIANT_LINK})."
)


def test_query_reference_documents_the_safe_text_output_contract() -> None:
    text = _collapsed(QUERY_REFERENCE)
    # The contract belongs to the output section, not to a command's description.
    section = text[text.index("## Output and exit status") :]
    assert SAFE_TEXT_RULE in section
    assert SAFE_TEXT_EXCEPTIONS in section
    assert GUIDE_JSON_FRAGMENT_ESCAPES in section
    assert GUIDE_JSON_FRAGMENT_SITES in section


def test_system_readme_documents_the_cross_system_safe_text_invariants() -> None:
    text = _collapsed(QUERY_AND_SYSTEM_REPORTING_README)
    # The shared helper lives in this system, so it owns the invariant section;
    # the owner rule places it before the interaction claims.
    assert text.index("## Cross-system invariants") < text.index("## Expected interactions")
    section = text[
        text.index("## Cross-system invariants") : text.index("## Expected interactions")
    ]
    assert ESCAPE_HELPER_SENTENCE in section
    assert PER_FIELD_STDOUT_SENTENCE in section
    assert SYSTEMS_DIFF_QUOTING_SENTENCE in section
    assert README_JSON_FRAGMENT_ESCAPES in section
    assert README_JSON_FRAGMENT_SITES in section
    assert STDERR_WRITER_SENTENCE in section
    assert UNESCAPED_JSON_SENTENCE in section
    assert "points to" not in section, "the invariant text must not read as an interaction claim"


def test_command_interface_readme_links_the_cross_system_safe_text_invariants() -> None:
    text = _collapsed(COMMAND_INTERFACE_README)
    # The stderr writer lives here, but the shared rule is owned by the other
    # system, so this README carries one pointer before its interaction claims.
    before_interactions = text[: text.index("## Expected interactions")]
    # The pointer sentence embeds the link target, so its assertion also covers
    # the exact anchor; the repository-wide link check resolves it.
    assert COMMAND_INTERFACE_INVARIANT_POINTER in before_interactions
    assert "points to" not in before_interactions, "the pointer must not read as a claim"


SOURCE_PATH_INVARIANT_LINK = "../source-presentation/README.md#cross-system-invariants"

SOURCE_PATH_OWNER_SENTENCES = (
    "A path recorded in a graph is read from disk only through `resolve_source_path` in "
    "`src/minotaur/source.py`, which this system owns.",
    "it resolves the joined path strictly and returns the resolved path, or `None` when "
    "the path is missing, a parent component is not a directory, it contains NUL, it "
    "loops through symlinks, or it resolves outside the root, including through a "
    "symlink.",
    "Any other `OSError`, such as a permission error, propagates, so an unreadable "
    "directory is never mistaken for a missing file.",
    "a caller reads the returned path, never the root joined with the graph path, so a "
    "symlink cannot be followed past the containment check.",
    "`read_source_path` reports the source as unavailable and `capture_source_bytes` omits it",
)

QUERY_SOURCE_PATH_POINTER = (
    "Every read of a graph-recorded source path, in freshness drift, source context, and "
    "the unreferenced-symbol text scan, follows the "
    f"[source presentation cross-system invariants]({SOURCE_PATH_INVARIANT_LINK})"
)

PROJECT_ACQUISITION_SOURCE_PATH_POINTER = (
    "Comparison source capture follows the "
    f"[source presentation cross-system invariants]({SOURCE_PATH_INVARIANT_LINK}): each "
    "captured path is resolved with `resolve_source_path` before its bytes are read."
)


def test_source_presentation_readme_owns_the_source_path_containment_invariant() -> None:
    text = _collapsed(SOURCE_PRESENTATION_README)
    assert text.index("## Cross-system invariants") < text.index("## Expected interactions")
    section = text[
        text.index("## Cross-system invariants") : text.index("## Expected interactions")
    ]
    for sentence in SOURCE_PATH_OWNER_SENTENCES:
        assert sentence in section
    assert "points to" not in section, "the invariant text must not read as an interaction claim"


def test_source_path_readers_link_the_containment_invariant() -> None:
    query = _collapsed(QUERY_AND_SYSTEM_REPORTING_README)
    assert QUERY_SOURCE_PATH_POINTER in query[: query.index("## Expected interactions")]
    assert (
        "the path is resolved with `resolve_source_path` and the returned path is read."
        in (query[: query.index("## Expected interactions")])
    )
    acquisition = _collapsed(PROJECT_ACQUISITION_README)
    before_interactions = acquisition[: acquisition.index("## Expected interactions")]
    assert PROJECT_ACQUISITION_SOURCE_PATH_POINTER in before_interactions
    assert "points to" not in before_interactions, "the pointer must not read as a claim"
