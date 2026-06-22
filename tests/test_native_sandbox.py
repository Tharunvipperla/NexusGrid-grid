"""Native sandbox helper tests (Wave 4 Step 5 / item 2.8)."""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from nexus.core import LOCAL_SETTINGS
from nexus.runtime.native_sandbox import (
    SandboxUnavailable,
    assign_to_job_object,
    get_sandbox_mode,
    make_resource_limits,
    release_job_object,
    wrap_command_with_sandbox,
)


# ---------------------------------------------------------------------------
# get_sandbox_mode
# ---------------------------------------------------------------------------

@pytest.fixture
def restore_settings():
    saved = LOCAL_SETTINGS.get("native_sandbox_mode", "auto")
    yield
    LOCAL_SETTINGS["native_sandbox_mode"] = saved


def test_get_sandbox_mode_default(restore_settings):
    LOCAL_SETTINGS["native_sandbox_mode"] = "auto"
    assert get_sandbox_mode() == "auto"


def test_get_sandbox_mode_strict(restore_settings):
    LOCAL_SETTINGS["native_sandbox_mode"] = "strict"
    assert get_sandbox_mode() == "strict"


def test_get_sandbox_mode_off(restore_settings):
    LOCAL_SETTINGS["native_sandbox_mode"] = "off"
    assert get_sandbox_mode() == "off"


def test_get_sandbox_mode_invalid_falls_back(restore_settings):
    LOCAL_SETTINGS["native_sandbox_mode"] = "garbage"
    assert get_sandbox_mode() == "auto"


# ---------------------------------------------------------------------------
# wrap_command_with_sandbox
# ---------------------------------------------------------------------------

def test_wrap_off_returns_unchanged(tmp_path, restore_settings):
    LOCAL_SETTINGS["native_sandbox_mode"] = "off"
    cmd, log = wrap_command_with_sandbox(
        ["python", "main.py"], workspace_dir=str(tmp_path), profile="maximum"
    )
    assert cmd == ["python", "main.py"]
    assert "disabled" in log.lower()


def test_wrap_unavailable_auto_returns_unchanged(tmp_path, restore_settings):
    """Non-Linux or no bwrap in auto mode: passes through with a note."""
    LOCAL_SETTINGS["native_sandbox_mode"] = "auto"
    with patch("nexus.runtime.native_sandbox._bwrap_available", return_value=False):
        cmd, log = wrap_command_with_sandbox(
            ["python", "main.py"], workspace_dir=str(tmp_path), profile="maximum"
        )
    assert cmd == ["python", "main.py"]
    assert "bwrap not available" in log


def test_wrap_unavailable_strict_raises(tmp_path, restore_settings):
    LOCAL_SETTINGS["native_sandbox_mode"] = "strict"
    with patch("nexus.runtime.native_sandbox._bwrap_available", return_value=False):
        with pytest.raises(SandboxUnavailable):
            wrap_command_with_sandbox(
                ["python", "main.py"],
                workspace_dir=str(tmp_path),
                profile="maximum",
            )


def test_wrap_with_bwrap_prepends(tmp_path, restore_settings):
    LOCAL_SETTINGS["native_sandbox_mode"] = "auto"
    with patch("nexus.runtime.native_sandbox._bwrap_available", return_value=True):
        cmd, log = wrap_command_with_sandbox(
            ["python", "main.py"],
            workspace_dir=str(tmp_path),
            profile="maximum",
        )
    assert cmd[0] == "bwrap"
    assert "--die-with-parent" in cmd
    assert "--unshare-net" in cmd  # maximum profile -> network blocked
    # original command appears at the end after `--`
    assert cmd[-2] == "python"
    assert cmd[-1] == "main.py"
    assert "wrapped with bwrap" in log


def test_wrap_with_bwrap_standard_profile_allows_net(tmp_path, restore_settings):
    LOCAL_SETTINGS["native_sandbox_mode"] = "auto"
    with patch("nexus.runtime.native_sandbox._bwrap_available", return_value=True):
        cmd, _log = wrap_command_with_sandbox(
            ["python", "main.py"],
            workspace_dir=str(tmp_path),
            profile="standard",
        )
    assert "--unshare-net" not in cmd


def test_wrap_passes_through_env(tmp_path, monkeypatch, restore_settings):
    LOCAL_SETTINGS["native_sandbox_mode"] = "auto"
    monkeypatch.setenv("NEXUS_TEST_VAR", "hello")
    with patch("nexus.runtime.native_sandbox._bwrap_available", return_value=True):
        cmd, _log = wrap_command_with_sandbox(
            ["python", "main.py"],
            workspace_dir=str(tmp_path),
            profile="standard",
            extra_env_passthrough=("NEXUS_TEST_VAR",),
        )
    # --setenv NEXUS_TEST_VAR hello should appear consecutively
    joined = " ".join(cmd)
    assert "--setenv NEXUS_TEST_VAR hello" in joined


# ---------------------------------------------------------------------------
# make_resource_limits
# ---------------------------------------------------------------------------

def test_make_resource_limits_returns_none_on_windows():
    with patch.object(sys, "platform", "win32"):
        assert make_resource_limits(512) is None


def test_make_resource_limits_returns_callable_on_posix():
    with patch.object(sys, "platform", "linux"):
        fn = make_resource_limits(512)
    assert callable(fn)


# ---------------------------------------------------------------------------
# assign_to_job_object / release_job_object (Windows-only behavior)
# ---------------------------------------------------------------------------

def test_assign_to_job_object_noop_on_non_windows():
    with patch.object(sys, "platform", "linux"):
        assert assign_to_job_object(12345) is False


def test_release_job_object_silent_for_unknown_pid():
    # Should not raise even if pid was never assigned.
    release_job_object(999_999)
