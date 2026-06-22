"""C7 — one-click local database-engine bring-up for DBaaS.

(:mod:`nexus.runtime.db_provider`) provisions a per-consumer database on
an engine the host *already runs*, given an admin DSN. C7 removes that setup
step: start a managed engine container with one click and get back the admin DSN
to drop into the service's ``db_provider`` config. From there W71 mints
per-consumer databases as before.

The spec table + DSN builder here are pure (unit-tested). ``start_engine`` is the
only part that needs Docker — it launches the container, waits for the port to
open, and returns the connection details.
"""

from __future__ import annotations

import logging
import secrets
import socket
import time

_log = logging.getLogger("nexus.runtime.db_engine")


# engine -> launch spec. ``password_env`` is the image's admin-password env var
# (None when the password is passed via the command, e.g. redis). ``extra_env``
# carries any fixed env the image needs (e.g. mongo's root username).
_ENGINES: dict[str, dict] = {
    "postgres": {
        "image": "postgres:16-alpine", "port": 5432,
        "password_env": "POSTGRES_PASSWORD",
        "dsn": "postgresql://postgres:{pw}@{host}:{port}/postgres", "kind": "postgres",
    },
    "mysql": {
        "image": "mysql:8.4", "port": 3306,
        "password_env": "MYSQL_ROOT_PASSWORD",
        "dsn": "mysql://root:{pw}@{host}:{port}", "kind": "mysql",
    },
    "redis": {
        "image": "redis:7-alpine", "port": 6379, "password_env": None,
        "dsn": "redis://:{pw}@{host}:{port}", "kind": "redis",
    },
    "mongo": {
        "image": "mongo:7", "port": 27017,
        "password_env": "MONGO_INITDB_ROOT_PASSWORD",
        "extra_env": {"MONGO_INITDB_ROOT_USERNAME": "root"},
        "dsn": "mongodb://root:{pw}@{host}:{port}", "kind": "mongo",
    },
}


def list_engines() -> list[str]:
    """Engines this node can bring up with one click."""
    return sorted(_ENGINES)


def build_admin_dsn(engine: str, host: str, port: int, password: str) -> str:
    """Admin connection string for a started *engine* (pure)."""
    spec = _ENGINES.get((engine or "").strip().lower())
    if not spec:
        raise ValueError(f"unsupported engine '{engine}'")
    return spec["dsn"].format(pw=password, host=host, port=int(port))


def engine_container_spec(engine: str, password: str, host_port: int, name: str) -> dict:
    """Pure ``docker run`` kwargs for an engine container — published only on
    loopback, with a restart policy so it survives a node restart."""
    spec = _ENGINES.get((engine or "").strip().lower())
    if not spec:
        raise ValueError(f"unsupported engine '{engine}'")
    env = dict(spec.get("extra_env") or {})
    if spec["password_env"]:
        env[spec["password_env"]] = password
    kwargs: dict = {
        "image": spec["image"],
        "name": name,
        "detach": True,
        "environment": env,
        "ports": {f"{spec['port']}/tcp": ("127.0.0.1", int(host_port))},
        "restart_policy": {"Name": "unless-stopped"},
        "labels": {"nexus.dbaas.engine": spec["kind"]},
    }
    if (engine or "").strip().lower() == "redis":
        kwargs["command"] = ["redis-server", "--requirepass", password]
    return kwargs


def _free_loopback_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _wait_port(host: str, port: int, timeout: float = 45.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            time.sleep(1)
    return False


def start_engine(engine: str) -> dict:
    """Launch a managed *engine* container on a free loopback port and return
    ``{engine, kind, container, container_id, host, port, admin_dsn, ready}``.
    Blocking (Docker pull/run/wait) — call via ``asyncio.to_thread``."""
    engine = (engine or "").strip().lower()
    spec = _ENGINES.get(engine)
    if not spec:
        raise ValueError(f"unsupported engine '{engine}'")
    from nexus.runtime.docker_client import get_docker_client

    client = get_docker_client()
    password = secrets.token_urlsafe(18)
    host_port = _free_loopback_port()
    name = f"nexus-db-{engine}-{secrets.token_hex(4)}"
    try:
        client.images.get(spec["image"])
    except Exception:
        client.images.pull(spec["image"])
    container = client.containers.run(
        **engine_container_spec(engine, password, host_port, name)
    )
    ready = _wait_port("127.0.0.1", host_port)
    return {
        "engine": engine,
        "kind": spec["kind"],
        "container": name,
        "container_id": getattr(container, "id", ""),
        "host": "127.0.0.1",
        "port": host_port,
        "admin_dsn": build_admin_dsn(engine, "127.0.0.1", host_port, password),
        "ready": ready,
    }


def stop_engine(container: str) -> bool:
    """Stop + remove a managed engine container by name. Only acts on containers
    we labelled (``nexus.dbaas.engine``) so it can't be used to kill arbitrary
    containers. Returns True if one was removed."""
    from nexus.runtime.docker_client import get_docker_client

    client = get_docker_client()
    try:
        c = client.containers.get(container)
    except Exception:
        return False
    if "nexus.dbaas.engine" not in (c.labels or {}):
        return False
    try:
        c.stop(timeout=5)
    except Exception:
        pass
    try:
        c.remove(force=True)
    except Exception:
        pass
    return True


__all__ = [
    "list_engines", "build_admin_dsn", "engine_container_spec",
    "start_engine", "stop_engine",
]
