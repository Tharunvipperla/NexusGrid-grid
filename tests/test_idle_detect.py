"""Idle-detection auto-scaling tests (Wave 4 Step 8 / item 6.3)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from nexus.core import LOCAL_SETTINGS
from nexus.runtime import idle_detect


@pytest.fixture
def reset_idle_settings():
    saved = {
        "node_online": LOCAL_SETTINGS.get("node_online"),
        "idle_auto_accept": LOCAL_SETTINGS.get("idle_auto_accept"),
        "idle_threshold_sec": LOCAL_SETTINGS.get("idle_threshold_sec"),
    }
    yield
    LOCAL_SETTINGS.update(saved)


# ---------------------------------------------------------------------------
# is_idle
# ---------------------------------------------------------------------------

def test_is_idle_above_threshold_returns_true(reset_idle_settings):
    LOCAL_SETTINGS["idle_threshold_sec"] = 60
    with patch.object(idle_detect, "seconds_since_input", return_value=120.0):
        assert idle_detect.is_idle() is True


def test_is_idle_below_threshold_returns_false(reset_idle_settings):
    LOCAL_SETTINGS["idle_threshold_sec"] = 60
    with patch.object(idle_detect, "seconds_since_input", return_value=10.0):
        assert idle_detect.is_idle() is False


def test_is_idle_no_signal_treated_as_idle(reset_idle_settings):
    """Headless / Wayland / unsupported -> always-on by user pref."""
    with patch.object(idle_detect, "seconds_since_input", return_value=None):
        assert idle_detect.is_idle() is True


def test_is_idle_explicit_threshold_overrides_settings(reset_idle_settings):
    LOCAL_SETTINGS["idle_threshold_sec"] = 60
    with patch.object(idle_detect, "seconds_since_input", return_value=30.0):
        assert idle_detect.is_idle(threshold_sec=10) is True
        assert idle_detect.is_idle(threshold_sec=300) is False


# ---------------------------------------------------------------------------
# is_node_online_effective
# ---------------------------------------------------------------------------

def test_effective_off_when_user_pref_off(reset_idle_settings):
    LOCAL_SETTINGS["node_online"] = False
    LOCAL_SETTINGS["idle_auto_accept"] = True
    with patch.object(idle_detect, "is_idle", return_value=True):
        assert idle_detect.is_node_online_effective() is False


def test_effective_on_when_auto_accept_off(reset_idle_settings):
    """User pref wins when idle_auto_accept is disabled."""
    LOCAL_SETTINGS["node_online"] = True
    LOCAL_SETTINGS["idle_auto_accept"] = False
    with patch.object(idle_detect, "is_idle", return_value=False):
        assert idle_detect.is_node_online_effective() is True


def test_effective_gates_on_idle_when_auto_accept_on(reset_idle_settings):
    LOCAL_SETTINGS["node_online"] = True
    LOCAL_SETTINGS["idle_auto_accept"] = True
    with patch.object(idle_detect, "is_idle", return_value=True):
        assert idle_detect.is_node_online_effective() is True
    with patch.object(idle_detect, "is_idle", return_value=False):
        assert idle_detect.is_node_online_effective() is False


# ---------------------------------------------------------------------------
# Wave 14 — is_peer_link_allowed (decoupled from idle gate)
# ---------------------------------------------------------------------------

def test_peer_link_allowed_when_node_online(reset_idle_settings):
    LOCAL_SETTINGS["node_online"] = True
    LOCAL_SETTINGS["idle_auto_accept"] = True
    # Even when actively in use, peer links must stay open so storage and
    # control frames keep flowing.
    with patch.object(idle_detect, "is_idle", return_value=False):
        assert idle_detect.is_peer_link_allowed() is True


def test_peer_link_blocked_when_node_offline(reset_idle_settings):
    LOCAL_SETTINGS["node_online"] = False
    LOCAL_SETTINGS["idle_auto_accept"] = False
    with patch.object(idle_detect, "is_idle", return_value=True):
        assert idle_detect.is_peer_link_allowed() is False


def test_peer_link_independent_of_idle_setting(reset_idle_settings):
    """The two gates intentionally diverge under load: compute pauses, links don't."""
    LOCAL_SETTINGS["node_online"] = True
    LOCAL_SETTINGS["idle_auto_accept"] = True
    with patch.object(idle_detect, "is_idle", return_value=False):
        assert idle_detect.is_node_online_effective() is False
        assert idle_detect.is_peer_link_allowed() is True


# ---------------------------------------------------------------------------
# normalize_local_settings clamps idle_threshold_sec
# ---------------------------------------------------------------------------

def test_normalize_clamps_threshold_low():
    from nexus.core.config import normalize_local_settings

    out = normalize_local_settings({"idle_threshold_sec": 1})
    assert out["idle_threshold_sec"] == 30


def test_normalize_clamps_threshold_high():
    from nexus.core.config import normalize_local_settings

    out = normalize_local_settings({"idle_threshold_sec": 999_999})
    assert out["idle_threshold_sec"] == 86_400


def test_normalize_idle_auto_accept_default_false():
    from nexus.core.config import normalize_local_settings

    out = normalize_local_settings({})
    assert out["idle_auto_accept"] is False
