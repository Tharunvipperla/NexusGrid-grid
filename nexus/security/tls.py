"""TLS cert/key bootstrap and fingerprint helpers.

The node ships TLS *by default*: on first start we
generate a self-signed RSA-2048 cert at ``BASE_DIR/.nexus_cert.pem`` +
``BASE_DIR/.nexus_key.pem`` (chmod 0o600), and uvicorn binds with them.
``--no-tls`` is the opt-out.

Because each node mints its own cert there is no PKI; trust is bootstrapped
by exchanging SHA-256 cert fingerprints during the existing peer
join handshake (``/peer/request_join`` + ``/peer/callback_accept``). On
subsequent outbound calls, :mod:`nexus.networking.peer_http` re-fetches the
served cert and compares against the stored fingerprint — mismatch is
treated as a connection failure.
"""

from __future__ import annotations

import datetime
import hashlib
import socket
import ssl
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from nexus.core.paths import BASE_DIR, secure_file_permissions

CERT_FILE = ".nexus_cert.pem"
KEY_FILE = ".nexus_key.pem"

_CERT_VALIDITY_DAYS = 365 * 10

_local_fingerprint: Optional[str] = None


def _resolve(filename: str) -> Path:
    return Path(BASE_DIR) / filename


def cert_path() -> Path:
    return _resolve(CERT_FILE)


def key_path() -> Path:
    return _resolve(KEY_FILE)


def _generate_self_signed(cert_dst: Path, key_dst: Path) -> None:
    """Generate a fresh RSA-2048 self-signed cert + key pair on disk."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    hostname = socket.gethostname() or "nexus-node"
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, hostname),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "NexusGrid"),
        ]
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(
            (now - datetime.timedelta(minutes=5)).replace(tzinfo=None)
        )
        .not_valid_after(
            (now + datetime.timedelta(days=_CERT_VALIDITY_DAYS)).replace(tzinfo=None)
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(hostname), x509.DNSName("localhost")]
            ),
            critical=False,
        )
        .sign(private_key=key, algorithm=hashes.SHA256())
    )

    key_dst.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    secure_file_permissions(key_dst)
    cert_dst.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    secure_file_permissions(cert_dst)


def ensure_local_cert() -> tuple[Path, Path]:
    """Return ``(cert_path, key_path)``, generating on first call if missing."""
    cert = cert_path()
    key = key_path()
    if cert.exists() and key.exists():
        return cert, key
    _generate_self_signed(cert, key)
    global _local_fingerprint
    _local_fingerprint = None  # force recomputation
    return cert, key


def compute_fingerprint(cert_pem: bytes | str) -> str:
    """Return the lowercase hex SHA-256 fingerprint of *cert_pem*.

    Accepts either PEM-encoded text or DER bytes. The fingerprint is taken
    over the DER representation so it matches what ``openssl x509 -fingerprint``
    reports.
    """
    if isinstance(cert_pem, str):
        cert_pem = cert_pem.encode("utf-8")
    cert = x509.load_pem_x509_certificate(cert_pem)
    der = cert.public_bytes(serialization.Encoding.DER)
    return hashlib.sha256(der).hexdigest()


def get_local_fingerprint() -> str:
    """Return our own cert's fingerprint, generating the cert if needed."""
    global _local_fingerprint
    if _local_fingerprint:
        return _local_fingerprint
    cert, _ = ensure_local_cert()
    _local_fingerprint = compute_fingerprint(cert.read_bytes())
    return _local_fingerprint


def fetch_peer_fingerprint(host: str, port: int, *, timeout: float = 3.0) -> str:
    """Open a brief TLS connection and return the served cert fingerprint.

    No certificate verification is performed (trust is established via
    fingerprint pinning, not a CA chain). Raises ``OSError`` on connection
    failure or non-TLS endpoints.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    with socket.create_connection((host, port), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    if not der:
        raise OSError("peer presented no certificate")
    return hashlib.sha256(der).hexdigest()


def _reset_for_testing() -> None:
    global _local_fingerprint
    _local_fingerprint = None


__all__ = [
    "CERT_FILE",
    "KEY_FILE",
    "cert_path",
    "key_path",
    "ensure_local_cert",
    "compute_fingerprint",
    "get_local_fingerprint",
    "fetch_peer_fingerprint",
]
