"""B3 — result/artifact browser.

The existing ``/local/download/{task_id}`` zips a whole result directory. This
module lets the UI browse a completed task's output **per file**: list the
bundles under ``completed_tasks/``, list the files inside one, and resolve a
single file path safely (no traversal outside the bundle) for preview/download.

Filesystem-backed and read-only — it scans ``completed_tasks/`` directly, so it
works regardless of whether a matching task row still exists in the DB.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# Files this many bytes or smaller may be previewed inline as text by the UI.
TEXT_PREVIEW_MAX = 256 * 1024


def results_root() -> Path:
    from nexus.core.paths import BASE_DIR
    return BASE_DIR / "completed_tasks"


def _safe_task_id(task_id: str) -> str:
    """A task id is a single path segment — strip anything that could escape."""
    tid = str(task_id or "").strip()
    if not tid or tid in (".", ".."):
        return ""
    if "/" in tid or "\\" in tid or os.sep in tid:
        return ""
    return tid


def _dir_stats(d: Path) -> tuple[int, int, float]:
    """(file_count, total_bytes, latest_mtime) for a bundle dir."""
    count = total = 0
    latest = 0.0
    for root, _dirs, files in os.walk(d):
        for f in files:
            try:
                st = (Path(root) / f).stat()
            except OSError:
                continue
            count += 1
            total += st.st_size
            latest = max(latest, st.st_mtime)
    return count, total, latest


def list_bundles() -> list[dict]:
    """All result bundles, newest first."""
    root = results_root()
    if not root.is_dir():
        return []
    out: list[dict] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        count, total, latest = _dir_stats(entry)
        out.append({
            "task_id": entry.name,
            "file_count": count,
            "total_bytes": total,
            "modified_at": latest,
        })
    out.sort(key=lambda b: b["modified_at"], reverse=True)
    return out


def list_files(task_id: str) -> list[dict] | None:
    """Files in one bundle as ``{path, bytes}`` (POSIX-relative). ``None`` if the
    bundle doesn't exist."""
    tid = _safe_task_id(task_id)
    if not tid:
        return None
    bundle = results_root() / tid
    if not bundle.is_dir():
        return None
    out: list[dict] = []
    for root, _dirs, files in os.walk(bundle):
        for f in files:
            p = Path(root) / f
            try:
                size = p.stat().st_size
            except OSError:
                continue
            rel = p.relative_to(bundle).as_posix()
            out.append({"path": rel, "bytes": size})
    out.sort(key=lambda x: x["path"])
    return out


def resolve_file(task_id: str, rel_path: str) -> Path | None:
    """Resolve ``rel_path`` within a bundle to a real file, rejecting any path
    that escapes the bundle directory. ``None`` if invalid or not a file."""
    tid = _safe_task_id(task_id)
    if not tid:
        return None
    bundle = (results_root() / tid).resolve()
    if not bundle.is_dir():
        return None
    rel = str(rel_path or "").replace("\\", "/").lstrip("/")
    if not rel:
        return None
    target = (bundle / rel).resolve()
    try:
        target.relative_to(bundle)  # raises if it escaped via ../
    except ValueError:
        return None
    if not target.is_file():
        return None
    return target


def delete_bundle(task_id: str) -> bool:
    """Delete one result bundle directory. ``True`` if it existed and is gone."""
    tid = _safe_task_id(task_id)
    if not tid:
        return False
    bundle = (results_root() / tid).resolve()
    root = results_root().resolve()
    try:
        bundle.relative_to(root)  # never delete outside completed_tasks/
    except ValueError:
        return False
    if not bundle.is_dir():
        return False
    shutil.rmtree(bundle, ignore_errors=True)
    return not bundle.exists()


def write_log_artifact(task_id: str, lines: list[str]) -> str:
    """Write *lines* into the task's result bundle as a timestamped log file and
    return its bundle-relative path. Used by B4 to persist a live/streamed log
    buffer (esp. for services, which never produce a completed-task bundle) so
    it shows up in the result/artifact browser. Traversal-safe."""
    from datetime import datetime, timezone

    tid = _safe_task_id(task_id)
    if not tid:
        raise ValueError("invalid task id")
    root = results_root().resolve()
    bundle = (root / tid).resolve()
    bundle.relative_to(root)  # raises if tid tried to escape the results root
    logs_dir = bundle / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"live-log-{stamp}.log"
    (logs_dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"logs/{name}"


def delete_all_bundles() -> int:
    """Remove every result bundle. Returns the count deleted. Used by the
    telemetry "Clear database" wipe so artifacts go with the task rows."""
    root = results_root()
    if not root.is_dir():
        return 0
    n = 0
    for entry in list(root.iterdir()):
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
            if not entry.exists():
                n += 1
    return n


__all__ = [
    "TEXT_PREVIEW_MAX", "results_root",
    "list_bundles", "list_files", "resolve_file",
    "delete_bundle", "delete_all_bundles", "write_log_artifact",
]
