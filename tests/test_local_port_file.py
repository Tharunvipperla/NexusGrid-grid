"""The node publishes its bound port to ``.nexus_local_port`` next to the local
token, so local clients (the VS Code extension, a CLI) discover it instead of
assuming the default 8000. Guards ``nexus.security.tokens.write_local_port``.
"""

from __future__ import annotations

from nexus.security import tokens


def test_writes_port_file(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    tokens.write_local_port(8123)
    assert (tmp_path / tokens.LOCAL_PORT_FILE).read_text(encoding="utf-8") == "8123"


def test_overwrites_on_each_call(tmp_path, monkeypatch):
    # Each startup rewrites it, so the file always reflects the latest run.
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    tokens.write_local_port(8000)
    tokens.write_local_port(9001)
    assert (tmp_path / tokens.LOCAL_PORT_FILE).read_text(encoding="utf-8") == "9001"


def test_sits_next_to_local_token(tmp_path, monkeypatch):
    # The extension finds the token and the port by the same dir-walk, so both
    # must live in the same directory.
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    tokens._reset_for_testing()
    try:
        tokens.get_local_api_token()  # creates .nexus_local_token
        tokens.write_local_port(8000)
        assert (tmp_path / tokens.LOCAL_TOKEN_FILE).exists()
        assert (tmp_path / tokens.LOCAL_PORT_FILE).exists()
    finally:
        tokens._reset_for_testing()


def test_port_is_stringified_int(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    tokens.write_local_port(8001)
    content = (tmp_path / tokens.LOCAL_PORT_FILE).read_text(encoding="utf-8")
    assert content.isdigit() and int(content) == 8001
