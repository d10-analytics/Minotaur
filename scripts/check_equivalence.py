#!/usr/bin/env python3
"""Compare two Minotaur source trees on the same graph workloads.

The harness deliberately does not import Minotaur itself.  Every operation is
performed by a child process with a side-specific ``PYTHONPATH`` so an
editable installation cannot accidentally make both sides execute the same
checkout.

Roots (specification ``D-06``): (1) ``tests/fixtures/equivalence_root`` beside
this script -- a committed Python substrate small enough to read and rich
enough that every query class has a real hit; (2) the baseline worktree's own
``src``; (3) an optional large checkout supplied with ``--optional-root``,
the only root that may be missing without failing the run.

Queries come from a per-root JSON file keyed by the root directory's name, so
a root is never compared with a query list written for a different tree.
When supplied, ``--workload KEY`` names the workload for each optional root in
the same position; provide either no keys or exactly one key per optional root
when a checkout's directory name differs from its committed key (the Onyx
checkout, say).  A run fails when a root's queries do not actually produce
output: an empty comparison is not a passing comparison, and every root that
is present must be compared -- a present root that cannot be is a failure,
never a skip.  The final ``totals:`` line reports how many roots were compared
against how many were requested so the number itself is the merge gate.

Both interpreters must be CPython 3.11 or newer: import isolation rests on
``PYTHONSAFEPATH``, which older interpreters ignore, and with the working
directory set to a source root the branch side would then silently import the
baseline package instead of its own.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "equivalence_root"
SCRATCH_PLACEHOLDER = "<scratch>"


@dataclass(frozen=True)
class Side:
    name: str
    src: Path


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False

    @property
    def stdout_text(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", "replace")


@dataclass(frozen=True)
class RootQueries:
    """The committed workload for one root."""

    selection: str
    queries: list[dict[str, Any]]


@dataclass
class Tally:
    """What a root comparison actually did, so vacuity is visible."""

    comparisons: int = 0
    queries_expected: int = 0
    queries_answered: int = 0
    roots: list[str] = field(default_factory=list)

    def absorb(self, other: Tally) -> None:
        self.comparisons += other.comparisons
        self.queries_expected += other.queries_expected
        self.queries_answered += other.queries_answered
        self.roots.extend(other.roots)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-src", type=Path, required=True)
    parser.add_argument("--branch-src", type=Path, required=True)
    parser.add_argument(
        "--root",
        type=Path,
        action="append",
        help="required root; a missing one fails the run",
    )
    parser.add_argument(
        "--optional-root",
        type=Path,
        action="append",
        help="root that may be absent (D-06 root (3)); skipped with a loud message",
    )
    parser.add_argument(
        "--workload",
        action="append",
        metavar="KEY",
        help=(
            "workload key for each optional root in the same position; provide"
            " either none or exactly one per optional root when a checkout"
            " directory is not named after its committed workload"
        ),
    )
    parser.add_argument(
        "--queries", type=Path, default=Path(__file__).with_name("equivalence_queries.json")
    )
    parser.add_argument("--scratch", type=Path)
    parser.add_argument("--scenarios", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    return parser


def _resolve(path: Path) -> Path:
    return path.expanduser().resolve()


def _env(src: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        # A coverage-instrumented parent would make every child write a
        # ``.coverage.*`` file into its working directory -- the fixture root
        # and both checkouts -- and flip the provenance guard on the next run.
        if not key.startswith("COVERAGE_")
    }
    env["PYTHONPATH"] = str(src)
    env["PYTHONSAFEPATH"] = "1"
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run(
    side: Side,
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float = 300,
) -> Completed:
    """Run one side's subprocess and capture raw bytes.

    Output is compared as bytes: ``text=True`` would decode with the ambient
    locale and translate newlines, so a real byte difference could be
    normalised away by the instrument that exists to detect it.
    """

    try:
        result = subprocess.run(
            [sys.executable, *args],
            cwd=cwd,
            env=_env(side.src),
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as expired:
        return Completed(
            returncode=-1,
            stdout=expired.stdout or b"",
            stderr=f"<timed out after {timeout:g}s>".encode(),
            timed_out=True,
        )
    return Completed(result.returncode, result.stdout, result.stderr)


def _fail(message: str) -> int:
    print(f"equivalence: {message}", file=sys.stderr)
    return 1


def _under(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError:
        return False
    return True


IMPORT_PROBE = (
    "import sys; import minotaur; print(sys.version_info[0], sys.version_info[1]);"
    " print(minotaur.__file__)"
)


def _check_import(side: Side, cwd: Path | None = None) -> str | None:
    """Prove *side* imports its own package from *cwd*.

    The comparison subprocesses run with the working directory set to the root
    under test, and root (2) is the baseline's own ``src``: exactly the
    directory a non-isolated interpreter would put first on ``sys.path``.  The
    probe therefore runs in that same configuration rather than in the
    harness's own directory.
    """
    result = _run(side, ["-c", IMPORT_PROBE], cwd=cwd)
    where = f" from {cwd}" if cwd is not None else ""
    if result.returncode:
        return (
            f"{side.name} import failed{where} (exit {result.returncode}):"
            f" {result.stderr_text.strip()}"
        )
    version_line, separator, imported_line = result.stdout_text.strip().partition("\n")
    if not separator:
        return f"{side.name} import probe returned no import location{where}"
    try:
        major, minor = (int(value) for value in version_line.split())
    except ValueError:
        return f"{side.name} import probe returned an invalid Python version: {version_line!r}"
    if (major, minor) < (3, 11):
        return (
            f"{side.name} interpreter is Python {major}.{minor}; import isolation needs"
            " PYTHONSAFEPATH, which only CPython 3.11+ honours"
        )
    imported = Path(imported_line.strip()).resolve()
    if not _under(imported, side.src):
        return f"minotaur imported{where} from {imported}, outside {side.src}"
    return None


def _git_info(side: Side) -> tuple[Path, str, bool] | str:
    checkout = side.src.parent.resolve()
    top = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if top.returncode or _resolve(Path(top.stdout.strip())) != checkout:
        return f"{side.name} source is not a repository checkout: {checkout}"
    head = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    status = subprocess.run(
        ["git", "-C", str(checkout), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if head.returncode:
        return f"{side.name} could not read HEAD for {checkout}"
    clean = status.returncode == 0 and not status.stdout
    return checkout, head.stdout.strip(), clean


def _guards(sides: tuple[Side, Side], *, scratch_branch: Path | None = None) -> int:
    baseline, branch = sides
    if baseline.src == branch.src:
        return _fail(f"identical trees: {baseline.src}")
    for side in sides:
        error = _check_import(side)
        if error:
            return _fail(f"{side.name}: {error}")

    infos: list[tuple[Path, str, bool] | None] = []
    for side in sides:
        if scratch_branch is not None and side.src == scratch_branch:
            print(f"self-test mode: provenance guard skipped for {side.src}", file=sys.stderr)
            infos.append(None)
            continue
        info = _git_info(side)
        if isinstance(info, str):
            return _fail(info)
        checkout, head, clean = info
        print(f"{side.name}: git -C {checkout} rev-parse HEAD = {head}", file=sys.stderr)
        print(f"{side.name}: git status --porcelain empty = {clean}", file=sys.stderr)
        infos.append(info)
    if infos[0] is not None and infos[1] is not None:
        first, second = infos
        assert first is not None and second is not None
        if first[1] == second[1] and first[2] and second[2]:
            return _fail(
                f"same clean HEAD for baseline {sides[0].src} and branch {sides[1].src}: {first[1]}"
            )
    return 0


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalise_stderr(value: bytes, *scratches: Path) -> bytes:
    """Replace every scratch root with one shared placeholder.

    Side-specific placeholders would make any diagnostic that echoes the graph
    path differ by construction -- a false DIFFERENT from the normaliser
    itself.
    """

    placeholder = SCRATCH_PLACEHOLDER.encode()
    for scratch in scratches:
        value = value.replace(str(scratch).encode(), placeholder)
    return value


def _row(
    label: str,
    baseline: Any,
    branch: Any,
    *,
    display: str | None = None,
) -> bool:
    if baseline == branch:
        print(f"{label}: IDENTICAL {display if display is not None else baseline}")
        return True
    print(f"{label}: DIFFERENT\nbaseline={baseline!r}\nbranch={branch!r}")
    return False


def _compare_processes(
    label: str,
    baseline: Completed,
    branch: Completed,
    *scratches: Path,
) -> bool:
    if baseline.timed_out or branch.timed_out:
        print(
            f"{label}: FAILED timeout"
            f" baseline={baseline.timed_out} branch={branch.timed_out}"
            f" ({baseline.stderr_text.strip() or branch.stderr_text.strip()})"
        )
        return False
    left = (
        baseline.returncode,
        baseline.stdout,
        _normalise_stderr(baseline.stderr, *scratches),
    )
    right = (
        branch.returncode,
        branch.stdout,
        _normalise_stderr(branch.stderr, *scratches),
    )
    return _row(label, left, right)


EMPTY_QUERY_OUTPUTS = {
    "callers": b"no callers\n",
    "definitions": b"no definitions\n",
    "unreferenced": b"no unreferenced symbols\n",
    "diff": b"no changes\n",
}


def _load_queries(path: Path) -> dict[str, RootQueries]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or not loaded:
        raise ValueError("equivalence query file must map a root name to its workload")
    workloads: dict[str, RootQueries] = {}
    for key, value in loaded.items():
        if not isinstance(value, dict):
            raise ValueError(f"workload for root {key!r} must be an object")
        selection = value.get("selection")
        queries = value.get("queries")
        if not isinstance(selection, str) or not selection:
            raise ValueError(f"workload for root {key!r} needs a 'selection' target")
        if (
            not isinstance(queries, list)
            or not queries
            or not all(isinstance(item, dict) for item in queries)
        ):
            raise ValueError(f"workload for root {key!r} needs a non-empty list of query objects")
        for item in queries:
            expect = item.get("expect", "ok")
            if expect not in ("ok", "empty", "error"):
                raise ValueError(f"query {item.get('name')!r} has unknown expect {expect!r}")
            if expect == "empty" and item.get("command") not in EMPTY_QUERY_OUTPUTS:
                raise ValueError(
                    f"query {item.get('name')!r} expects empty output from an unsupported command"
                )
        workloads[key] = RootQueries(selection=selection, queries=queries)
    return workloads


def _analyze(
    side: Side, root: Path, graph: Path, target: str = ".", *, force: bool = True
) -> Completed:
    args = ["-m", "minotaur", "analyze", "--root", str(root), "--output", str(graph)]
    if force:
        args.append("--force")
    args.append(target)
    return _run(side, args, cwd=root)


def _query_args(item: dict[str, Any], graph: Path, root: Path, selection: Path) -> list[str]:
    command = str(item["command"])
    args = [
        str(value).replace("{graph}", str(graph)).replace("{selection}", str(selection))
        for value in item.get("args", [])
    ]
    if command == "diff":
        return ["-m", "minotaur", "query", command, *args]
    return ["-m", "minotaur", "query", command, *args, "--graph", str(graph), "--root", str(root)]


def _query_variants(item: dict[str, Any]) -> Iterable[tuple[str, list[str]]]:
    command = str(item["command"])
    variants = item.get("variants", {})
    if not isinstance(variants, dict):
        raise ValueError(f"variants for {command} must be an object")
    names: list[tuple[str, list[str]]] = [("human", [])]
    if variants.get("json"):
        names.append(("json", ["--json"]))
    if variants.get("no_refresh") and command != "diff":
        names.extend(
            (f"{name}+no-refresh", [*extra, "--no-refresh"]) for name, extra in list(names)
        )
    yield from names


def _answered(result: Completed) -> bool:
    return (
        not result.timed_out
        and result.returncode == 0
        and bool(result.stdout.strip())
        and result.stdout not in EMPTY_QUERY_OUTPUTS.values()
    )


def _empty(result: Completed, command: str) -> bool:
    return (
        not result.timed_out
        and result.returncode == 0
        and result.stdout == EMPTY_QUERY_OUTPUTS[command]
    )


def _errored(result: Completed) -> bool:
    return not result.timed_out and result.returncode == 2


def _compare_queries(
    sides: tuple[Side, Side],
    root: Path,
    workload: RootQueries,
    graphs: tuple[Path, Path],
    selections: tuple[Path, Path],
    scratches: tuple[Path, Path],
    tally: Tally,
) -> bool:
    """Compare every query, and require each one to have really answered.

    Byte identity between two runs that both printed ``unknown symbol`` or
    ``no definitions`` is not evidence of anything.  The harness insists that
    every answering entry exits 0 with meaningful stdout on both sides, every
    empty-result entry prints its command's exact human literal, and every
    control entry really is the error path it claims to be.
    """

    baseline, branch = sides
    ok = True
    for item in workload.queries:
        command = str(item["command"])
        name = str(item.get("name", command))
        expect = str(item.get("expect", "ok"))
        tally.queries_expected += 1
        satisfied = True
        for variant_name, extra in _query_variants(item):
            args = [*_query_args(item, graphs[0], root, selections[0]), *extra]
            branch_args = [*_query_args(item, graphs[1], root, selections[1]), *extra]
            left = _run(baseline, args, cwd=root)
            right = _run(branch, branch_args, cwd=root)
            tally.comparisons += 1
            if not _compare_processes(
                f"root={root} query={name} variant={variant_name}", left, right, *scratches
            ):
                ok = False
            probe = (
                _answered
                if expect == "ok"
                else (lambda result, command=command: _empty(result, command))
                if expect == "empty"
                else _errored
            )
            for label, result in (("baseline", left), ("branch", right)):
                if not probe(result):
                    satisfied = False
                    print(
                        f"root={root} query={name} variant={variant_name} {label}:"
                        f" VACUOUS expected={expect} exit={result.returncode}"
                        f" stdout_bytes={len(result.stdout)}",
                        file=sys.stderr,
                    )
        if satisfied and expect == "ok":
            tally.queries_answered += 1
        if not satisfied:
            ok = False
    return ok


def _compare_root(
    sides: tuple[Side, Side],
    root: Path,
    workload: RootQueries,
    scratch: Path,
) -> tuple[bool, Tally]:
    baseline, branch = sides
    tally = Tally(roots=[f"{root}"])
    baseline_dir = scratch / "baseline"
    branch_dir = scratch / "branch"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    branch_dir.mkdir(parents=True, exist_ok=True)
    scratches = (baseline_dir, branch_dir)
    baseline_graph = baseline_dir / "graph.json"
    branch_graph = branch_dir / "graph.json"
    baseline_analyze = _analyze(baseline, root, baseline_graph)
    branch_analyze = _analyze(branch, root, branch_graph)
    tally.comparisons += 1
    ok = _compare_processes(f"root={root} analyze", baseline_analyze, branch_analyze, *scratches)
    if baseline_analyze.returncode or branch_analyze.returncode:
        print(f"root={root} analyze: FAILED not comparable")
        return False, tally
    tally.comparisons += 1
    if not _row(
        f"root={root} artifact=analyze graph SHA-256",
        _sha(baseline_graph),
        _sha(branch_graph),
    ):
        ok = False

    baseline_html = baseline_dir / "graph.html"
    branch_html = branch_dir / "graph.html"
    base_visualize = _run(
        baseline,
        [
            "-m",
            "minotaur",
            "visualize",
            "--input",
            str(baseline_graph),
            "--output",
            str(baseline_html),
            "--source-root",
            str(root),
            "--force",
        ],
        cwd=root,
    )
    branch_visualize = _run(
        branch,
        [
            "-m",
            "minotaur",
            "visualize",
            "--input",
            str(branch_graph),
            "--output",
            str(branch_html),
            "--source-root",
            str(root),
            "--force",
        ],
        cwd=root,
    )
    tally.comparisons += 1
    if not _compare_processes(
        f"root={root} visualize", base_visualize, branch_visualize, *scratches
    ):
        ok = False
    if base_visualize.returncode or branch_visualize.returncode:
        print(f"root={root} visualize: FAILED not comparable")
        return False, tally
    tally.comparisons += 1
    if not _row(
        f"root={root} artifact=visualize HTML SHA-256", _sha(baseline_html), _sha(branch_html)
    ):
        ok = False

    baseline_selection = baseline_dir / "selection.json"
    branch_selection = branch_dir / "selection.json"
    for side, output in ((baseline, baseline_selection), (branch, branch_selection)):
        result = _analyze(side, root, output, workload.selection)
        if result.returncode:
            print(
                f"root={root}: selection analyze of {workload.selection} failed for {side.name}:"
                f" {result.stderr_text.strip()}",
                file=sys.stderr,
            )
            ok = False
    if not _compare_queries(
        sides,
        root,
        workload,
        (baseline_graph, branch_graph),
        (baseline_selection, branch_selection),
        scratches,
        tally,
    ):
        ok = False

    if not _compare_drift(sides, root, baseline_graph, tally):
        ok = False
    return ok, tally


# Paths travel as ``argv`` so a scratch directory containing a quote, a
# trailing backslash, or the literal text of a placeholder cannot corrupt the
# snippet.
DRIFT_CODE = (
    "import sys; from pathlib import Path; "
    "from minotaur.graph_model.loading import load_graph_file; "
    "from minotaur.query.freshness import drift; "
    "d=drift(load_graph_file(Path(sys.argv[1])).document, Path(sys.argv[2])); "
    "print((sorted(d.changed), sorted(d.missing), sorted(d.added)))"
)


def _compare_drift(sides: tuple[Side, Side], root: Path, graph: Path, tally: Tally) -> bool:
    """Compare the ``Drift`` value each tree computes from the same graph.

    A side whose subprocess failed has no value to compare, so the row is a
    named failure rather than a comparison against whatever the other side
    happened to produce.
    """

    ok = True
    values: dict[str, str | None] = {}
    for label, side in (("baseline", sides[0]), ("branch", sides[1])):
        result = _run(side, ["-c", DRIFT_CODE, str(graph), str(root)], cwd=root)
        if result.returncode or result.timed_out:
            ok = False
            values[label] = None
            print(f"root={root} artifact=Drift {label}: subprocess failed: {result.stderr_text}")
        else:
            values[label] = result.stdout_text.strip()
    baseline_value, branch_value = values["baseline"], values["branch"]
    if baseline_value is None or branch_value is None:
        print(f"root={root} artifact=Drift: FAILED not comparable")
        return False
    tally.comparisons += 1
    if not _row(f"root={root} artifact=Drift", baseline_value, branch_value):
        ok = False
    return ok


def _copy_without_git(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, symlinks=True, ignore=shutil.ignore_patterns(".git"))


def _inject_serialize(src: Path) -> None:
    """Perturb the graph bytes: one extra byte out of the JCS encoder."""

    parsing = src / "minotaur" / "graph_model" / "_parsing.py"
    with parsing.open("a", encoding="utf-8") as stream:
        stream.write(
            "\n\n# Self-test-only output perturbation.\n"
            "_equivalence_original_serialize = _jcs_serialize\n"
            "def _jcs_serialize(value):\n"
            "    return _equivalence_original_serialize(value) + b'\\x00'\n"
        )


def _inject_visualize(src: Path) -> None:
    """Perturb the visualize HTML only, leaving the graph bytes untouched."""

    render = src / "minotaur" / "graph_visualizer" / "html" / "render.py"
    with render.open("a", encoding="utf-8") as stream:
        stream.write(
            "\n\n# Self-test-only output perturbation.\n"
            "_equivalence_original_render_html = render_html\n"
            "def render_html(presentation):\n"
            "    return _equivalence_original_render_html(presentation)"
            " + b'<!-- equivalence -->'\n"
        )


def _inject_query(src: Path) -> None:
    """Perturb query stdout only, leaving graph and HTML bytes untouched."""

    render = src / "minotaur" / "query" / "render.py"
    with render.open("a", encoding="utf-8") as stream:
        stream.write(
            "\n\n# Self-test-only output perturbation.\n"
            "_equivalence_original_dump_json = dump_json\n"
            "def dump_json(payload):\n"
            "    return _equivalence_original_dump_json(payload) + '\\n'\n"
        )


def _scenario_copy(root: Path, parent: Path, suffix: str) -> Path:
    destination = parent / suffix
    _copy_without_git(root, destination)
    return destination


def _first_python(root: Path) -> Path | None:
    return next(iter(sorted(root.rglob("*.py"))), None)


def _scenario_side(side: Side, root: Path, destination: Path) -> tuple[Path, Path] | None:
    source = _first_python(destination)
    if source is None:
        raise RuntimeError(f"scenario: copy has no Python source file for {root}")
    graph = destination.parent / f"{destination.name}.json"
    initial = _analyze(side, destination, graph)
    if initial.returncode:
        print(
            f"scenario: initial analyze failed for {destination}: {initial.stderr_text}",
            file=sys.stderr,
        )
        return None
    return source, graph


def _scenario_query(
    side: Side,
    root: Path,
    graph: Path,
    no_refresh: bool,
    *,
    symbol: str,
    validate: bool = False,
) -> Completed:
    args = [
        "-m",
        "minotaur",
        "query",
        "definitions",
        symbol,
        "--graph",
        str(graph),
        "--root",
        str(root),
    ]
    if no_refresh:
        args.append("--no-refresh")
    if validate:
        args.append("--validate")
    return _run(side, args, cwd=root)


SCENARIO_STEPS = "abcdefgijk"
SIDECAR_STEPS = "ijk"


def _apply_sidecar_step(letter: str, graph: Path) -> None:
    """Move the trusted-load stamp out from under the query (``R-08``)."""

    stamp = graph.with_name(graph.name + ".sha256")
    if letter == "i":
        stamp.unlink(missing_ok=True)
    elif letter == "j":
        stamp.write_text(f"{'0' * 64}  {graph.name}\n", encoding="utf-8")


def _sidecar_bytes(graph: Path) -> bytes | None:
    stamp = graph.with_name(graph.name + ".sha256")
    return stamp.read_bytes() if stamp.is_file() else None


def _expected_sidecar(graph: Path) -> bytes:
    return f"{_sha(graph)}\n".encode("ascii")


def _check_sidecar_setup(letter: str, root: Path, states: Sequence[tuple[Path, Path]]) -> bool:
    """Prove that a sidecar scenario actually reached its intended load state."""

    ok = True
    for side_name, (_, graph) in zip(("baseline", "branch"), states, strict=True):
        actual = _sidecar_bytes(graph)
        expected = _expected_sidecar(graph)
        invalid = (
            actual is None
            if letter == "i"
            else actual != expected
            if letter == "j"
            else actual == expected
        )
        if invalid:
            continue
        print(
            f"scenario root={root} step={letter} sidecar setup {side_name}: FAILED"
            f" expected={letter!r} actual={actual!r}",
            file=sys.stderr,
        )
        ok = False
    return ok


def _check_sidecar_stamps(letter: str, root: Path, states: Sequence[tuple[Path, Path]]) -> bool:
    """Require the query to leave both scenario sidecars truthful and comparable."""

    actual = [_sidecar_bytes(graph) for _, graph in states]
    ok = _row(f"scenario root={root} step={letter} sidecar bytes", actual[0], actual[1])
    for side_name, value, (_, graph) in zip(("baseline", "branch"), actual, states, strict=True):
        expected = _expected_sidecar(graph)
        if value == expected:
            continue
        print(
            f"scenario root={root} step={letter} sidecar {side_name}: FAILED"
            f" expected={expected!r} actual={value!r}",
            file=sys.stderr,
        )
        ok = False
    return ok


def _scenario_symbol(workload: RootQueries) -> str:
    """The symbol the scenario query must answer: the root's own ``definitions`` query."""

    for item in workload.queries:
        if item.get("command") == "definitions" and item.get("expect", "ok") == "ok":
            args = item.get("args", [])
            if args:
                return str(args[0])
    raise ValueError("workload has no answering 'definitions' query for scenario mode")


def _run_scenarios(
    sides: tuple[Side, Side], roots: list[tuple[Path, RootQueries]], scratch: Path
) -> bool:
    ok = True
    for root_index, (root, workload) in enumerate(roots):
        symbol = _scenario_symbol(workload)
        # Per root: a step that failed to prepare for this root must not fall
        # back on the previous root's retained copies.
        retained: dict[str, tuple[list[Path], list[tuple[Path, Path]]]] = {}
        scenario_root = scratch / f"scenario-{root_index}"
        scenario_root.mkdir(parents=True, exist_ok=True)
        if _first_python(root) is None:
            print(
                f"scenario: root {root} has no Python source to mutate;"
                " scenario mode would be vacuous",
                file=sys.stderr,
            )
            ok = False
            continue
        for letter in SCENARIO_STEPS:
            copies = [
                _scenario_copy(root, scenario_root, f"{letter}-baseline"),
                _scenario_copy(root, scenario_root, f"{letter}-branch"),
            ]
            prepared = [
                _scenario_side(side, root, copy) for side, copy in zip(sides, copies, strict=True)
            ]
            if any(state is None for state in prepared):
                print(f"scenario: failed to prepare step {letter} for {root}", file=sys.stderr)
                ok = False
                continue
            states = [state for state in prepared if state is not None]
            before_sha = [_sha(state[1]) for state in states]
            for source, graph in states:
                original = source.read_bytes()
                if letter in "ab":
                    source.write_bytes(original + b"\n# equivalence edit\n")
                elif letter == "c":
                    (source.parent / "__equivalence_added__.py").write_text(
                        "added = True\n", encoding="utf-8"
                    )
                elif letter == "d":
                    source.unlink()
                elif letter == "e":
                    source.rename(source.with_name(source.stem + "_renamed.py"))
                elif letter == "f":
                    os.utime(source, None)
                elif letter == "g":
                    source.write_bytes(original + b"\n# temporary\n")
                    source.write_bytes(original)
                elif letter in SIDECAR_STEPS:
                    _apply_sidecar_step(letter, graph)
            if letter in SIDECAR_STEPS and not _check_sidecar_setup(letter, root, states):
                ok = False
            left = _scenario_query(
                sides[0],
                copies[0],
                states[0][1],
                letter == "b",
                symbol=symbol,
                validate=letter == "k",
            )
            right = _scenario_query(
                sides[1],
                copies[1],
                states[1][1],
                letter == "b",
                symbol=symbol,
                validate=letter == "k",
            )
            if not _compare_processes(
                f"scenario root={root} step={letter}", left, right, copies[0], copies[1]
            ):
                ok = False
            if left.returncode not in (0, 1) or right.returncode not in (0, 1):
                ok = False
            if letter in SIDECAR_STEPS and not _check_sidecar_stamps(letter, root, states):
                ok = False
            # Every step must really answer on both sides: two identical
            # ``no definitions`` outputs would compare IDENTICAL while proving
            # nothing about the freshness sequence under test.  Step (d)
            # deletes the source that may define the symbol, so it is the one
            # step judged only on exit code and byte identity.
            if letter != "d" and not (_answered(left) and _answered(right)):
                print(
                    f"scenario root={root} step={letter}: query produced no answer"
                    f" (baseline exit {left.returncode}, branch exit {right.returncode})",
                    file=sys.stderr,
                )
                ok = False
            if left.returncode in (0, 1) and right.returncode in (0, 1):
                left_sha = _sha(states[0][1])
                right_sha = _sha(states[1][1])
                if not _row(
                    f"scenario root={root} step={letter} graph SHA-256", left_sha, right_sha
                ):
                    ok = False
                if letter in "fg":
                    if left_sha != before_sha[0] or right_sha != before_sha[1]:
                        print(
                            f"scenario root={root} step={letter}: graph changed unexpectedly",
                            file=sys.stderr,
                        )
                        ok = False
                    _row(
                        f"scenario root={root} step={letter} graph SHA-256 before/after",
                        (before_sha[0], left_sha),
                        (before_sha[1], right_sha),
                    )
            if letter in "fg" and (
                "minotaur: refreshed graph" in left.stderr_text
                or "minotaur: refreshed graph" in right.stderr_text
            ):
                print(f"scenario root={root} step={letter}: unexpected refresh", file=sys.stderr)
                ok = False
            if letter in ("a", "f"):
                retained[letter] = (copies, states)
        # Keep the two copies needed for the final analyze decision explicit.
        for letter in ("a", "f"):
            kept = retained.get(letter)
            if kept is None:
                print(
                    f"scenario root={root} step=h-{letter}: step {letter} left no copy to reuse",
                    file=sys.stderr,
                )
                ok = False
                continue
            copies, states = kept
            before_sha = [_sha(state[1]) for state in states]
            for (source, _), _copy in zip(states, copies, strict=True):
                if letter == "a":
                    # Step (a)'s query refreshes once; this follow-up edit
                    # keeps the retained copy stale for the rewrite proof.
                    source.write_bytes(source.read_bytes() + b"\n# equivalence edit\n")
                else:
                    os.utime(source, None)
            results = [
                _analyze(side, copy, state[1], force=False)
                for side, copy, state in zip(sides, copies, states, strict=True)
            ]
            if not _compare_processes(
                f"scenario root={root} step=h-{letter} copies={letter}",
                results[0],
                results[1],
                copies[0],
                copies[1],
            ):
                ok = False
            skip_message = "minotaur: graph is up to date, skipping analysis"
            if letter == "a":
                if any(skip_message in result.stderr_text for result in results):
                    print(
                        f"scenario root={root} step=h-{letter}: unexpected clean skip",
                        file=sys.stderr,
                    )
                    ok = False
                if any(
                    _sha(state[1]) == original_sha
                    for state, original_sha in zip(states, before_sha, strict=True)
                ):
                    print(
                        f"scenario root={root} step=h-{letter}: graph was not rewritten",
                        file=sys.stderr,
                    )
                    ok = False
            else:
                if any(skip_message not in result.stderr_text for result in results):
                    print(
                        f"scenario root={root} step=h-{letter}: missing clean-skip message",
                        file=sys.stderr,
                    )
                    ok = False
                if any(
                    _sha(state[1]) != original_sha
                    for state, original_sha in zip(states, before_sha, strict=True)
                ):
                    print(
                        f"scenario root={root} step=h-{letter}: clean graph changed",
                        file=sys.stderr,
                    )
                    ok = False
    return ok


def _compare(
    sides: tuple[Side, Side],
    roots: Sequence[tuple[Path, bool, str | None]],
    workloads: dict[str, RootQueries],
    scratch: Path,
    scenarios: bool,
) -> bool:
    ok = True
    total = Tally()
    compared: list[tuple[Path, RootQueries]] = []
    present = 0
    for index, (root, optional, key) in enumerate(roots):
        if not root.exists():
            if optional:
                print(f"equivalence: SKIPPED optional root {root} (absent)", file=sys.stderr)
                continue
            _fail(f"required root does not exist: {root}")
            ok = False
            continue
        present += 1
        workload_key = key or root.name
        workload = workloads.get(workload_key)
        if workload is None:
            _fail(
                f"no query workload for root {root} (key {workload_key!r});"
                f" known keys: {sorted(workloads)} -- pass --workload KEY for a checkout"
                " whose directory is not named after its workload"
            )
            ok = False
            continue
        root_ok, tally = _compare_root(sides, root, workload, scratch / f"root-{index}")
        total.absorb(tally)
        compared.append((root, workload))
        print(
            f"root={root} summary: comparisons={tally.comparisons}"
            f" queries_answered={tally.queries_answered}/{tally.queries_expected}"
        )
        if not root_ok:
            ok = False
    if not total.comparisons:
        _fail("no artifact comparisons were performed")
        ok = False
    if len(compared) != present:
        _fail(f"only {len(compared)} of {present} present roots were compared")
        ok = False
    print(
        f"totals: roots_compared={len(compared)} roots_present={present}"
        f" roots_requested={len(roots)} comparisons={total.comparisons}"
    )
    if scenarios:
        ok = _run_scenarios(sides, compared[:2], scratch / "scenarios") and ok
    return ok


def _root_list(
    arguments: argparse.Namespace, baseline: Side
) -> list[tuple[Path, bool, str | None]]:
    required = (
        [_resolve(path) for path in arguments.root]
        if arguments.root
        else [FIXTURE_ROOT, baseline.src]
    )
    optional = [_resolve(path) for path in arguments.optional_root or []]
    keys: list[str | None] = list(arguments.workload or [])
    if keys and len(keys) != len(optional):
        raise ValueError(
            f"{len(keys)} --workload keys for {len(optional)} optional roots;"
            " provide either none or exactly one per optional root"
        )
    keys.extend([None] * (len(optional) - len(keys)))
    return [(path, False, None) for path in required] + [
        (path, True, key) for path, key in zip(optional, keys, strict=True)
    ]


def _normal_run(arguments: argparse.Namespace, *, scratch_branch: Path | None = None) -> int:
    baseline = Side("baseline", _resolve(arguments.baseline_src))
    branch = Side("branch", _resolve(arguments.branch_src))
    if not baseline.src.is_dir() or not branch.src.is_dir():
        return _fail("both source paths must be directories")
    if _guards((baseline, branch), scratch_branch=scratch_branch):
        return 1
    try:
        roots = _root_list(arguments, baseline)
    except ValueError as error:
        return _fail(str(error))
    # The isolation proof must run where the comparisons run: with the working
    # directory set to each present root.
    for root, _optional, _key in roots:
        if not root.is_dir():
            continue
        for side in (baseline, branch):
            problem = _check_import(side, cwd=root)
            if problem:
                return _fail(f"{side.name}: {problem}")
    try:
        workloads = _load_queries(_resolve(arguments.queries))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return _fail(f"could not load queries: {error}")
    scratch = (
        _resolve(arguments.scratch)
        if arguments.scratch
        else Path(tempfile.mkdtemp(prefix="minotaur-equivalence-"))
    )
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        return (
            0 if _compare((baseline, branch), roots, workloads, scratch, arguments.scenarios) else 1
        )
    finally:
        if arguments.scratch is None:
            shutil.rmtree(scratch, ignore_errors=True)


INJECTIONS: tuple[tuple[str, Any, str], ...] = (
    ("serialize", _inject_serialize, "artifact=analyze graph SHA-256: DIFFERENT"),
    ("visualize", _inject_visualize, "artifact=visualize HTML SHA-256: DIFFERENT"),
    ("query", _inject_query, "query="),
)


def _injected_run(
    arguments: argparse.Namespace, branch_source: Path, temporary: Path, index: int
) -> str | None:
    """Run one injection and return a message when it was not caught properly."""

    name, inject, marker = INJECTIONS[index]
    copied = temporary / f"injected-src-{name}"
    _copy_without_git(branch_source, copied)
    inject(copied)
    injected = argparse.Namespace(**vars(arguments))
    injected.branch_src = copied
    injected.scenarios = False
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        failed = _normal_run(injected, scratch_branch=copied)
    output = captured.getvalue()
    print(output, end="")
    if failed == 0:
        return f"self-test injection {name} unexpectedly passed"
    differing = [line for line in output.splitlines() if line.endswith(": DIFFERENT")]
    if not any(marker in line for line in differing):
        return (
            f"self-test injection {name} exited nonzero without the {marker!r} row"
            " -- the injection no longer perturbs what the harness compares"
        )
    return None


def _self_test(arguments: argparse.Namespace) -> int:
    """Prove the harness reports each artifact class it claims to compare.

    A nonzero exit is not enough evidence: an injection that stopped applying
    (a renamed helper, say) would break the import guard and still exit
    nonzero.  Each injection must produce the specific differing row for the
    artifact class it perturbs.
    """

    branch_source = _resolve(arguments.branch_src)
    with tempfile.TemporaryDirectory(prefix="minotaur-equivalence-self-test-") as temporary:
        for index, (name, _inject, _marker) in enumerate(INJECTIONS):
            failure = _injected_run(arguments, branch_source, Path(temporary), index)
            if failure:
                return _fail(failure)
            print(f"self-test: injection {name} detected")
        clean_copy = Path(temporary) / "clean-src"
        _copy_without_git(branch_source, clean_copy)
        clean = argparse.Namespace(**vars(arguments))
        clean.branch_src = clean_copy
        clean.scenarios = False
        if _normal_run(clean, scratch_branch=clean_copy):
            return _fail("self-test clean comparison failed")
    print("self-test: PASS")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    return _self_test(arguments) if arguments.self_test else _normal_run(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
