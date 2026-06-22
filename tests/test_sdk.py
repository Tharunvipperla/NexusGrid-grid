"""D2 — SDK/CLI: OpenAPI operation listing, client request building, CLI dispatch."""

from __future__ import annotations

import json

import httpx
import pytest

from nexus.sdk import NexusClient, list_operations
from nexus.sdk import cli


SPEC = {
    "paths": {
        "/local/network": {
            "get": {"summary": "Network snapshot", "tags": ["Diagnostics"],
                    "operationId": "get_network"},
        },
        "/local/secrets": {
            "get": {"summary": "List secrets", "tags": ["Secrets"]},
            "post": {"summary": "Set secret", "tags": ["Secrets"]},
        },
        "/local/secrets/{name}": {
            "delete": {"summary": "Delete secret", "tags": ["Secrets"],
                       "parameters": [{"name": "name", "in": "path"}]},
        },
        "/local/x": {"parameters": [{"name": "shared"}]},  # non-method key ignored
    }
}


# ---- list_operations -------------------------------------------------------

def test_list_operations_flattens_and_sorts():
    ops = list_operations(SPEC)
    assert ("Diagnostics", "/local/network", "GET") == \
        (ops[0]["tag"], ops[0]["path"], ops[0]["method"])
    # Secrets group sorted by path then method.
    secrets = [o for o in ops if o["tag"] == "Secrets"]
    assert [(o["path"], o["method"]) for o in secrets] == [
        ("/local/secrets", "GET"), ("/local/secrets", "POST"),
        ("/local/secrets/{name}", "DELETE")]
    assert "shared" not in str(ops)  # the bare "parameters" key produced no op


def test_list_operations_tag_and_grep_filters():
    assert all(o["tag"] == "Secrets" for o in list_operations(SPEC, tag="secrets"))
    grep = list_operations(SPEC, grep="network")
    assert len(grep) == 1 and grep[0]["path"] == "/local/network"


def test_list_operations_captures_params_and_opid():
    d = next(o for o in list_operations(SPEC) if o["method"] == "DELETE")
    assert d["params"] == ["name"]
    g = next(o for o in list_operations(SPEC) if o["path"] == "/local/network")
    assert g["operation_id"] == "get_network"


# ---- NexusClient (mocked transport) ----------------------------------------

def _client(handler):
    return NexusClient("https://node:8000", "TOK",
                       transport=httpx.MockTransport(handler))


def test_client_attaches_token_and_parses_json():
    seen = {}

    def handler(req):
        seen["method"] = req.method
        seen["url"] = str(req.url)
        seen["token"] = req.headers.get("X-Local-Token")
        return httpx.Response(200, json={"ok": True})

    out = _client(handler).get("/local/network", params={"since": "5"})
    assert out == {"ok": True}
    assert seen["method"] == "GET"
    assert seen["token"] == "TOK"
    assert seen["url"] == "https://node:8000/local/network?since=5"


def test_client_post_sends_json_body():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"status": "ok"})

    out = _client(handler).post("/local/secrets", json={"name": "K", "value": "v"})
    assert out == {"status": "ok"}
    assert seen["body"] == {"name": "K", "value": "v"}


def test_client_raises_on_http_error():
    def handler(req):
        return httpx.Response(404, json={"detail": "nope"})

    with pytest.raises(httpx.HTTPStatusError):
        _client(handler).get("/local/missing")


def test_from_local_reads_token(monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.get_local_api_token", lambda: "FILE_TOK")
    c = NexusClient.from_local(base_url="http://127.0.0.1:8090")
    assert c.token == "FILE_TOK" and c.base_url == "http://127.0.0.1:8090"


# ---- CLI -------------------------------------------------------------------

def test_cli_parse_query_helper():
    assert cli._parse_query(["a=1", "b=2"]) == {"a": "1", "b": "2"}
    assert cli._parse_query([]) is None
    with pytest.raises(SystemExit):
        cli._parse_query(["bad"])


def test_cli_ops_lists_from_spec(monkeypatch, capsys):
    monkeypatch.setattr(NexusClient, "from_local",
                        classmethod(lambda cls, **kw: cls("http://x", "T")))
    monkeypatch.setattr(NexusClient, "openapi", lambda self: SPEC)
    rc = cli.main(["ops", "--tag", "Secrets"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "/local/secrets" in out and "Diagnostics" not in out


def test_cli_call_invokes_client(monkeypatch, capsys):
    monkeypatch.setattr(NexusClient, "from_local",
                        classmethod(lambda cls, **kw: cls("http://x", "T")))
    captured = {}

    def fake_request(self, method, path, params=None, json=None):
        captured.update(method=method, path=path, params=params, json=json)
        return {"ok": True}

    monkeypatch.setattr(NexusClient, "request", fake_request)
    rc = cli.main(["call", "POST", "/local/secrets", "--data", '{"name":"K"}',
                   "--query", "dry=1"])
    assert rc == 0
    assert captured["method"] == "POST" and captured["path"] == "/local/secrets"
    assert captured["json"] == {"name": "K"} and captured["params"] == {"dry": "1"}
    assert '"ok": true' in capsys.readouterr().out
