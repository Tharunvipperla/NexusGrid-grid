"""Service endpoint helpers (Wave 4 Step 9c).

These tests cover the lightweight projection / template logic that powers
``/local/services`` without spinning up the full FastAPI app + DB. End-to-end
endpoint coverage is exercised manually per the Step 9 verification plan.
"""

from __future__ import annotations

import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from nexus.core import STATE
from nexus.runtime.service_kinds import KINDS, connection_string


@pytest.fixture(autouse=True)
def reset_state():
    STATE.service_records.clear()
    STATE.service_tunnels.clear()
    yield
    STATE.service_records.clear()
    STATE.service_tunnels.clear()


# ---------------------------------------------------------------------------
# connection_string templates
# ---------------------------------------------------------------------------

def test_connection_string_postgres():
    assert connection_string("postgres", 15432) == (
        "psql -h localhost -p 15432 -U postgres"
    )


def test_connection_string_redis():
    assert connection_string("redis", 16379) == "redis-cli -p 16379"


def test_connection_string_mongo():
    assert connection_string("mongo", 17017) == (
        "mongosh mongodb://localhost:17017"
    )


def test_connection_string_mysql():
    assert connection_string("mysql", 13306) == (
        "mysql -h 127.0.0.1 -P 13306"
    )


def test_connection_string_http():
    assert connection_string("http", 18080) == "http://localhost:18080"


def test_connection_string_tcp_default():
    assert connection_string("tcp", 19000) == "localhost:19000"


def test_connection_string_unknown_falls_through_to_tcp():
    assert connection_string("kafka", 19092) == "localhost:19092"


def test_connection_string_case_insensitive():
    assert connection_string("POSTGRES", 5432) == (
        "psql -h localhost -p 5432 -U postgres"
    )


def test_connection_string_empty_kind_uses_tcp():
    assert connection_string("", 1234) == "localhost:1234"


def test_kinds_table_lists_expected_protocols():
    # Sanity guard so a typo in the table is caught at test time.
    assert set(KINDS) == {"postgres", "redis", "mongo", "mysql", "http", "tcp"}


# ---------------------------------------------------------------------------
# _read_service_manifest
# ---------------------------------------------------------------------------

def _zip_with_manifest(manifest: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("task.json", json.dumps(manifest))
    return buf.getvalue()


def test_read_service_manifest_returns_none_for_non_service():
    from nexus.api.local import _read_service_manifest

    payload = _zip_with_manifest({"runtime": "docker", "image": "alpine"})
    task = SimpleNamespace(id="t-docker-1", payload=payload)
    assert _read_service_manifest(task) is None


def test_read_service_manifest_returns_dict_for_service():
    from nexus.api.local import _read_service_manifest

    payload = _zip_with_manifest(
        {"runtime": "service", "image": "redis:7", "expose_ports": [6379]}
    )
    task = SimpleNamespace(id="t-svc-1", payload=payload)
    manifest = _read_service_manifest(task)
    assert manifest is not None
    assert manifest["image"] == "redis:7"
    assert manifest["expose_ports"] == [6379]


def test_read_service_manifest_case_insensitive_runtime():
    from nexus.api.local import _read_service_manifest

    payload = _zip_with_manifest({"runtime": "SERVICE", "image": "x"})
    task = SimpleNamespace(id="t-svc-2", payload=payload)
    assert _read_service_manifest(task) is not None


# ---------------------------------------------------------------------------
# _service_status_summary projection
# ---------------------------------------------------------------------------

def test_service_status_summary_active_with_listener():
    from nexus.api.local import _service_status_summary

    STATE.service_tunnels["t1"] = {"port": 49152, "streams": {}}
    task = SimpleNamespace(
        id="t1", worker="worker-A", status="serving", payload=b""
    )
    manifest = {
        "image": "redis:7",
        "service_kind": "redis",
        "expose_ports": [6379],
    }
    out = _service_status_summary(task, manifest)
    assert out["task_id"] == "t1"
    assert out["worker"] == "worker-A"
    assert out["image"] == "redis:7"
    assert out["service_kind"] == "redis"
    assert out["expose_ports"] == [6379]
    assert out["container_port"] == 6379
    assert out["local_port"] == 49152
    assert out["connection_string"] == "redis-cli -p 49152"
    assert out["status"] == "active"
    assert out["raw_status"] == "serving"


def test_service_status_summary_no_listener_yields_blank_connection():
    from nexus.api.local import _service_status_summary

    task = SimpleNamespace(
        id="t2", worker="worker-A", status="serving", payload=b""
    )
    manifest = {"image": "redis:7", "service_kind": "redis", "expose_ports": [6379]}
    out = _service_status_summary(task, manifest)
    assert out["local_port"] == 0
    assert out["connection_string"] == ""
    assert out["status"] == "active"


def test_service_status_summary_terminal_task_not_active():
    from nexus.api.local import _service_status_summary

    task = SimpleNamespace(
        id="t3", worker="worker-A", status="completed", payload=b""
    )
    manifest = {"image": "x", "service_kind": "tcp", "expose_ports": [9000]}
    out = _service_status_summary(task, manifest)
    assert out["status"] == "completed"
    assert out["raw_status"] == "completed"


def test_service_status_summary_no_worker_not_active():
    from nexus.api.local import _service_status_summary

    task = SimpleNamespace(id="t4", worker="", status="processing", payload=b"")
    manifest = {"image": "x", "service_kind": "tcp", "expose_ports": [9000]}
    out = _service_status_summary(task, manifest)
    assert out["status"] == "processing"
    assert out["worker"] == ""


def test_service_status_summary_no_ports_uses_zero_container_port():
    from nexus.api.local import _service_status_summary

    task = SimpleNamespace(
        id="t5", worker="worker-A", status="serving", payload=b""
    )
    manifest = {"image": "x", "service_kind": "tcp", "expose_ports": []}
    out = _service_status_summary(task, manifest)
    assert out["container_port"] == 0
    assert out["expose_ports"] == []
