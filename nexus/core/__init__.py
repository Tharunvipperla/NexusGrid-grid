"""Foundation layer: config, constants, identity, shared state, event bus.

See ``README.md`` for the contract. Public API is re-exported below;
everything else in this package is internal.
"""

from nexus.core import events
from nexus.core.config import (
    DEFAULT_LOCAL_SETTINGS,
    LOCAL_SETTINGS,
    get_settings,
    normalize_bool,
    normalize_list_field,
    normalize_local_settings,
)
from nexus.core.constants import (
    ALLOWED_TRANSITIONS,
    DEFAULT_BIND_HOST,
    DEFAULT_DISCOVERY_PORT,
    DEFAULT_GRID_KEY,
    DEFAULT_HTTP_PORT,
    MAX_LOG_LINES,
    PEER_PRESENCE_TIMEOUT,
    TASK_STATES,
    TERMINAL_STATES,
)
from nexus.core.identity import (
    NODE_UUID,
    clear_mappings,
    fmt_peer,
    generate_random_display_name,
    get_node_identity,
    get_node_port,
    get_or_create_node_uuid,
    register_peer_uuid,
    resolve_ip_to_uuid,
    resolve_uuid_to_ip,
    set_node_port,
    set_persist_hook,
    snapshot_mappings,
)
from nexus.core.paths import (
    BASE_DIR,
    cache_dir,
    get_resource_dir,
    secure_file_permissions,
)
from nexus.core.state import STATE, SharedState

__all__ = [
    # config
    "DEFAULT_LOCAL_SETTINGS",
    "LOCAL_SETTINGS",
    "get_settings",
    "normalize_bool",
    "normalize_list_field",
    "normalize_local_settings",
    # constants
    "ALLOWED_TRANSITIONS",
    "DEFAULT_BIND_HOST",
    "DEFAULT_DISCOVERY_PORT",
    "DEFAULT_GRID_KEY",
    "DEFAULT_HTTP_PORT",
    "MAX_LOG_LINES",
    "PEER_PRESENCE_TIMEOUT",
    "TASK_STATES",
    "TERMINAL_STATES",
    # identity
    "NODE_UUID",
    "clear_mappings",
    "fmt_peer",
    "generate_random_display_name",
    "get_node_identity",
    "get_node_port",
    "get_or_create_node_uuid",
    "register_peer_uuid",
    "resolve_ip_to_uuid",
    "resolve_uuid_to_ip",
    "set_node_port",
    "set_persist_hook",
    "snapshot_mappings",
    # paths
    "BASE_DIR",
    "cache_dir",
    "get_resource_dir",
    "secure_file_permissions",
    # state
    "STATE",
    "SharedState",
    # events
    "events",
]
