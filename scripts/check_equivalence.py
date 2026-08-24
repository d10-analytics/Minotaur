#!/usr/bin/env python3
"""Compare two Minotaur source trees on the same graph workloads.

The harness deliberately does not import Minotaur itself.  Every operation is
performed by a child process with a side-specific ``PYTHONPATH`` so an
editable installation cannot accidentally make both sides execute the same
checkout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Side:
    name: str
    src: Path


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: str
    stderr: str


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-src", type=Path, required=True)
    parser.add_argument("--branch-src", type=Path, required=True)
    parser.add_argument("--root", type=Path, action="append")
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
    env = os.environ.copy()
    env["PYTHONPATH"] = str(src)
    return env


def _run(
    side: Side,
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float = 300,
) -> Completed:
    result = subprocess.run(
        [sys.executable, *args],
        cwd=cwd,
        env=_env(side.src),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
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


def _check_import(side: Side) -> str | None:
    result = _run(side, ["-c", "import minotaur; print(minotaur.__file__)"])
    if result.returncode:
        return f"{side.name} import failed (exit {result.returncode}): {result.stderr.strip()}"
    imported = Path(result.stdout.strip()).resolve()
    if not _under(imported, side.src):
        return f"minotaur imported from {imported}, outside {side.src}"
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


def _normalise_stderr(value: str, baseline_scratch: Path, branch_scratch: Path) -> str:
    return value.replace(str(baseline_scratch), "<baseline-scratch>").replace(
        str(branch_scratch), "<branch-scratch>"
    )


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
    baseline_scratch: Path,
    branch_scratch: Path,
) -> bool:
    left = (
        baseline.returncode,
        baseline.stdout,
        _normalise_stderr(baseline.stderr, baseline_scratch, branch_scratch),
    )
    right = (
        branch.returncode,
        branch.stdout,
        _normalise_stderr(branch.stderr, baseline_scratch, branch_scratch),
    )
    return _row(label, left, right)


def _load_queries(path: Path) -> list[dict[str, Any]]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, list) or not all(isinstance(item, dict) for item in loaded):
        raise ValueError("equivalence query file must contain a list of objects")
    return loaded


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
    names = [("human", [])]
    if variants.get("json"):
        names.append(("json", ["--json"]))
    if variants.get("no_refresh") and command != "diff":
        names.extend(
            (f"{name}+no-refresh", [*extra, "--no-refresh"]) for name, extra in list(names)
        )
    yield from names


def _compare_root(
    sides: tuple[Side, Side],
    root: Path,
    queries: list[dict[str, Any]],
    scratch: Path,
) -> bool:
    baseline, branch = sides
    baseline_dir = scratch / "baseline"
    branch_dir = scratch / "branch"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    branch_dir.mkdir(parents=True, exist_ok=True)
    baseline_graph = baseline_dir / "graph.json"
    branch_graph = branch_dir / "graph.json"
    baseline_analyze = _analyze(baseline, root, baseline_graph)
    branch_analyze = _analyze(branch, root, branch_graph)
    ok = _compare_processes("analyze", baseline_analyze, branch_analyze, baseline_dir, branch_dir)
    if baseline_analyze.returncode or branch_analyze.returncode:
        return False
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
    if not _compare_processes(
        "visualize", base_visualize, branch_visualize, baseline_dir, branch_dir
    ):
        ok = False
    if (
        base_visualize.returncode == 0
        and branch_visualize.returncode == 0
        and not _row(
            f"root={root} artifact=visualize HTML SHA-256", _sha(baseline_html), _sha(branch_html)
        )
    ):
        ok = False

    selection_target = "minotaur" if (root / "minotaur").is_dir() else "."
    baseline_selection = baseline_dir / "selection.json"
    branch_selection = branch_dir / "selection.json"
    for side, output in ((baseline, baseline_selection), (branch, branch_selection)):
        result = _analyze(side, root, output, selection_target)
        if result.returncode:
            ok = False
    for item in queries:
        command = str(item["command"])
        for variant_name, extra in _query_variants(item):
            args = _query_args(item, baseline_graph, root, baseline_selection)
            branch_args = _query_args(item, branch_graph, root, branch_selection)
            args.extend(extra)
            branch_args.extend(extra)
            left = _run(baseline, args, cwd=root)
            right = _run(branch, branch_args, cwd=root)
            if not _compare_processes(
                f"root={root} query={item.get('name', command)} variant={variant_name}",
                left,
                right,
                baseline_dir,
                branch_dir,
            ):
                ok = False

    drift_code = (
        "from pathlib import Path; "
        "from minotaur.graph_model.loading import load_graph_file; "
        "from minotaur.query.freshness import drift; "
        "d=drift(load_graph_file(Path(r'GRAPH')).document, Path(r'ROOT')); "
        "print((sorted(d.changed), sorted(d.missing), sorted(d.added)))"
    )
    for label, side in (("baseline", baseline), ("branch", branch)):
        command = drift_code.replace("GRAPH", str(baseline_graph)).replace("ROOT", str(root))
        result = _run(side, ["-c", command], cwd=root)
        if result.returncode:
            ok = False
            print(f"root={root} artifact=Drift {label}: subprocess failed: {result.stderr}")
        elif label == "baseline":
            baseline_drift = result.stdout.strip()
        else:
            if not _row(f"root={root} artifact=Drift", baseline_drift, result.stdout.strip()):
                ok = False
    return ok


def _copy_without_git(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, symlinks=True, ignore=shutil.ignore_patterns(".git"))


def _inject_serialize(src: Path) -> None:
    parsing = src / "minotaur" / "graph_model" / "_parsing.py"
    with parsing.open("a", encoding="utf-8") as stream:
        stream.write(
            "\n\n# Self-test-only output perturbation.\n"
            "_equivalence_original_serialize = _jcs_serialize\n"
            "def _jcs_serialize(value):\n"
            "    return _equivalence_original_serialize(value) + b'\\x00'\n"
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
        print(f"scenario: skipped {root}; no Python source file", file=sys.stderr)
        return None
    graph = destination.parent / f"{destination.name}.json"
    initial = _analyze(side, destination, graph)
    if initial.returncode:
        print(
            f"scenario: initial analyze failed for {destination}: {initial.stderr}", file=sys.stderr
        )
        return None
    return source, graph


def _scenario_query(side: Side, root: Path, graph: Path, no_refresh: bool) -> Completed:
    args = [
        "-m",
        "minotaur",
        "query",
        "definitions",
        "main",
        "--graph",
        str(graph),
        "--root",
        str(root),
    ]
    if no_refresh:
        args.append("--no-refresh")
    return _run(side, args, cwd=root)


def _run_scenarios(sides: tuple[Side, Side], roots: list[Path], scratch: Path) -> bool:
    ok = True
    for root_index, root in enumerate(roots):
        scenario_root = scratch / f"scenario-{root_index}"
        scenario_root.mkdir(parents=True, exist_ok=True)
        for letter in "abcdefg":
            copies = [
                _scenario_copy(root, scenario_root, f"{letter}-baseline"),
                _scenario_copy(root, scenario_root, f"{letter}-branch"),
            ]
            states = [
                _scenario_side(side, root, copy) for side, copy in zip(sides, copies, strict=True)
            ]
            if any(state is None for state in states):
                continue
            for source, _ in states:
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
            left = _scenario_query(sides[0], copies[0], states[0][1], letter == "b")
            right = _scenario_query(sides[1], copies[1], states[1][1], letter == "b")
            if not _compare_processes(
                f"scenario root={root} step={letter}", left, right, copies[0], copies[1]
            ):
                ok = False
            if left.returncode not in (0, 1) or right.returncode not in (0, 1):
                ok = False
            if left.returncode in (0, 1) and right.returncode in (0, 1):
                left_sha = _sha(states[0][1])
                right_sha = _sha(states[1][1])
                if not _row(
                    f"scenario root={root} step={letter} graph SHA-256", left_sha, right_sha
                ):
                    ok = False
            if letter in "fg" and (
                "minotaur: refreshed graph" in left.stderr
                or "minotaur: refreshed graph" in right.stderr
            ):
                print(f"scenario root={root} step={letter}: unexpected refresh", file=sys.stderr)
                ok = False
        # Keep the two copies needed for the final analyze decision explicit.
        for letter in ("a", "f"):
            copies = [
                _scenario_copy(root, scenario_root, f"h-{letter}-baseline"),
                _scenario_copy(root, scenario_root, f"h-{letter}-branch"),
            ]
            states = [
                _scenario_side(side, root, copy) for side, copy in zip(sides, copies, strict=True)
            ]
            if any(state is None for state in states):
                continue
            before_sha = [_sha(state[1]) for state in states]
            for (source, _), _copy in zip(states, copies, strict=True):
                if letter == "a":
                    source.write_bytes(source.read_bytes() + b"\n# equivalence edit\n")
                else:
                    os.utime(source, None)
            results = [
                _analyze(side, copy, state[1], force=False)
                for side, copy, state in zip(sides, copies, states, strict=True)
            ]
            if not _compare_processes(
                f"scenario root={root} step=h-{letter}",
                results[0],
                results[1],
                copies[0].parent,
                copies[1].parent,
            ):
                ok = False
            skip_message = "minotaur: graph is up to date, skipping analysis"
            if letter == "a":
                if any(skip_message in result.stderr for result in results):
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
                if any(skip_message not in result.stderr for result in results):
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
    roots: list[Path],
    queries: list[dict[str, Any]],
    scratch: Path,
    scenarios: bool,
) -> bool:
    ok = True
    for index, root in enumerate(roots):
        if not root.exists():
            print(f"equivalence: skipped missing root {root}", file=sys.stderr)
            continue
        if not _compare_root(sides, root, queries, scratch / f"root-{index}"):
            ok = False
    if scenarios:
        ok = _run_scenarios(sides, roots[:2], scratch / "scenarios") and ok
    return ok


def _normal_run(arguments: argparse.Namespace, *, scratch_branch: Path | None = None) -> int:
    baseline = Side("baseline", _resolve(arguments.baseline_src))
    branch = Side("branch", _resolve(arguments.branch_src))
    if not baseline.src.is_dir() or not branch.src.is_dir():
        return _fail("both source paths must be directories")
    if _guards((baseline, branch), scratch_branch=scratch_branch):
        return 1
    roots = (
        [_resolve(path) for path in arguments.root]
        if arguments.root
        else [
            baseline.src.parent / "examples/python-workflow",
            baseline.src,
        ]
    )
    try:
        queries = _load_queries(_resolve(arguments.queries))
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
            0 if _compare((baseline, branch), roots, queries, scratch, arguments.scenarios) else 1
        )
    finally:
        if arguments.scratch is None:
            shutil.rmtree(scratch, ignore_errors=True)


def _self_test(arguments: argparse.Namespace) -> int:
    branch_source = _resolve(arguments.branch_src)
    with tempfile.TemporaryDirectory(prefix="minotaur-equivalence-self-test-") as temporary:
        copied = Path(temporary) / "injected-src"
        _copy_without_git(branch_source, copied)
        _inject_serialize(copied)
        injected = argparse.Namespace(**vars(arguments))
        injected.branch_src = copied
        injected.scenarios = False
        failed = _normal_run(injected, scratch_branch=copied)
        if failed == 0:
            return _fail("self-test injection unexpectedly passed")
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
