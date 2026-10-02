"""Analyzer output fingerprints and public freshness behavior across versions."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from minotaur import cli, language_interpreter
from minotaur.graph_model.evidence import Producer
from minotaur.graph_model.loading import load_graph_file, stamp_path
from minotaur.graph_model.serialization import serialize
from minotaur.language_interpreter import registry
from minotaur.query.freshness import AnalyzerChange
from minotaur.query.system import QueryInvocation

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLES = {
    "python": FIXTURES / "equivalence_root",
    "javascript": FIXTURES / "analyzer_semantics/javascript",
    "sql": FIXTURES / "analyzer_semantics/sql",
    "empty": FIXTURES / "analyzer_semantics/empty",
}
OLD = "ffffffffffffffff"


def _sample_documents(root: Path):
    documents = []
    for name, sample in SAMPLES.items():
        copied = root / name
        shutil.copytree(sample, copied)
        _, _, result = cli._produce_selection(copied, (copied,))
        assert not result.errors, result.diagnostics
        documents.append(result.document)
    return documents


def _fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for document in _sample_documents(root):
        document = replace(
            document,
            generated_by=replace(document.generated_by, version=None),
            source_control=None,
        )
        digest.update(serialize(document))
    return digest.hexdigest()[:16]


def test_analyzer_version_matches_output_fingerprint(tmp_path: Path) -> None:
    fresh = _fingerprint(tmp_path)
    assert re.fullmatch(r"[0-9a-f]{16}", language_interpreter.ANALYZER_SEMANTICS_VERSION)
    assert language_interpreter.ANALYZER_SEMANTICS_VERSION == fresh, f"Fresh fingerprint: {fresh}"


def test_fingerprint_covers_every_registered_interpreter_once(tmp_path: Path) -> None:
    documents = _sample_documents(tmp_path)
    expected = Counter({item.namespace for item in registry.default_registry().registrations})
    assert Counter(doc.generated_by.name for doc in documents[:-1]) == expected
    assert documents[-1].generated_by.name == "minotaur"
    assert documents[-1].nodes == ()


@pytest.mark.parametrize("language", ["python", "javascript", "sql"])
def test_each_interpreter_output_contributes_to_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, language: str
) -> None:
    original = getattr(registry, f"analyze_{language}_files")

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        first, *rest = result.document.nodes
        return replace(
            result,
            document=replace(
                result.document, nodes=(replace(first, label=first.label + "!"), *rest)
            ),
        )

    monkeypatch.setattr(registry, f"analyze_{language}_files", changed)
    assert _fingerprint(tmp_path) != language_interpreter.ANALYZER_SEMANTICS_VERSION


def _analyze(root: Path, graph: Path, *targets: Path) -> int:
    return cli.main(
        [
            "analyze",
            "--root",
            str(root),
            "--output",
            str(graph),
            *(str(target) for target in (targets or (root,))),
        ]
    )


def _project(root: Path) -> Path:
    root.mkdir()
    (root / "lib.py").write_text("def target():\n    return 1\n\ndef mentioned():\n    pass\n")
    (root / "app.py").write_text(
        "from lib import target\n# mentioned is documented only in text\n"
        "def one():\n    return target()\n"
        "def two():\n    return target()\n"
        "def three():\n    return target()\n"
    )
    (root / ".minotaur.toml").write_text(
        '[minotaur]\nschema_version = 1\nroot = "."\ngraph = "graph.json"\ntargets = ["."]\n'
    )
    directory = root / "docs/systems/lib"
    directory.mkdir(parents=True)
    (directory / "system.toml").write_text(
        'schema_version = 1\nname = "library"\nfiles = ["lib.py"]\n'
    )
    return root / "graph.json"


def _old_graph(root: Path, graph: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = cli._dispatch

    def old_dispatch(*args, **kwargs):
        result = original(*args, **kwargs)
        return replace(
            result,
            document=replace(
                result.document,
                relationships=tuple(
                    rel for rel in result.document.relationships if rel.kind != "calls"
                ),
            ),
        )

    with monkeypatch.context() as context:
        context.setattr(language_interpreter, "ANALYZER_SEMANTICS_VERSION", OLD)
        context.setattr(cli, "_dispatch", old_dispatch)
        assert _analyze(root, graph) == 0
    assert json.loads(graph.read_bytes())["generated_by"]["version"] == OLD


def _remove_version(graph: Path, *, whole_producer: bool = False) -> None:
    payload = json.loads(graph.read_bytes())
    if whole_producer:
        payload.pop("generated_by")
    else:
        payload["generated_by"].pop("version")
    graph.write_text(json.dumps(payload))


def _notice(old: str | None = OLD, *, refresh: bool = True) -> str:
    action = "refreshing" if refresh else "not refreshing"
    return (
        f"minotaur: analyzer version changed ({old or 'none'} -> "
        f"{language_interpreter.ANALYZER_SEMANTICS_VERSION}), {action}\n"
    )


def _query(root: Path, graph: Path, *args: str) -> int:
    return cli.main(["query", *args, "--root", str(root), "--graph", str(graph)])


@pytest.mark.parametrize("language", SAMPLES)
def test_public_analyze_stamps_every_language_and_empty_selection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], language: str
) -> None:
    root = tmp_path / "source"
    shutil.copytree(SAMPLES[language], root)
    graph = tmp_path / "graph.json"
    assert _analyze(root, graph) == 0
    payload = json.loads(graph.read_bytes())
    assert payload["generated_by"] == {
        "name": "minotaur" if language == "empty" else f"minotaur-{language}",
        "version": language_interpreter.ANALYZER_SEMANTICS_VERSION,
    }
    assert all(
        "version" not in evidence["producer"]
        for relationship in payload["relationships"]
        for evidence in relationship["evidence"]
    )
    capsys.readouterr()
    assert _analyze(root, graph) == 0
    assert capsys.readouterr().err == "minotaur: graph is up to date, skipping analysis\n"


@pytest.mark.parametrize("missing", [False, True])
def test_analyze_refreshes_old_version_then_skips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    missing: bool,
) -> None:
    root = tmp_path / "source"
    graph = _project(root)
    _old_graph(root, graph, monkeypatch)
    if missing:
        _remove_version(graph)
    capsys.readouterr()
    assert _analyze(root, graph) == 0
    assert capsys.readouterr().err == _notice(None if missing else OLD)
    assert (
        json.loads(graph.read_bytes())["generated_by"]["version"]
        == language_interpreter.ANALYZER_SEMANTICS_VERSION
    )
    assert _analyze(root, graph) == 0
    assert capsys.readouterr().err == "minotaur: graph is up to date, skipping analysis\n"


def test_analyze_notice_precedes_path_work_but_follows_output_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "source"
    graph = _project(root)
    _old_graph(root, graph, monkeypatch)
    (root / "app.py").write_text((root / "app.py").read_text() + "# edited\n")
    capsys.readouterr()
    before = graph.read_bytes()
    assert _analyze(root, graph, root / "app.py") == 2
    assert capsys.readouterr().err == (
        "minotaur: error: output graph was produced for a different selection or tree "
        f"(pass --force to replace it): {graph}\n"
    )
    assert graph.read_bytes() == before
    assert _analyze(root, graph) == 0
    assert capsys.readouterr().err == _notice()


@pytest.mark.parametrize("no_refresh", [False, True])
def test_query_naturally_changes_answer_only_when_refreshing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_refresh: bool,
) -> None:
    root = tmp_path / "source"
    graph = _project(root)
    _old_graph(root, graph, monkeypatch)
    capsys.readouterr()
    before = graph.read_bytes()
    args = ("callers", "lib.target", *(["--no-refresh"] if no_refresh else []))
    assert _query(root, graph, *args) == 0
    output = capsys.readouterr()
    assert output.err == _notice(refresh=not no_refresh)
    if no_refresh:
        assert output.out == "no callers\n"
        assert graph.read_bytes() == before
    else:
        assert all(f"app.{name}" in output.out for name in ("one", "two", "three"))
        assert len(output.out.splitlines()) == 3
        assert graph.read_bytes() != before
        assert _query(root, graph, *args) == 0
        assert capsys.readouterr().err == ""


@pytest.mark.parametrize("no_refresh", [False, True])
@pytest.mark.parametrize("query", [("callers", "lib.target"), ("surface", "library"), ("systems",)])
@pytest.mark.parametrize("missing", [False, True])
def test_all_envelopes_report_observed_version_and_then_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_refresh: bool,
    query: tuple[str, ...],
    missing: bool,
) -> None:
    root = tmp_path / "source"
    graph = _project(root)
    monkeypatch.chdir(root)
    _old_graph(root, graph, monkeypatch)
    if missing:
        _remove_version(graph)
    capsys.readouterr()
    args = (*query, "--json", *(["--no-refresh"] if no_refresh else []))
    assert _query(root, graph, *args) == 0
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["stale_analyzer"] == {
        "graph": None if missing else OLD,
        "current": language_interpreter.ANALYZER_SEMANTICS_VERSION,
    }
    assert payload["stale"] == []
    assert payload["refreshed"] is not no_refresh
    assert output.err == _notice(None if missing else OLD, refresh=not no_refresh)
    if no_refresh:
        assert _analyze(root, graph) == 0
        capsys.readouterr()
    assert _query(root, graph, *args) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["stale_analyzer"] is None
    assert output.err == ""


@pytest.mark.parametrize("mode", ["refresh", "no-refresh", "mixed"])
def test_version_and_path_drift_order_and_failed_refresh_atomicity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    root = tmp_path / "source"
    graph = _project(root)
    _old_graph(root, graph, monkeypatch)
    if mode == "mixed":
        (root / "app.js").write_text("export function other() {}\n")
        path = "app.js"
    else:
        (root / "app.py").write_text((root / "app.py").read_text() + "# edited\n")
        path = "app.py"
    before = (graph.read_bytes(), stamp_path(graph).read_bytes())
    capsys.readouterr()
    assert _query(
        root, graph, "callers", "lib.target", *(["--no-refresh"] if mode == "no-refresh" else [])
    ) == (2 if mode == "mixed" else 0)
    output = capsys.readouterr()
    expected = _notice(refresh=mode != "no-refresh")
    if mode != "no-refresh":
        expected += "minotaur: refreshing graph (1 drifted paths)\n"
    expected += f"minotaur: stale: {path}\n"
    if mode == "mixed":
        expected += "minotaur: error: selected files require unsupported multi-interpreter graph composition\n"
        assert output.out == ""
    assert output.err == expected
    if mode != "refresh":
        assert (graph.read_bytes(), stamp_path(graph).read_bytes()) == before


def test_missing_whole_producer_refreshes_and_wrong_root_refuses_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "source"
    graph = _project(root)
    _old_graph(root, graph, monkeypatch)
    _remove_version(graph, whole_producer=True)
    wrong = tmp_path / "wrong"
    wrong.mkdir()
    capsys.readouterr()
    before = graph.read_bytes()
    assert _query(wrong, graph, "callers", "lib.target") == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == (
        f"minotaur: error: graph belongs to a different tree: all 2 recorded files "
        f"are missing under root {wrong}; use the correct --root, --no-refresh to query "
        "the saved graph, or analyze --force to replace it\n"
    )
    assert graph.read_bytes() == before
    assert _query(root, graph, "callers", "lib.target", "--json") == 0
    output = capsys.readouterr()
    assert output.err == _notice(None)
    assert json.loads(output.out)["stale_analyzer"] == {
        "graph": None,
        "current": language_interpreter.ANALYZER_SEMANTICS_VERSION,
    }
    assert len(json.loads(output.out)["results"]) == 3


def test_version_only_stale_unreferenced_disables_text_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "source"
    graph = _project(root)
    _old_graph(root, graph, monkeypatch)
    capsys.readouterr()
    assert _query(root, graph, "unreferenced", "--no-refresh", "--text-fallback", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    mentioned = next(item for item in payload["results"] if item["symbol"] == "lib.mentioned")
    assert mentioned["text_mention"] is False
    assert _analyze(root, graph) == 0
    capsys.readouterr()
    assert _query(root, graph, "unreferenced", "--text-fallback", "--json") == 0
    current = json.loads(capsys.readouterr().out)
    assert (
        next(item for item in current["results"] if item["symbol"] == "lib.mentioned")[
            "text_mention"
        ]
        is True
    )


@pytest.mark.parametrize("selection", [None, []])
def test_library_graphs_without_nonempty_selection_are_exempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], selection
) -> None:
    root = tmp_path / "source"
    graph = _project(root)
    monkeypatch.chdir(root)
    assert _analyze(root, graph) == 0
    document = load_graph_file(graph).document
    extensions = {"minotaur": {"selection": selection}} if selection is not None else None
    graph.write_bytes(
        serialize(
            replace(document, generated_by=Producer("foreign", "unknown"), extensions=extensions)
        )
    )
    before = graph.read_bytes()
    capsys.readouterr()
    for query in [("callers", "lib.target"), ("surface", "library"), ("systems",)]:
        assert _query(root, graph, *query, "--json") == 0
        output = capsys.readouterr()
        assert output.err == ""
        assert json.loads(output.out)["stale_analyzer"] is None
        assert graph.read_bytes() == before


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.mark.parametrize("in_git", [False, True])
@pytest.mark.parametrize("scope", [False, True])
@pytest.mark.parametrize("missing", [False, True])
def test_committed_diff_refuses_before_even_failing_analysis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    in_git: bool,
    scope: bool,
    missing: bool,
) -> None:
    root = tmp_path / "source"
    graph = _project(root)
    monkeypatch.chdir(root)
    if in_git:
        _git(root, "init", "-q")
        _git(root, "config", "user.name", "Test")
        _git(root, "config", "user.email", "test@example.invalid")
    _old_graph(root, graph, monkeypatch)
    if missing:
        _remove_version(graph)
        stamp_path(graph).write_text(hashlib.sha256(graph.read_bytes()).hexdigest() + "\n")
    definition = root / "docs/systems/lib"
    (definition / "system.toml").write_text(
        'schema_version = 1\nname = "library"\nfiles = ["app.py", "app.js"]\n'
    )
    if scope:
        shutil.copyfile(graph, definition / "graph.json")
        shutil.copyfile(stamp_path(graph), stamp_path(definition / "graph.json"))
    if in_git:
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "old analyzer graph")
    (root / "app.js").write_text("export function other() {}\n")
    calls = []
    original = cli._produce_selection

    def observe(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(cli, "_produce_selection", observe)
    capsys.readouterr()
    assert cli.main(["query", "diff", *(["--scope", "library"] if scope else [])]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == (
        f"minotaur: error: committed graph was produced by analyzer version {'none' if missing else OLD}, "
        f"but this Minotaur uses {language_interpreter.ANALYZER_SEMANTICS_VERSION}; "
        "run analyze and commit the refreshed graph and its sidecar\n"
    )
    assert calls == []


def test_refresh_and_committed_diff_share_version_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "source"
    graph = _project(root)
    monkeypatch.chdir(root)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    assert _analyze(root, graph) == 0
    (root / "app.py").write_text((root / "app.py").read_text() + "# edited\n")
    assert _query(root, graph, "callers", "lib.target") == 0
    assert (
        json.loads(graph.read_bytes())["generated_by"]["version"]
        == language_interpreter.ANALYZER_SEMANTICS_VERSION
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "current graph")
    versions = []
    original = cli._diff_output

    def observe(query, old, new):
        versions.append(new.generated_by.version)
        return original(query, old, new)

    monkeypatch.setattr(cli, "_diff_output", observe)
    capsys.readouterr()
    assert cli.main(["query", "diff"]) == 0
    assert capsys.readouterr().err == ""
    assert versions == [language_interpreter.ANALYZER_SEMANTICS_VERSION]


def test_invocation_validates_and_serializes_analyzer_change() -> None:
    change = AnalyzerChange(None, language_interpreter.ANALYZER_SEMANTICS_VERSION)
    assert (
        QueryInvocation(False, stale_analyzer=change).to_dict()["stale_analyzer"]
        == change.to_dict()
    )
    assert QueryInvocation(False).to_dict()["stale_analyzer"] is None
    with pytest.raises(ValueError, match="stale_analyzer must be AnalyzerChange or None"):
        QueryInvocation(False, stale_analyzer={"graph": OLD})
