"""Static checks over the GitHub workflow files and the build guidance.

The workflow files are read line by line; no YAML library is a dependency.
Each rule is a function over file texts that returns violation messages, so
the same rule runs against the real files and against deliberately broken
copies that prove the rule fails.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
WORKFLOW_NAMES = ("ci.yml", "release.yml", "pages.yml")
GUIDE_PATH = REPO_ROOT / "docs" / "guides" / "contributing.md"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"

_USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(?:\s+#\s*(\S.*?))?\s*$")
_PINNED_REF_RE = re.compile(r"^([\w.-]+/[\w./-]+)@([0-9a-f]{40})$")
_VERSION_COMMENT_RE = re.compile(r"^v\d+\.\d+\.\d+$")
_EXACT_BUILD_RE = re.compile(r"^build==(\d+(?:\.\d+)*)$")
_FLOOR_RE = re.compile(r"^setuptools==(\d+(?:\.\d+)*)$")
_FORBIDDEN_BUILD_TOKENS = frozenset({"--wheel", "-w", "--sdist", "-s"})
_PIP_WHEEL_RE = re.compile(r"\bpip3?\s+wheel\b")


def _read_workflows() -> dict[str, str]:
    return {name: (WORKFLOW_DIR / name).read_text() for name in WORKFLOW_NAMES}


def _code_lines(text: str) -> list[tuple[int, str]]:
    """Return (line number, line) pairs with whole-line comments removed."""
    return [
        (number, line)
        for number, line in enumerate(text.splitlines(), start=1)
        if not line.lstrip().startswith("#")
    ]


def _tokens(command: str) -> list[str]:
    """Split a command on whitespace and drop quote characters from each token."""
    return [token.strip("\"'") for token in command.split()]


def _build_invocations(command: str) -> list[list[str]]:
    """Return the tokens that follow each `-m build` in a command."""
    tokens = _tokens(command)
    return [
        tokens[index + 2 :]
        for index in range(len(tokens) - 1)
        if tokens[index] == "-m" and tokens[index + 1] == "build"
    ]


def _build_token_violations(label: str, command: str) -> list[str]:
    violations = []
    for arguments in _build_invocations(command):
        bad = sorted(_FORBIDDEN_BUILD_TOKENS.intersection(arguments))
        if bad:
            violations.append(f"{label}: -m build uses {', '.join(bad)}")
    return violations


def _pip_install_requirements(command: str) -> list[str]:
    """Return the requirement tokens of a `pip install` command (python3 -m pip or bare)."""
    tokens = _tokens(command)
    for index in range(len(tokens) - 1):
        if tokens[index] == "pip" and tokens[index + 1] == "install":
            return [token for token in tokens[index + 2 :] if not token.startswith("-")]
    return []


def _installs_of_build(texts: dict[str, str]) -> list[tuple[str, int, str]]:
    found = []
    for name, text in texts.items():
        for number, line in _code_lines(text):
            for requirement in _pip_install_requirements(line):
                if re.match(r"^build(?![\w-])", requirement):
                    found.append((name, number, requirement))
    return found


def uses_violations(texts: dict[str, str]) -> list[str]:
    """Every `uses:` pins a 40-hex SHA with a vX.Y.Z comment; one pin per action."""
    violations = []
    seen: dict[str, set[tuple[str, str]]] = {}
    for name, text in texts.items():
        for number, line in _code_lines(text):
            match = _USES_RE.match(line)
            if not match:
                continue
            where = f"{name}:{number}"
            ref, comment = match.group(1), match.group(2)
            pinned = _PINNED_REF_RE.match(ref)
            if not pinned:
                violations.append(f"{where}: {ref} is not owner/repo@<40-hex SHA>")
                continue
            if comment is None or not _VERSION_COMMENT_RE.match(comment):
                violations.append(f"{where}: {ref} lacks a '# vX.Y.Z' comment")
                continue
            seen.setdefault(pinned.group(1), set()).add((pinned.group(2), comment))
    for action, pins in sorted(seen.items()):
        if len(pins) > 1:
            violations.append(f"{action} has {len(pins)} different pins: {sorted(pins)}")
    return violations


def build_install_violations(texts: dict[str, str]) -> list[str]:
    """Every build install names one exact version, shared by both workflows."""
    violations = []
    versions = set()
    installed_in = set()
    for name, number, requirement in _installs_of_build(texts):
        installed_in.add(name)
        match = _EXACT_BUILD_RE.match(requirement)
        if match:
            versions.add(match.group(1))
        else:
            violations.append(f"{name}:{number}: build install '{requirement}' is not exact")
    for name in ("ci.yml", "release.yml"):
        if name in texts and name not in installed_in:
            violations.append(f"{name}: no build install found")
    if len(versions) > 1:
        violations.append(f"build installs disagree: {sorted(versions)}")
    return violations


def build_form_violations(texts: dict[str, str]) -> list[str]:
    """No `-m build` invocation uses a single-artifact flag and no `pip wheel` appears."""
    violations = []
    for name, text in texts.items():
        for number, line in _code_lines(text):
            violations.extend(_build_token_violations(f"{name}:{number}", line))
            if _PIP_WHEEL_RE.search(line):
                violations.append(f"{name}:{number}: pip wheel builds from the working tree")
    return violations


def release_permission_violations(release_text: str) -> list[str]:
    """`release.yml` declares a top-level permissions block with only `contents: read`."""
    lines = _code_lines(release_text)
    starts = [
        index for index, (_, line) in enumerate(lines) if re.match(r"^permissions:\s*$", line)
    ]
    if len(starts) != 1:
        return [f"release.yml has {len(starts)} top-level permissions blocks, expected 1"]
    entries = []
    for _, line in lines[starts[0] + 1 :]:
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")):
            break
        entries.append(re.sub(r"\s+", " ", line.strip()))
    if entries != ["contents: read"]:
        return [f"release.yml permissions entries are {entries}, expected ['contents: read']"]
    return []


def floor_parity_violations(ci_text: str, pyproject_text: str) -> list[str]:
    """The floor step's `setuptools==X` equals pyproject's declared `setuptools>=X`."""
    declared = re.findall(r"setuptools>=(\d+(?:\.\d+)*)", pyproject_text)
    if len(declared) != 1:
        return [f"pyproject.toml declares {len(declared)} setuptools floors, expected 1"]
    pinned = []
    for _, line in _code_lines(ci_text):
        for requirement in _pip_install_requirements(line):
            match = _FLOOR_RE.match(requirement)
            if match:
                pinned.append(match.group(1))
    if pinned != declared:
        return [f"floor step pins setuptools {pinned}, pyproject declares {declared}"]
    return []


def guide_violations(guide_text: str) -> list[str]:
    """The guide names a plain `-m build`, with the build-token rule, and the archive order."""
    commands: list[str] = []
    in_fence = False
    for line in guide_text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif in_fence:
            commands.append(line)
        else:
            commands.extend(re.findall(r"`([^`]+)`", line))
    build_commands = [command for command in commands if _build_invocations(command)]
    violations = []
    if not build_commands:
        violations.append("contributing guide names no `-m build` command")
    for command in build_commands:
        violations.extend(_build_token_violations(f"contributing guide `{command}`", command))
    prose = re.sub(r"\s+", " ", guide_text)
    if not re.search(r"source archive first and then the wheel from that archive", prose):
        violations.append("contributing guide does not say the wheel is built from the archive")
    return violations


def test_uses_refs_are_sha_pinned_with_version_comment() -> None:
    assert uses_violations(_read_workflows()) == []


def test_every_workflow_uses_ref_is_scanned() -> None:
    texts = _read_workflows()
    counts = {
        name: sum(1 for _, line in _code_lines(text) if _USES_RE.match(line))
        for name, text in texts.items()
    }
    assert counts == {"ci.yml": 12, "release.yml": 2, "pages.yml": 4}


def test_build_installs_are_one_exact_version() -> None:
    assert build_install_violations(_read_workflows()) == []


def test_builds_use_the_plain_form() -> None:
    assert build_form_violations(_read_workflows()) == []


def test_release_token_is_read_only() -> None:
    assert release_permission_violations(_read_workflows()["release.yml"]) == []


def test_floor_step_matches_declared_floor() -> None:
    assert floor_parity_violations(_read_workflows()["ci.yml"], PYPROJECT_PATH.read_text()) == []


def test_contributing_guide_build_guidance() -> None:
    assert guide_violations(GUIDE_PATH.read_text()) == []


def _replace_once(text: str, old: str, new: str) -> str:
    assert text.count(old) >= 1, old
    return text.replace(old, new, 1)


_CHECKOUT_PIN = "actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0"


@pytest.mark.parametrize(
    ("name", "old", "new"),
    [
        ("ci.yml", _CHECKOUT_PIN, "actions/checkout@v4"),
        ("ci.yml", " # v4.4.0", ""),
        (
            "pages.yml",
            "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
            "actions/checkout@" + "0" * 40,
        ),
    ],
    ids=["tag-ref", "deleted-version-comment", "different-sha-one-occurrence"],
)
def test_uses_rule_rejects_broken_pins(name: str, old: str, new: str) -> None:
    texts = _read_workflows()
    texts[name] = _replace_once(texts[name], old, new)
    assert uses_violations(texts)


@pytest.mark.parametrize(
    ("name", "old", "new"),
    [
        ("ci.yml", 'pip install "build==1.6.1"', 'pip install "build"'),
        ("release.yml", "pip install build==1.6.1", "pip install build"),
        ("release.yml", "pip install build==1.6.1", "pip install build>=1.6"),
        ("release.yml", "pip install build==1.6.1", "pip install build==1.6.0"),
    ],
    ids=["ci-floating", "release-floating", "release-range", "unequal"],
)
def test_build_install_rule_rejects_unpinned_or_unequal(name: str, old: str, new: str) -> None:
    texts = _read_workflows()
    texts[name] = _replace_once(texts[name], old, new)
    assert build_install_violations(texts)


@pytest.mark.parametrize(
    ("name", "old", "new"),
    [
        ("release.yml", "python3 -m build", "python3 -m build --wheel"),
        ("ci.yml", "python3 -m build\n", "python3 -m build -s\n"),
        ("ci.yml", "python3 -m build\n", "python3 -m build -w\n"),
        ("ci.yml", "python3 -m build\n", "python3 -m build --sdist\n"),
        ("ci.yml", "python3 -m build\n", "pip wheel .\n"),
    ],
    ids=["wheel", "short-s", "short-w", "sdist", "pip-wheel"],
)
def test_build_form_rule_rejects_single_artifact_builds(name: str, old: str, new: str) -> None:
    texts = _read_workflows()
    texts[name] = _replace_once(texts[name], old, new)
    assert build_form_violations(texts)


def test_build_form_rule_accepts_no_isolation_and_outdir() -> None:
    command = ".floor-venv/bin/python -m build --no-isolation --outdir floor-dist"
    assert _build_token_violations("probe", command) == []


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("permissions:\n  contents: read\n\n", ""),
        ("  contents: read\n", "  contents: write\n"),
        ("  contents: read\n", "  contents: read\n  id-token: write\n"),
    ],
    ids=["removed", "write", "widened"],
)
def test_permission_rule_rejects_missing_or_widened_block(old: str, new: str) -> None:
    release = _replace_once(_read_workflows()["release.yml"], old, new)
    assert release_permission_violations(release)


def test_floor_rule_rejects_unequal_pin() -> None:
    ci = _replace_once(_read_workflows()["ci.yml"], "setuptools==77.0.1", "setuptools==77.0.2")
    assert floor_parity_violations(ci, PYPROJECT_PATH.read_text())


def test_floor_rule_rejects_raised_declaration_alone() -> None:
    pyproject = _replace_once(PYPROJECT_PATH.read_text(), "setuptools>=77.0.1", "setuptools>=78")
    assert floor_parity_violations(_read_workflows()["ci.yml"], pyproject)


def test_guide_rule_rejects_wheel_flag_on_the_command() -> None:
    guide = _replace_once(GUIDE_PATH.read_text(), "`python -m build`", "`python -m build --wheel`")
    assert guide_violations(guide)


def test_guide_rule_rejects_missing_archive_sentence() -> None:
    guide = _replace_once(
        GUIDE_PATH.read_text(), "source archive first and then the wheel", "wheel"
    )
    assert guide_violations(guide)


def test_guide_rule_rejects_missing_build_command() -> None:
    guide = _replace_once(GUIDE_PATH.read_text(), "`python -m build`", "`make dist`")
    assert guide_violations(guide)
