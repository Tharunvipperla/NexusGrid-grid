"""SQLAlchemy-backed SQLite persistence.

See ``README.md`` for the contract. Public surface re-exported below.
"""

from nexus.storage.database import (
    dispose,
    get_engine,
    get_session,
    init_db,
)
from nexus.storage.models import (
    SCHEMA_VERSION,
    AuditEvent,
    Base,
    CloudCredential,
    ForeignStorageDBGrace,
    ForeignStorageDeposit,
    LocalConfigRecord,
    Peer,
    PresenceEvent,
    Secret,
    TaskRecord,
)
from nexus.storage.repositories import (
    get_peer_by_ip,
    list_peers,
    load_local_settings_from_db,
    persist_resolved_ip,
    save_local_settings_to_db,
    seed_identity_mappings,
)

__all__ = [
    # database
    "init_db",
    "dispose",
    "get_engine",
    "get_session",
    # models
    "Base",
    "SCHEMA_VERSION",
    "TaskRecord",
    "Peer",
    "LocalConfigRecord",
    "AuditEvent",
    "PresenceEvent",
    "ForeignStorageDeposit",
    "ForeignStorageDBGrace",
    "CloudCredential",
    "Secret",
    # repositories
    "load_local_settings_from_db",
    "save_local_settings_to_db",
    "get_peer_by_ip",
    "list_peers",
    "persist_resolved_ip",
    "seed_identity_mappings",
]
