"""Behavioral proofs for the versioned project configuration resolver.

Covers discovery (AC-06), anchoring, absolutization, pass-through, and
per-field precedence of the resolved set (AC-11), and the Python 3.10 TOML
mechanism (AC-12): the guarded ``tomllib``/``tomli`` import shim and the
conditional ``tomli`` dependency marker in ``pyproject.toml``.  Every named
assertion fails if the behavior it pins is removed.
"""

from __future__ import annotations

import builtins
import importlib
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

from minotaur import config
from minotaur.config import ConfigError, find_config, resolve_config

_CONFIG = '[minotaur]\nschema_version = 1\ntargets = ["src"]\n'


def _write(root: Path, path: str, text: str) -> Path:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# AC-06: discovery over real filesystem and real Git repository layouts
# ---------------------------------------------------------------------------


def test_discovery_walks_up_and_prefers_the_nearest_config(tmp_path: Path) -> None:
    upper = _write(
        tmp_path, "upper/.minotaur.toml", '[minotaur]\nschema_version = 1\ntargets = ["top.py"]\n'
    )
    start = tmp_path / "upper" / "a" / "b"
    start.mkdir(parents=True)

    resolved = resolve_config(start)

    assert resolved.config_file == upper.resolve()
    assert resolved.targets == ((tmp_path / "upper" / "top.py").resolve(),)

    nearer = _write(
        tmp_path, "upper/a/.minotaur.toml", '[minotaur]\nschema_version = 1\ntargets = ["mid.py"]\n'
    )
    resolved_nearer = resolve_config(start)

    assert resolved_nearer.config_file == nearer.resolve()
    assert resolved_nearer.targets == ((tmp_path / "upper" / "a" / "mid.py").resolve(),)


def test_discovery_stops_at_git_work_tree_root(tmp_path: Path) -> None:
    # A config above the work-tree root must never bind: with no config inside
    # the repo discovery must stop and report no config at all.
    _write(tmp_path, ".minotaur.toml", '[minotaur]\nschema_version = 1\ntargets = ["outer.py"]\n')
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _git(repo, "init").returncode == 0
    repo_config = _write(
        repo, ".minotaur.toml", '[minotaur]\nschema_version = 1\ntargets = ["inner.py"]\n'
    )
    inside = repo / "a" / "b"
    inside.mkdir(parents=True)

    governed = resolve_config(inside)
    assert governed.config_file == repo_config.resolve()
    assert governed.targets == ((repo / "inner.py").resolve(),)

    empty_repo = tmp_path / "empty-repo"
    empty_repo.mkdir()
    assert _git(empty_repo, "init").returncode == 0
    (empty_repo / "a").mkdir(parents=True)

    ungoverned = resolve_config(
        empty_repo / "a", explicit_root=empty_repo, explicit_graph=empty_repo / "g.json"
    )

    assert ungoverned.config_file is None


def test_discovery_continues_when_git_probe_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    top = _write(tmp_path, ".minotaur.toml", _CONFIG)
    start = tmp_path / "a" / "b"
    start.mkdir(parents=True)

    def unavailable(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("git unavailable")

    monkeypatch.setattr(config.subprocess, "run", unavailable)

    resolved = resolve_config(start)

    assert resolved.config_file == top.resolve()


def test_explicit_config_selection_disables_walk_and_never_merges(tmp_path: Path) -> None:
    _write(
        tmp_path, "start/.minotaur.toml", '[minotaur]\nschema_version = 1\ntargets = ["near.py"]\n'
    )
    explicit = _write(
        tmp_path, "explicit/other.toml", '[minotaur]\nschema_version = 1\ntargets = ["far.py"]\n'
    )
    start = tmp_path / "start"

    resolved = resolve_config(start, config=Path("../explicit/other.toml"))

    assert resolved.config_file == explicit.resolve()
    assert resolved.targets == ((tmp_path / "explicit" / "far.py").resolve(),)
    # The nearer start config never composes or merges into the selection.
    assert (tmp_path / "start" / "near.py").resolve() not in resolved.targets


def test_explicit_config_that_does_not_exist_is_rejected_naming_the_path(tmp_path: Path) -> None:
    missing = tmp_path / "absent.toml"
    with pytest.raises(ConfigError, match=r"config file does not exist: .*absent\.toml"):
        resolve_config(tmp_path, config=missing)
    with pytest.raises(ConfigError, match=r"config file does not exist: .*absent\.toml"):
        find_config(tmp_path, config=missing)


# ---------------------------------------------------------------------------
# AC-11: anchoring, absolutization, pass-through, graph freedom, and merging
# ---------------------------------------------------------------------------


def test_omitted_root_defaults_to_config_directory_and_default_graph(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    _write(cfg, ".minotaur.toml", _CONFIG)

    resolved = resolve_config(cfg)

    assert resolved.root == cfg.resolve()
    assert resolved.graph == (cfg / "minotaur-graph.json").resolve()
    assert resolved.targets == ((cfg / "src").resolve(),)


def test_relative_config_values_anchor_at_the_declared_root(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path,
        "cfg/.minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "../proj"\n'
        'targets = ["a.py", "sub/b.py"]\ngraph = "out/g.json"\n',
    )
    cfg.parent.mkdir(parents=True, exist_ok=True)
    _ = cfg

    resolved = resolve_config(tmp_path / "cfg")

    project_root = (tmp_path / "proj").resolve()
    assert resolved.root == project_root
    assert resolved.targets == (
        (project_root / "a.py").resolve(),
        (project_root / "sub" / "b.py").resolve(),
    )
    assert resolved.graph == (project_root / "out" / "g.json").resolve()


def test_config_sourced_target_escaping_the_root_is_rejected(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path,
        "cfg/.minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "proj"\ntargets = ["../esc.py"]\n',
    )
    cfg.parent.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ConfigError) as relative:
        resolve_config(tmp_path / "cfg")
    assert "escapes root" in str(relative.value)
    assert "../esc.py" in str(relative.value)

    _write(
        tmp_path,
        "cfg/.minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "proj"\n'
        f'targets = ["{tmp_path / "outside" / "x.py"}"]\n',
    )
    with pytest.raises(ConfigError, match="escapes root"):
        resolve_config(tmp_path / "cfg")


def test_configured_graph_is_anchored_but_never_root_containment_checked(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "cfg/.minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "proj"\ntargets = ["."]\n'
        'graph = "../outside/g.json"\n',
    )

    resolved = resolve_config(tmp_path / "cfg")

    # Resolving succeeds and the graph lands outside the declared root.
    assert resolved.graph == (tmp_path / "cfg" / "outside" / "g.json").resolve()


def test_explicit_values_win_and_pass_through_unmodified(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "cfg/.minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "conf-root"\n'
        'targets = ["conf.py"]\ngraph = "conf-g.json"\n',
    )

    resolved = resolve_config(
        tmp_path / "cfg",
        explicit_root=Path("my-root"),
        explicit_graph=Path("g.json"),
        explicit_targets=(Path("x.py"),),
    )

    assert resolved.root == Path("my-root")  # Relative stays relative.
    assert resolved.graph == Path("g.json")
    assert resolved.targets == (Path("x.py"),)

    absolute = tmp_path / "abs-root"
    resolved_absolute = resolve_config(tmp_path / "cfg", explicit_root=absolute)

    assert resolved_absolute.root == absolute  # Absolute stays absolute.


def test_present_config_is_validated_even_with_fully_explicit_values(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "cfg/.minotaur.toml",
        '[minotaur]\nschema_version = 1\ntargets = ["src"]\nunknown_future = true\n',
    )

    with pytest.raises(ConfigError, match="unknown config field: unknown_future"):
        resolve_config(
            tmp_path / "cfg",
            explicit_root=tmp_path,
            explicit_graph=tmp_path / "g.json",
            explicit_targets=(tmp_path / "t.py",),
        )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param("schema_version = 1\ntargets = []\n", r"\[minotaur\]", id="missing-section"),
        pytest.param('[minotaur]\ntargets = ["src"]\n', "schema_version", id="missing-version"),
        pytest.param(
            '[minotaur]\nschema_version = "1"\ntargets = ["src"]\n',
            "schema_version",
            id="version-wrong-type",
        ),
        pytest.param(
            '[minotaur]\nschema_version = 2\ntargets = ["src"]\n',
            "unsupported schema_version",
            id="version-unsupported",
        ),
        pytest.param(
            '[minotaur]\nschema_version = 1\ntargets = ["src"]\nsystems_dir = 5\n',
            "systems_dir",
            id="systems-dir-wrong-type",
        ),
        pytest.param(
            '[minotaur]\nschema_version = 1\ntargets = ["src"]\nexpectations_dir = "exp"\n',
            "expectations_dir",
            id="future-field-expectations-dir",
        ),
        pytest.param(
            '[minotaur]\nschema_version = 1\nroot = 5\ntargets = ["src"]\n',
            "root",
            id="root-wrong-type",
        ),
        pytest.param(
            '[minotaur]\nschema_version = 1\ntargets = ["src"]\ngraph = []\n',
            "graph",
            id="graph-wrong-type",
        ),
        pytest.param("[minotaur]\nschema_version = 1\n", "targets", id="missing-targets"),
        pytest.param(
            "[minotaur]\nschema_version = 1\ntargets = []\n",
            "targets",
            id="empty-targets",
        ),
        pytest.param(
            '[minotaur]\nschema_version = 1\ntargets = "src"\n',
            "targets",
            id="targets-wrong-type",
        ),
        pytest.param(
            "[minotaur]\nschema_version = 1\ntargets = [1]\n",
            "targets",
            id="targets-non-string-item",
        ),
        pytest.param(
            "[minotaur]\nschema_version = 1\ntargets = [\n", "invalid TOML", id="bad-toml"
        ),
    ],
)
def test_every_validation_violation_raises_config_error_naming_the_field(
    tmp_path: Path, body: str, expected: str
) -> None:
    cfg = _write(tmp_path, "cfg/.minotaur.toml", body)

    with pytest.raises(ConfigError, match=expected):
        resolve_config(cfg.parent)


# ---------------------------------------------------------------------------
# systems_dir field (D-08/R-02): acceptance, anchoring, default, single owner
# ---------------------------------------------------------------------------


def test_configured_systems_dir_is_accepted_and_anchored_at_the_declared_root(
    tmp_path: Path,
) -> None:
    """A config with a relative systems_dir resolves with it anchored at root."""
    _write(
        tmp_path,
        "cfg/.minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "../proj"\n'
        'targets = ["a.py"]\nsystems_dir = "systems"\n',
    )

    resolved = resolve_config(tmp_path / "cfg")

    project_root = (tmp_path / "proj").resolve()
    assert resolved.systems_dir == (project_root / "systems").resolve()


def test_omitted_systems_dir_defaults_to_docs_systems_under_the_declared_root(
    tmp_path: Path,
) -> None:
    """An omitted systems_dir resolves the docs/systems default under root."""
    _write(
        tmp_path,
        "cfg/.minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "../proj"\ntargets = ["a.py"]\n',
    )

    resolved = resolve_config(tmp_path / "cfg")

    project_root = (tmp_path / "proj").resolve()
    assert resolved.systems_dir == (project_root / "docs" / "systems").resolve()


def test_omitted_systems_dir_defaults_under_the_config_directory_when_root_omitted(
    tmp_path: Path,
) -> None:
    """With no root declared, the docs/systems default sits under the config dir."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    _write(cfg, ".minotaur.toml", '[minotaur]\nschema_version = 1\ntargets = ["a.py"]\n')

    resolved = resolve_config(cfg)

    assert resolved.systems_dir == (cfg / "docs" / "systems").resolve()


def test_configless_explicit_root_still_emits_a_docs_systems_default(
    tmp_path: Path,
) -> None:
    """D-11: with no located config, systems_dir defaults under the explicit root."""
    root = tmp_path / "proj"

    resolved = resolve_config(tmp_path, explicit_root=root, explicit_graph=root / "g.json")

    assert resolved.config_file is None
    assert resolved.systems_dir == root / "docs" / "systems"


def test_systems_dir_is_resolved_exactly_once_by_the_single_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One resolution reads the config file exactly once through the one seam."""
    cfg = _write(
        tmp_path,
        "cfg/.minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "."\ntargets = ["a.py"]\nsystems_dir = "systems"\n',
    )
    calls: list[Path] = []
    original = config.read_toml_file

    def counting(path: Path) -> dict[str, object]:
        calls.append(path)
        return original(path)

    monkeypatch.setattr(config, "read_toml_file", counting)

    resolved = resolve_config(cfg.parent)

    assert calls == [cfg.resolve()]
    assert resolved.systems_dir == (cfg.parent / "systems").resolve()


# ---------------------------------------------------------------------------
# read_toml_file seam (D-08): vocabulary-neutral, path-attributed errors
# ---------------------------------------------------------------------------


def test_read_toml_file_is_vocabulary_neutral_and_returns_the_raw_table(
    tmp_path: Path,
) -> None:
    """The seam parses any TOML: no [minotaur] section or field checks apply."""
    payload = _write(tmp_path, "notes/system.toml", '[system]\nname = "alpha"\n')

    parsed = config.read_toml_file(payload)

    assert parsed == {"system": {"name": "alpha"}}


def test_read_toml_file_read_failure_raises_config_error_naming_the_path(
    tmp_path: Path,
) -> None:
    """An unreadable file fails with a ConfigError that names the path."""
    missing = tmp_path / "does-not-exist.toml"

    with pytest.raises(ConfigError, match=re.escape(str(missing))):
        config.read_toml_file(missing)


def test_read_toml_file_parse_failure_raises_config_error_naming_the_path(
    tmp_path: Path,
) -> None:
    """Invalid TOML fails with a ConfigError that names the path."""
    broken = _write(tmp_path, "bad/system.toml", "[system\nname = nope\n")

    with pytest.raises(ConfigError, match=re.escape(str(broken))):
        config.read_toml_file(broken)


# ---------------------------------------------------------------------------
# AC-12: guarded tomllib/tomli shim and the pyproject.toml backport marker
# ---------------------------------------------------------------------------


def test_config_reloads_through_the_tomli_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = importlib.import_module("minotaur.config")
    # Reload re-executes the module body, so every module-scope class (notably
    # ConfigError) gets a NEW identity that no longer matches what other test
    # modules captured at import time. Snapshot the state and restore it in the
    # finally block so the reload leaves no re-aliased classes behind.
    pre_state = dict(module.__dict__)
    real_tomllib = module.tomllib
    stand_in = types.ModuleType("tomli")
    loads_calls: list[str] = []

    def stand_in_loads(text: str) -> dict[str, object]:
        loads_calls.append(text)
        return real_tomllib.loads(text)

    stand_in.loads = stand_in_loads
    monkeypatch.setitem(sys.modules, "tomli", stand_in)
    real_import = builtins.__import__

    def block_tomllib(name: str, *args: object, **kwargs: object) -> object:
        if name == "tomllib":
            raise ModuleNotFoundError("No module named 'tomllib'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_tomllib)
    try:
        importlib.reload(module)
        assert module.tomllib is stand_in

        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir()
        _write(cfg_dir, ".minotaur.toml", _CONFIG)

        resolved = module.resolve_config(cfg_dir)

        assert resolved.targets == ((cfg_dir / "src").resolve(),)
        assert loads_calls  # The real config file was parsed via the stand-in.
    finally:
        monkeypatch.undo()
        module.__dict__.clear()
        module.__dict__.update(pre_state)


def test_pyproject_declares_the_tomli_backport_marker() -> None:
    # A text match rather than a TOML import, mirroring the sibling dependency
    # assertion; the marker is install-time metadata a runtime reload cannot see.
    text = (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    dependencies = text.split("dependencies = [", 1)[1].split("]", 1)[0]
    assert re.search(r"tomli>=2\.0; python_version < \"3\.11\"", dependencies)
