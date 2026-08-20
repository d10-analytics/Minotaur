"""Command-line interface for local Minotaur source analysis."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from difflib import get_close_matches
from pathlib import Path

from minotaur.graph_model.document import GraphDocument
from minotaur.graph_model.loading import GraphLoadError, load_graph_file
from minotaur.graph_model.serialization import serialize
from minotaur.graph_visualizer.html.render import render_html
from minotaur.graph_visualizer.presentation import build_presentation
from minotaur.graph_visualizer.source import prepare_excerpts
from minotaur.language_interpreter.contract import AnalysisResult, Diagnostic
from minotaur.language_interpreter.registry import InterpreterRegistration, default_registry
from minotaur.language_interpreter.selection import SelectionError, select_sources
from minotaur.language_interpreter.workspace import Workspace
from minotaur.query.freshness import Drift, drift, recorded_selection
from minotaur.query.index import GraphIndex
from minotaur.query.render import QueryRecord, render_json, render_text
from minotaur.query.symbols import callers, definitions


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
        return _query_skeleton(arguments)
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
                if (
                    drift(existing.document, root).is_clean
                    and recorded_selection(existing.document) == current_selection
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


def _query_skeleton(arguments: argparse.Namespace) -> int:
    """Dispatch a fixed query against one graph snapshot."""
    try:
        query = _query_parser().parse_args(arguments.query_args)
    except SystemExit:
        return 2
    try:
        document, diagnostics, _ = _load_and_refresh_graph(
            Path(query.graph), Path(query.root).resolve(), query.no_refresh
        )
        index = GraphIndex.build(document)
        records: Sequence[QueryRecord]
        if query.name == "callers":
            if query.symbol not in index.symbols_by_label:
                suggestions = get_close_matches(query.symbol, index.labels(), n=5, cutoff=0.0)
                _error(_unknown_symbol_message(query.symbol, suggestions))
                return 2
            records = callers(index, query.symbol)
        else:
            records = definitions(index, query.symbol)
        output = (
            render_json(query.name, records) if query.json else render_text(query.name, records)
        )
        print(output, end="")
        return 1 if diagnostics else 0
    except (GraphLoadError, OSError, ValueError) as error:
        _error(str(error))
        return 2


def _query_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minotaur query")
    commands = parser.add_subparsers(dest="name", required=True)
    callers_parser = commands.add_parser("callers", help="find callers of a qualified symbol")
    callers_parser.add_argument("symbol", metavar="QUALIFIED_NAME")
    definitions_parser = commands.add_parser("definitions", help="find definitions of a bare name")
    definitions_parser.add_argument("symbol", metavar="BARE_NAME")
    for command in (callers_parser, definitions_parser):
        command.add_argument("--graph", required=True, help="analyzed graph JSON file")
        command.add_argument("--root", required=True, help="source root used for freshness checks")
        command.add_argument(
            "--no-refresh", action="store_true", help="answer from the graph as-is"
        )
        command.add_argument("--json", action="store_true", help="emit stable JSON records")
    return parser


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
    query.add_argument("query_args", nargs=argparse.REMAINDER)
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
