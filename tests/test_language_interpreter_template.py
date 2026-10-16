"""Behavioral proof that the language template instantiates into a clean package.

The replay test follows the template README literally: it copies the template
files to the documented destinations inside a temporary copy of ``src/``,
applies the README's ordered renames, runs the README's import-sort and format
commands, then lints, type-checks, imports, and exercises the copy.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

import minotaur.language_interpreter as language_interpreter
from minotaur.graph_model import provenance
from minotaur.language_interpreter import default_registry, exclusions
from minotaur.language_interpreter.emission import file_node

ROOT = Path(__file__).parents[1]
TEMPLATE_ROOT = ROOT / "templates" / "language_interpreter"
TEMPLATE_README = TEMPLATE_ROOT / "README.md"
CONTRIBUTOR_GUIDE = ROOT / "docs" / "guides" / "create-a-language-interpreter.md"
QUALIFIED_TEMPLATE = TEMPLATE_ROOT / "test_example_language_interpreter.py.tmpl"
OLD_TEMPLATE = TEMPLATE_ROOT / "test_interpreter.py.tmpl"
QUALIFIED_DESTINATION = (
    "tests/language_interpreter/example_language/test_example_language_interpreter.py"
)

# The copy plan the README documents. The README step sentences are built from
# the same values, so a step changed in only the README or only here fails.
PACKAGE_DESTINATION = "src/minotaur/language_interpreter/example_language/"
TEST_DESTINATION = "tests/language_interpreter/example_language/"
PACKAGE_SOURCES = ("__init__.py.tmpl", "interpreter.py.tmpl")
TEMPLATE_FILES = frozenset({"README.md", *PACKAGE_SOURCES, QUALIFIED_TEMPLATE.name})

CREATE_STEP = f"Create `{PACKAGE_DESTINATION}` and `{TEST_DESTINATION}`."
PACKAGE_COPY_STEP = (
    f"Copy `{PACKAGE_SOURCES[0]}` and `{PACKAGE_SOURCES[1]}` to `{PACKAGE_DESTINATION}`, "
    "removing `.tmpl` from each destination filename."
)
TEST_COPY_STEP = f"Copy `{QUALIFIED_TEMPLATE.name}` to `{QUALIFIED_DESTINATION}`."
RENAME_SCOPE_STEP = (
    "Rename the placeholders in the copied file contents and destination paths, in this order:"
)
RENAME_SENTENCES = (
    "Replace every `example_language` with the new language's lowercase Python package name.",
    "Replace every `example-language` with the new language's graph language identifier, "
    f"which must match `{provenance._LANGUAGE_RE.pattern}` and need not equal the package name.",
    "Replace every `minotaur-example` with the new language's registered namespace.",
    "Replace every `.example-lang` with the new language's normalized file extension.",
)
FORMAT_STEP = (
    "Sort the imports and format the copied files, where `<name>` is the new package name:"
)
FORMAT_COMMANDS = (
    "ruff check --select I --fix src/minotaur/language_interpreter/<name> "
    "tests/language_interpreter/<name>",
    "ruff format src/minotaur/language_interpreter/<name> tests/language_interpreter/<name>",
)
README_HELPER_OMISSION = (
    "The template omits the optional whole-workspace helper, so the copied package exposes "
    "only the selected-file function."
)
README_SELECTION_OWNER = (
    "The shared `select_sources()` selection function owns folder walking, workspace "
    "containment, symlinks, extension matching, and excluded folders; library callers that "
    "want a whole-root analysis use it."
)
README_PROHIBITIONS = (
    "A language package must not walk folders, list excluded directory names, or import "
    "`selection` at module level."
)
GUIDE_HELPER_OMISSION = (
    "The language interpreter template omits the whole-workspace helper, so a new language "
    "package exposes only its selected-file function."
)
GUIDE_SELECTION_OWNER = (
    "Library callers that want a whole-root analysis use the shared `select_sources()` "
    "selection function, which owns folder walking, and a new language package must not "
    "walk folders."
)
GUIDE_ABSENT = (
    "analyze_example_language_workspace",
    "discover_example_language_files",
    "Future languages should follow the same arrangement",
)
GUIDE_RETIRED_RULES = (
    "Language-specific discovery belongs only in this optional wrapper",
    "the whole-workspace convenience wrapper, if supplied",
)
GUIDE_PYTHON_DISCOVERY_SCOPE = (
    "This Python arrangement is not a pattern for new languages: a new language package "
    "defines no discovery function, and whole-root callers select its files through "
    "`select_sources()`."
)
GUIDE_PYTHON_HELPER_CHECK = (
    "for the Python interpreter, its retained `analyze_python_workspace()` helper still "
    "delegates to the same selected-file analysis behavior; a new language package "
    "supplies no such helper."
)
STARTER_MESSAGE = "replace the template stub with tested analysis"
BARE_EXTENSION_PLACEHOLDER = re.compile(r"\.example\b(?!-lang)")
WALKER_TOKENS = ("rglob", "os.walk", "walk(", "scandir", "iterdir", "listdir(", "glob(")


def _collapsed(text: str) -> str:
    return " ".join(text.split())


def _placeholder(sentence: str) -> str:
    match = re.search(r"`([^`]+)`", sentence)
    assert match is not None, sentence
    return match.group(1)


def _checkout_snapshot() -> frozenset[str]:
    return frozenset(
        path.relative_to(ROOT).as_posix()
        for base in (ROOT / "src", ROOT / "tests")
        for path in base.rglob("*")
        if "__pycache__" not in path.relative_to(ROOT).parts
    )


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", *command],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"{shlex.join(command)} exited {result.returncode}\n{result.stdout}\n{result.stderr}"
    )
    return result


@dataclass(frozen=True)
class TemplateCase:
    package: str
    language: str
    namespace: str
    extension: str

    def replacements(self) -> tuple[tuple[str, str], ...]:
        values = (self.package, self.language, self.namespace, self.extension)
        return tuple(
            (_placeholder(sentence), value)
            for sentence, value in zip(RENAME_SENTENCES, values, strict=True)
        )

    def rename(self, text: str) -> str:
        for placeholder, value in self.replacements():
            text = text.replace(placeholder, value)
        return text


CASES = (
    TemplateCase("zq", "zq-lang", "minotaur-zq", ".zq"),
    TemplateCase("zz_thirty_two_character_language", "zz-dialect", "minotaur-zz", ".zz"),
    TemplateCase("example_parser", "example-parser", "minotaur-ep", ".ep"),
)


@pytest.fixture
def isolated_import(case: TemplateCase) -> Iterator[list[str]]:
    """Remove every module the case imports, plus the parent attribute, at teardown."""
    module_names: list[str] = []
    yield module_names
    prefix = f"{language_interpreter.__name__}.{case.package}"
    for name in [name for name in sys.modules if name == prefix or name.startswith(f"{prefix}.")]:
        del sys.modules[name]
    for name in module_names:
        sys.modules.pop(name, None)
    if hasattr(language_interpreter, case.package):
        delattr(language_interpreter, case.package)
    importlib.invalidate_caches()


def test_template_folder_holds_only_the_documented_scaffold() -> None:
    assert not OLD_TEMPLATE.exists()
    assert {path.name for path in TEMPLATE_ROOT.iterdir()} == TEMPLATE_FILES

    readme = TEMPLATE_README.read_text(encoding="utf-8")
    assert set(re.findall(r"`(\w[\w.]*\.tmpl)`", readme)) == TEMPLATE_FILES - {"README.md"}
    assert "discovery.py.tmpl" not in readme
    assert QUALIFIED_TEMPLATE.name in readme
    assert QUALIFIED_DESTINATION in readme
    for forbidden in ("test_interpreter.py.tmpl", "minotaur-sql", "--import-mode"):
        assert forbidden not in readme

    for name in sorted(TEMPLATE_FILES):
        text = (TEMPLATE_ROOT / name).read_text(encoding="utf-8")
        assert BARE_EXTENSION_PLACEHOLDER.findall(text) == [], name


def test_readme_states_every_replayed_step_in_order() -> None:
    readme = _collapsed(TEMPLATE_README.read_text(encoding="utf-8"))
    steps = (
        CREATE_STEP,
        PACKAGE_COPY_STEP,
        TEST_COPY_STEP,
        RENAME_SCOPE_STEP,
        *RENAME_SENTENCES,
        FORMAT_STEP,
        *FORMAT_COMMANDS,
    )
    positions = [readme.find(step) for step in steps]
    assert -1 not in positions, [
        step for step, at in zip(steps, positions, strict=True) if at == -1
    ]
    assert positions == sorted(positions)
    assert len(set(positions)) == len(positions)

    graph_language_sentence = RENAME_SENTENCES[1]
    assert provenance._LANGUAGE_RE.pattern in graph_language_sentence
    assert "need not equal the package name" in graph_language_sentence
    assert [_placeholder(sentence) for sentence in RENAME_SENTENCES] == [
        "example_language",
        "example-language",
        "minotaur-example",
        ".example-lang",
    ]

    for sentence in (README_HELPER_OMISSION, README_SELECTION_OWNER, README_PROHIBITIONS):
        assert sentence in readme


def test_guide_names_the_shared_selection_owner_instead_of_a_language_walker() -> None:
    guide = CONTRIBUTOR_GUIDE.read_text(encoding="utf-8")
    for absent in GUIDE_ABSENT:
        assert absent not in guide
    collapsed = _collapsed(guide)
    for retired in GUIDE_RETIRED_RULES:
        assert retired not in collapsed
    assert GUIDE_PYTHON_DISCOVERY_SCOPE in collapsed
    assert GUIDE_PYTHON_HELPER_CHECK in collapsed
    assert GUIDE_HELPER_OMISSION in collapsed
    assert GUIDE_SELECTION_OWNER in collapsed
    assert QUALIFIED_DESTINATION in guide


def test_template_extension_placeholder_is_not_registered() -> None:
    registry = default_registry()
    registered = {registration.extension for registration in registry.registrations}
    for extension in (".example-lang", ".example"):
        assert extension not in registered
        assert registry.registration_for(Path(f"sample{extension}")) is None


@pytest.mark.parametrize("case", CASES, ids=[case.package for case in CASES])
def test_readme_replay_instantiates_a_clean_importable_package(
    case: TemplateCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_import: list[str],
) -> None:
    before = _checkout_snapshot()

    copy_root = tmp_path / "checkout"
    shutil.copytree(ROOT / "src", copy_root / "src", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(ROOT / "pyproject.toml", copy_root / "pyproject.toml")

    copies = [(name, PACKAGE_DESTINATION + name.removesuffix(".tmpl")) for name in PACKAGE_SOURCES]
    copies.append((QUALIFIED_TEMPLATE.name, QUALIFIED_DESTINATION))
    instantiated: list[Path] = []
    for source, destination in copies:
        target = copy_root / case.rename(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        text = (TEMPLATE_ROOT / source).read_text(encoding="utf-8")
        target.write_text(case.rename(text), encoding="utf-8")
        instantiated.append(target)

    package_dir = copy_root / "src" / "minotaur" / "language_interpreter" / case.package
    starter_path = (
        copy_root
        / "tests"
        / "language_interpreter"
        / case.package
        / f"test_{case.package}_interpreter.py"
    )
    assert instantiated == [
        package_dir / "__init__.py",
        package_dir / "interpreter.py",
        starter_path,
    ]

    for command in FORMAT_COMMANDS:
        _run(shlex.split(command.replace("<name>", case.package)), copy_root)
    instantiated_dirs = [
        f"src/minotaur/language_interpreter/{case.package}",
        f"tests/language_interpreter/{case.package}",
    ]
    _run(["ruff", "check", *instantiated_dirs], copy_root)
    typed = _run(["mypy"], copy_root)
    checked = re.search(r"no issues found in (\d+) source files", typed.stdout)
    assert checked is not None, typed.stdout
    assert int(checked.group(1)) == len(list((copy_root / "src" / "minotaur").rglob("*.py")))

    texts = {path: path.read_text(encoding="utf-8") for path in instantiated}
    for path, text in texts.items():
        for excluded in exclusions._EXCLUDED_DIRECTORY_NAMES:
            assert f'"{excluded}"' not in text, (path, excluded)
            assert f"'{excluded}'" not in text, (path, excluded)
    for path in instantiated[:2]:
        for token in WALKER_TOKENS:
            assert token not in texts[path], (path, token)
    assert f'"sample{case.extension}"' in texts[starter_path]
    assert f"`{case.extension}`" in texts[package_dir / "interpreter.py"]
    copied_sources = (copy_root / "src").rglob("*.py")
    assert sum('".mypy_cache"' in path.read_text(encoding="utf-8") for path in copied_sources) == 1

    monkeypatch.setattr(
        language_interpreter,
        "__path__",
        [*language_interpreter.__path__, str(package_dir.parent)],
    )
    importlib.invalidate_caches()
    package = importlib.import_module(f"{language_interpreter.__name__}.{case.package}")
    interpreter = importlib.import_module(f"{package.__name__}.interpreter")
    for module in (package, interpreter):
        assert module.__file__ is not None
        assert Path(module.__file__).resolve().is_relative_to(tmp_path.resolve())
        leaked = [
            name
            for name in dir(module)
            if name.startswith("discover_") or name.endswith("_workspace")
        ]
        assert leaked == [], module.__name__
    assert package.__all__ == [f"analyze_{case.package}_files"]

    language: str = interpreter.LANGUAGE
    namespace: str = interpreter.NAMESPACE
    assert language == case.language
    assert namespace == case.namespace
    assert language != case.package
    node = file_node(f"sample{case.extension}", b"x", namespace, language)
    assert node.language == case.language
    with pytest.raises(ValueError, match="language must match"):
        file_node(f"sample{case.extension}", b"x", namespace, "example_language")

    starter_name = f"_instantiated_template_starter_{case.package}"
    spec = importlib.util.spec_from_file_location(starter_name, starter_path)
    assert spec is not None and spec.loader is not None
    starter = importlib.util.module_from_spec(spec)
    isolated_import.append(starter_name)
    sys.modules[starter_name] = starter
    spec.loader.exec_module(starter)
    starter_tests = [name for name in vars(starter) if name.startswith("test_")]
    assert len(starter_tests) == 1, starter_tests
    fresh = tmp_path / "starter"
    fresh.mkdir()
    with pytest.raises(AssertionError, match=STARTER_MESSAGE):
        getattr(starter, starter_tests[0])(fresh)

    assert _checkout_snapshot() == before
