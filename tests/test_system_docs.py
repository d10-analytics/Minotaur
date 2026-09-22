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

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
QUERY_GUIDE = ROOT / "docs/guides/system-definitions.md"
FORMAT_REFERENCE = ROOT / "docs/formats/system-definition-v1.md"
PURPOSE = ROOT / "docs/concepts/purpose.md"
QUERY_REFERENCE = ROOT / "docs/guides/query-reference.md"
README = ROOT / "README.md"
SYSTEM_WALKTHROUGH = ROOT / "examples/system-walkthrough/README.md"


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


def test_query_reference_documents_system_coverage_and_exit_semantics() -> None:
    text = _collapsed(QUERY_REFERENCE)
    assert "Coverage distinguishes the saved selection, final graph file count" in text
    assert "an observed refresh with no diagnostics is recorded as count `0`" in text
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
