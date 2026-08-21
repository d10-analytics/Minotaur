"""Command-line interface for local Minotaur source analysis."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from difflib import get_close_matches
from pathlib import Path
from typing import Any

from minotaur.graph_model.document import GraphDocument, SourceControl
from minotaur.graph_model.loading import GraphLoadError, load_graph_file
from minotaur.graph_model.serialization import serialize
from minotaur.graph_visualizer.html.render import render_html
from minotaur.graph_visualizer.presentation import build_presentation
from minotaur.graph_visualizer.source import prepare_excerpts
from minotaur.language_interpreter.contract import AnalysisResult, Diagnostic
from minotaur.language_interpreter.registry import InterpreterRegistration, default_registry
from minotaur.language_interpreter.selection import SelectionError, select_sources
from minotaur.language_interpreter.workspace import Workspace
from minotaur.query import context as context_query
from minotaur.query import diff as diff_query
from minotaur.query import impact as impact_query
from minotaur.query import symbols as symbols_query
from minotaur.query import unreferenced as unreferenced_query
from minotaur.query.freshness import Drift, drift, recorded_selection
from minotaur.query.index import GraphIndex
from minotaur.query.render import QueryRecord, render_json


def main(argv: Sequence[str] | None = None) -> int:
    """Run the console entry point and return its process exit status.

    The command deliberately completes selection and output checks before it
    asks an interpreter to read source.  That ordering gives users a useful
    safety promise: a bad path, unsupported file, or unsafe output request
    cannot leave behind a graph that looks like a successful analysis.
    """
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "visualize":
        return _visualize(arguments)
    if arguments.command == "query":
        return _query(arguments)
    if arguments.command != "analyze":  # pragma: no cover - argparse enforces this.
        parser.error("a command is required")

    root = Path(arguments.root).resolve()
    targets = tuple(Path(target) for target in arguments.targets)
    try:
        # Validate the current workspace and targets before considering a
        # clean-output skip. A deleted root or explicitly selected file must
        # remain a command error even when an old graph happens to load.
        select_sources(root, targets, default_registry())
        # D-11 deliberately runs before output preflight. A graph that Minotaur
        # can load is ours to refresh after drift, while an unrelated existing
        # file still follows the normal refusal path below.
        refresh_force = arguments.force
        if Path(arguments.output).exists() and not arguments.force:
            try:
                existing = load_graph_file(Path(arguments.output))
            except (GraphLoadError, OSError, ValueError):
                existing = None
            if existing is not None:
                current_selection = _target_selection(root, targets)
                current_source_control = _git_source_control(root)
                if (
                    drift(existing.document, root).is_clean
                    and recorded_selection(existing.document) == current_selection
                    and current_source_control == existing.document.source_control
                ):
                    print("minotaur: graph is up to date, skipping analysis", file=sys.stderr)
                    return 0
                # A valid graph with drift was previously produced by this
                # command, so replacement is safe after the freshness check.
                refresh_force = True
        result = _analyze_selection(root, targets, Path(arguments.output), refresh_force)
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
    recorded_targets = targets if metadata_targets is None else metadata_targets
    output = _preflight_output(output_path, selection.files, force)
    result = _dispatch(workspace, selection.files)
    source_control = _git_source_control(workspace.root)
    if source_control is not None:
        result = replace(
            result,
            document=replace(result.document, source_control=source_control),
        )
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
    _write_atomically(output, serialize(result.document))
    return result


def _git_source_control(root: Path) -> SourceControl | None:
    """Return the Git snapshot metadata for ``root``, when it is available."""
    work_tree = _run_git(root, ("rev-parse", "--is-inside-work-tree"))
    if work_tree is None:
        return None
    if work_tree.returncode != 0 or work_tree.stdout.strip() != "true":
        return None

    commit_result = _run_git(root, ("rev-parse", "HEAD"))
    branch_result = _run_git(root, ("branch", "--show-current"))
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


def _run_git(root: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[str] | None:
    """Run one Git probe, treating unavailable or failed commands as unknown."""
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            capture_output=True,
            check=False,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _load_and_refresh_graph(
    graph_path: Path, root: Path, no_refresh: bool
) -> tuple[GraphDocument, tuple[Diagnostic, ...], Drift]:
    """Load a query graph, refreshing its recorded selection when stale.

    Callers use the returned drift's ``paths``/``is_clean`` value for warnings
    and exit-code selection.
    """
    loaded = load_graph_file(graph_path)
    observed = drift(loaded.document, root)
    if observed.is_clean:
        return loaded.document, (), observed
    if no_refresh:
        for path in observed.paths:
            print(f"minotaur: stale: {path}", file=sys.stderr)
        return loaded.document, (), observed

    recorded = recorded_selection(loaded.document)
    if not recorded:
        raise ValueError("graph has no recorded source selection; cannot refresh")
    all_targets = tuple(root / target for target in recorded)
    existing_targets = tuple(target for target in all_targets if target.exists())
    result = _analyze_selection(
        root,
        existing_targets,
        graph_path,
        True,
        metadata_targets=all_targets,
    )
    return result.document, result.diagnostics, observed


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
    requires_known_symbol: bool = False


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
        # A stale no-refresh query is intentionally graph-only. The saved
        # graph remains queryable even when selected files have been removed
        # or can no longer be read, so text fallback must not inspect the
        # current workspace in this mode.
        text_fallback=query.text_fallback and not stale_graph,
    )


def _run_diff(query: argparse.Namespace) -> str:
    old = load_graph_file(Path(query.old)).document
    new = load_graph_file(Path(query.new)).document
    result = diff_query.diff(old, new)
    return diff_query.render_json(result) if query.json else diff_query.render_text(result)


def _run_context(query: argparse.Namespace) -> str:
    document = load_graph_file(Path(query.graph)).document
    record = context_query.context(
        document,
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
        requires_known_symbol=True,
    ),
    "definitions": _GraphQuery(
        run=_run_definitions,
        render_text=symbols_query.render_definitions_text,
    ),
    "impact": _GraphQuery(
        run=_run_impact,
        render_text=impact_query.render_text,
        requires_known_symbol=True,
    ),
    "unreferenced": _GraphQuery(
        run=_run_unreferenced,
        render_text=unreferenced_query.render_text,
    ),
}

# Snapshot queries intentionally do not call _load_and_refresh_graph: diff
# compares two graphs as they were recorded, and context needs the current
# excerpt while its per-file hash check makes a changed source explicit in the
# result.  They also keep their own JSON envelopes, which are not record lists.
_SNAPSHOT_QUERIES: Mapping[str, Callable[[argparse.Namespace], str]] = {
    "diff": _run_diff,
    "context": _run_context,
}


def _query(arguments: argparse.Namespace) -> int:
    """Dispatch a fixed query against one graph snapshot.

    The query subcommands are registered directly on the main parser (see
    ``_parser``), so ``arguments`` here is already a fully parsed query
    invocation — there is no nested ``parse_args`` call left to raise
    ``SystemExit``. That matters for exit codes: argparse itself now handles
    ``--help`` (exit 0) and usage errors (exit 2) exactly as it does for
    ``analyze`` and ``visualize``, instead of this function collapsing both
    outcomes to a single hard-coded exit 2.
    """
    try:
        snapshot = _SNAPSHOT_QUERIES.get(arguments.name)
        if snapshot is not None:
            print(snapshot(arguments), end="")
            return 0
        return _run_graph_query(arguments)
    except (GraphLoadError, OSError, ValueError) as error:
        _error(str(error))
        return 2


def _run_graph_query(query: argparse.Namespace) -> int:
    """Answer one index-backed query, refreshing the graph when it has drifted."""
    handler = _GRAPH_QUERIES.get(query.name)
    if handler is None:  # pragma: no cover - argparse restricts the subcommand set.
        raise ValueError(f"unsupported query: {query.name}")
    document, diagnostics, observed = _load_and_refresh_graph(
        Path(query.graph), Path(query.root).resolve(), query.no_refresh
    )
    index = GraphIndex.build(document)
    if handler.requires_known_symbol and query.symbol not in index.symbols_by_label:
        suggestions = get_close_matches(query.symbol, index.labels(), n=5, cutoff=0.0)
        _error(_unknown_symbol_message(query.symbol, suggestions))
        return 2
    records = handler.run(query, index, observed)
    output = render_json(query.name, records) if query.json else handler.render_text(records)
    print(output, end="")
    return 1 if diagnostics else 0


def _add_query_subparsers(query: argparse.ArgumentParser) -> None:
    """Register the query subcommands directly on the ``query`` subparser.

    Nesting these on the main parser (instead of parsing a captured
    ``argparse.REMAINDER`` blob in a second, throwaway parser) is what makes
    ``minotaur query <name> --help`` exit 0 and ``minotaur query --help``
    list the subcommands: argparse owns the whole invocation in one
    ``parse_args`` call, so its help and error handling behave the same way
    here as they do for ``analyze`` and ``visualize``.
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
    unreferenced_parser.add_argument("--text-fallback", action="store_true")
    diff_parser = commands.add_parser("diff", help="compare two analyzed graph snapshots")
    diff_parser.add_argument("old", metavar="OLD")
    diff_parser.add_argument("new", metavar="NEW")
    diff_parser.add_argument("--json", action="store_true", help="emit stable JSON records")
    context_parser = commands.add_parser("context", help="show source context around a line")
    context_parser.add_argument("--site", required=True, metavar="PATH:LINE")
    context_parser.add_argument("--before", type=int, default=3)
    context_parser.add_argument("--after", type=int, default=3)
    for command in (
        callers_parser,
        definitions_parser,
        impact_parser,
        unreferenced_parser,
        context_parser,
    ):
        command.add_argument("--graph", required=True, help="analyzed graph JSON file")
        command.add_argument("--root", required=True, help="source root used for freshness checks")
        command.add_argument(
            "--no-refresh", action="store_true", help="answer from the graph as-is"
        )
        command.add_argument("--json", action="store_true", help="emit stable JSON records")


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
                candidate == target or target.is_dir() and target in candidate.parents
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


def _unknown_symbol_message(symbol: str, suggestions: Sequence[str]) -> str:
    if not suggestions:
        return f"unknown symbol: {symbol}"
    return f"unknown symbol: {symbol}; nearest labels: {', '.join(suggestions)}"


def _target_selection(root: Path, targets: tuple[Path, ...]) -> tuple[str, ...]:
    """Normalize command targets exactly as document selection metadata."""
    return tuple(sorted({target.resolve().relative_to(root).as_posix() for target in targets}))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minotaur")
    commands = parser.add_subparsers(dest="command", required=True)
    analyze = commands.add_parser("analyze", help="analyze selected supported source files")
    analyze.add_argument("--root", required=True, help="existing source root")
    analyze.add_argument("--output", required=True, help="destination graph JSON file")
    analyze.add_argument("--force", action="store_true", help="replace an existing output file")
    analyze.add_argument("targets", nargs="+", metavar="TARGET", help="source files or directories")
    visualize = commands.add_parser(
        "visualize", help="render a portable interactive graph HTML file"
    )
    visualize.add_argument("--input", required=True, help="canonical Minotaur graph JSON file")
    visualize.add_argument("--output", required=True, help="destination HTML file")
    visualize.add_argument("--source-root", help="optional source root for embedded excerpts")
    visualize.add_argument("--force", action="store_true", help="replace an existing output file")
    query = commands.add_parser("query", help="query an analyzed graph")
    _add_query_subparsers(query)
    return parser


def _visualize(arguments: argparse.Namespace) -> int:
    """Load a verified graph and write a self-contained visualizer atomically.

    Visualization deliberately reuses the graph-loading boundary instead of
    accepting convenient partial JSON. A polished interactive display lends
    input credibility, so it must never be the first consumer to relax the
    canonical graph contract.
    """
    input_path = Path(arguments.input)
    try:
        loaded = load_graph_file(input_path)
        # D-12: a freshness guard was considered for visualize but declined;
        # rendering need not have a source root and remains cheap to repeat.
        output = _preflight_output(Path(arguments.output), (input_path.resolve(),), arguments.force)
        source_root = Path(arguments.source_root) if arguments.source_root is not None else None
        excerpts = prepare_excerpts(loaded.canonical, source_root)
        content = render_html(build_presentation(loaded.canonical, excerpts))
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
                generated_by=Producer(name="minotaur-python"),
            )
        )
    if len(grouped) != 1:
        raise ValueError("selected files require unsupported multi-interpreter graph composition")
    registration, interpreter_files = next(iter(grouped.items()))
    return registration.analyze_files(workspace, tuple(interpreter_files))


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
