"""Leaf helpers used across nexus. Stdlib only.

See ``README.md`` for the public API contract. Only the symbols re-exported
here are considered public; everything else in these modules is internal.
"""

from nexus.utils.fs import dir_size_bytes
from nexus.utils.hashing import content_hash, stable_hash
from nexus.utils.net import (
    client_host,
    env_flag,
    get_local_ip,
    is_private_or_loopback_host,
)
from nexus.utils.text import (
    MASKED_IP_PLACEHOLDER,
    mask_ips_in_log,
    prepare_multiline_command,
    safe_extractall,
    sanitize_shell_token,
    split_csv,
)
from nexus.utils.time import format_elapsed, now_epoch, timestamp

__all__ = [
    # time
    "timestamp",
    "now_epoch",
    "format_elapsed",
    # net
    "get_local_ip",
    "env_flag",
    "is_private_or_loopback_host",
    "client_host",
    # text
    "MASKED_IP_PLACEHOLDER",
    "mask_ips_in_log",
    "sanitize_shell_token",
    "prepare_multiline_command",
    "split_csv",
    "safe_extractall",
    # hashing
    "content_hash",
    "stable_hash",
    # fs
    "dir_size_bytes",
]
