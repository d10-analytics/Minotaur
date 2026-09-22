"""Command-line interface for local Minotaur source analysis."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from minotaur import git
from minotaur.comparison import (
    _captured_analysis,
    _current_config_route,
    _select_worktree,
    _validate_live_current_contract,
)
from minotaur.comparison_snapshot import capture_local_revision, capture_working_tree
from minotaur.config import ConfigError, find_config, resolve_config
from minotaur.graph_model.document import GraphDocument, SourceControl
from minotaur.graph_model.loading import (
    GraphLoadError,
    LoadedGraph,
    graph_digest,
    load_graph_blob,
    load_graph_file,
    stamp_path,
)
from minotaur.graph_model.serialization import serialize
from minotaur.graph_visualizer.html.render import render_html
from minotaur.graph_visualizer.presentation import (
    build_comparison_presentation,
    build_presentation,
)
from minotaur.graph_visualizer.source import prepare_excerpts
from minotaur.language_interpreter.contract import (
    IMPORT_ROOT_HINT,
    IMPORTS_RESOLVED,
    IMPORTS_ROOT_MISMATCHED,
    IMPORTS_UNRESOLVED,
    AnalysisResult,
    Diagnostic,
)
from minotaur.language_interpreter.registry import InterpreterRegistration, default_registry
from minotaur.language_interpreter.selection import SelectionError, SourceSelection, select_sources
from minotaur.language_interpreter.workspace import Workspace
from minotaur.query import context as context_query
from minotaur.query import diff as diff_query
from minotaur.query import impact as impact_query
from minotaur.query import symbols as symbols_query
from minotaur.query import system as system_query
from minotaur.query import system_diff as system_diff_query
from minotaur.query import system_diff_view
from minotaur.query import unreferenced as unreferenced_query
from minotaur.query.freshness import Drift, drift, recorded_selection
from minotaur.query.index import GraphIndex
from minotaur.query.render import (
    QueryRecord,
    dump_json,
    render_json,
    render_system_json,
    render_system_text,
    render_systems_json,
    render_systems_text,
)
from minotaur.source import capture_source_bytes
from minotaur.system import System, absent_files, load_systems, resolve_system

_CONFIG_CONSUMING_COMMANDS = frozenset({"analyze", "visualize"})
_CONFIG_CONSUMING_QUERIES = frozenset(
    {
        "callers",
        "consumers",
        "context",
        "definitions",
        "impact",
        "surface",
        "system-deps",
        "systems",
        "unreferenced",
    }
)
#: The system boundary queries resolve a declared system name and strict-load
#: the committed systems tree from the resolved ``systems_dir`` before any
#: graph freshness decision (AC-12), so an invalid declaration exits 2 with
#: no answer and no refresh or rewrite on every freshness state.
_SYSTEM_QUERIES = frozenset({"surface", "consumers", "system-deps", "systems"})


def main(argv: Sequence[str] | None = None) -> int:
    """Run the console entry point and return its process exit status.

    The command deliberately completes selection and output checks before it
    asks an interpreter to read source.  That ordering gives users a useful
    safety promise: a bad path, unsupported file, or unsafe output request
    cannot leave behind a graph that looks like a successful analysis.

    Project configuration (D-05) wraps that promise without duplicating any
    owner: a locate-only step classifies the raw invocation and finds the
    governing ``.minotaur.toml`` (walk-up or ``--config``) before the parser
    is built, so the strict no-config grammar relaxes only for
    config-consuming commands when a config was located.  After parsing, each
    config-consuming command runs the shared resolver exactly once and hands
    every owner one resolved value set.  Help only performs locate-only
    discovery for the config-capable committed ``query diff`` grammar; it
    never parses or validates the located config. Explicit OLD NEW help stays
    config-free.
    """
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    systems_mode = _is_systems_mode(raw_argv)
    try:
        # Systems mode deliberately bypasses ordinary config discovery.  The
        # comparison acquisition owner must see the raw config spelling and
        # cwd so links, nested ``..`` routes, and missing inputs retain their
        # exact route attribution. It still uses the relaxed parser
        # grammar so the acquisition owner, rather than argparse, reports the
        # unusable-config case.
        located = None if systems_mode else _locate_config(raw_argv)
    except ConfigError as error:
        _error(str(error))
        return 2
    parser = _parser(config_located=located is not None or systems_mode, systems_mode=systems_mode)
    arguments = parser.parse_args(raw_argv)
    if arguments.command == "visualize":
        return _visualize(arguments, located)
    if arguments.command == "query":
        return _query(arguments, located)
    if arguments.command != "analyze":  # pragma: no cover - argparse enforces this.
        parser.error("a command is required")
    return _analyze(arguments, located)


def _locate_config(raw_argv: Sequence[str]) -> Path | None:
    """Locate the governing config without parsing or validating it (D-05).

    ``raw_argv`` is scanned as plain tokens, not with argparse, because the
    parser itself must be built differently depending on the answer.  ``None``
    keeps the strict grammar for explicit graph positionals, an unrecognized
    command, or no config discoverable from the working directory. Configured
    ``query diff --help`` is locate-only: it can expose the committed-mode
    grammar without parsing or validating the config. An explicit
    ``--config`` with an empty value or a value that does not exist raises a
    :class:`ConfigError` naming the option or path, so the failure happens
    before any parsing or analysis and never falls back to walk-up discovery.
    """
    command_index = _first_bare_token(raw_argv, 0)
    if command_index is None:
        return None
    command = raw_argv[command_index]
    if command == "query":
        subcommand_index = _first_bare_token(raw_argv, command_index + 1)
        if subcommand_index is None:
            return None
        subcommand = raw_argv[subcommand_index]
        if subcommand == "diff":
            # Explicit OLD NEW remains config-free. A bare committed-mode
            # invocation (possibly carrying --scope/--config) consumes config.
            option_tokens = raw_argv[subcommand_index + 1 :]
            if _diff_positional_tokens(option_tokens):
                return None
            # Config discovery is locate-only here. A config is never parsed
            # or validated on the help path; explicit OLD NEW help returned
            # above remains config-free.
            return find_config(Path.cwd(), config=_explicit_config(option_tokens))
        if subcommand not in _CONFIG_CONSUMING_QUERIES:
            return None
        option_tokens = raw_argv[subcommand_index + 1 :]
    elif command in _CONFIG_CONSUMING_COMMANDS:
        option_tokens = raw_argv[command_index + 1 :]
    else:
        return None
    if any(token in ("-h", "--help") for token in raw_argv):
        return None
    return find_config(Path.cwd(), config=_explicit_config(option_tokens))


def _is_systems_mode(raw_argv: Sequence[str]) -> bool:
    """Classify the systems diff from raw tokens before config resolution.

    This scanner intentionally understands only options that consume values;
    a value such as ``--config --systems`` is not mistaken for a mode flag.
    It lets systems acquisition retain the caller's lexical config route while
    ordinary diff keeps its existing config-free/config-located grammar.
    """
    command_index = _first_bare_token(raw_argv, 0)
    if command_index is None or raw_argv[command_index] != "query":
        return False
    subcommand_index = _first_bare_token(raw_argv, command_index + 1)
    if subcommand_index is None or raw_argv[subcommand_index] != "diff":
        return False
    expects_value = False
    for token in raw_argv[subcommand_index + 1 :]:
        if expects_value:
            expects_value = False
            continue
        if token in ("--config", "--scope", "--system"):
            expects_value = True
        elif token.startswith(("--config=", "--scope=", "--system=")):
            continue
        elif token == "--systems":
            return True
    return False


def _diff_positional_tokens(tokens: Sequence[str]) -> tuple[str, ...]:
    """Return graph positionals, ignoring values belonging to diff options."""
    positionals: list[str] = []
    expects_value = False
    for token in tokens:
        if expects_value:
            expects_value = False
            continue
        if token in ("--scope", "--config"):
            expects_value = True
        elif token.startswith(("--scope=", "--config=", "-")):
            continue
        else:
            positionals.append(token)
    return tuple(positionals)


def _first_bare_token(tokens: Sequence[str], start: int) -> int | None:
    """Return the index of the first non-option token at or after ``start``."""
    for index in range(start, len(tokens)):
        if not tokens[index].startswith("-"):
            return index
    return None


def _explicit_config(option_tokens: Sequence[str]) -> Path | None:
    """Read the raw ``--config`` value with argparse ``store`` semantics.

    ``--config VALUE`` consumes the following token and ``--config=VALUE``
    carries its own; a repeated option keeps its last value.  A missing value
    is not guessed here — the parser reports that usage error itself.  An
    empty value (``--config=`` or ``--config ""``) is rejected as a
    :class:`ConfigError` naming the option: ``Path("")`` would collapse to
    the working directory and be misreported as a nonexistent file.
    """
    value: str | None = None
    index = 0
    while index < len(option_tokens):
        token = option_tokens[index]
        if token == "--config":
            if index + 1 < len(option_tokens) and not option_tokens[index + 1].startswith("-"):
                value = option_tokens[index + 1]
                index += 2
                continue
        elif token.startswith("--config="):
            value = token.partition("=")[2]
        index += 1
    if value == "":
        raise ConfigError(f"--config requires a non-empty file path: got {value!r}")
    return Path(value) if value is not None else None


def _as_path(value: Any) -> Path | None:
    return Path(value) if value is not None else None


def _as_targets(value: Any) -> tuple[Path, ...] | None:
    """Convert parsed positional targets, treating "none given" as absent.

    The relaxed grammar parses an omitted positional list as ``[]``; only a
    non-empty list is an explicit override, otherwise the configured targets
    would be silently suppressed by an empty explicit value.
    """
    if not value:
        return None
    return tuple(Path(target) for target in value)


def _analyze(arguments: argparse.Namespace, located: Path | None) -> int:
    """Run one ``analyze`` invocation against its resolved project contract.

    The resolver runs before selection, output preflight, or any graph write,
    so a config that fails validation (R-05/R-06) exits 2 naming the field or
    path and creates no graph and no stamp sidecar.  Config-sourced root,
    graph, and targets reach the owners in one canonical absolute spelling;
    explicit values keep their caller spelling and win per field (R-03).
    """
    try:
        scope = getattr(arguments, "scope", None)
        if scope is not None and arguments.targets:
            raise ValueError("--scope cannot be combined with positional targets")
        if scope is not None and arguments.output is not None:
            raise ValueError("--scope cannot be combined with --output")
        if scope is not None and located is None:
            raise ValueError("--scope requires a located project config")
        resolved = resolve_config(
            Path.cwd(),
            config=located,
            explicit_root=_as_path(arguments.root),
            explicit_graph=_as_path(arguments.output),
            explicit_targets=_as_targets(arguments.targets),
        )
        root = resolved.root.resolve()
        targets: tuple[Path, ...] | None
        if scope is not None:
            systems = load_systems(resolved.systems_dir)
            selected_system = resolve_system(systems, scope)
            targets = tuple(root / relative for relative in selected_system.files)
            definition_directory = selected_system.definition_directory
            if (
                definition_directory is None
            ):  # pragma: no cover - only valid loader results reach here.
                raise ValueError(f"system definition directory unavailable: {selected_system.name}")
            output = definition_directory / "graph.json"
        else:
            targets = resolved.targets
            output = resolved.graph
        if targets is None:  # pragma: no cover - grammar or config validation supplies targets.
            raise ConfigError(
                "no analysis targets: pass positional targets or a config file with targets"
            )
        # Validate the current workspace and targets before considering a
        # clean-output skip. A deleted root or explicitly selected file must
        # remain a command error even when an old graph happens to load.
        select_sources(root, targets, default_registry())
        # D-11 deliberately runs before output preflight. A graph that Minotaur
        # can load is ours to refresh after drift, while an unrelated existing
        # file still follows the normal refusal path below.
        refresh_force = arguments.force
        if output.exists() and not arguments.force:
            try:
                existing = load_graph_file(output)
            except (GraphLoadError, OSError, ValueError):
                existing = None
            if existing is not None:
                current_selection = _target_selection(root, targets)
                if (
                    drift(existing.document, root).is_clean
                    and recorded_selection(existing.document) == current_selection
                ):
                    print("minotaur: graph is up to date, skipping analysis", file=sys.stderr)
                    return 0
                # A valid graph with drift was previously produced by this
                # command, so replacement is safe after the freshness check.
                refresh_force = True
        metadata_targets = targets if scope is not None else None
        result = _analyze_selection(root, targets, output, refresh_force, metadata_targets)
    except (OSError, SelectionError, ValueError) as error:
        _error(str(error))
        return 2

    for diagnostic in result.diagnostics:
        # Diagnostics describe source problems, not command misuse.  The
        # partial graph has already been written so tools can still consume
        # facts from readable files; stderr remains the human-facing summary.
        print(_format_diagnostic(diagnostic), file=sys.stderr)
    return 1 if result.diagnostics else 0


def _analyze_selection(
    root: Path,
    targets: tuple[Path, ...],
    output_path: Path,
    force: bool,
    metadata_targets: tuple[Path, ...] | None = None,
) -> AnalysisResult:
    """Analyze and atomically write one selected source set.

    Both the user-facing ``analyze`` command and query freshness refreshes use
    this function so selection, metadata, output preflight, and diagnostics
    cannot drift between the two write paths.
    """
    workspace, selection = select_sources(root, targets, default_registry())
    output = _preflight_output(output_path, selection.files, force)
    _, _, result = _produce_selection(
        root, targets, metadata_targets, prepared=(workspace, selection)
    )
    content = serialize(result.document)
    # The graph is written to the resolved path so the atomic replace targets
    # the real file rather than swapping a symlink out for a regular file.  The
    # sidecar, in contrast, must sit beside the path the caller gave: every
    # reader derives the stamp from its own unresolved path (``load_graph_file``
    # and ``_stamp_if_validated`` both call ``stamp_path`` on the caller path),
    # so stamping the resolved side would leave a symlinked graph permanently
    # unstamped and revalidated on every read.
    _write_atomically(output, content)
    sidecar = stamp_path(output_path)
    try:
        _write_atomically(sidecar, f"{graph_digest(content)}\n".encode("ascii"))
    except OSError as error:
        # M-3: the graph itself is already durable at this point (the write
        # above succeeded); only the trust sidecar failed. Re-raise with a
        # message naming the sidecar path and stating that fact, rather than
        # letting the raw mkstemp temporary-file errno bubble up to the user
        # (D-04's exit-2 policy for this failure is unchanged).
        reason = error.strerror or str(error)
        raise OSError(
            f"could not write graph stamp {sidecar}: {reason} (the graph itself was written)"
        ) from error
    _warn_unresolved_imports(result.document, workspace.root)
    return result


def _produce_selection(
    root: Path,
    targets: tuple[Path, ...],
    metadata_targets: tuple[Path, ...] | None = None,
    *,
    prepared: tuple[Workspace, SourceSelection] | None = None,
) -> tuple[Workspace, SourceSelection, AnalysisResult]:
    """Produce one selected graph document in memory, without filesystem writes.

    Selection, interpreter dispatch, recorded target metadata, and Git
    provenance are deliberately shared by every graph-writing path.  Callers
    own output preflight, serialization, atomic writes, sidecars, and warnings.
    """
    if prepared is None:
        workspace, selection = select_sources(root, targets, default_registry())
    else:
        workspace, selection = prepared
    result = _dispatch(workspace, selection.files)
    source_control = _git_source_control(workspace.root)
    if source_control is not None:
        result = replace(
            result,
            document=replace(result.document, source_control=source_control),
        )
    recorded_targets = targets if metadata_targets is None else metadata_targets
    result = replace(
        result,
        document=replace(
            result.document,
            extensions=_with_selection_extension(
                result.document.extensions,
                workspace.root,
                recorded_targets,
            ),
        ),
    )
    return workspace, selection, result


_ROOT_MISMATCH_WARNING_RATIO = 0.05


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _warn_unresolved_imports(document: GraphDocument, root: Path) -> None:
    """Warn when at least 5% of imports would resolve under a different root.

    Every interpreter may record ``imports_resolved`` / ``imports_unresolved``
    / ``imports_root_mismatched`` and an ``import_root_hint`` under its own
    extension namespace; the totals are summed so the check stays
    language-agnostic. Only root-mismatched imports count: third-party and
    out-of-selection imports are legitimately unresolved and must not warn.
    """
    total = mismatched = 0
    hint: str | None = None
    for value in (document.extensions or {}).values():
        total += _count(value.get(IMPORTS_RESOLVED)) + _count(value.get(IMPORTS_UNRESOLVED))
        mismatched += _count(value.get(IMPORTS_ROOT_MISMATCHED))
        candidate = value.get(IMPORT_ROOT_HINT)
        if hint is None and isinstance(candidate, str) and candidate:
            hint = candidate
    if total == 0 or mismatched / total < _ROOT_MISMATCH_WARNING_RATIO:
        return
    percent = round(100 * mismatched / total)
    suggestion = f"--root {root / hint}" if hint else "a --root matching the package layout"
    print(
        f"minotaur: warning: {percent}% of imports ({mismatched} of {total}) only resolve "
        f"with a different root; pass {suggestion} so module names match import names",
        file=sys.stderr,
    )


def _git_source_control(root: Path) -> SourceControl | None:
    """Return the Git snapshot metadata for ``root``, when it is available."""
    work_tree = git.run_git(root, ("rev-parse", "--is-inside-work-tree"))
    if work_tree is None:
        return None
    if work_tree.returncode != 0 or work_tree.stdout.strip() != "true":
        return None

    commit_result = git.run_git(root, ("rev-parse", "HEAD"))
    branch_result = git.run_git(root, ("branch", "--show-current"))
    commit = (
        commit_result.stdout.strip()
        if commit_result is not None and commit_result.returncode == 0
        else None
    )
    branch = (
        branch_result.stdout.strip()
        if branch_result is not None and branch_result.returncode == 0
        else None
    )
    if not commit and not branch:
        return None
    return SourceControl(system="git", commit=commit or None, branch=branch or None)


@dataclass(frozen=True, slots=True)
class _QueryGraph:
    """One graph a query will answer from, with how it was obtained.

    ``refreshed`` is recorded here rather than re-derived by each caller from
    ``drift``/``--no-refresh``: whether the file on disk was rewritten is a
    fact of this function, and a second spelling of the condition elsewhere
    could disagree with what actually happened.
    """

    document: GraphDocument
    diagnostics: tuple[Diagnostic, ...]
    drift: Drift
    refreshed: bool


def _report_stale(paths: Sequence[str]) -> None:
    """Print one ``stale:`` line per drifted path.

    Both freshness modes report the same per-path lines through this helper so
    an agent can parse one form regardless of whether the graph was then
    refreshed or answered as-is.
    """
    for path in paths:
        print(f"minotaur: stale: {path}", file=sys.stderr)


def _report_absent_files(systems: Sequence[System], index: GraphIndex) -> None:
    """Print one ``warning:`` line per listed-but-absent file (D-10).

    The warning follows the house ``minotaur: <kind>: <message>`` prefix
    family with the deliberate ``warning:`` kind (never ``stale:``), one line
    per listed-but-absent file of the queried system in deterministic order,
    spelled exactly ``minotaur: warning: {path} (listed by system {name})``.
    It runs only after the graph was loaded or refreshed, against the final
    index (D-09), and never affects the query's own exit status.
    """
    for absent in absent_files(systems, index.nodes.values()):
        print(
            f"minotaur: warning: {absent.file} (listed by system {absent.system.name})",
            file=sys.stderr,
        )


def _load_and_refresh_graph(
    graph_path: Path, root: Path, no_refresh: bool, *, validate: bool = False
) -> _QueryGraph:
    """Load a query graph, refreshing its recorded selection when stale.

    Callers use the returned drift's ``paths``/``is_clean`` value for warnings
    and exit-code selection.
    """
    loaded = load_graph_file(graph_path, validate=validate)
    _stamp_if_validated(graph_path, loaded)
    observed = drift(loaded.document, root)
    if observed.is_clean:
        return _QueryGraph(loaded.document, (), observed, False)
    if no_refresh:
        _report_stale(observed.paths)
        return _QueryGraph(loaded.document, (), observed, False)

    recorded = recorded_selection(loaded.document)
    if not recorded:
        raise ValueError("graph has no recorded source selection; cannot refresh")
    # Announce the attempt before performing it, and from the same drift the
    # refusal path reports: re-analysis may fail before replacement, so this
    # line must not claim that a new graph exists yet.
    print(
        f"minotaur: refreshing graph ({len(observed.paths)} drifted paths)",
        file=sys.stderr,
    )
    _report_stale(observed.paths)
    all_targets = tuple(root / target for target in recorded)
    existing_targets = tuple(target for target in all_targets if target.exists())
    # Analysis only reads the targets that still exist, but the selection
    # metadata records every target, including a deleted one. Because the
    # deleted target was never analyzed it gets no file node, so
    # ``drift._file_nodes`` has nothing to report it ``missing`` against and
    # it never forces a perpetual refresh. Keeping it in the recorded
    # selection matters for the opposite case: if the file is recreated,
    # ``drift._added_files`` walks the recorded selection and finds a path
    # that exists on disk but still has no file node, so the recreated file
    # is correctly detected as `added`. Recording only ``existing_targets``
    # would drop the deleted path from the selection entirely and a
    # recreated file at that path would never be picked up again.
    result = _analyze_selection(
        root,
        existing_targets,
        graph_path,
        True,
        metadata_targets=all_targets,
    )
    return _QueryGraph(result.document, result.diagnostics, observed, True)


@dataclass(frozen=True, slots=True)
class _GraphQuery:
    """One query answered from a freshness-checked graph index.

    The table below is the single place that knows how a query is run and
    rendered.  Keeping the per-query differences as data means a new query is
    one entry rather than another branch in the dispatcher, and the shared
    steps (refresh, index build, JSON envelope, diagnostics exit code) cannot
    be spelled differently for one query by accident.
    """

    run: Callable[[argparse.Namespace, GraphIndex, Drift], Sequence[QueryRecord]]
    render_text: Callable[[Sequence[Any]], str]


def _run_callers(
    query: argparse.Namespace, index: GraphIndex, observed: Drift
) -> Sequence[QueryRecord]:
    return symbols_query.callers(index, query.symbol)


def _run_definitions(
    query: argparse.Namespace, index: GraphIndex, observed: Drift
) -> Sequence[QueryRecord]:
    return symbols_query.definitions(index, query.symbol)


def _run_impact(
    query: argparse.Namespace, index: GraphIndex, observed: Drift
) -> Sequence[QueryRecord]:
    return impact_query.impact(index, query.symbol, query.depth)


def _run_unreferenced(
    query: argparse.Namespace, index: GraphIndex, observed: Drift
) -> Sequence[QueryRecord]:
    root = Path(query.root).resolve()
    stale_graph = query.no_refresh and not observed.is_clean
    selected_paths = _query_source_paths(
        index,
        root,
        query.paths,
        validate_current_paths=not stale_graph,
    )
    excluded_names = frozenset(query.exclude) | unreferenced_query.load_exclusions(
        query.exclude_file
    )
    return unreferenced_query.unreferenced(
        index,
        root,
        selected_paths,
        excluded_names,
        excluded_patterns=unreferenced_query.compile_patterns(query.exclude_pattern),
        # A stale no-refresh query is intentionally graph-only. The saved
        # graph remains queryable even when selected files have been removed
        # or can no longer be read, so text fallback must not inspect the
        # current workspace in this mode.
        text_fallback=query.text_fallback and not stale_graph,
    )


def _run_surface(
    query: argparse.Namespace, index: GraphIndex, observed: Drift
) -> Sequence[QueryRecord]:
    return _run_system_query(system_query.surface, query, index)


def _run_consumers(
    query: argparse.Namespace, index: GraphIndex, observed: Drift
) -> Sequence[QueryRecord]:
    return _run_system_query(system_query.consumers, query, index)


def _run_system_deps(
    query: argparse.Namespace, index: GraphIndex, observed: Drift
) -> Sequence[QueryRecord]:
    return _run_system_query(system_query.system_deps, query, index)


def _run_system_query(
    producer: Callable[[Sequence[System], GraphIndex, System], Sequence[QueryRecord]],
    query: argparse.Namespace,
    index: GraphIndex,
) -> Sequence[QueryRecord]:
    # D-10/AC-11: the absent-file diagnosis runs after load or refresh against
    # the final index, as one warning line per listed-but-absent file of the
    # queried system, and never changes the answer or its exit status.
    _report_absent_files((query.system,), index)
    return producer(query.systems, index, query.system)


def _diff_output(
    query: argparse.Namespace, old: GraphDocument, new: GraphDocument
) -> tuple[str, bool]:
    """Render one diff and report whether it contains any structural change."""
    result = diff_query.diff(old, new)
    changed = any(
        (
            result.added,
            result.removed,
            result.relocated,
            result.relationships_added,
            result.relationships_removed,
        )
    )
    output = diff_query.render_json(result) if query.json else diff_query.render_text(result)
    return output, changed


def _run_explicit_diff(query: argparse.Namespace) -> int:
    """Print explicit-mode output and return the structure-based exit code."""
    old_path, new_path = Path(query.old), Path(query.new)
    old_loaded = load_graph_file(old_path, validate=query.validate)
    _stamp_if_validated(old_path, old_loaded)
    new_loaded = load_graph_file(new_path, validate=query.validate)
    _stamp_if_validated(new_path, new_loaded)
    output, changed = _diff_output(query, old_loaded.document, new_loaded.document)
    print(output, end="")
    return 1 if changed else 0


def _committed_graph_path(resolved: Any, scope: System | None) -> Path:
    """Return the configured or loader-provenanced graph for a scope."""
    if scope is None:
        return Path(resolved.graph)
    if scope.definition_directory is None:
        raise ValueError(f"system definition directory unavailable: {scope.name}")
    return scope.definition_directory / "graph.json"


def _load_committed_old(graph_path: Path, *, root: Path, validate: bool) -> LoadedGraph:
    """Load the strict committed graph pair, or the read-only disk fallback."""
    work_tree = git.work_tree_root(root)
    if work_tree is None:
        # Outside Git (or with an unavailable probe), disk is the deliberate
        # fallback. load_graph_file itself only reads; this path never stamps.
        return load_graph_file(graph_path, validate=validate)
    try:
        relative = graph_path.resolve().relative_to(work_tree).as_posix()
    except ValueError as error:
        raise ValueError(f"committed graph is outside the Git work tree: {graph_path}") from error
    content = git.read_head_blob(work_tree, relative)
    if content is None:
        raise ValueError(f"committed graph is absent at HEAD: {graph_path}")
    sidecar_relative = f"{relative}.sha256"
    sidecar = git.read_head_blob(work_tree, sidecar_relative)
    if sidecar is None:
        raise ValueError(f"committed graph sidecar is absent at HEAD: {graph_path}.sha256")
    return load_graph_blob(content, sidecar, validate=validate)


def _run_committed_diff(query: argparse.Namespace, located: Path) -> int:
    """Compare the current in-memory selection with its committed HEAD graph."""
    resolved = resolve_config(Path.cwd(), config=located)
    scope_name = getattr(query, "scope", None)
    systems: tuple[System, ...] = ()
    scope: System | None = None
    if scope_name is not None:
        systems = load_systems(resolved.systems_dir)
        scope = resolve_system(systems, scope_name)
        targets = tuple(resolved.root / relative for relative in scope.files)
        metadata_targets: tuple[Path, ...] | None = targets
    else:
        if resolved.targets is None:
            raise ConfigError("no analysis targets configured")
        targets = resolved.targets
        metadata_targets = None
    graph_path = _committed_graph_path(resolved, scope)
    old_loaded = _load_committed_old(graph_path, root=resolved.root, validate=query.validate)
    _, _, produced = _produce_selection(resolved.root, targets, metadata_targets=metadata_targets)
    for diagnostic in produced.diagnostics:
        print(_format_diagnostic(diagnostic), file=sys.stderr)
    output, changed = _diff_output(query, old_loaded.document, produced.document)
    print(output, end="")
    return 1 if changed else 0


def _run_context(query: argparse.Namespace) -> str:
    graph_path = Path(query.graph)
    loaded = load_graph_file(graph_path, validate=query.validate)
    _stamp_if_validated(graph_path, loaded)
    record = context_query.context(
        loaded.document,
        Path(query.root).resolve(),
        query.site,
        before=query.before,
        after=query.after,
    )
    return context_query.render_json(record) if query.json else context_query.render_text(record)


_GRAPH_QUERIES: Mapping[str, _GraphQuery] = {
    "callers": _GraphQuery(
        run=_run_callers,
        render_text=symbols_query.render_callers_text,
    ),
    "definitions": _GraphQuery(
        run=_run_definitions,
        render_text=symbols_query.render_definitions_text,
    ),
    "impact": _GraphQuery(
        run=_run_impact,
        render_text=impact_query.render_text,
    ),
    "unreferenced": _GraphQuery(
        run=_run_unreferenced,
        render_text=unreferenced_query.render_text,
    ),
    "surface": _GraphQuery(
        run=_run_surface,
        render_text=system_query.render_surface_text,
    ),
    "consumers": _GraphQuery(
        run=_run_consumers,
        render_text=system_query.render_consumers_text,
    ),
    "system-deps": _GraphQuery(
        run=_run_system_deps,
        render_text=system_query.render_system_deps_text,
    ),
}

# The context snapshot query intentionally does not call _load_and_refresh_graph:
# it needs the current excerpt while its per-file hash check makes a changed
# source explicit in the result. It keeps its own JSON envelope, which is not a
# record list.
_SNAPSHOT_QUERIES: Mapping[str, Callable[[argparse.Namespace], str]] = {
    "context": _run_context,
}


def _query(arguments: argparse.Namespace, located: Path | None) -> int:
    """Dispatch a fixed query against one graph snapshot.

    The query subcommands are registered directly on the main parser (see
    ``_parser``), so ``arguments`` here is already a fully parsed query
    invocation — there is no nested ``parse_args`` call left to raise
    ``SystemExit``. That matters for exit codes: argparse itself now handles
    ``--help`` (exit 0) and usage errors (exit 2) exactly as it does for
    ``analyze`` and ``visualize``, instead of this function collapsing both
    outcomes to a single hard-coded exit 2.

    ``query diff`` with two explicit graph files never locates, parses, or
    validates a config (D-05), so it answers straight from its arguments;
    committed mode resolves the located config exactly once. Every other query
    resolves exactly once and substitutes one resolved value set into the
    namespace before the snapshot or graph-query owner reads it, so
    config-sourced ``graph`` and ``root`` reach loads,
    refreshes, and stamps in a single canonical spelling.
    """
    try:
        if arguments.name == "diff":
            if getattr(arguments, "systems", False):
                return _run_systems_diff(arguments)
            if arguments.old is None and located is not None:
                return _run_committed_diff(arguments, located)
            if arguments.old is None or arguments.new is None:
                raise ValueError("diff requires either OLD NEW or a located project config")
            return _run_explicit_diff(arguments)
        snapshot = _SNAPSHOT_QUERIES.get(arguments.name)
        if arguments.name != "diff":
            resolved = resolve_config(
                Path.cwd(),
                config=located,
                explicit_root=_as_path(arguments.root),
                explicit_graph=_as_path(arguments.graph),
            )
            arguments.graph = resolved.graph
            arguments.root = resolved.root
            if arguments.name in _SYSTEM_QUERIES:
                # AC-12 (D-09): the strict system-tree load runs here, in
                # _query's system-query path, before _run_graph_query can
                # invoke _load_and_refresh_graph -- so an invalid committed
                # declaration exits 2 before any freshness refresh or rewrite
                # can start, on every freshness state (clean graph, drifted
                # graph, and --no-refresh alike).  The loaded systems and the
                # resolved name are stashed on the namespace for the run
                # handler, which consumes them after the graph is final.
                arguments.systems = load_systems(resolved.systems_dir)
                if arguments.name != "systems":
                    arguments.system = resolve_system(arguments.systems, arguments.system_name)
        if snapshot is not None:
            print(snapshot(arguments), end="")
            return 0
        return _run_graph_query(arguments)
    except (GraphLoadError, OSError, ValueError) as error:
        _error(str(error))
        return 2


_SYSTEMS_SHORT_ID_LENGTH = 7
_LARGE_ARTIFACT_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _SystemsPair:
    """One acquired source pair plus the evidence needed to publish it."""

    old_snapshot: Any
    new_snapshot: Any
    before_sources: Mapping[str, bytes]
    after_sources: Mapping[str, bytes]
    old_call_observations: tuple[Any, ...]
    new_call_observations: tuple[Any, ...]
    old_revision: str | None
    new_revision: str | None
    comparison_context: Mapping[str, object]
    protected_files: frozenset[Path]
    protected_directories: frozenset[Path]


@dataclass(frozen=True, slots=True)
class _HtmlOutputPlan:
    """The identity captured when a comparison report destination was accepted."""

    display: Path
    resolved: Path
    existed: bool
    identity: tuple[int, int] | None
    parent: Path
    parent_identity: tuple[int, int] | None


def _run_systems_diff(query: argparse.Namespace) -> int:
    """Acquire, compare, and publish one configured source pair.

    The grammar is exactly zero or two positional revisions, and the HTML
    option is exclusive to this route.  Output preflight precedes acquisition;
    the rendered report is published atomically only after the complete result
    and requested artifact exist in memory, so a failure cannot leave a
    successful-looking partial stdout report behind.
    """
    before_revision = getattr(query, "old", None)
    after_revision = getattr(query, "new", None)
    if (before_revision is None) != (after_revision is None):
        raise ValueError("--systems requires either zero or exactly two revisions")
    html_path = _as_path(getattr(query, "html", None))
    force = bool(getattr(query, "force", False))
    if force and html_path is None:
        raise ValueError("--force requires --html")

    plan = _preflight_html_output(html_path, force=force) if html_path is not None else None
    pair = _acquire_systems_pair(query)
    complete = system_diff_query.compare_systems(
        pair.old_snapshot,
        pair.new_snapshot,
        old_call_observations=pair.old_call_observations,
        new_call_observations=pair.new_call_observations,
        old_revision=pair.old_revision,
        new_revision=pair.new_revision,
    )
    requested_system = getattr(query, "system", None)
    selected = system_diff_view.filter_system_diff(complete, requested_system)

    content: bytes | None = None
    if plan is not None:
        presentation = build_comparison_presentation(
            complete, {"before": pair.before_sources, "after": pair.after_sources}
        )
        comparison_payload = presentation.get("comparison")
        if isinstance(comparison_payload, dict):
            comparison_payload.update(pair.comparison_context)
            comparison_payload["selected_system"] = requested_system
        content = render_html(presentation)

    if query.json:
        context = complete.to_dict()
        context.update(pair.comparison_context)
        context["selected_system"] = requested_system
        payload = selected.to_dict()
        payload["comparison"] = context
        payload["selected_system"] = requested_system
        output = dump_json(payload)
    else:
        output = system_diff_view.render_text(selected, details=query.details)

    if plan is not None and content is not None:
        _publish_comparison_html(
            plan,
            content,
            protected_files=pair.protected_files,
            protected_directories=pair.protected_directories,
        )
    print(output, end="")
    if content is not None and len(content) > _LARGE_ARTIFACT_BYTES:
        print("minotaur: warning: comparison report exceeds 10 MiB", file=sys.stderr)
    return selected.exit_code


def _acquire_systems_pair(query: argparse.Namespace) -> _SystemsPair:
    """Capture and analyze one zero- or two-revision source pair.

    Both sides reuse the shared comparison owners: the capture layer
    materializes immutable source trees and the captured-analysis owner parses
    each side's own configuration, selection, and system definitions.  Saved
    graphs and sidecars are never read, so a stale committed graph cannot
    change the source-derived baseline.
    """
    before_revision: str | None = getattr(query, "old", None)
    after_revision: str | None = getattr(query, "new", None)
    validate = bool(getattr(query, "validate", False))
    worktree, preserved_start = _select_worktree(Path.cwd())
    shared = _shared_config_coordinate(worktree, preserved_start, getattr(query, "config", None))
    before_override = _side_config_coordinate(
        getattr(query, "before_config", None), label="--before-config"
    )
    after_override = _side_config_coordinate(
        getattr(query, "after_config", None), label="--after-config"
    )

    if after_revision is None:
        # The working-tree side validates its live route before any capture, so
        # an unsafe live declaration is a current input error rather than a
        # capture-side absence.
        live_raw = _live_config_argument(query, worktree, after_override)
        _validate_live_current_contract(
            worktree, _current_config_route(worktree, preserved_start, live_raw)
        )

    if before_revision is None:
        before = capture_local_revision(worktree, "HEAD", side="before")
    else:
        before = capture_local_revision(worktree, before_revision, side="before")
    try:
        if after_revision is None:
            after = capture_working_tree(worktree, side="after")
        else:
            after = capture_local_revision(worktree, after_revision, side="after")
    except BaseException:
        before.close()
        raise

    with before, after:
        after_start = after.root / preserved_start.relative_to(worktree)
        discovered: str | None = None
        if shared is None and (before_override is None or after_override is None):
            discovered = _current_config_route(after.root, after_start, None).coordinate
        before_coordinate = _require_config_coordinate(before_override or shared or discovered)
        after_coordinate = _require_config_coordinate(after_override or shared or discovered)
        # The captured-analysis owner consumes the source producer but does not
        # retain its call-expression stream, so record each side's observations
        # at the same producer boundary. Call facts stay paired with the exact
        # captured bytes that produced them.
        old_observations: list[Any] = []
        new_observations: list[Any] = []

        def _producer_for(
            sink: list[Any],
        ) -> Callable[..., tuple[Workspace, SourceSelection, AnalysisResult]]:
            def _record(
                root: Path,
                targets: tuple[Path, ...],
                metadata_targets: tuple[Path, ...] | None = None,
            ) -> tuple[Workspace, SourceSelection, AnalysisResult]:
                workspace, selection, result = _produce_selection(root, targets, metadata_targets)
                sink.extend(result.call_expressions)
                return workspace, selection, result

            return _record

        old = _captured_analysis(
            before, before_coordinate, _producer_for(old_observations), validate=validate
        )
        new = _captured_analysis(
            after, after_coordinate, _producer_for(new_observations), validate=True
        )
        _require_pair_targets_materialized(old, new)
        before_sources = capture_source_bytes(before.root, _document_paths(old.graph.document))
        after_sources = capture_source_bytes(after.root, _document_paths(new.graph.document))
        protected_files, protected_directories = _protected_comparison_paths(
            worktree, (old, new), (before_coordinate, after_coordinate)
        )
        comparison_context = {
            "schema_version": 1,
            "before": _side_context_record(
                kind="commit",
                requested_revision="HEAD" if before_revision is None else before_revision,
                commit=old.snapshot.commit,
                config_path=before_coordinate,
                analysis=old,
            ),
            "after": _side_context_record(
                kind="working-tree" if after_revision is None else "commit",
                requested_revision=after_revision,
                commit=None if after_revision is None else new.snapshot.commit,
                config_path=after_coordinate,
                analysis=new,
            ),
        }
        return _SystemsPair(
            old_snapshot=system_query.ReportingSnapshot.prepare(old.graph.document, old.systems),
            new_snapshot=system_query.ReportingSnapshot.prepare(new.graph.document, new.systems),
            before_sources=before_sources,
            after_sources=after_sources,
            old_call_observations=tuple(old_observations),
            new_call_observations=tuple(new_observations),
            old_revision=_revision_label(
                "HEAD" if before_revision is None else before_revision, old.snapshot.commit
            ),
            new_revision=(
                "Working tree at report generation"
                if after_revision is None
                else _revision_label(after_revision, new.snapshot.commit)
            ),
            comparison_context=comparison_context,
            protected_files=protected_files,
            protected_directories=protected_directories,
        )


def _side_context_record(
    *,
    kind: str,
    requested_revision: str | None,
    commit: str | None,
    config_path: str,
    analysis: Any,
) -> dict[str, object]:
    """Build one schema-versioned captured-side record for the comparison object."""
    return {
        "kind": kind,
        "requested_revision": requested_revision,
        "commit": commit,
        "config_path": config_path,
        "root": analysis.normalized_root,
        "targets": list(analysis.normalized_targets),
        "source_digest": _capture_digest(analysis.snapshot.manifest),
    }


def _capture_digest(manifest: Any) -> str:
    """Digest the captured logical entries without any temporary path."""
    digest = hashlib.sha256()
    for entry in manifest.entries:
        token = f"{entry.path}\x00{entry.kind}\x00{entry.mode}\x00{entry.size}\x00{entry.digest}\n"
        digest.update(token.encode())
    return digest.hexdigest()


def _repository_relative(value: str, *, label: str) -> str:
    """Normalize a repository-relative path declaration without touching disk."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} requires a non-empty path")
    if value.startswith("/"):
        raise ValueError(f"{label} must be a repository-relative path: {value}")
    parts: list[str] = []
    for component in value.split("/"):
        if component in {"", "."}:
            continue
        if component == "..":
            if not parts:
                raise ValueError(f"{label} escapes the repository: {value}")
            parts.pop()
        else:
            parts.append(component)
    if not parts:
        raise ValueError(f"{label} requires a non-empty path")
    return "/".join(parts)


def _shared_config_coordinate(worktree: Path, start: Path, raw: str | None) -> str | None:
    """Convert the shared config spelling into a repository-relative coordinate.

    A relative shared path keeps its existing working-directory meaning. An
    absolute shared path is accepted only when it is lexically inside the
    invoking repository; both are converted without resolving through the
    current tree.
    """
    if raw is None:
        return None
    if not raw.strip():
        raise ValueError("--config requires a non-empty file path")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = start / candidate
    try:
        candidate = candidate.relative_to(worktree)
    except ValueError as error:
        raise ValueError(f"--config path is outside the invoking repository: {raw}") from error
    return _repository_relative(candidate.as_posix(), label="--config")


def _side_config_coordinate(value: str | None, *, label: str) -> str | None:
    if value is None:
        return None
    return _repository_relative(value, label=label)


def _require_config_coordinate(value: str | None) -> str:
    if value is None:  # pragma: no cover - an override, shared path, or discovery supplies one.
        raise ValueError("no configuration path could be determined for a comparison side")
    return value


def _live_config_argument(
    query: argparse.Namespace, worktree: Path, after_override: str | None
) -> Path | None:
    """Resolve the live working-tree config argument for the After side."""
    if after_override is not None:
        return worktree / after_override
    raw = getattr(query, "config", None)
    return Path(raw) if raw is not None else None


def _revision_label(requested: str, commit: str) -> str:
    return f"{requested} · {commit[:_SYSTEMS_SHORT_ID_LENGTH]}"


def _document_paths(document: GraphDocument) -> tuple[str, ...]:
    return tuple(sorted({node.path for node in document.nodes if node.path}))


def _present_targets(analysis: Any) -> frozenset[str]:
    """Return the declared target coordinates that exist in one capture."""
    present: set[str] = set()
    root = analysis.snapshot.root
    for coordinate in analysis.normalized_targets:
        parts = tuple(part for part in coordinate.split("/") if part not in {"", "."})
        candidate = root.joinpath(*parts) if parts else root
        try:
            os.lstat(candidate)
        except OSError:
            continue
        present.add(coordinate)
    return frozenset(present)


def _require_pair_targets_materialized(old: Any, new: Any) -> None:
    """Reject a declared target that is absent on both captured sides."""
    before_present = _present_targets(old)
    after_present = _present_targets(new)
    declared = set(old.normalized_targets) | set(new.normalized_targets)
    absent = sorted(
        coordinate
        for coordinate in declared
        if coordinate not in before_present and coordinate not in after_present
    )
    if absent:
        raise ValueError(
            "configured target is absent on both comparison sides: " + ", ".join(absent)
        )


def _live_path(worktree: Path, coordinate: str) -> Path:
    parts = tuple(part for part in coordinate.split("/") if part not in {"", "."})
    candidate = worktree.joinpath(*parts) if parts else worktree
    return candidate.resolve()


def _git_metadata_directories(worktree: Path) -> set[Path]:
    """Resolve the repository marker plus both Git directory routes."""
    directories: set[Path] = set()
    with suppress(OSError):
        directories.add((worktree / ".git").resolve())
    completed = git.run_git(worktree, ("rev-parse", "--git-dir", "--git-common-dir"))
    if completed is None or completed.returncode != 0:
        return directories
    for line in completed.stdout.splitlines():
        value = line.strip()
        if not value:
            continue
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = worktree / candidate
        try:
            directories.add(candidate.resolve())
        except OSError:  # pragma: no cover - resolve is lexical for ordinary paths.
            continue
    return directories


def _protected_comparison_paths(
    worktree: Path, analyses: Sequence[Any], coordinates: Sequence[str]
) -> tuple[frozenset[Path], frozenset[Path]]:
    """Collect the live input paths that a report destination must not alias."""
    files: set[Path] = set()
    directories: set[Path] = set()
    for coordinate in coordinates:
        if coordinate:
            files.add(_live_path(worktree, coordinate))
    for analysis in analyses:
        files.add(_live_path(worktree, analysis.config_coordinate))
        graph = _live_path(worktree, analysis.normalized_graph)
        files.add(graph)
        files.add(graph.with_name(graph.name + ".sha256"))
        root = _live_path(worktree, analysis.normalized_root)
        for node in analysis.graph.document.nodes:
            if node.path:
                files.add((root / node.path).resolve())
        systems = _live_path(worktree, analysis.normalized_systems_dir)
        if systems != worktree.resolve():
            directories.add(systems)
        for declared in analysis.systems:
            definition = declared.definition_directory
            if definition is None:
                continue
            try:
                relative = Path(definition).relative_to(analysis.snapshot.root)
            except ValueError:  # pragma: no cover - loader provenance stays in capture.
                continue
            files.add((worktree / relative).resolve())
    directories.update(_git_metadata_directories(worktree))
    return frozenset(files), frozenset(directories)


def _is_within(path: Path, directory: Path) -> bool:
    return path == directory or directory in path.parents


def _refuse_protected_aliases(
    resolved: Path, protected_files: frozenset[Path], protected_directories: frozenset[Path]
) -> None:
    """Refuse a destination that aliases any acquired comparison input."""
    for directory in protected_directories:
        if _is_within(resolved, directory):
            raise ValueError(f"output path is inside a protected comparison input: {resolved}")
    if resolved in protected_files:
        raise ValueError(f"output path is also a comparison input: {resolved}")
    try:
        info = os.stat(resolved)
    except OSError:
        info = None
    if info is None:
        return
    for candidate in protected_files:
        try:
            candidate_info = os.stat(candidate)
        except OSError:
            continue
        if (info.st_dev, info.st_ino) == (candidate_info.st_dev, candidate_info.st_ino):
            raise ValueError(f"output hard-links a comparison input: {resolved}")


def _preflight_html_output(output: Path, *, force: bool) -> _HtmlOutputPlan:
    """Validate the report destination and record its preflight identity."""
    resolved = _preflight_output(output, (), force)
    parent = output.parent
    try:
        parent_info = os.stat(parent)
        parent_identity: tuple[int, int] | None = (parent_info.st_dev, parent_info.st_ino)
    except OSError:  # pragma: no cover - the preflight already proved the parent exists.
        parent_identity = None
    try:
        info = os.stat(resolved)
    except OSError:
        info = None
    return _HtmlOutputPlan(
        display=output,
        resolved=resolved,
        existed=info is not None,
        identity=(info.st_dev, info.st_ino) if info is not None else None,
        parent=parent,
        parent_identity=parent_identity,
    )


def _write_no_clobber(output: Path, content: bytes) -> None:
    """Publish complete bytes only when the destination is still unoccupied."""
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(temporary, 0o666 & ~umask)
        try:
            os.link(temporary, output)
        except FileExistsError as error:
            raise ValueError(
                f"output destination was created during comparison: {output}"
            ) from error
    finally:
        temporary.unlink(missing_ok=True)


def _publish_comparison_html(
    plan: _HtmlOutputPlan,
    content: bytes,
    *,
    protected_files: frozenset[Path],
    protected_directories: frozenset[Path],
) -> None:
    """Recheck the preflight identity, then publish atomically or not at all."""
    try:
        parent_info = os.stat(plan.parent)
    except OSError as error:
        raise ValueError(f"output parent directory is unavailable: {plan.display}") from error
    if (
        plan.parent_identity is not None
        and (parent_info.st_dev, parent_info.st_ino) != plan.parent_identity
    ):
        raise ValueError(f"output parent directory changed during comparison: {plan.display}")
    try:
        info = os.stat(plan.resolved)
    except FileNotFoundError:
        info = None
    except OSError as error:
        raise ValueError(
            f"could not inspect comparison output destination: {plan.display}"
        ) from error
    if plan.existed:
        if info is None or (info.st_dev, info.st_ino) != plan.identity:
            raise ValueError(f"output destination changed during comparison: {plan.display}")
    elif info is not None:
        raise ValueError(f"output destination was created during comparison: {plan.display}")
    _refuse_protected_aliases(plan.resolved, protected_files, protected_directories)
    if plan.existed:
        _write_atomically(plan.resolved, content)
    else:
        _write_no_clobber(plan.resolved, content)


def _run_graph_query(query: argparse.Namespace) -> int:
    """Answer one index-backed query, refreshing the graph when it has drifted."""
    graph = _load_and_refresh_graph(
        Path(query.graph), Path(query.root).resolve(), query.no_refresh, validate=query.validate
    )
    if query.name == "systems":
        snapshot = system_query.ReportingSnapshot.prepare(graph.document, query.systems)
        _report_absent_files(snapshot.systems, snapshot.index)
        overview_report = snapshot.all_systems_report(details=query.details)
        invocation = system_query.QueryInvocation(
            refreshed=graph.refreshed,
            stale=graph.drift.paths,
            source_diagnostics=len(graph.diagnostics) if graph.refreshed else None,
        )
        composed = system_query.compose_system_query(overview_report, invocation)
        output = render_systems_json(composed) if query.json else render_systems_text(composed)
        print(output, end="")
        return 1 if graph.diagnostics else 0
    handler = _GRAPH_QUERIES.get(query.name)
    if handler is None:  # pragma: no cover - argparse restricts the subcommand set.
        raise ValueError(f"unsupported query: {query.name}")
    index = GraphIndex.build(graph.document)
    # No symbol guard here: each query resolves its own name through
    # GraphIndex.resolve, whose SymbolResolutionError is a ValueError and so
    # reaches _query's handler as an exit-2 error message.  A membership test
    # duplicated here previously accepted duplicate labels that the queries
    # then answered with an empty result.
    if query.name in _SYSTEM_QUERIES:
        _report_absent_files((query.system,), index)
        snapshot = system_query.ReportingSnapshot.prepare(graph.document, query.systems)
        report = snapshot.report(query.name, query.system_name, details=query.details)
        invocation = system_query.QueryInvocation(
            refreshed=graph.refreshed,
            stale=graph.drift.paths,
            source_diagnostics=len(graph.diagnostics) if graph.refreshed else None,
        )
        composed = system_query.compose_system_query(report, invocation)
        summary = handler.render_text(report.results)
        output = (
            render_system_json(composed) if query.json else render_system_text(composed, summary)
        )
    else:
        records = handler.run(query, index, graph.drift)
        output = (
            render_json(
                query.name,
                records,
                refreshed=graph.refreshed,
                stale=graph.drift.paths,
            )
            if query.json
            else handler.render_text(records)
        )
    print(output, end="")
    return 1 if graph.diagnostics else 0


def _add_query_subparsers(
    query: argparse.ArgumentParser,
    *,
    config_located: bool = False,
    systems_mode: bool = False,
) -> None:
    """Register the query subcommands directly on the ``query`` subparser.

    Nesting these on the main parser (instead of parsing a captured
    ``argparse.REMAINDER`` blob in a second, throwaway parser) is what makes
    ``minotaur query <name> --help`` exit 0 and ``minotaur query --help``
    list the subcommands: argparse owns the whole invocation in one
    ``parse_args`` call, so its help and error handling behave the same way
    here as they do for ``analyze`` and ``visualize``.

    ``query diff`` keeps the strict config-free grammar when explicit OLD NEW
    positionals are present. When a config is located, its OLD/NEW positionals
    become optional and the committed mode registers ``--scope`` and
    ``--config``. The remaining subcommands consume config ``graph``/``root``,
    so when a config was located their declarations relax and ``--config`` is
    registered.
    """
    commands = query.add_subparsers(dest="name", required=True)
    callers_parser = commands.add_parser("callers", help="find callers of a qualified symbol")
    callers_parser.add_argument("symbol", metavar="QUALIFIED_NAME")
    definitions_parser = commands.add_parser("definitions", help="find definitions of a bare name")
    definitions_parser.add_argument("symbol", metavar="BARE_NAME")
    impact_parser = commands.add_parser("impact", help="find inbound calls and imports")
    impact_parser.add_argument("symbol", metavar="QUALIFIED_NAME")
    impact_parser.add_argument("--depth", type=int, help="maximum inbound traversal depth")
    unreferenced_parser = commands.add_parser(
        "unreferenced", help="find symbols without inbound calls or references"
    )
    unreferenced_parser.add_argument("paths", nargs="*", metavar="PATH")
    unreferenced_parser.add_argument("--exclude", action="append", default=[])
    unreferenced_parser.add_argument("--exclude-file", type=Path)
    unreferenced_parser.add_argument(
        "--exclude-pattern",
        action="append",
        default=[],
        metavar="REGEX",
        help="exclude symbols whose qualified name matches this regex (repeatable)",
    )
    unreferenced_parser.add_argument("--text-fallback", action="store_true")
    surface_parser = commands.add_parser(
        "surface", help="show a declared system's symbols reached from outside"
    )
    surface_parser.add_argument("system_name", metavar="SYSTEM_NAME")
    surface_parser.add_argument(
        "--details", action="store_true", help="include relationship evidence details"
    )
    consumers_parser = commands.add_parser(
        "consumers", help="list files outside a declared system that use it"
    )
    consumers_parser.add_argument("system_name", metavar="SYSTEM_NAME")
    consumers_parser.add_argument(
        "--details", action="store_true", help="include relationship evidence details"
    )
    system_deps_parser = commands.add_parser(
        "system-deps", help="list the targets a declared system depends on"
    )
    system_deps_parser.add_argument("system_name", metavar="SYSTEM_NAME")
    system_deps_parser.add_argument(
        "--details", action="store_true", help="include relationship evidence details"
    )
    systems_parser = commands.add_parser(
        "systems", help="show the inventory and coverage of all declared systems"
    )
    systems_parser.add_argument(
        "--details", action="store_true", help="include declared paths and connections"
    )
    if config_located or systems_mode:
        diff_description = (
            "Compare the current working tree with the committed graph at HEAD "
            "(or compare two explicit graph snapshots)."
        )
        diff_epilog = (
            "Modes: with no OLD and NEW, use the located project config and "
            "optionally --scope NAME; with OLD NEW, compare those explicit files "
            "without using project config. Exit status: 0 means structures are "
            "identical, 1 means structures differ, and 2 means the command "
            "could not complete; the caller decides the consequence."
        )
    else:
        diff_description = "Compare two explicit analyzed graph snapshots."
        diff_epilog = (
            "Exit status: 0 means structures are identical, 1 means structures "
            "differ, and 2 means the command could not complete; the caller "
            "decides the consequence."
        )
    diff_parser = commands.add_parser(
        "diff",
        help="compare analyzed graph snapshots",
        description=diff_description,
        epilog=diff_epilog,
    )
    diff_parser.add_argument(
        "old", nargs="?" if (config_located or systems_mode) else None, metavar="OLD"
    )
    diff_parser.add_argument(
        "new", nargs="?" if (config_located or systems_mode) else None, metavar="NEW"
    )
    diff_parser.add_argument("--json", action="store_true", help="emit stable JSON records")
    if config_located and not systems_mode:
        diff_parser.add_argument("--scope", metavar="NAME", help="compare one committed system")
        diff_parser.add_argument("--config", metavar="CONFIG", help="explicit project config file")
    if systems_mode:
        diff_parser.add_argument(
            "--systems", action="store_true", help="compare configured system snapshots"
        )
        diff_parser.add_argument("--system", metavar="NAME", help="limit changes to one system")
        diff_parser.add_argument(
            "--details", action="store_true", help="include change records and evidence"
        )
        diff_parser.add_argument("--config", metavar="CONFIG", help="explicit project config file")
        diff_parser.add_argument(
            "--before-config", metavar="PATH", help="config coordinate for the Before side"
        )
        diff_parser.add_argument(
            "--after-config", metavar="PATH", help="config coordinate for the After side"
        )
        diff_parser.add_argument(
            "--html", metavar="PATH", help="write the offline comparison report"
        )
        diff_parser.add_argument(
            "--force", action="store_true", help="replace an existing comparison report"
        )
    context_parser = commands.add_parser("context", help="show source context around a line")
    context_parser.add_argument("--site", required=True, metavar="PATH:LINE")
    context_parser.add_argument("--before", type=int, default=3)
    context_parser.add_argument("--after", type=int, default=3)
    for command in (
        callers_parser,
        definitions_parser,
        impact_parser,
        unreferenced_parser,
        surface_parser,
        consumers_parser,
        system_deps_parser,
        systems_parser,
        context_parser,
    ):
        command.add_argument(
            "--graph", required=not config_located, help="analyzed graph JSON file"
        )
        command.add_argument(
            "--root", required=not config_located, help="source root used for freshness checks"
        )
        command.add_argument(
            "--no-refresh", action="store_true", help="answer from the graph as-is"
        )
        command.add_argument("--json", action="store_true", help="emit stable JSON records")
        _add_validate_flag(command)
        if config_located:
            command.add_argument("--config", metavar="CONFIG", help="explicit project config file")
    _add_validate_flag(diff_parser)


def _query_source_paths(
    index: GraphIndex,
    root: Path,
    paths: Sequence[str],
    *,
    validate_current_paths: bool = True,
) -> tuple[str, ...]:
    """Filter graph files by optional root-relative query paths.

    Normal queries validate that each command path still exists and preserve
    directory selection semantics. A stale ``--no-refresh`` query instead
    filters the saved graph lexically: its command paths may be deleted or
    unreadable, but containment checks still reject paths outside ``root``.
    """
    graph_paths = {node.path for node in index.nodes.values() if node.path is not None}
    if not paths:
        return tuple(sorted(graph_paths))
    targets: list[tuple[Path, str]] = []
    for value in paths:
        target = Path(value)
        if not target.is_absolute():
            target = root / target
        resolved = target.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise ValueError(f"query path escapes root: {value}") from error
        if validate_current_paths and not resolved.exists():
            raise ValueError(f"query path does not exist: {value}")
        targets.append((resolved, resolved.relative_to(root).as_posix()))

    selected: set[str] = set()
    if validate_current_paths:
        for relative in graph_paths:
            candidate = root / relative
            if any(
                candidate == target or (target.is_dir() and target in candidate.parents)
                for target, _ in targets
            ):
                selected.add(relative)
    else:
        for relative in graph_paths:
            if any(
                target_relative == "."
                or relative == target_relative
                or relative.startswith(f"{target_relative}/")
                for _, target_relative in targets
            ):
                selected.add(relative)
    return tuple(sorted(selected))


def _target_selection(root: Path, targets: tuple[Path, ...]) -> tuple[str, ...]:
    """Normalize command targets exactly as document selection metadata."""
    return tuple(sorted({target.resolve().relative_to(root).as_posix() for target in targets}))


def _add_validate_flag(parser: argparse.ArgumentParser) -> None:
    """Add ``--validate`` to force full schema validation on graph load."""
    parser.add_argument(
        "--validate",
        action="store_true",
        help="force full schema validation even when the sidecar stamp matches",
    )


def _parser(config_located: bool = False, *, systems_mode: bool = False) -> argparse.ArgumentParser:
    """Build the CLI parser, toggling config-defaultable declarations (D-05).

    With ``config_located`` false the parser is today's strict grammar:
    ``analyze --root``/``--output``/``targets`` and the query subcommand
    ``--graph``/``--root`` remain required and no ``--config`` option exists,
    so no-config usage errors keep argparse's exact exit-``2`` text.  With a
    config located, only those declarations relax (argparse never receives a
    configuration value as a default); the resolver fills whatever the
    command line left out after parsing.  ``--config`` is registered per
    command on config-consuming commands and on the config-located committed
    ``query diff`` mode.
    """
    parser = argparse.ArgumentParser(prog="minotaur")
    commands = parser.add_subparsers(dest="command", required=True)
    analyze = commands.add_parser("analyze", help="analyze selected supported source files")
    analyze.add_argument("--root", required=not config_located, help="existing source root")
    analyze.add_argument(
        "--output", required=not config_located, help="destination graph JSON file"
    )
    if config_located:
        analyze.add_argument(
            "--scope",
            metavar="NAME",
            help="analyze one committed system into its system graph",
        )
    analyze.add_argument("--force", action="store_true", help="replace an existing output file")
    if config_located:
        analyze.add_argument(
            "targets",
            nargs="*",
            default=None,
            metavar="TARGET",
            help="source files or directories",
        )
        analyze.add_argument("--config", metavar="CONFIG", help="explicit project config file")
    else:
        analyze.add_argument(
            "targets", nargs="+", metavar="TARGET", help="source files or directories"
        )
    visualize = commands.add_parser(
        "visualize", help="render a portable interactive graph HTML file"
    )
    visualize.add_argument(
        "--input", required=not config_located, help="canonical Minotaur graph JSON file"
    )
    visualize.add_argument("--output", required=True, help="destination HTML file")
    visualize.add_argument("--source-root", help="optional source root for embedded excerpts")
    visualize.add_argument("--force", action="store_true", help="replace an existing output file")
    _add_validate_flag(visualize)
    if config_located:
        visualize.add_argument("--config", metavar="CONFIG", help="explicit project config file")
    query = commands.add_parser("query", help="query an analyzed graph")
    _add_query_subparsers(query, config_located=config_located, systems_mode=systems_mode)
    return parser


def _visualize(arguments: argparse.Namespace, located: Path | None) -> int:
    """Load a verified graph and write a self-contained visualizer atomically.

    Visualization deliberately reuses the graph-loading boundary instead of
    accepting convenient partial JSON. A polished interactive display lends
    input credibility, so it must never be the first consumer to relax the
    canonical graph contract.

    ``visualize`` shares the configuration owner with partial defaults (D-09):
    with a config located, ``--input`` defaults from config ``graph`` and
    ``--source-root`` from config ``root`` only when that root exists as a
    directory (otherwise excerpts stay disabled); ``--output`` remains
    required.  With no config the strict grammar requires ``--input`` and no
    resolution runs, because this command has no explicit root to source one
    from.
    """
    try:
        source_root: Path | None
        systems: tuple[System, ...] = ()
        if located is not None:
            resolved = resolve_config(
                Path.cwd(),
                config=located,
                explicit_graph=_as_path(arguments.input),
            )
            input_path = resolved.graph
            if arguments.source_root is None and resolved.root.is_dir():
                source_root = resolved.root
            else:
                source_root = _as_path(arguments.source_root)
            systems = load_systems(resolved.systems_dir)
        else:
            input_path = Path(arguments.input)
            source_root = _as_path(arguments.source_root)
        loaded = load_graph_file(input_path, validate=arguments.validate)
        # D-12: a freshness guard was considered for visualize but declined;
        # rendering need not have a source root and remains cheap to repeat.
        output = _preflight_output(Path(arguments.output), (input_path.resolve(),), arguments.force)
        # M-4: stamp only after the output preflight passes. Stamping before
        # this check meant `visualize --output existing.html` without
        # `--force` exited 2 while still creating `<input>.sha256` on disk —
        # a filesystem write from a command that was about to refuse to run.
        # `loaded.digest` is immutable, so deferring the stamp changes nothing
        # about what gets written to the sidecar.
        _stamp_if_validated(input_path, loaded)
        excerpts = prepare_excerpts(loaded.canonical, source_root)
        content = render_html(
            build_presentation(
                loaded.canonical,
                excerpts,
                systems=systems,
                document_nodes=loaded.document.nodes,
            )
        )
        _write_atomically(output, content)
    except (GraphLoadError, OSError, ValueError) as error:
        _error(str(error))
        return 2
    if len(content) > 10 * 1024 * 1024:
        print("minotaur: warning: embedded visualizer exceeds 10 MiB", file=sys.stderr)
    return 0


def _preflight_output(output: Path, files: tuple[Path, ...], force: bool) -> Path:
    """Validate the destination before any analysis work creates an artifact.

    Resolving the destination matters for the same reason targets are
    resolved: a symlink must not hide that the output aliases a source file.
    Refusing replacement by default also prevents a typo from silently
    destroying a previously generated graph.
    """
    if not output.parent.is_dir():
        raise ValueError(f"output parent directory does not exist: {output.parent}")
    resolved = output.resolve()
    if resolved in files:
        raise ValueError(f"output is also a selected source file: {output}")
    if output.exists() and output.is_dir():
        raise ValueError(f"output path is a directory: {output}")
    if output.exists() and not force:
        raise ValueError(f"output already exists (pass --force to replace it): {output}")
    return resolved


def _with_selection_extension(
    extensions: Mapping[str, Mapping[str, object]] | None,
    root: Path,
    targets: tuple[Path, ...],
) -> dict[str, dict[str, object]]:
    """Add the canonical CLI targets without changing other producer metadata."""
    existing = {name: dict(value) for name, value in (extensions or {}).items()}
    minotaur = dict(existing.get("minotaur", {}))
    minotaur["selection"] = sorted(
        {target.resolve().relative_to(root).as_posix() for target in targets}
    )
    existing["minotaur"] = minotaur
    return existing


def _dispatch(workspace: Workspace, files: tuple[Path, ...]) -> AnalysisResult:
    """Pass one language's selected files to its registered interpreter.

    Selection is extension-based, rather than a Python-specific CLI branch,
    so adding an interpreter does not require another user-facing command.
    We intentionally reject a mixed-language selection for now: combining
    independently produced graphs needs explicit rules for producer metadata,
    IDs, and relationship merging. Pretending that concatenation is safe would
    make the CLI's graph contract ambiguous.
    """
    grouped: dict[InterpreterRegistration, list[Path]] = defaultdict(list)
    registry = default_registry()
    for path in files:
        registration = registry.registration_for(path)
        if registration is None:  # Selection guarantees this.
            raise ValueError(f"no interpreter registered for {path}")
        grouped[registration].append(path)
    if not grouped:
        # An empty graph is a useful successful result: the selection was valid
        # but contained no files supported by this installation.  Returning a
        # normal GraphDocument keeps downstream consumers from special-casing
        # an absent output file.
        from minotaur.graph_model.document import GraphDocument
        from minotaur.graph_model.evidence import Producer
        from minotaur.graph_model.provenance import CoordinateEncoding

        return AnalysisResult(
            GraphDocument(
                coordinate_encoding=CoordinateEncoding.UTF_8,
                generated_by=Producer(name="minotaur"),
            )
        )
    if len(grouped) != 1:
        raise ValueError("selected files require unsupported multi-interpreter graph composition")
    registration, interpreter_files = next(iter(grouped.items()))
    return registration.analyze_files(workspace, tuple(interpreter_files))


def _stamp_if_validated(path: Path, loaded: LoadedGraph) -> None:
    """Write the sidecar stamp when *loaded* was fully validated.

    The helper writes ``loaded.digest`` — the digest of the exact bytes this
    process parsed — and never re-reads *path*.  A concurrent writer that
    replaces the file between the load and this call therefore cannot trick
    this process into stamping bytes it never validated (D-14, AR-03).

    Writing is best-effort: a read-only filesystem or a permission failure
    must not turn a successful query into a command error.
    """
    if not loaded.validated:
        return
    try:
        _write_atomically(stamp_path(path), f"{loaded.digest}\n".encode("ascii"))
    except OSError:
        return


def _write_atomically(output: Path, content: bytes) -> None:
    """Replace ``output`` only after a complete canonical document is durable.

    The temporary file is deliberately created beside the destination.  A
    same-directory ``os.replace`` is atomic on supported local filesystems,
    so a reader sees either the previous graph or the complete new graph,
    never a partially written JSON document.
    """
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # L-1: mkstemp creates the temporary file mode 0600, which the graph
        # and sidecar would otherwise inherit through os.replace. In a shared
        # checkout that leaves the file unreadable to anyone but the writer,
        # who then silently pays the full-validation path forever. Widen the
        # mode to whatever the process umask allows, same as a normal create.
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(temporary, 0o666 & ~umask)
        os.replace(temporary, output)
    except OSError:
        # If writing fails before replacement, remove only the file created by
        # this call.  The prior destination is left untouched by atomic replace.
        temporary.unlink(missing_ok=True)
        raise


def _format_diagnostic(diagnostic: Diagnostic) -> str:
    """Render stable, editor-friendly diagnostics without inventing locations."""
    if diagnostic.location is None:
        return f"{diagnostic.path}: {diagnostic.code.value}: {diagnostic.message}"
    start = diagnostic.location.range.start
    return (
        f"{diagnostic.path}:{start.line}:{start.character}: "
        f"{diagnostic.code.value}: {diagnostic.message}"
    )


def _error(message: str) -> None:
    """Keep command errors on stderr so stdout stays available to future piping."""
    print(f"minotaur: error: {message}", file=sys.stderr)
