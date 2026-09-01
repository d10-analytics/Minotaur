#!/usr/bin/env bash
# Run the repository's GitHub Actions checks in fresh temporary environments.

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$script_dir/.."

python_bin="${PYTHON_BIN:-python3}"

usage() {
    cat <<'EOF'
Usage: scripts/run_ci.sh [all|test|lint|typecheck|package|browser]

Run one GitHub Actions lane locally. Each lane uses a fresh temporary virtual
environment and removes it when the lane finishes. "all" runs every lane.

Set PYTHON_BIN to use a different Python interpreter.
EOF
}

run_in_environment() (
    environment_dir="$(mktemp -d "${TMPDIR:-/tmp}/minotaur-ci.XXXXXX")"
    trap 'rm -rf "$environment_dir"' EXIT

    "$python_bin" -m venv "$environment_dir"
    ci_python="$environment_dir/bin/python"
    ci_pip="$environment_dir/bin/pip"

    "$@"
)

install_dev() {
    "$ci_pip" install --upgrade pip
    "$ci_pip" install -e ".[dev]"
}

test_lane() {
    install_dev
    if "$ci_python" -m pytest tests/ -v; then
        return 0
    else
        status=$?
    fi
    [[ "$status" -eq 5 ]]
}

lint_lane() {
    "$ci_pip" install ruff==0.16.3
    "$environment_dir/bin/ruff" check .
    "$environment_dir/bin/ruff" format --check .
}

typecheck_lane() {
    install_dev
    "$environment_dir/bin/mypy"
}

package_lane() {
    "$ci_pip" install --upgrade pip
    "$ci_pip" install .
    "$ci_python" -c 'from minotaur.graph_model.loading import schema; assert schema()["$id"] == "urn:minotaur:schemas:minotaur-graph:0.1.0"'
}

browser_lane() {
    "$ci_pip" install --upgrade pip
    "$ci_pip" install -e ".[dev,visualizer]"
    "$ci_python" -m playwright install chromium
    "$ci_python" -m pytest tests/test_visualizer_browser.py -v
}

run_lane() {
    case "$1" in
        test) run_in_environment test_lane ;;
        lint) run_in_environment lint_lane ;;
        typecheck) run_in_environment typecheck_lane ;;
        package) run_in_environment package_lane ;;
        browser) run_in_environment browser_lane ;;
        *)
            usage >&2
            return 2
            ;;
    esac
}

case "${1:-all}" in
    all)
        run_lane test
        run_lane lint
        run_lane typecheck
        run_lane package
        run_lane browser
        ;;
    --help|-h) usage ;;
    test|lint|typecheck|package|browser) run_lane "$1" ;;
    *)
        usage >&2
        exit 2
        ;;
esac
