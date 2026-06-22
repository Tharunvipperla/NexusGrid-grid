"""Wave 14 — opt-out + capacity helpers + dynamic capability bit."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from nexus.core import LOCAL_SETTINGS
from nexus.runtime import capacity, foreign_storage_quota


@pytest.fixture(autouse=True)
def _restore_settings():
    snapshot = dict(LOCAL_SETTINGS)
    yield
    LOCAL_SETTINGS.clear()
    LOCAL_SETTINGS.update(snapshot)


def test_is_accepting_offers_default_true():
    LOCAL_SETTINGS.pop("foreign_storage_accept_offers", None)
    assert foreign_storage_quota.is_accepting_offers() is True


def test_is_accepting_offers_can_be_disabled():
    LOCAL_SETTINGS["foreign_storage_accept_offers"] = False
    assert foreign_storage_quota.is_accepting_offers() is False


def test_is_accepting_offers_handles_string():
    LOCAL_SETTINGS["foreign_storage_accept_offers"] = "off"
    assert foreign_storage_quota.is_accepting_offers() is False
    LOCAL_SETTINGS["foreign_storage_accept_offers"] = "true"
    assert foreign_storage_quota.is_accepting_offers() is True


def test_is_accepting_offers_couples_to_master_node_online_toggle():
    """``node_online=False`` must veto FS hosting too, not just compute.

    User toggled the master "Accept Network Tasks (Node Online)" off and
    expected available-hosts lists on peers to stop showing them as
    Accepting. Without coupling, the FS path ignores that switch.
    """
    LOCAL_SETTINGS["foreign_storage_accept_offers"] = True
    LOCAL_SETTINGS["node_online"] = False
    assert foreign_storage_quota.is_accepting_offers() is False
    LOCAL_SETTINGS["node_online"] = True
    assert foreign_storage_quota.is_accepting_offers() is True


def test_effective_free_caps_at_pledge_remaining():
    LOCAL_SETTINGS["storage_max_total_gb"] = 10
    LOCAL_SETTINGS["foreign_storage_disk_safety_gb"] = 0
    with patch.object(foreign_storage_quota, "used_gb", return_value=4.0), \
         patch.object(foreign_storage_quota, "disk_free_gb", return_value=500.0):
        # Pledge - used = 6, disk - safety = 500 → cap at 6.
        assert foreign_storage_quota.effective_free_gb() == pytest.approx(6.0)


def test_effective_free_caps_at_disk_when_smaller():
    LOCAL_SETTINGS["storage_max_total_gb"] = 1000
    LOCAL_SETTINGS["foreign_storage_disk_safety_gb"] = 1.0
    with patch.object(foreign_storage_quota, "used_gb", return_value=0.0), \
         patch.object(foreign_storage_quota, "disk_free_gb", return_value=2.5):
        # Disk - safety = 1.5 < pledge - used = 1000 → cap at 1.5.
        assert foreign_storage_quota.effective_free_gb() == pytest.approx(1.5)


def test_effective_free_clamps_to_zero_when_overcommitted():
    LOCAL_SETTINGS["storage_max_total_gb"] = 10
    LOCAL_SETTINGS["foreign_storage_disk_safety_gb"] = 1.0
    with patch.object(foreign_storage_quota, "used_gb", return_value=20.0), \
         patch.object(foreign_storage_quota, "disk_free_gb", return_value=0.5):
        assert foreign_storage_quota.effective_free_gb() == 0.0


def test_capability_bit_off_when_opted_out():
    LOCAL_SETTINGS["foreign_storage_accept_offers"] = False
    with patch.object(foreign_storage_quota, "effective_free_gb", return_value=999.0), \
         patch.object(foreign_storage_quota, "disk_free_gb", return_value=999.0):
        caps = capacity.local_capabilities()
        assert caps["foreign_storage"] is False
        assert caps["foreign_storage_free_gb"] == 0.0


def test_capability_bit_off_when_no_room():
    LOCAL_SETTINGS["foreign_storage_accept_offers"] = True
    LOCAL_SETTINGS["storage_max_total_gb"] = 5
    with patch.object(foreign_storage_quota, "effective_free_gb", return_value=0.0), \
         patch.object(foreign_storage_quota, "disk_free_gb", return_value=999.0):
        caps = capacity.local_capabilities()
        assert caps["foreign_storage"] is False


def test_capability_bit_on_when_accepting_with_room():
    LOCAL_SETTINGS["foreign_storage_accept_offers"] = True
    LOCAL_SETTINGS["storage_max_total_gb"] = 5
    with patch.object(foreign_storage_quota, "effective_free_gb", return_value=42.5), \
         patch.object(foreign_storage_quota, "disk_free_gb", return_value=999.0):
        caps = capacity.local_capabilities()
        assert caps["foreign_storage"] is True
        assert caps["foreign_storage_free_gb"] == 42.5


def test_auto_opt_out_when_disk_smaller_than_pledge():
    LOCAL_SETTINGS["foreign_storage_accept_offers"] = True
    LOCAL_SETTINGS["storage_max_total_gb"] = 5
    with patch.object(foreign_storage_quota, "disk_free_gb", return_value=2.0):
        assert foreign_storage_quota.auto_opt_out_reason() != ""
        assert foreign_storage_quota.is_effectively_accepting() is False


def test_no_auto_opt_out_when_disk_meets_pledge():
    LOCAL_SETTINGS["foreign_storage_accept_offers"] = True
    LOCAL_SETTINGS["storage_max_total_gb"] = 5
    with patch.object(foreign_storage_quota, "disk_free_gb", return_value=10.0):
        assert foreign_storage_quota.auto_opt_out_reason() == ""
        assert foreign_storage_quota.is_effectively_accepting() is True


def test_capability_bit_off_when_auto_opt_out_fires():
    LOCAL_SETTINGS["foreign_storage_accept_offers"] = True
    LOCAL_SETTINGS["storage_max_total_gb"] = 5
    with patch.object(foreign_storage_quota, "effective_free_gb", return_value=42.0), \
         patch.object(foreign_storage_quota, "disk_free_gb", return_value=2.0):
        caps = capacity.local_capabilities()
        assert caps["foreign_storage"] is False
        assert caps["foreign_storage_free_gb"] == 0.0
