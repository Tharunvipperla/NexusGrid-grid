"""Depositor IP/copyright consent for cloud task-data sources.

The depositor must accept this once before the master will dispatch any
task whose manifest references ``data_sources`` or ``workspace_source``
(i.e. cloud-sourced inputs). The accepted version is persisted in
``LOCAL_SETTINGS`` so it survives restarts; bumping the version constant
forces re-acceptance.

Modelled on :mod:`nexus.security.foreign_storage_terms`.
"""

from __future__ import annotations

from nexus.core import LOCAL_SETTINGS

TASK_DATA_TERMS_VERSION = "v1"

TASK_DATA_TERMS_V1 = (
    "I confirm the data I attach to tasks does not include licensed, "
    "IP-protected, or government-restricted content I am not authorized "
    "to share. I will not replicate or redistribute content received "
    "from other peers when running tasks for them."
)


def current_terms_text() -> str:
    """Return the latest task-data terms text."""
    return TASK_DATA_TERMS_V1


def current_version() -> str:
    """Return the latest version identifier (used for accept tracking)."""
    return TASK_DATA_TERMS_VERSION


def accepted_version() -> str:
    """Return the version currently accepted by this node, or ''."""
    return str(LOCAL_SETTINGS.get("task_data_terms_accepted_version") or "")


def is_current_accepted() -> bool:
    """True iff the user has accepted the latest version."""
    return accepted_version() == current_version()


__all__ = [
    "TASK_DATA_TERMS_VERSION",
    "TASK_DATA_TERMS_V1",
    "current_terms_text",
    "current_version",
    "accepted_version",
    "is_current_accepted",
]
