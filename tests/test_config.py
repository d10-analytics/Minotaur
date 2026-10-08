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
import json
import os
import re
import subprocess
import sys
import types
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from minotaur import config, git
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

    monkeypatch.setattr(git.subprocess, "run", unavailable)

    resolved = resolve_config(start)

    assert resolved.config_file == top.resolve()


def test_discovery_uses_shared_work_tree_root_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A config above the shared owner's reported root must not bind. Replacing
    # the lower-level probe as well makes this fail if discovery bypasses the
    # shared work_tree_root owner and calls run_git directly.
    outer = _write(tmp_path, ".minotaur.toml", _CONFIG)
    repo = tmp_path / "repo"
    start = repo / "nested"
    start.mkdir(parents=True)
    calls: list[Path] = []

    def shared_owner(path: Path) -> Path:
        calls.append(path)
        return repo.resolve()

    def bypassed_probe(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("config discovery bypassed the shared Git owner")

    monkeypatch.setattr(git, "work_tree_root", shared_owner)
    monkeypatch.setattr(git, "run_git", bypassed_probe)

    assert find_config(start) is None
    assert calls == [start]
    assert outer.exists()


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
    assert resolved.sql.view_depth_threshold == 3


def test_sql_view_depth_setting_is_validated_and_resolved(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path,
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\ntargets = ["src"]\n'
        "[minotaur.sql]\nview_depth_threshold = 7\n",
    )
    resolved = resolve_config(tmp_path)
    assert resolved.sql.view_depth_threshold == 7

    for value in ('"seven"', "0", "-1"):
        cfg.write_text(
            '[minotaur]\nschema_version = 1\ntargets = ["src"]\n'
            f"[minotaur.sql]\nview_depth_threshold = {value}\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="view_depth_threshold"):
            resolve_config(tmp_path)


def test_sql_migration_patterns_are_immutable_and_match_whole_components(
    tmp_path: Path,
) -> None:
    cfg = _write(
        tmp_path,
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\ntargets = ["src"]\n'
        '[minotaur.sql]\nmigration_patterns = ["migrations/**/*.sql"]\n',
    )
    resolved = resolve_config(tmp_path)

    assert resolved.sql.migration_patterns == ("migrations/**/*.sql",)
    assert resolved.sql.matches_migration("migrations/001.sql")
    assert resolved.sql.matches_migration("migrations/nested/002.sql")
    assert not config.SqlSettings(migration_patterns=("migrations/*.sql",)).matches_migration(
        "migrations/nested/002.sql"
    )
    assert not config.SqlSettings(migration_patterns=("*.sql",)).matches_migration(
        "migrations/001.sql"
    )
    assert not config.SqlSettings(migration_patterns=("*.sql",)).matches_migration(
        "migrations/nested/002.sql"
    )
    with pytest.raises(FrozenInstanceError):
        resolved.sql.migration_patterns = ()  # type: ignore[misc]
    assert cfg.exists()


def test_sql_migration_pattern_matching_visits_repeated_recursive_states_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path_parts = tuple([*(f"part{index}" for index in range(6)), "other.sql"])
    pattern = "/".join([*("**" for _ in range(6)), "target.sql"])
    calls: list[tuple[str, str]] = []
    real_fnmatchcase = config.fnmatchcase

    def observed_fnmatchcase(name: str, candidate: str) -> bool:
        calls.append((name, candidate))
        return real_fnmatchcase(name, candidate)

    monkeypatch.setattr(config, "fnmatchcase", observed_fnmatchcase)

    assert not config.SqlSettings(migration_patterns=(pattern,)).matches_migration(
        "/".join(path_parts)
    )
    assert len(calls) == len(path_parts)
    assert {name for name, _ in calls} == set(path_parts)
    assert {candidate for _, candidate in calls} == {"target.sql"}


def test_sql_migration_pattern_matching_does_not_depend_on_recursion_depth() -> None:
    pattern = "/".join([*("**" for _ in range(1_500)), "target.sql"])
    settings = config.SqlSettings(migration_patterns=(pattern,))

    assert settings.matches_migration("target.sql")
    assert settings.matches_migration("nested/target.sql")
    assert not settings.matches_migration("nested/other.sql")


def test_omitted_sql_migration_patterns_default_to_empty(tmp_path: Path) -> None:
    _write(tmp_path, ".minotaur.toml", _CONFIG)
    resolved = resolve_config(tmp_path)

    assert resolved.sql.migration_patterns == ()
    assert not resolved.sql.matches_migration("migrations/001.sql")


def test_sql_foreign_key_target_files_are_normalized_and_immutable(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\ntargets = ["src"]\n'
        '["minotaur"."sql"]\n"foreign_key_target_files" = '
        '{ "Parent" = "schema/parent.sql", '
        '"DBO.Child" = "schema/child.sql" }\n',
    )

    resolved = resolve_config(tmp_path)

    assert resolved.sql.foreign_key_target_files == {
        "parent": "schema/parent.sql",
        "dbo.child": "schema/child.sql",
    }
    assert resolved.sql.foreign_key_target_files["parent"] == "schema/parent.sql"
    assert resolved.sql.foreign_key_target_files["dbo.child"] == "schema/child.sql"
    with pytest.raises(TypeError):
        resolved.sql.foreign_key_target_files["parent"] = "other.sql"  # type: ignore[index]
    assert resolved.sql.foreign_key_target_files["parent"] == "schema/parent.sql"
    assert config.SqlSettings().foreign_key_target_files == {}


@pytest.mark.parametrize(
    "declaration",
    [
        'foreign_key_target_files = ["schema/parent.sql"]',
        'foreign_key_target_files = { "dbo.Parent" = 1 }',
        "foreign_key_target_files = { \"dbo.Parent\" = 'schema\\\\parent.sql' }",
        'foreign_key_target_files = { "dbo.Parent" = "/schema/parent.sql" }',
        'foreign_key_target_files = { "dbo.Parent" = "schema/*.sql" }',
        'foreign_key_target_files = { "dbo.Parent" = "schema/../parent.sql" }',
        'foreign_key_target_files = { "dbo.Parent" = "schema/parent.txt" }',
        'foreign_key_target_files = { "a.b.c" = "schema/parent.sql" }',
        'foreign_key_target_files = { Parent = "schema/parent.sql" }',
        'foreign_key_target_files = { "dbo.Parent" = "schema/parent.sql", '
        '"DBO.parent" = "other.sql" }',
    ],
)
def test_invalid_sql_foreign_key_target_files_are_rejected_before_resolution(
    tmp_path: Path, declaration: str
) -> None:
    _write(
        tmp_path,
        ".minotaur.toml",
        f'[minotaur]\nschema_version = 1\ntargets = ["src"]\n[minotaur.sql]\n{declaration}\n',
    )

    with pytest.raises(ConfigError, match="foreign_key_target_files"):
        resolve_config(tmp_path)


def test_dotted_sql_foreign_key_target_file_mapping_still_requires_quoted_keys() -> None:
    """The supported dotted spelling reaches the same lexical SQL-key check."""
    data = (
        b'[minotaur]\nschema_version = 1\ntargets = ["src"]\n'
        b'sql.foreign_key_target_files = { Parent = "schema/parent.sql" }\n'
    )

    with pytest.raises(ConfigError, match="target keys must be quoted"):
        config.parse_config_bytes(data, source="dotted.toml")


_TARGET_KEY_REFUSAL = "invalid minotaur.sql.foreign_key_target_files: target keys must be quoted"


def _foreign_key_config(spelling: int, key: str) -> str:
    """Return one target-files spelling with ``key`` as its target-key literal.

    Spellings 7 and 8 declare the base fields in the same form as the target
    key; the others extend a base ``[minotaur]`` table.
    """
    base = '[minotaur]\nschema_version = 1\ntargets = ["src"]\n'
    value = '"schema/parent.sql"'
    if spelling == 1:
        return base + f"[minotaur.sql.foreign_key_target_files]\n{key} = {value}\n"
    if spelling == 2:
        return base + f'["minotaur"."sql"."foreign_key_target_files"]\n{key} = {value}\n'
    if spelling == 3:
        return base + f"[ minotaur . sql . foreign_key_target_files ]\n{key} = {value}\n"
    if spelling == 4:
        return base + f"[minotaur.sql]\nforeign_key_target_files.{key} = {value}\n"
    if spelling == 5:
        return base + f"sql.foreign_key_target_files.{key} = {value}\n"
    if spelling == 6:
        return base + f"sql = {{ foreign_key_target_files = {{ {key} = {value} }} }}\n"
    if spelling == 7:
        return (
            'minotaur.schema_version = 1\nminotaur.targets = ["src"]\n'
            f"minotaur.sql.foreign_key_target_files.{key} = {value}\n"
        )
    if spelling == 8:
        return (
            'minotaur = { schema_version = 1, targets = ["src"], '
            f"sql = {{ foreign_key_target_files = {{ {key} = {value} }} }} }}\n"
        )
    raise AssertionError(f"unknown spelling: {spelling}")


@pytest.mark.parametrize("spelling", range(1, 9))
def test_every_target_file_key_spelling_refuses_a_bare_key(tmp_path: Path, spelling: int) -> None:
    """Each accepted TOML spelling checks the quoted-key grammar on both routes."""
    text = _foreign_key_config(spelling, "Parent")
    _write(tmp_path, ".minotaur.toml", text)
    located_path = find_config(tmp_path)

    with pytest.raises(ConfigError) as blob:
        config.parse_config_bytes(text.encode(), source="blob.toml")
    assert str(blob.value) == f"{_TARGET_KEY_REFUSAL} (in blob.toml)"

    with pytest.raises(ConfigError) as located:
        resolve_config(tmp_path)
    assert str(located.value) == f"{_TARGET_KEY_REFUSAL} (in {located_path})"


@pytest.mark.parametrize("spelling", range(1, 9))
@pytest.mark.parametrize("key", ['"Parent"', "'Parent'"])
def test_every_target_file_key_spelling_accepts_a_quoted_key(
    tmp_path: Path, spelling: int, key: str
) -> None:
    """Each spelling resolves the same mapping when the target key is quoted."""
    text = _foreign_key_config(spelling, key)
    _write(tmp_path, ".minotaur.toml", text)

    blob = config.parse_config_bytes(text.encode(), source="blob.toml")
    located = resolve_config(tmp_path)

    assert dict(blob.sql.foreign_key_target_files) == {"parent": "schema/parent.sql"}
    assert dict(located.sql.foreign_key_target_files) == {"parent": "schema/parent.sql"}


def test_target_file_table_refuses_a_bare_key_beside_a_quoted_key(tmp_path: Path) -> None:
    """One bare key is refused even when another key in the same table is quoted."""
    text = (
        '[minotaur]\nschema_version = 1\ntargets = ["src"]\n'
        "[minotaur.sql.foreign_key_target_files]\n"
        '"Parent" = "schema/parent.sql"\nOther = "schema/other.sql"\n'
    )
    _write(tmp_path, ".minotaur.toml", text)
    located_path = find_config(tmp_path)

    with pytest.raises(ConfigError) as blob:
        config.parse_config_bytes(text.encode(), source="blob.toml")
    assert str(blob.value) == f"{_TARGET_KEY_REFUSAL} (in blob.toml)"

    with pytest.raises(ConfigError) as located:
        resolve_config(tmp_path)
    assert str(located.value) == f"{_TARGET_KEY_REFUSAL} (in {located_path})"


def test_target_file_table_accepts_two_quoted_keys(tmp_path: Path) -> None:
    """A table whose keys are all quoted resolves each target on both routes."""
    text = (
        '[minotaur]\nschema_version = 1\ntargets = ["src"]\n'
        "[minotaur.sql.foreign_key_target_files]\n"
        '"Parent" = "schema/parent.sql"\n\'Other\' = "schema/other.sql"\n'
    )
    _write(tmp_path, ".minotaur.toml", text)
    expected = {"parent": "schema/parent.sql", "other": "schema/other.sql"}

    blob = config.parse_config_bytes(text.encode(), source="blob.toml")
    located = resolve_config(tmp_path)

    assert dict(blob.sql.foreign_key_target_files) == expected
    assert dict(located.sql.foreign_key_target_files) == expected


def test_target_file_quoted_keys_ignore_comments_and_string_values(tmp_path: Path) -> None:
    """A comment line and a string value containing the field text do not trigger."""
    text = (
        '[minotaur]\nschema_version = 1\ntargets = ["src"]\n'
        "[minotaur.sql.foreign_key_target_files]\n"
        '# Parent = "x"\n'
        '"Parent" = "schema/parent.sql"\n'
        '"Note" = "see Parent = note.sql"\n'
    )
    _write(tmp_path, ".minotaur.toml", text)
    expected = {"parent": "schema/parent.sql", "note": "see Parent = note.sql"}

    blob = config.parse_config_bytes(text.encode(), source="blob.toml")
    located = resolve_config(tmp_path)

    assert dict(blob.sql.foreign_key_target_files) == expected
    assert dict(located.sql.foreign_key_target_files) == expected


@pytest.mark.parametrize(
    "declaration",
    [
        'migration_patterns = "migrations/**/*.sql"',
        'migration_patterns = ["migrations/**/*.sql", 1]',
        'migration_patterns = ["migrations\\\\**/*.sql"]',
        'migration_patterns = ["/migrations/**/*.sql"]',
        'migration_patterns = ["migrations//**/*.sql"]',
        'migration_patterns = ["migrations/./**/*.sql"]',
        'migration_patterns = ["migrations/../**/*.sql"]',
        'migration_patterns = ["migrations/{unknown}.sql"]',
    ],
)
def test_invalid_sql_migration_patterns_are_rejected_before_resolution(
    tmp_path: Path, declaration: str
) -> None:
    _write(
        tmp_path,
        ".minotaur.toml",
        f'[minotaur]\nschema_version = 1\ntargets = ["src"]\n[minotaur.sql]\n{declaration}\n',
    )

    with pytest.raises(ConfigError, match="migration_patterns"):
        resolve_config(tmp_path)


def test_unknown_sql_setting_is_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\ntargets = ["src"]\n[minotaur.sql]\nunknown = 1\n',
    )
    with pytest.raises(ConfigError, match="unknown SQL config field"):
        resolve_config(tmp_path)


def test_relative_config_values_anchor_at_the_declared_root(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path,
        "cfg/.minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "proj"\n'
        'targets = ["a.py", "sub/b.py"]\ngraph = "out/g.json"\n',
    )
    cfg.parent.mkdir(parents=True, exist_ok=True)
    _ = cfg

    resolved = resolve_config(tmp_path / "cfg")

    project_root = (tmp_path / "cfg" / "proj").resolve()
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
        '[minotaur]\nschema_version = 1\nroot = "proj"\n'
        'targets = ["a.py"]\nsystems_dir = "systems"\n',
    )

    resolved = resolve_config(tmp_path / "cfg")

    project_root = (tmp_path / "cfg" / "proj").resolve()
    assert resolved.systems_dir == (project_root / "systems").resolve()


def test_omitted_systems_dir_defaults_to_docs_systems_under_the_declared_root(
    tmp_path: Path,
) -> None:
    """An omitted systems_dir resolves the docs/systems default under root."""
    _write(
        tmp_path,
        "cfg/.minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "proj"\ntargets = ["a.py"]\n',
    )

    resolved = resolve_config(tmp_path / "cfg")

    project_root = (tmp_path / "cfg" / "proj").resolve()
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


# ---------------------------------------------------------------------------
# Config-folder confinement and link-folder anchoring
# ---------------------------------------------------------------------------


def _refused_config(cfg: Path, body: str) -> tuple[ConfigError, Path]:
    """Write ``body`` as ``cfg/.minotaur.toml`` and return its refusal and path."""
    source = _write(cfg, ".minotaur.toml", body)
    with pytest.raises(ConfigError) as error:
        resolve_config(cfg)
    return error.value, source


def _assert_escapes_config_folder(
    error: ConfigError, *, field: str, raw: str, resolved: Path, folder: Path, source: Path
) -> None:
    """A confinement refusal names the field, raw value, real path, folder and source."""
    message = str(error)
    assert f"configured {field} escapes the config folder" in message
    assert raw in message
    assert str(resolved) in message
    assert f"(allowed folder is {folder})" in message
    assert str(source) in message


def test_config_sourced_values_outside_the_config_folder_are_refused(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (tmp_path / "victim").mkdir()
    outside_src = tmp_path / "outside-src"
    outside_src.mkdir()
    outside_etc = tmp_path / "etc"
    outside_etc.mkdir()
    outside_graph = tmp_path / "outside-graph.json"

    scenarios = [
        ('root = "../victim_home"\n', "root", "../victim_home", tmp_path / "victim_home"),
        (f"root = {json.dumps(str(outside_src))}\n", "root", str(outside_src), outside_src),
        (
            'graph = "../victim/x.json"\n',
            "graph",
            "../victim/x.json",
            tmp_path / "victim" / "x.json",
        ),
        (
            f"graph = {json.dumps(str(outside_graph))}\n",
            "graph",
            str(outside_graph),
            outside_graph,
        ),
        (
            f"systems_dir = {json.dumps(str(outside_etc))}\n",
            "systems_dir",
            str(outside_etc),
            outside_etc,
        ),
    ]

    for declaration, field, raw, resolved in scenarios:
        body = '[minotaur]\nschema_version = 1\ntargets = ["a.py"]\n' + declaration
        error, source = _refused_config(cfg, body)
        _assert_escapes_config_folder(
            error,
            field=field,
            raw=raw,
            resolved=resolved.resolve(),
            folder=cfg.resolve(),
            source=source,
        )


def test_links_whose_targets_leave_the_config_folder_are_refused(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    outside_src = tmp_path / "outside-src"
    outside_src.mkdir()
    (outside_src / "mod.py").write_text("value = 1\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_systems = tmp_path / "outside-systems"
    outside_systems.mkdir()
    (cfg / "src-link").symlink_to(outside_src)
    (cfg / "out-link").symlink_to(outside)
    (cfg / "sys-link").symlink_to(outside_systems)
    (cfg / "dangling.json").symlink_to(outside / "missing.json")

    scenarios = [
        ('root = "src-link"\n', "root", "src-link", outside_src),
        ('graph = "out-link/g.json"\n', "graph", "out-link/g.json", outside / "g.json"),
        ('systems_dir = "sys-link"\n', "systems_dir", "sys-link", outside_systems),
        ('graph = "dangling.json"\n', "graph", "dangling.json", outside / "missing.json"),
    ]

    for declaration, field, raw, resolved in scenarios:
        body = '[minotaur]\nschema_version = 1\ntargets = ["a.py"]\n' + declaration
        error, source = _refused_config(cfg, body)
        _assert_escapes_config_folder(
            error,
            field=field,
            raw=raw,
            resolved=resolved.resolve(),
            folder=cfg.resolve(),
            source=source,
        )


def test_a_link_that_resolves_inside_the_config_folder_is_allowed(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "real-systems").mkdir()
    (cfg / "sys-link").symlink_to("real-systems")
    _write(
        cfg,
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\ntargets = ["a.py"]\nsystems_dir = "sys-link"\n',
    )

    resolved = resolve_config(cfg)

    assert resolved.systems_dir == (cfg / "real-systems").resolve()


def test_linked_config_anchors_at_the_link_folder(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    base = _write(
        shared,
        "base.toml",
        '[minotaur]\nschema_version = 1\ntargets = ["a.py"]\ngraph = "g.json"\n',
    )
    (proj / ".minotaur.toml").symlink_to(base)

    resolved = resolve_config(proj)

    assert resolved.config_file == proj.resolve() / ".minotaur.toml"
    assert resolved.root == proj.resolve()
    assert resolved.graph == (proj / "g.json").resolve()

    error, source = _refused_config(
        proj,
        '[minotaur]\nschema_version = 1\ntargets = ["a.py"]\ngraph = "../shared/g.json"\n',
    )

    _assert_escapes_config_folder(
        error,
        field="graph",
        raw="../shared/g.json",
        resolved=(shared / "g.json").resolve(),
        folder=proj.resolve(),
        source=source,
    )


def test_confinement_reports_root_then_graph_then_targets_then_systems_dir(
    tmp_path: Path,
) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()

    error, _ = _refused_config(
        cfg,
        '[minotaur]\nschema_version = 1\ntargets = ["a.py"]\n'
        'root = "../victim_home"\ngraph = "../victim/g.json"\n',
    )
    assert "configured root escapes the config folder" in str(error)
    assert "configured graph escapes the config folder" not in str(error)

    error, _ = _refused_config(
        cfg,
        '[minotaur]\nschema_version = 1\ntargets = ["a.py"]\n'
        'graph = "../victim/g.json"\nsystems_dir = "../victim/systems"\n',
    )
    assert "configured graph escapes the config folder" in str(error)
    assert "configured systems_dir escapes the config folder" not in str(error)

    error, _ = _refused_config(
        cfg,
        '[minotaur]\nschema_version = 1\ntargets = ["../esc.py"]\n'
        'systems_dir = "../victim/systems"\n',
    )
    assert "config target escapes root" in str(error)
    assert "configured systems_dir escapes the config folder" not in str(error)


def test_explicit_cli_values_are_never_confinement_checked(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    _write(
        cfg,
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\nroot = "proj"\ntargets = ["a.py"]\n',
    )
    outside = tmp_path / "outside"
    outside.mkdir()

    resolved = resolve_config(cfg, explicit_root=outside, explicit_graph=outside / "g.json")

    assert resolved.root == outside
    assert resolved.graph == outside / "g.json"


def test_an_escaping_config_is_refused_even_beside_an_explicit_graph(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    error, source = _refused_config(
        cfg,
        '[minotaur]\nschema_version = 1\ntargets = ["a.py"]\ngraph = "../victim/g.json"\n',
    )

    _assert_escapes_config_folder(
        error,
        field="graph",
        raw="../victim/g.json",
        resolved=(tmp_path / "victim" / "g.json").resolve(),
        folder=cfg.resolve(),
        source=source,
    )

    with pytest.raises(ConfigError, match="configured graph escapes the config folder"):
        resolve_config(cfg, explicit_graph=tmp_path / "explicit.json")


def test_a_sibling_folder_that_only_shares_the_config_folder_prefix_is_refused(
    tmp_path: Path,
) -> None:
    """Containment is per path component, never a string prefix.

    ``cfg2`` and ``cfg-extra`` begin with the textual prefix of ``cfg`` but are
    not under it, so a lexical ``startswith`` check would wrongly accept them
    for every confined field.
    """
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (tmp_path / "cfg2").mkdir()
    (tmp_path / "cfg-extra").mkdir()

    scenarios = [
        ('root = "../cfg2/proj"\n', "root", "../cfg2/proj", tmp_path / "cfg2" / "proj"),
        ('graph = "../cfg2/g.json"\n', "graph", "../cfg2/g.json", tmp_path / "cfg2" / "g.json"),
        (
            'systems_dir = "../cfg-extra/systems"\n',
            "systems_dir",
            "../cfg-extra/systems",
            tmp_path / "cfg-extra" / "systems",
        ),
    ]

    for declaration, field, raw, resolved in scenarios:
        body = '[minotaur]\nschema_version = 1\ntargets = ["a.py"]\n' + declaration
        error, source = _refused_config(cfg, body)
        _assert_escapes_config_folder(
            error,
            field=field,
            raw=raw,
            resolved=resolved.resolve(),
            folder=cfg.resolve(),
            source=source,
        )


def test_repository_config_resolves_inside_the_repository() -> None:
    repository = Path(__file__).parents[1].resolve()

    resolved = resolve_config(repository)

    assert resolved.config_file == repository / ".minotaur.toml"
    assert resolved.root == (repository / "src").resolve()
    assert resolved.graph == (repository / "minotaur-system-definitions.json").resolve()
    assert resolved.systems_dir == (repository / "docs" / "systems").resolve()
    assert resolved.targets is not None


def test_parse_config_bytes_preserves_raw_values_without_source_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Captured labels are diagnostics only, including a currently-linked label."""
    target = tmp_path / "current" / ".minotaur.toml"
    target.parent.mkdir()
    linked = tmp_path / "archive" / "label.toml"
    linked.parent.mkdir()
    linked.symlink_to(target)
    absent = tmp_path / "missing" / "historical.toml"
    data = (
        b"[minotaur]\n"
        b"schema_version = 1\nroot = 'raw-root'\n"
        b"graph = 'raw-graph'\ntargets = ['z.py', '', 'a.py']\n"
        b"systems_dir = 'raw-systems'\n"
    )

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("supplied config parsing consulted the filesystem")

    monkeypatch.setattr(Path, "resolve", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(config, "_anchor_config", forbidden)

    first = config.parse_config_bytes(data, source=absent)
    second = config.parse_config_bytes(data, source=linked)

    assert first == second
    assert first.root == "raw-root"
    assert first.graph == "raw-graph"
    assert first.targets == ("z.py", "", "a.py")
    assert first.systems_dir == "raw-systems"
    with pytest.raises(AttributeError):
        first.root = "changed"  # type: ignore[misc]


def test_config_lexical_validation_ignores_mapping_syntax_inside_toml_values() -> None:
    """Captured configs keep multiline values and comments outside the mapping grammar."""
    systems_dir = 'foreign_key_target_files = { Parent = "schema/parent.sql" }\n'
    data = (
        '[minotaur]\nschema_version = 1\ntargets = ["src"]\n'
        '# foreign_key_target_files = { Parent = "schema/parent.sql" }\n'
        'systems_dir = """\n'
        f"{systems_dir}"
        '"""\n'
    ).encode()

    parsed = config.parse_config_bytes(data, source="captured.toml")

    assert parsed.systems_dir == systems_dir


def test_disk_config_lexical_validation_ignores_mapping_syntax_inside_toml_values(
    tmp_path: Path,
) -> None:
    """Located configs keep multiline values and comments outside the mapping grammar."""
    systems_dir = 'foreign_key_target_files = { Parent = "schema/parent.sql" }\n'
    cfg = _write(
        tmp_path,
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\ntargets = ["src"]\n'
        '# foreign_key_target_files = { Parent = "schema/parent.sql" }\n'
        'systems_dir = """\n'
        f"{systems_dir}"
        '"""\n',
    )

    resolved = resolve_config(tmp_path)

    assert resolved.config_file == cfg.resolve()
    assert resolved.systems_dir == (tmp_path / systems_dir).resolve()


def test_capture_config_ignores_foreign_key_mapping_in_an_unrelated_table() -> None:
    """A similarly named mapping outside Minotaur SQL settings remains ordinary TOML."""
    data = (
        b'[metadata]\nforeign_key_target_files = { Parent = "schema/p.sql" }\n'
        b'[minotaur]\nschema_version = 1\ntargets = ["src"]\n'
    )

    parsed = config.parse_config_bytes(data, source="captured.toml")

    assert parsed.sql.foreign_key_target_files == {}


def test_disk_config_ignores_foreign_key_mapping_in_an_unrelated_table(tmp_path: Path) -> None:
    """A located unrelated mapping does not invoke Minotaur SQL-key validation."""
    cfg = _write(
        tmp_path,
        ".minotaur.toml",
        '[metadata]\nforeign_key_target_files = { Parent = "schema/p.sql" }\n'
        '[minotaur]\nschema_version = 1\ntargets = ["src"]\n',
    )

    resolved = resolve_config(tmp_path)

    assert resolved.config_file == cfg.resolve()
    assert resolved.sql.foreign_key_target_files == {}


def test_capture_config_ignores_foreign_key_mapping_in_an_unrelated_array_table() -> None:
    """An array-table header replaces the preceding SQL table's lexical context."""
    data = (
        b'[minotaur]\nschema_version = 1\ntargets = ["src"]\n[minotaur.sql]\n'
        b'[[metadata]]\nforeign_key_target_files = { Parent = "schema/p.sql" }\n'
    )

    parsed = config.parse_config_bytes(data, source="captured.toml")

    assert parsed.sql.foreign_key_target_files == {}


def test_disk_config_ignores_foreign_key_mapping_in_an_unrelated_array_table(
    tmp_path: Path,
) -> None:
    """A located array-table mapping does not inherit SQL-key validation."""
    cfg = _write(
        tmp_path,
        ".minotaur.toml",
        '[minotaur]\nschema_version = 1\ntargets = ["src"]\n[minotaur.sql]\n'
        '[[metadata]]\nforeign_key_target_files = { Parent = "schema/p.sql" }\n',
    )

    resolved = resolve_config(tmp_path)

    assert resolved.config_file == cfg.resolve()
    assert resolved.sql.foreign_key_target_files == {}


@pytest.mark.parametrize(
    ("data", "message"),
    [
        pytest.param(b"\xff", "invalid UTF-8", id="invalid-utf8"),
        pytest.param(b"[minotaur\n", "invalid TOML", id="invalid-toml"),
    ],
)
def test_supplied_bytes_decode_failures_name_the_source(data: bytes, message: str) -> None:
    source = "historical-label.toml"

    with pytest.raises(ConfigError, match=re.escape(source)) as error:
        config.parse_config_bytes(data, source=source)

    assert message in str(error.value)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        pytest.param(b"\xff", "invalid UTF-8", id="invalid-utf8"),
        pytest.param(b"[minotaur\n", "invalid TOML", id="invalid-toml"),
    ],
)
def test_disk_decode_failures_name_the_file(tmp_path: Path, data: bytes, message: str) -> None:
    path = tmp_path / "broken.toml"
    path.write_bytes(data)

    with pytest.raises(ConfigError, match=re.escape(str(path))) as error:
        config.read_toml_file(path)

    assert message in str(error.value)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        pytest.param("title = 'no section'\n", "[minotaur]", id="missing-section"),
        pytest.param("minotaur = 5\n", "must be a table", id="non-mapping-section"),
        pytest.param(
            "[minotaur]\nschema_version = 1\ntargets = ['src']\nunknown = true\n",
            "unknown config field: unknown",
            id="unknown-key",
        ),
        pytest.param("[minotaur]\ntargets = ['src']\n", "schema_version", id="missing-version"),
        pytest.param(
            "[minotaur]\nschema_version = true\ntargets = ['src']\n",
            "schema_version must be an integer",
            id="boolean-version",
        ),
        pytest.param(
            "[minotaur]\nschema_version = '1'\ntargets = ['src']\n",
            "schema_version must be an integer",
            id="non-integer-version",
        ),
        pytest.param(
            "[minotaur]\nschema_version = 2\ntargets = ['src']\n",
            "unsupported schema_version",
            id="unsupported-version",
        ),
        pytest.param(
            "[minotaur]\nschema_version = 1\nroot = 5\ntargets = ['src']\n",
            "config root must be a string",
            id="non-string-root",
        ),
        pytest.param(
            "[minotaur]\nschema_version = 1\ngraph = 5\ntargets = ['src']\n",
            "config graph must be a string",
            id="non-string-graph",
        ),
        pytest.param(
            "[minotaur]\nschema_version = 1\nsystems_dir = 5\ntargets = ['src']\n",
            "config systems_dir must be a string",
            id="non-string-systems-dir",
        ),
        pytest.param(
            "[minotaur]\nschema_version = 1\n",
            "missing required field: targets",
            id="missing-targets",
        ),
        pytest.param(
            "[minotaur]\nschema_version = 1\ntargets = []\n",
            "config targets must not be empty",
            id="empty-targets",
        ),
        pytest.param(
            "[minotaur]\nschema_version = 1\ntargets = 'src'\n",
            "config targets must be a list of strings",
            id="non-list-targets",
        ),
        pytest.param(
            "[minotaur]\nschema_version = 1\ntargets = [1]\n",
            "config targets must be a list of strings",
            id="non-string-target",
        ),
    ],
)
def test_supplied_schema_failures_name_the_source(body: str, message: str) -> None:
    source = "captured-config.toml"

    with pytest.raises(ConfigError, match=re.escape(source)) as error:
        config.parse_config_bytes(body.encode(), source=source)

    assert message in str(error.value)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        pytest.param("title = 'no section'\n", "[minotaur]", id="missing-section"),
        pytest.param("minotaur = 5\n", "must be a table", id="non-mapping-section"),
        pytest.param(
            "[minotaur]\nschema_version = 1\ntargets = ['src']\nunknown = true\n",
            "unknown config field: unknown",
            id="unknown-key",
        ),
        pytest.param("[minotaur]\ntargets = ['src']\n", "schema_version", id="missing-version"),
        pytest.param(
            "[minotaur]\nschema_version = true\ntargets = ['src']\n",
            "schema_version must be an integer",
            id="boolean-version",
        ),
        pytest.param(
            "[minotaur]\nschema_version = '1'\ntargets = ['src']\n",
            "schema_version must be an integer",
            id="non-integer-version",
        ),
        pytest.param(
            "[minotaur]\nschema_version = 2\ntargets = ['src']\n",
            "unsupported schema_version",
            id="unsupported-version",
        ),
        pytest.param(
            "[minotaur]\nschema_version = 1\nroot = 5\ntargets = ['src']\n",
            "config root must be a string",
            id="non-string-root",
        ),
        pytest.param(
            "[minotaur]\nschema_version = 1\ngraph = 5\ntargets = ['src']\n",
            "config graph must be a string",
            id="non-string-graph",
        ),
        pytest.param(
            "[minotaur]\nschema_version = 1\nsystems_dir = 5\ntargets = ['src']\n",
            "config systems_dir must be a string",
            id="non-string-systems-dir",
        ),
        pytest.param(
            "[minotaur]\nschema_version = 1\n",
            "missing required field: targets",
            id="missing-targets",
        ),
        pytest.param(
            "[minotaur]\nschema_version = 1\ntargets = []\n",
            "config targets must not be empty",
            id="empty-targets",
        ),
        pytest.param(
            "[minotaur]\nschema_version = 1\ntargets = 'src'\n",
            "config targets must be a list of strings",
            id="non-list-targets",
        ),
        pytest.param(
            "[minotaur]\nschema_version = 1\ntargets = [1]\n",
            "config targets must be a list of strings",
            id="non-string-target",
        ),
    ],
)
def test_disk_schema_failures_name_the_config_source(
    tmp_path: Path, body: str, message: str
) -> None:
    cfg = _write(tmp_path, ".minotaur.toml", body)

    with pytest.raises(ConfigError, match=re.escape(str(cfg))) as error:
        resolve_config(tmp_path)

    assert message in str(error.value)


def test_raw_defaults_and_explicit_empty_values_remain_distinct_from_disk_values(
    tmp_path: Path,
) -> None:
    omitted = config.parse_config_bytes(
        b"[minotaur]\nschema_version = 1\ntargets = ['src']\n",
        source="omitted.toml",
    )
    assert omitted == config.ValidatedConfig(
        root="", graph="minotaur-graph.json", targets=("src",), systems_dir="docs/systems"
    )

    cfg = _write(
        tmp_path,
        "cfg/.minotaur.toml",
        "[minotaur]\nschema_version = 1\nroot = ''\ngraph = ''\ntargets = ['']\nsystems_dir = ''\n",
    )
    raw = config.parse_config_bytes(cfg.read_bytes(), source=cfg)
    resolved = resolve_config(cfg.parent)

    assert raw == config.ValidatedConfig(root="", graph="", targets=("",), systems_dir="")
    assert resolved.root == cfg.parent.resolve()
    assert resolved.graph == cfg.parent.resolve()
    assert resolved.targets == (cfg.parent.resolve(),)
    assert resolved.systems_dir == cfg.parent.resolve()


def test_raw_validation_precedes_anchor_for_mixed_invalid_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write(
        tmp_path,
        ".minotaur.toml",
        "[minotaur]\nschema_version = 1\ntargets = ['../escape.py']\nsystems_dir = 5\n",
    )

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("invalid raw config entered disk anchoring")

    monkeypatch.setattr(config, "_anchor_config", forbidden)

    with pytest.raises(ConfigError, match=re.escape(str(cfg))) as error:
        resolve_config(tmp_path)

    assert "config systems_dir must be a string" in str(error.value)


def test_valid_raw_escaping_target_reaches_anchor_and_reports_containment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write(
        tmp_path,
        ".minotaur.toml",
        "[minotaur]\nschema_version = 1\ntargets = ['../escape.py']\nsystems_dir = 'systems'\n",
    )
    calls = 0
    original = config._anchor_config

    def counting(validated: config.ValidatedConfig, *, source: Path) -> object:
        nonlocal calls
        calls += 1
        return original(validated, source=source)

    monkeypatch.setattr(config, "_anchor_config", counting)

    with pytest.raises(ConfigError, match=re.escape(str(cfg))) as error:
        resolve_config(tmp_path)

    assert calls == 1
    assert "config target escapes root" in str(error.value)


def test_disk_and_supplied_routes_share_the_private_raw_validator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write(tmp_path, ".minotaur.toml", _CONFIG)
    original = config._validate_config
    calls: list[Path | str] = []

    def changed(raw: dict[str, object], *, source: Path | str) -> config.ValidatedConfig:
        calls.append(source)
        validated = original(raw, source=source)
        return config.ValidatedConfig(
            root="mutated-root",
            graph=validated.graph,
            targets=("mutated.py",),
            systems_dir=validated.systems_dir,
        )

    monkeypatch.setattr(config, "_validate_config", changed)

    disk = resolve_config(tmp_path)
    supplied = config.parse_config_bytes(_CONFIG.encode(), source="captured.toml")

    assert disk.root == (tmp_path / "mutated-root").resolve()
    assert disk.targets == ((tmp_path / "mutated-root" / "mutated.py").resolve(),)
    assert supplied.root == "mutated-root"
    assert supplied.targets == ("mutated.py",)
    assert calls == [cfg.resolve(), "captured.toml"]


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


def test_read_toml_bytes_reports_excessive_nesting_without_a_cause() -> None:
    """A document deeper than the parser supports fails as the attributed error.

    ``RecursionError`` is not a ``ValueError``, so without the handler it would
    escape every caller; the raised error must suppress the parser context.
    """
    data = b"x = " + b"[" * 5000 + b"]" * 5000

    with pytest.raises(ConfigError) as error:
        config.read_toml_bytes(data, source="blob")

    assert str(error.value) == "TOML nests too deeply: blob"
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True


def test_read_toml_bytes_still_reports_ordinary_malformed_toml() -> None:
    """Shallow malformed TOML keeps the decode-error message, not the depth one."""
    with pytest.raises(ConfigError) as error:
        config.read_toml_bytes(b"[minotaur\n", source="shallow.toml")

    assert "invalid TOML in shallow.toml" in str(error.value)
    assert "nests too deeply" not in str(error.value)


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
        captured = module.parse_config_bytes(_CONFIG.encode(), source="historical.toml")

        assert resolved.targets == ((cfg_dir / "src").resolve(),)
        assert captured.targets == ("src",)
        assert len(loads_calls) >= 2  # Disk and supplied routes use tomli.loads.
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


def test_discovery_ignores_absolute_git_dir_from_subdirectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    sub = root / "sub"
    sub.mkdir(parents=True)
    assert _git(root, "init", "-q").returncode == 0
    top = _write(root, ".minotaur.toml", _CONFIG)
    monkeypatch.setenv("GIT_DIR", str(root / ".git"))
    assert find_config(sub) == top


def test_discovery_preserves_inherited_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer = _write(tmp_path, ".minotaur.toml", _CONFIG)
    root = tmp_path / "repo"
    sub = root / "sub"
    sub.mkdir(parents=True)
    assert _git(root, "init", "-q").returncode == 0
    assert find_config(sub) is None
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(root))
    assert find_config(sub) == outer


def test_discovery_walks_above_bare_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer = _write(tmp_path, ".minotaur.toml", _CONFIG)
    bare = tmp_path / "bare.git"
    bare.mkdir()
    assert _git(bare, "init", "--bare", "-q").returncode == 0
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "safe.bareRepository")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "all")
    assert find_config(bare) == outer


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux byte filenames")
def test_discovery_non_utf8_directory_preserves_git_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, ".minotaur.toml", _CONFIG)
    root = tmp_path / os.fsdecode(b"t8-\xe9")
    root.mkdir()
    assert _git(root, "init", "-q").returncode == 0
    assert _git(root, "config", "user.email", "tests@example.invalid").returncode == 0
    assert _git(root, "config", "user.name", "Minotaur Tests").returncode == 0
    _write(root, "app.py", "value = 1\n")
    assert _git(root, "add", "app.py").returncode == 0
    assert _git(root, "commit", "-qm", "initial").returncode == 0
    inside = root / "nested"
    inside.mkdir()
    monkeypatch.chdir(inside)

    assert find_config(Path.cwd()) is None
    inner = _write(root, ".minotaur.toml", _CONFIG)
    assert find_config(Path.cwd()) == inner


def test_discovery_stops_in_orphaned_linked_worktree(tmp_path: Path) -> None:
    _write(tmp_path, ".minotaur.toml", _CONFIG)
    main = tmp_path / "main"
    main.mkdir()
    assert _git(main, "init", "-q").returncode == 0
    assert _git(main, "config", "user.email", "tests@example.invalid").returncode == 0
    assert _git(main, "config", "user.name", "Minotaur Tests").returncode == 0
    assert _git(main, "commit", "-q", "--allow-empty", "-m", "initial").returncode == 0
    linked = tmp_path / "linked"
    assert _git(main, "worktree", "add", "-q", str(linked)).returncode == 0
    main.rename(tmp_path / "moved")

    with pytest.raises(
        ConfigError, match="Git work-tree discovery failed: .*not a git repository:"
    ):
        find_config(linked)
