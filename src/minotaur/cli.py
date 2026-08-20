"""Command-line interface for local Minotaur source analysis."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from minotaur.graph_model.loading import GraphLoadError, load_graph_file
from minotaur.graph_model.serialization import serialize
from minotaur.graph_visualizer.html.render import render_html
from minotaur.graph_visualizer.presentation import build_presentation
from minotaur.graph_visualizer.source import prepare_excerpts
from minotaur.language_interpreter.contract import AnalysisResult, Diagnostic
from minotaur.language_interpreter.registry import InterpreterRegistration, default_registry
from minotaur.language_interpreter.selection import SelectionError, select_sources
from minotaur.language_interpreter.workspace import Workspace


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
    if arguments.command != "analyze":  # pragma: no cover - argparse enforces this.
        parser.error("a command is required")

    try:
        workspace, selection = select_sources(
            Path(arguments.root),
            tuple(Path(target) for target in arguments.targets),
            default_registry(),
        )
        output = _preflight_output(Path(arguments.output), selection.files, arguments.force)
        result = _dispatch(workspace, selection.files)
        _write_atomically(output, serialize(result.document))
    except (OSError, SelectionError, ValueError) as error:
        _error(str(error))
        return 2

    for diagnostic in result.diagnostics:
        # Diagnostics describe source problems, not command misuse.  The
        # partial graph has already been written so tools can still consume
        # facts from readable files; stderr remains the human-facing summary.
        print(_format_diagnostic(diagnostic), file=sys.stderr)
    return 1 if result.diagnostics else 0


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
