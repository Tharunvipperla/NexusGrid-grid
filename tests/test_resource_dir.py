"""Resource-dir resolution must stay independent of the writable data dir.

Regression: running from source with ``--data-dir`` / ``NEXUS_DATA_DIR`` set
relocated ``BASE_DIR``, and ``get_resource_dir()`` followed it — so the UI
assets were looked up under the data dir and the app returned 500 on ``/app``.
Bundled read-only resources always live next to the code.
"""

from pathlib import Path

from nexus.core.paths import get_resource_dir


def test_resource_dir_points_at_source_tree():
    rd = get_resource_dir()
    # The UI shell and the React bundle source live under the resource dir.
    assert (rd / "nexus" / "ui" / "index.html").exists()
    assert (rd / "webui" / "index.html").exists()


def test_resource_dir_ignores_data_dir_override(monkeypatch, tmp_path):
    # Even with the data dir relocated, resources resolve to the source tree.
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    rd = get_resource_dir()
    assert rd != Path(tmp_path)
    assert (rd / "nexus" / "ui" / "index.html").exists()
