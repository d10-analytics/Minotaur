"""Behavioral proofs for committed system definitions (AC-03) and membership (AC-04).

Loader cells build real ``system.toml`` trees under ``tmp_path`` and pin the
deterministic, flat, all-or-nothing strict-load contract of
:mod:`minotaur.system`; membership fixture cells classify constructed
endpoint nodes (a source-location symbol, a file node, an unresolved
reference with a site path, and a path-less upstream symbol) against those
loaded systems.  Every named assertion fails if the loader or classifier
behavior it pins is removed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from minotaur import system
from minotaur.config import ConfigError
from minotaur.graph_model.identity import IdentityBasis, NodeIdentity, compute_node_id
from minotaur.graph_model.location import Location, Position, Range
from minotaur.graph_model.node import Node, NodeClass
from minotaur.system import (
    AbsentFile,
    DuplicateSystemName,
    EndpointKind,
    EndpointMembership,
    FileListedInTwoSystems,
    InvalidFileEntry,
    InvalidFileList,
    InvalidSystemName,
    MissingField,
    System,
    UnknownSystem,
    UnknownSystemField,
    UnsupportedSchemaVersion,
)

_VALID = 'schema_version = 1\nname = "auth"\nfiles = ["src/auth/api.py"]\n'
_NODE_ID_ORIGIN = "node:sha256:" + "a" * 64


def _write_definition(root: Path, directory: str, text: str) -> Path:
    """Write one ``system.toml`` under ``root/<directory>`` and return its path."""
    target = root / directory / "system.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _file_node(path: str) -> Node:
    """Construct an analyzed file node for ``path``."""
    identity = NodeIdentity(IdentityBasis.FILE_PATH, "test")
    return Node(
        id=compute_node_id(identity, node_class=NodeClass.FILE.value, path=path),
        identity=identity,
        node_class=NodeClass.FILE,
        label=path,
        path=path,
    )


def _source_symbol(file: str, label: str = "symbol") -> Node:
    """Construct a symbol node defined at a location inside ``file``."""
    location = Location(path=file, range=Range(start=Position(0, 0), end=Position(0, 8)))
    identity = NodeIdentity(IdentityBasis.SOURCE_LOCATION, "test")
    return Node(
        id=compute_node_id(
            identity,
            node_class=NodeClass.SYMBOL.value,
            symbol_kind="function",
            location=location,
        ),
        identity=identity,
        node_class=NodeClass.SYMBOL,
        label=label,
        symbol_kind="function",
        language="python",
        location=location,
    )


def _unresolved_reference(file: str) -> Node:
    """Construct an unresolved-reference node whose site sits inside ``file``."""
    location = Location(path=file, range=Range(start=Position(1, 0), end=Position(1, 12)))
    identity = NodeIdentity(
        IdentityBasis.UNRESOLVED_REFERENCE, "test", originating_node=_NODE_ID_ORIGIN
    )
    reference_text = "auth.api"
    return Node(
        id=compute_node_id(
            identity,
            node_class=NodeClass.UNRESOLVED_REFERENCE.value,
            location=location,
            reference_text=reference_text,
        ),
        identity=identity,
        node_class=NodeClass.UNRESOLVED_REFERENCE,
        label=reference_text,
        reference_text=reference_text,
        location=location,
    )


def _upstream_symbol(name: str) -> Node:
    """Construct a path-less upstream symbol node (no location, no path)."""
    identity = NodeIdentity(IdentityBasis.UPSTREAM_IDENTIFIER, "test", upstream_identifier=name)
    return Node(
        id=compute_node_id(identity, node_class=NodeClass.SYMBOL.value, symbol_kind="function"),
        identity=identity,
        node_class=NodeClass.SYMBOL,
        label=name,
        symbol_kind="function",
        language="python",
    )


@pytest.fixture()
def loaded_systems(tmp_path: Path) -> tuple[tuple[System, ...], Path]:
    """Two loaded systems: ``auth`` (three files) and ``orders`` (two files)."""
    systems_dir = tmp_path / "systems"
    _write_definition(
        systems_dir,
        "auth",
        "schema_version = 1\n"
        'name = "auth"\n'
        'files = ["src/auth/api.py", "src/auth/missing.py", "src/auth/extra.py"]\n',
    )
    _write_definition(
        systems_dir,
        "orders",
        'schema_version = 1\nname = "orders"\nfiles = ["src/orders.py", "src/orders/report.py"]\n',
    )
    return system.load_systems(systems_dir), systems_dir


# ---------------------------------------------------------------------------
# AC-03: deterministic flat discovery over real system.toml trees
# ---------------------------------------------------------------------------


def test_load_systems_returns_declarations_in_stable_declared_name_order(tmp_path: Path) -> None:
    """Directories are scanned deterministically; results sort by declared name."""
    systems_dir = tmp_path / "systems"
    _write_definition(
        systems_dir, "b-directory", 'schema_version = 1\nname = "omega"\nfiles = ["src/o.py"]\n'
    )
    # A duplicate entry inside one definition is deduplicated, keeping its
    # first position, and is not a load error.
    _write_definition(
        systems_dir,
        "a-directory",
        "schema_version = 1\n"
        'name = "alpha"\n'
        'files = ["src/a/one.py", "src/a/one.py", "src/a/two.py"]\n',
    )

    loaded = system.load_systems(systems_dir)

    assert [item.name for item in loaded] == ["alpha", "omega"]
    assert loaded[0] == System(name="alpha", files=("src/a/one.py", "src/a/two.py"))
    assert loaded[1] == System(name="omega", files=("src/o.py",))
    assert loaded[0].definition_directory == systems_dir / "a-directory"
    assert loaded[1].definition_directory == systems_dir / "b-directory"


def test_results_order_by_declared_name_not_directory_scan_order(tmp_path: Path) -> None:
    """load_systems orders by declared name, independent of directory order.

    Directory scan order is reversed relative to the declared names here, so
    this cell fails if the loader ever returns systems in sorted-directory
    order instead of the documented declared-name order.
    """
    systems_dir = tmp_path / "systems"
    _write_definition(
        systems_dir, "z-directory", 'schema_version = 1\nname = "alpha"\nfiles = ["src/a.py"]\n'
    )
    _write_definition(
        systems_dir, "a-directory", 'schema_version = 1\nname = "omega"\nfiles = ["src/o.py"]\n'
    )

    loaded = system.load_systems(systems_dir)

    # Sorted directory scan would give ["omega", "alpha"]; declared-name
    # order must give ["alpha", "omega"].
    assert [item.name for item in loaded] == ["alpha", "omega"]


def test_load_systems_without_a_systems_directory_is_the_empty_set(tmp_path: Path) -> None:
    assert system.load_systems(tmp_path / "no-such-systems") == ()


def test_load_systems_with_an_empty_or_file_systems_path_is_the_empty_set(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty-systems"
    empty.mkdir()
    assert system.load_systems(empty) == ()
    stray = tmp_path / "systems"
    stray.write_text("not a directory", encoding="utf-8")
    assert system.load_systems(stray) == ()


def test_only_immediate_child_directories_with_system_toml_define_systems(
    tmp_path: Path,
) -> None:
    """D-03: nested and stray entries under systems_dir are narrative."""
    systems_dir = tmp_path / "systems"
    _write_definition(systems_dir, "alpha", _VALID)
    # A system.toml in a nested subdirectory must not define a system, and a
    # nested definition that would itself be invalid must never be parsed.
    _write_definition(
        systems_dir,
        "alpha/sub",
        'schema_version = 1\nname = "hidden"\nfiles = ["src/hidden.py"]\n',
    )
    _write_definition(systems_dir, "docs/nested", "not even toml [[[\n")
    # Stray files anywhere under systems_dir define nothing either.
    (systems_dir / "stray.md").write_text("# notes\n", encoding="utf-8")
    (systems_dir / "system.toml").write_text(_VALID, encoding="utf-8")

    loaded = system.load_systems(systems_dir)

    assert [item.name for item in loaded] == ["auth"]


def test_narrative_files_inside_a_system_directory_are_ignored(tmp_path: Path) -> None:
    """AR-02: only system.toml is read; prose and other TOML files are ignored."""
    systems_dir = tmp_path / "systems"
    _write_definition(systems_dir, "auth", _VALID)
    (systems_dir / "auth" / "system.md").write_text("# Auth system\n", encoding="utf-8")
    (systems_dir / "auth" / "notes.toml").write_text("not even toml [[[\n", encoding="utf-8")

    loaded = system.load_systems(systems_dir)

    assert loaded == (System(name="auth", files=("src/auth/api.py",)),)


def test_committed_graph_and_sidecar_inside_system_directory_are_ignored(tmp_path: Path) -> None:
    """AC-04: committed artifacts beside system.toml do not alter loading."""
    systems_dir = tmp_path / "systems"
    _write_definition(systems_dir, "auth", _VALID)
    system_dir = systems_dir / "auth"
    (system_dir / "graph.json").write_text('{"not": "a fixture"}\n', encoding="utf-8")
    (system_dir / "graph.json.sha256").write_text("0" * 64 + "\n", encoding="ascii")

    assert system.load_systems(systems_dir) == (System(name="auth", files=("src/auth/api.py",)),)


# ---------------------------------------------------------------------------
# AC-03: every invalid shape raises a typed, file-attributed error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "error_type", "message"),
    [
        # -- unsupported, mistyped, or missing schema_version -----------------
        (
            'schema_version = 2\nname = "auth"\nfiles = ["src/a.py"]\n',
            UnsupportedSchemaVersion,
            "unsupported schema_version: 2",
        ),
        (
            'schema_version = "1"\nname = "auth"\nfiles = ["src/a.py"]\n',
            UnsupportedSchemaVersion,
            "schema_version must be an integer",
        ),
        (
            'schema_version = true\nname = "auth"\nfiles = ["src/a.py"]\n',
            UnsupportedSchemaVersion,
            "schema_version must be an integer",
        ),
        (
            'name = "auth"\nfiles = ["src/a.py"]\n',
            MissingField,
            "missing required field: schema_version",
        ),
        # -- unknown fields: hand-recorded / expectation / curated shapes ----
        # (AR-01, AR-06: no declared depends_on, consumer lists, edge lists,
        # expectations, or curated relationships in a system definition.)
        (
            'schema_version = 1\nname = "auth"\nfiles = ["src/a.py"]\ndepends_on = ["orders"]\n',
            UnknownSystemField,
            "unknown system field: depends_on",
        ),
        (
            'schema_version = 1\nname = "auth"\nfiles = ["src/a.py"]\nconsumers = ["billing"]\n',
            UnknownSystemField,
            "unknown system field: consumers",
        ),
        (
            'schema_version = 1\nname = "auth"\nfiles = ["src/a.py"]\n[expectations]\n',
            UnknownSystemField,
            "unknown system field: expectations",
        ),
        (
            'schema_version = 1\nname = "auth"\nfiles = ["src/a.py"]\n'
            "[relationships.auth_to_orders]\n",
            UnknownSystemField,
            "unknown system field: relationships",
        ),
        (
            'schema_version = 1\nname = "auth"\nfiles = ["src/a.py"]\nedges = ["auth -> orders"]\n',
            UnknownSystemField,
            "unknown system field: edges",
        ),
        # -- missing, mistyped, or empty name --------------------------------
        (
            'schema_version = 1\nfiles = ["src/a.py"]\n',
            MissingField,
            "missing required field: name",
        ),
        (
            'schema_version = 1\nname = 5\nfiles = ["src/a.py"]\n',
            InvalidSystemName,
            "system name must be a non-empty string",
        ),
        (
            'schema_version = 1\nname = ""\nfiles = ["src/a.py"]\n',
            InvalidSystemName,
            "system name must be a non-empty string",
        ),
        # -- missing, mistyped, or empty files list ---------------------------
        (
            'schema_version = 1\nname = "auth"\n',
            MissingField,
            "missing required field: files",
        ),
        (
            'schema_version = 1\nname = "auth"\nfiles = "src/a.py"\n',
            InvalidFileList,
            "system files must be a list of file paths",
        ),
        (
            'schema_version = 1\nname = "auth"\nfiles = []\n',
            InvalidFileList,
            "system files must not be empty",
        ),
        # -- non-file and root-escaping files entries -------------------------
        (
            'schema_version = 1\nname = "auth"\nfiles = ["src/a.py", 42]\n',
            InvalidFileEntry,
            "system files entries must be strings",
        ),
        (
            'schema_version = 1\nname = "auth"\nfiles = [""]\n',
            InvalidFileEntry,
            "system file entry must not be empty",
        ),
        (
            'schema_version = 1\nname = "auth"\nfiles = ["/etc/passwd"]\n',
            InvalidFileEntry,
            "must be root-relative, not an absolute path: /etc/passwd",
        ),
        (
            'schema_version = 1\nname = "auth"\nfiles = ["../escape.py"]\n',
            InvalidFileEntry,
            "escapes the repository root: ../escape.py",
        ),
        (
            'schema_version = 1\nname = "auth"\nfiles = ["src/../../x.py"]\n',
            InvalidFileEntry,
            "escapes the repository root: src/../../x.py",
        ),
        (
            'schema_version = 1\nname = "auth"\nfiles = ["src/*.py"]\n',
            InvalidFileEntry,
            "must name one file, not a glob or pattern: src/*.py",
        ),
        (
            'schema_version = 1\nname = "auth"\nfiles = ["src/lib/"]\n',
            InvalidFileEntry,
            "must name an individual root-relative file: src/lib/",
        ),
        (
            'schema_version = 1\nname = "auth"\nfiles = ["src//a.py"]\n',
            InvalidFileEntry,
            "must name an individual root-relative file: src//a.py",
        ),
        (
            'schema_version = 1\nname = "auth"\nfiles = ["src/./a.py"]\n',
            InvalidFileEntry,
            "must name an individual root-relative file: src/./a.py",
        ),
        (
            'schema_version = 1\nname = "auth"\nfiles = ["src\\\\a.py"]\n',
            InvalidFileEntry,
            "must be a slash-separated repository path",
        ),
        (
            'schema_version = 1\nname = "auth"\nfiles = ["node:sha256:' + "a" * 64 + '"]\n',
            InvalidFileEntry,
            "must name a repository file, not a node ID: node:sha256:",
        ),
    ],
)
def test_every_invalid_definition_raises_a_typed_error_naming_the_file(
    tmp_path: Path,
    body: str,
    error_type: type[system.SystemError],
    message: str,
) -> None:
    """Each R-03 violation raises its typed error before any system is produced."""
    systems_dir = tmp_path / "systems"
    definition = _write_definition(systems_dir, "broken", body)

    with pytest.raises(error_type) as error:
        system.load_systems(systems_dir)

    text = str(error.value)
    assert re.search(re.escape(message), text)
    assert str(definition) in text


def test_one_invalid_definition_fails_the_whole_load_before_any_system(
    tmp_path: Path,
) -> None:
    """Validation is all-or-nothing: a valid earlier system is never returned."""
    systems_dir = tmp_path / "systems"
    _write_definition(systems_dir, "aaa", _VALID)
    broken = _write_definition(
        systems_dir, "zzz", 'schema_version = 2\nname = "zeta"\nfiles = ["src/z.py"]\n'
    )

    with pytest.raises(UnsupportedSchemaVersion) as error:
        system.load_systems(systems_dir)

    assert str(broken) in str(error.value)


def test_duplicate_system_name_error_names_both_defining_files(tmp_path: Path) -> None:
    systems_dir = tmp_path / "systems"
    first = _write_definition(
        systems_dir, "a-first", 'schema_version = 1\nname = "dup"\nfiles = ["src/one.py"]\n'
    )
    second = _write_definition(
        systems_dir, "b-second", 'schema_version = 1\nname = "dup"\nfiles = ["src/two.py"]\n'
    )

    with pytest.raises(DuplicateSystemName) as error:
        system.load_systems(systems_dir)

    text = str(error.value)
    assert "duplicate system name: dup" in text
    assert str(first) in text
    assert str(second) in text


def test_file_listed_in_two_systems_error_names_the_file_and_both_defining_files(
    tmp_path: Path,
) -> None:
    systems_dir = tmp_path / "systems"
    first = _write_definition(
        systems_dir,
        "a-auth",
        'schema_version = 1\nname = "auth"\nfiles = ["src/shared.py", "src/auth.py"]\n',
    )
    second = _write_definition(
        systems_dir,
        "b-orders",
        'schema_version = 1\nname = "orders"\nfiles = ["src/orders.py", "src/shared.py"]\n',
    )

    with pytest.raises(FileListedInTwoSystems) as error:
        system.load_systems(systems_dir)

    text = str(error.value)
    assert "file listed in two systems: src/shared.py" in text
    assert str(first) in text
    assert str(second) in text


def test_invalid_toml_in_a_definition_fails_through_the_config_helper(
    tmp_path: Path,
) -> None:
    """The loader's parse path goes through read_toml_file (ConfigError names it)."""
    systems_dir = tmp_path / "systems"
    definition = _write_definition(systems_dir, "auth", "[system\nname = nope\n")

    with pytest.raises(ConfigError) as error:
        system.load_systems(systems_dir)

    text = str(error.value)
    assert "invalid TOML" in text
    assert str(definition) in text


# ---------------------------------------------------------------------------
# D-12 / F-02: unknown-system resolution carries nearest loaded names
# ---------------------------------------------------------------------------


def test_resolve_system_returns_the_one_system_and_unknown_raises_with_nearest(
    loaded_systems: tuple[tuple[System, ...], Path],
) -> None:
    systems, _ = loaded_systems

    assert system.resolve_system(systems, "orders") == systems[1]

    with pytest.raises(UnknownSystem) as error:
        system.resolve_system(systems, "order")

    assert error.value.name == "order"
    assert "orders" in error.value.nearest
    assert "unknown system: order" in str(error.value)
    assert "orders" in str(error.value)


# ---------------------------------------------------------------------------
# AC-04: membership is the exact-file test over declared lists
# ---------------------------------------------------------------------------


def test_file_membership_is_the_exact_listed_file_test(
    loaded_systems: tuple[tuple[System, ...], Path],
) -> None:
    systems, _ = loaded_systems

    assert system.system_for_file(systems, "src/auth/api.py") == systems[0]
    assert system.system_for_file(systems, "src/orders/report.py") == systems[1]
    # Unlisted paths are no_system, including an unlisted path under the
    # auth directory prefix: no package/module/directory mapping (AR-03).
    assert system.system_for_file(systems, "src/unlisted.py") is None
    assert system.system_for_file(systems, "src/auth/internal.py") is None


def test_endpoint_classification_answers_system_no_system_and_external(
    loaded_systems: tuple[tuple[System, ...], Path],
) -> None:
    systems, _ = loaded_systems
    auth = systems[0]
    orders = systems[1]

    # A listed file is one-system for every endpoint kind that derives a file:
    # a source-location symbol, a file node, and an unresolved-reference site.
    assert system.classify_endpoint(
        systems, _source_symbol("src/auth/api.py")
    ) == EndpointMembership(kind=EndpointKind.SYSTEM, system=auth, file="src/auth/api.py")
    assert system.classify_endpoint(systems, _file_node("src/orders.py")) == EndpointMembership(
        kind=EndpointKind.SYSTEM, system=orders, file="src/orders.py"
    )
    assert system.classify_endpoint(
        systems, _unresolved_reference("src/orders/report.py")
    ) == EndpointMembership(kind=EndpointKind.SYSTEM, system=orders, file="src/orders/report.py")

    # An unlisted path-carrying file is no_system, never name-mapped.
    assert system.classify_endpoint(
        systems, _source_symbol("src/unlisted.py")
    ) == EndpointMembership(kind=EndpointKind.NO_SYSTEM, file="src/unlisted.py")
    assert system.classify_endpoint(
        systems, _source_symbol("src/auth/internal.py")
    ) == EndpointMembership(kind=EndpointKind.NO_SYSTEM, file="src/auth/internal.py")

    # A path-less upstream node is external even when its label equals a
    # loaded system name (AR-03: no name-based mapping exists).
    assert system.classify_endpoint(systems, _upstream_symbol("orders:api")) == EndpointMembership(
        kind=EndpointKind.EXTERNAL
    )
    assert system.classify_endpoint(systems, _upstream_symbol("auth")) == EndpointMembership(
        kind=EndpointKind.EXTERNAL
    )


def test_listed_files_without_an_analyzed_node_are_surfaced_in_stable_order(
    loaded_systems: tuple[tuple[System, ...], Path],
) -> None:
    systems, _ = loaded_systems
    nodes = (
        _file_node("src/auth/api.py"),
        _source_symbol("src/auth/extra.py", "extra"),
        _file_node("src/orders.py"),
    )

    report = system.absent_files(systems, nodes)

    assert report == (
        AbsentFile(system=systems[0], file="src/auth/missing.py"),
        AbsentFile(system=systems[1], file="src/orders/report.py"),
    )
