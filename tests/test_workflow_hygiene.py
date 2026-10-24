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
PRE_COMMIT_PATH = REPO_ROOT / ".pre-commit-config.yaml"

_USES_KEY_RE = re.compile(r"^\s*(?:-\s+)?uses:")
_USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(?:\s+#\s*(\S.*?))?\s*$")
_PINNED_REF_RE = re.compile(r"^([\w.-]+/[\w./-]+)@([0-9a-f]{40})$")
_VERSION_COMMENT_RE = re.compile(r"^v\d+\.\d+\.\d+$")
_EXACT_BUILD_RE = re.compile(r"^build==(\d+(?:\.\d+)*)$")
_FLOOR_RE = re.compile(r"^setuptools==(\d+(?:\.\d+)*)$")
_FORBIDDEN_BUILD_TOKENS = frozenset({"--wheel", "-w", "--sdist", "-s"})
_EXACT_VERSION_RE = re.compile(r"^\d+(?:\.\d+)+$")
_PIP_WHEEL_RE = re.compile(r"\bpip3?\s+wheel\b")
_FOLDED_SCALAR_RE = re.compile(r":\s*>(?:[-+]?[1-9]?|[1-9][-+])(?:\s+#.*|\s*)$")


def _read_workflows() -> dict[str, str]:
    return {name: (WORKFLOW_DIR / name).read_text() for name in WORKFLOW_NAMES}


def _code_lines(text: str) -> list[tuple[int, str]]:
    """Return (line number, line) pairs with whole-line comments removed."""
    return [
        (number, line)
        for number, line in enumerate(text.splitlines(), start=1)
        if not line.lstrip().startswith("#")
    ]


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Return (first line number, command) pairs with continuations and folded scalars joined."""
    joined: list[tuple[int, str]] = []
    pending: tuple[int, str] | None = None
    folded: tuple[int, str, int] | None = None
    for number, line in _code_lines(text):
        if folded is not None:
            indent = len(line) - len(line.lstrip())
            if not line.strip() or indent > folded[2]:
                folded = (folded[0], (folded[1] + " " + line.strip()).strip(), folded[2])
                continue
            joined.append((folded[0], folded[1]))
            folded = None
        if _FOLDED_SCALAR_RE.search(line):
            folded = (number, line.rstrip(), len(line) - len(line.lstrip()))
            continue
        if pending is not None:
            number, line = pending[0], pending[1] + " " + line.strip()
            pending = None
        if line.rstrip().endswith("\\"):
            pending = (number, line.rstrip()[:-1].rstrip())
        else:
            joined.append((number, line))
    if pending is not None:
        joined.append(pending)
    if folded is not None:
        joined.append((folded[0], folded[1]))
    return joined


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
        if tokens[index] in {"pip", "pip3"} and tokens[index + 1] == "install":
            return [token for token in tokens[index + 2 :] if not token.startswith("-")]
    return []


def _installs_of_build(texts: dict[str, str]) -> list[tuple[str, int, str]]:
    found = []
    for name, text in texts.items():
        for number, line in _logical_lines(text):
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
            if not _USES_KEY_RE.match(line):
                continue
            where = f"{name}:{number}"
            match = _USES_RE.match(line)
            if not match:
                violations.append(f"{where}: unparseable uses line {line.strip()!r}")
                continue
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
        for number, line in _logical_lines(text):
            violations.extend(_build_token_violations(f"{name}:{number}", line))
            if _PIP_WHEEL_RE.search(line):
                violations.append(f"{name}:{number}: pip wheel builds from the working tree")
    return violations


def release_permission_violations(release_text: str) -> list[str]:
    """`release.yml` has one top-level `contents: read` block and no job-level permissions."""
    lines = _code_lines(release_text)
    job_level = [number for number, line in lines if re.match(r"^\s+permissions:", line)]
    if job_level:
        return [f"release.yml:{job_level[0]}: job-level permissions override the read-only token"]
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
    for _, line in _logical_lines(ci_text):
        for requirement in _pip_install_requirements(line):
            match = _FLOOR_RE.match(requirement)
            if match:
                pinned.append(match.group(1))
    if pinned != declared:
        return [f"floor step pins setuptools {pinned}, pyproject declares {declared}"]
    interpreters = set()
    for _, line in _logical_lines(ci_text):
        tokens = _tokens(line)
        for index in range(1, len(tokens) - 2):
            if tokens[index : index + 2] == ["-m", "pip"] and any(
                _FLOOR_RE.match(requirement) for requirement in _pip_install_requirements(line)
            ):
                interpreters.add(tokens[index - 1])
    if len(interpreters) != 1:
        return [f"floor step installs setuptools through {sorted(interpreters)}, expected 1"]
    violations = []
    builds = 0
    for number, line in _logical_lines(ci_text):
        tokens = _tokens(line)
        for index in range(1, len(tokens) - 1):
            if tokens[index : index + 2] == ["-m", "build"] and tokens[index - 1] in interpreters:
                builds += 1
                if "--no-isolation" not in tokens[index + 2 :]:
                    violations.append(
                        f"ci.yml:{number}: floor build does not use --no-isolation, so it"
                        " would build with the newest setuptools"
                    )
    if not builds:
        violations.append("floor step's interpreter has no -m build call")
    return violations


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


def ruff_pin_violations(pre_commit_text: str, pyproject_text: str, guide_text: str) -> list[str]:
    """The hook `rev`, the `dev` extra pin, and the guide sentence name one exact Ruff version."""
    revs = []
    lines = _code_lines(pre_commit_text)
    for index, (_, line) in enumerate(lines):
        if re.search(r"repo:\s*\S*ruff-pre-commit\b", line):
            for _, following in lines[index + 1 :]:
                if re.match(r"^\s*-\s+repo:", following):
                    break
                match = re.match(r"^\s*rev:\s*v(\S+)\s*$", following)
                if match:
                    revs.append(match.group(1))
    dev_extra = re.search(r"^dev\s*=\s*\[(.*?)^\]", pyproject_text, re.MULTILINE | re.DOTALL)
    pins = re.findall(r'"ruff([^"]*)"', dev_extra.group(1)) if dev_extra else []
    sentences = re.findall(
        r"pre-commit hooks and the `dev` extra both pin Ruff (\S+) exactly",
        re.sub(r"\s+", " ", guide_text),
    )
    violations = []
    if len(revs) != 1:
        violations.append(
            f".pre-commit-config.yaml has {len(revs)} ruff-pre-commit revs, expected 1"
        )
    if len(pins) != 1:
        violations.append(f"pyproject.toml dev extra has {len(pins)} ruff requirements, expected 1")
    if len(sentences) != 1:
        violations.append(f"contributing guide has {len(sentences)} Ruff pin sentences, expected 1")
    if violations:
        return violations
    dev = pins[0][2:] if pins[0].startswith("==") else None
    if dev is None:
        return [f"dev extra Ruff requirement 'ruff{pins[0]}' is not an exact == pin"]
    for label, version in (("hook rev", revs[0]), ("dev extra pin", dev), ("guide", sentences[0])):
        if not _EXACT_VERSION_RE.match(version):
            violations.append(f"{label} '{version}' is not an exact version")
    if not violations and not revs[0] == dev == sentences[0]:
        violations.append(
            f"Ruff versions differ: hook {revs[0]}, dev extra {dev}, guide {sentences[0]}"
        )
    return violations


def test_uses_refs_are_sha_pinned_with_version_comment() -> None:
    assert uses_violations(_read_workflows()) == []


def test_every_workflow_uses_line_is_seen_by_the_rule() -> None:
    for name, text in _read_workflows().items():
        mentioned = [number for number, line in _code_lines(text) if "uses:" in line]
        keyed = [number for number, line in _code_lines(text) if _USES_KEY_RE.match(line)]
        assert keyed, f"{name} has no uses lines"
        assert keyed == mentioned, name


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


def test_contributing_guide_describes_full_package_verification() -> None:
    prose = re.sub(r"\s+", " ", GUIDE_PATH.read_text())
    for claim in (
        "build the source archive and the wheel",
        "check both artifacts against the tracked files and the README metadata",
        "install the wheel and the source archive in clean environments",
        "build at the declared setuptools floor",
    ):
        assert claim in prose, claim


def _replace_once(text: str, old: str, new: str) -> str:
    assert text.count(old) >= 1, old
    return text.replace(old, new, 1)


_FOLDED_HEADERS = (">- # c", "> #c", ">2", ">-2", ">2-")

_CHECKOUT_PIN = "actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0"


def test_uses_rule_rejects_newly_added_unparseable_line() -> None:
    texts = _read_workflows()
    texts["pages.yml"] += "      - uses: actions/cache@v4 #\n"
    violations = uses_violations(texts)
    assert any("unparseable" in violation for violation in violations)


def test_uses_rule_rejects_newly_added_unpinned_action() -> None:
    texts = _read_workflows()
    texts["pages.yml"] += "      - uses: actions/cache@v4\n"
    assert uses_violations(texts)


def test_continued_install_form_is_parsed_as_one_command() -> None:
    texts = {"ci.yml": 'run: python3 -m pip install \\\n  "build==1.6.1"\n'}
    assert [r for _, _, r in _installs_of_build(texts)] == ["build==1.6.1"]


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
        ("ci.yml", _CHECKOUT_PIN, _CHECKOUT_PIN.replace("v4.4.0", "v4.4.1")),
        ("ci.yml", _CHECKOUT_PIN, _CHECKOUT_PIN + " #"),
        ("ci.yml", _CHECKOUT_PIN, _CHECKOUT_PIN.replace("# v4.4.0", "# stable")),
    ],
    ids=[
        "tag-ref",
        "deleted-version-comment",
        "different-sha-one-occurrence",
        "different-version-comment-one-occurrence",
        "trailing-bare-comment-marker",
        "malformed-version-comment",
    ],
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
        ("ci.yml", 'python3 -m pip install "build==1.6.1"', "pip3 install build"),
        (
            "ci.yml",
            'python3 -m pip install "build==1.6.1"',
            "python3 -m pip install \\\n            build",
        ),
        (
            "ci.yml",
            'run: |\n          python3 -m pip install "build==1.6.1"\n          python3 -m build\n',
            "run: >-\n          python3 -m pip install\n          build\n",
        ),
        (
            "release.yml",
            "pip install build==1.6.1\n          python3 -m build",
            "pip install build==1.6.1\n          python3 -m build\n"
            "      - name: Unpinned\n        run: >\n          python3 -m pip install\n"
            "          build\n",
        ),
        *[
            (
                "release.yml",
                "python3 -m build",
                f"python3 -m build\n      - run: {header}\n          python3 -m pip install\n"
                "          build",
            )
            for header in _FOLDED_HEADERS
        ],
    ],
    ids=[
        "ci-floating",
        "release-floating",
        "release-range",
        "unequal",
        "pip3-unpinned",
        "continued-unpinned",
        "folded-unpinned-replaces-pinned",
        "folded-unpinned-beside-pinned",
        *[f"folded-header-{header}-unpinned" for header in _FOLDED_HEADERS],
    ],
)
def test_build_install_rule_rejects_unpinned_or_unequal(name: str, old: str, new: str) -> None:
    texts = _read_workflows()
    texts[name] = _replace_once(texts[name], old, new)
    violations = build_install_violations(texts)
    assert any("not exact" in v or "disagree" in v for v in violations), violations


@pytest.mark.parametrize(
    ("name", "old", "new"),
    [
        ("release.yml", "python3 -m build", "python3 -m build --wheel"),
        ("ci.yml", "python3 -m build\n", "python3 -m build -s\n"),
        ("ci.yml", "python3 -m build\n", "python3 -m build -w\n"),
        ("ci.yml", "python3 -m build\n", "python3 -m build --sdist\n"),
        ("ci.yml", "python3 -m build\n", "pip wheel .\n"),
        (
            "ci.yml",
            "python3 -m build\n",
            "python3 -m build\n      - run: >-\n          python3 -m build\n          --wheel\n",
        ),
        (
            "release.yml",
            "python3 -m build",
            "python3 -m build\n      - run: >\n          python3 -m build\n          -s",
        ),
        *[
            (
                "release.yml",
                "python3 -m build",
                f"python3 -m build\n      - run: {header}\n          python3 -m build\n"
                "          --wheel",
            )
            for header in _FOLDED_HEADERS
        ],
    ],
    ids=["wheel", "short-s", "short-w", "sdist", "pip-wheel", "folded-wheel", "folded-short-s"]
    + [f"folded-header-{header}-wheel" for header in _FOLDED_HEADERS],
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
        (
            "    runs-on: ubuntu-latest\n",
            "    runs-on: ubuntu-latest\n    permissions:\n      contents: write\n",
        ),
    ],
    ids=["removed", "write", "widened", "job-level-write"],
)
def test_permission_rule_rejects_missing_or_widened_block(old: str, new: str) -> None:
    release = _replace_once(_read_workflows()["release.yml"], old, new)
    assert release_permission_violations(release)


def test_floor_rule_rejects_unequal_pin() -> None:
    ci = _replace_once(_read_workflows()["ci.yml"], "setuptools==77.0.1", "setuptools==77.0.2")
    assert floor_parity_violations(ci, PYPROJECT_PATH.read_text())


def test_floor_rule_rejects_isolated_floor_build() -> None:
    ci = _replace_once(
        _read_workflows()["ci.yml"], "-m build --no-isolation --outdir", "-m build --outdir"
    )
    assert any(
        "--no-isolation" in v for v in floor_parity_violations(ci, PYPROJECT_PATH.read_text())
    )


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


def test_ruff_pins_are_exact_equal_and_named_in_the_guide() -> None:
    assert (
        ruff_pin_violations(
            PRE_COMMIT_PATH.read_text(), PYPROJECT_PATH.read_text(), GUIDE_PATH.read_text()
        )
        == []
    )


@pytest.mark.parametrize(
    ("target", "old", "new"),
    [
        ("pre-commit", "rev: v0.17.2", "rev: v0.17.3"),
        ("pyproject", '"ruff==0.17.2"', '"ruff==0.17.3"'),
        ("pyproject", '"ruff==0.17.2"', '"ruff>=0.17.2"'),
        ("pre-commit", "rev: v0.17.2", "rev: v0.17"),
        ("guide", "pin Ruff 0.17.2 exactly", "pin Ruff 0.16.5 exactly"),
        ("guide", "both pin Ruff 0.17.2 exactly", "use the same Ruff version"),
        (
            "pyproject",
            '    "ruff==0.17.2",\n]\nvisualizer = [\n',
            ']\nvisualizer = [\n    "ruff==0.17.2",\n',
        ),
    ],
    ids=[
        "hook-moved-alone",
        "dev-moved-alone",
        "dev-returned-to-range",
        "hook-not-exact",
        "sentence-names-another-version",
        "sentence-removed",
        "pin-moved-out-of-dev-extra",
    ],
)
def test_ruff_pin_rule_rejects_divergence(target: str, old: str, new: str) -> None:
    texts = {
        "pre-commit": PRE_COMMIT_PATH.read_text(),
        "pyproject": PYPROJECT_PATH.read_text(),
        "guide": GUIDE_PATH.read_text(),
    }
    texts[target] = _replace_once(texts[target], old, new)
    assert ruff_pin_violations(texts["pre-commit"], texts["pyproject"], texts["guide"])
