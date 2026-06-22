"""D2 — minimal Python SDK client for a NexusGrid node's local API.

A thin, dependency-light wrapper around the node's HTTP API: it attaches the
``X-Local-Token`` header, talks to ``<base_url>/local/...``, and returns parsed
JSON. For fully-typed clients, generate one from the node's ``/openapi.json``
with any standard tool (see the API screen's "SDK & CLI" card) — this client is
the quick, hand-rolled option for scripts and the CLI.
"""

from __future__ import annotations


class NexusClient:
    def __init__(self, base_url: str, token: str, *,
                 verify: bool = False, timeout: float = 30.0, transport=None):
        # verify defaults False: a local node serves a self-signed cert over
        # https; the connection is to loopback, so this is intentional.
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._verify = verify
        self._timeout = timeout
        self._transport = transport

    @classmethod
    def from_local(cls, base_url: str = "https://127.0.0.1:8000",
                   token: str | None = None, **kw) -> "NexusClient":
        """Build a client for the local node, reading the API token from
        ``.nexus_local_token`` when not supplied."""
        if token is None:
            from nexus.security.tokens import get_local_api_token
            token = get_local_api_token()
        return cls(base_url, token, **kw)

    def request(self, method: str, path: str, *, params: dict | None = None,
                json: dict | list | None = None):
        import httpx

        url = self.base_url + (path if path.startswith("/") else "/" + path)
        headers = {"X-Local-Token": self.token}
        with httpx.Client(verify=self._verify, timeout=self._timeout,
                          transport=self._transport) as c:
            res = c.request(method.upper(), url, params=params, json=json,
                            headers=headers)
            res.raise_for_status()
            if "application/json" in res.headers.get("content-type", ""):
                return res.json()
            return res.text

    def get(self, path: str, **kw):
        return self.request("GET", path, **kw)

    def post(self, path: str, json=None, **kw):
        return self.request("POST", path, json=json, **kw)

    def put(self, path: str, json=None, **kw):
        return self.request("PUT", path, json=json, **kw)

    def delete(self, path: str, **kw):
        return self.request("DELETE", path, **kw)

    def openapi(self) -> dict:
        return self.request("GET", "/openapi.json")


__all__ = ["NexusClient"]
