"""Connection-string templates for service tasks.

A service manifest may set ``service_kind: "postgres"`` (or ``redis``,
``mongo``, ``mysql``, ``http``, ``tcp``). The UI uses :func:`connection_string`
to surface a copy-pasteable invocation pointed at the master's local
tunnel listener.
"""

from __future__ import annotations

KINDS: dict[str, str] = {
    "postgres": "psql -h localhost -p {port} -U postgres",
    "redis":    "redis-cli -p {port}",
    "mongo":    "mongosh mongodb://localhost:{port}",
    "mysql":    "mysql -h 127.0.0.1 -P {port}",
    "http":     "http://localhost:{port}",
    "tcp":      "localhost:{port}",
}


def connection_string(service_kind: str, port: int, *, tls: bool = False,
                      user: str = "", database: str = "") -> str:
    """Return a copy-pasteable connection string for *service_kind*.

    Unknown kinds fall through to the generic ``localhost:{port}`` form.
    when ``tls`` is set and the kind is ``http``, the
    string switches to ``https://``.

    (DBaaS): when ``user``/``database`` are given (a provisioned
    per-consumer login), they're folded into the SQL-client invocation so the
    consumer connects as their issued role. With both empty the output is
    unchanged from the bare-port templates above.
    """
    kind = str(service_kind or "tcp").lower()
    p = int(port)
    if tls and kind == "http":
        return f"https://localhost:{p}"
    if user or database:
        if kind == "postgres":
            db = f" {database}" if database else ""
            return f"psql -h localhost -p {p} -U {user or 'postgres'}{db}"
        if kind == "mysql":
            db = f" {database}" if database else ""
            return f"mysql -h 127.0.0.1 -P {p} -u {user or 'root'}{db}"
        if kind == "mongo":
            if user and database:
                return f"mongosh mongodb://{user}@localhost:{p}/{database}"
            return f"mongosh mongodb://localhost:{p}"
    template = KINDS.get(kind, KINDS["tcp"])
    return template.format(port=p)


__all__ = ["KINDS", "connection_string"]
