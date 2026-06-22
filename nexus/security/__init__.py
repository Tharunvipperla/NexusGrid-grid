"""Authentication, integrity, and threat scanning.

See ``README.md`` for the contract. Public surface re-exported below.
"""

from nexus.security.auth import (
    resolve_trusted_peer,
    verify_local_auth,
    verify_trusted_peer,
)
from nexus.security.crypto import (
    sign_bye,
    sign_bytes,
    verify_bye,
    verify_signature,
)
from nexus.security.entrypoint import (
    EntrypointError,
    validate_entrypoint,
    validate_setup_cmd,
)
from nexus.security.limits import (
    enforce_actual_size,
    enforce_content_length,
    get_max_result_bytes,
    get_max_ws_frame_bytes,
)
from nexus.security.profiles import PROFILES, get_docker_security_opts
from nexus.security.threat_scanner import (
    Finding,
    is_scan_required,
    scan_workspace_for_threats,
)
from nexus.security.tls import (
    compute_fingerprint,
    ensure_local_cert,
    fetch_peer_fingerprint,
    get_local_fingerprint,
)
from nexus.security.tokens import (
    get_local_api_token,
    get_signing_secret,
)

__all__ = [
    # crypto
    "sign_bytes",
    "verify_signature",
    "sign_bye",
    "verify_bye",
    # tokens
    "get_signing_secret",
    "get_local_api_token",
    # auth
    "verify_local_auth",
    "verify_trusted_peer",
    "resolve_trusted_peer",
    # threat scanner
    "Finding",
    "is_scan_required",
    "scan_workspace_for_threats",
    # profiles
    "PROFILES",
    "get_docker_security_opts",
    # limits
    "enforce_actual_size",
    "enforce_content_length",
    "get_max_result_bytes",
    "get_max_ws_frame_bytes",
    # entrypoint
    "EntrypointError",
    "validate_entrypoint",
    "validate_setup_cmd",
    # tls
    "compute_fingerprint",
    "ensure_local_cert",
    "fetch_peer_fingerprint",
    "get_local_fingerprint",
]
