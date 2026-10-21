# Minotaur repository instructions

## Environment and verification

- Minotaur supports Python 3.10 or newer; CI tests 3.11 and 3.12, and mypy
  targets 3.11 (`pyproject.toml`).
- Give each checkout its own virtual environment with the `dev` extra:
  `python3 -m venv .venv`, then `.venv/bin/python -m pip install -e ".[dev]"`
  (Linux/macOS paths; see [Quick start](README.md#quick-start) for Windows).
- An editable install imports the `src/` of the checkout it was installed
  from. In another clone or worktree, create that checkout's own `.venv`;
  another checkout's environment tests the other checkout's code.
- Shells that do not keep an activated environment call tools by path:
  `.venv/bin/python -m pytest <paths> -q`, `.venv/bin/ruff check .`,
  `.venv/bin/ruff format --check .`, and `.venv/bin/mypy`.
- Use focused pytest selections, lint, format, and type checks for feedback.
  Full verification is the GitHub-hosted CI described in
  [Development setup and checks](docs/guides/contributing.md#development-setup-and-checks),
  including when to run the full group before merge. Local results and
  pre-commit hooks are feedback, not verification evidence.

## Required project references

- Before design, implementation or review, follow the Reading path in
  [docs/systems/README.md](docs/systems/README.md#reading-path).

## Core conventions

- Analysis never imports or runs the analyzed project, and generated HTML
  makes no network requests ([README](README.md)).
- A change to analyzer output follows
  [Changing analyzer output](docs/guides/contributing.md#changing-analyzer-output).
- Adding, moving, or removing a module, or changing the dependencies between
  systems, follows the
  [system map rule](docs/systems/README.md#repository-wide-rules).
- Tests that read the documentation pin its claims, and
  `tests/test_system_docs.py` checks every same-repository `.md` link and
  anchor. Change a documented claim and its test together, and test
  user-visible contracts through real entry points.
- Mark wall-clock-gated tests `slow`. Browser tests skip when Playwright (the
  `visualizer` extra) is not installed, unless
  `MINOTAUR_VISUALIZER_BROWSER_REQUIRED=1` is set.

## Public repository

- This repository is public. Never commit credentials, developer-local paths,
  machine-specific configuration, or content from private repositories.
