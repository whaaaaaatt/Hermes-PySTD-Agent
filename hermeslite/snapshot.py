"""Quick state snapshots for rollback.

Creates timestamped copies of key state files under
~/.hermes-lite/snapshots/{YYYYMMDD-HHMMSS}[-label]/.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List

SNAPSHOT_DIR = "snapshots"
STATE_FILES = ["config.json", "active_profile", "cron.jsonl"]
DB_FILE = "state.db"
MAX_SNAPSHOTS = 20


def _get_snapshot_dir() -> Path:
    from .paths import get_hermes_home
    return get_hermes_home() / SNAPSHOT_DIR


def create_snapshot(label: str = "") -> Dict[str, Any]:
    """Create a snapshot. Returns manifest dict."""
    snap_base = _get_snapshot_dir()
    snap_base.mkdir(parents=True, exist_ok=True)

    ts = time.strftime("%Y%m%d-%H%M%S")
    name = f"{ts}{'-' + label if label else ''}"
    snap_dir = snap_base / name
    snap_dir.mkdir(parents=True, exist_ok=True)

    from .paths import get_hermes_home
    home = get_hermes_home()
    files: Dict[str, int] = {}

    for fname in STATE_FILES:
        src = home / fname
        if src.exists():
            dst = snap_dir / fname
            shutil.copy2(src, dst)
            files[fname] = dst.stat().st_size

    db_src = home / DB_FILE
    if db_src.exists():
        db_dst = snap_dir / DB_FILE
        try:
            src_conn = sqlite3.connect(str(db_src))
            dst_conn = sqlite3.connect(str(db_dst))
            src_conn.backup(dst_conn)
            src_conn.close()
            dst_conn.close()
            files[DB_FILE] = db_dst.stat().st_size
        except Exception:
            shutil.copy2(db_src, db_dst)
            files[DB_FILE] = db_dst.stat().st_size

    manifest = {
        "id": name,
        "timestamp": time.time(),
        "label": label,
        "file_count": len(files),
        "total_size": sum(files.values()),
        "files": files,
    }
    (snap_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    _prune_snapshots(keep=MAX_SNAPSHOTS)
    return manifest


def list_snapshots(limit: int = 20) -> List[Dict[str, Any]]:
    """List recent snapshots."""
    snap_base = _get_snapshot_dir()
    if not snap_base.exists():
        return []
    results: List[Dict[str, Any]] = []
    for d in sorted(snap_base.iterdir(), reverse=True):
        manifest_path = d / "manifest.json"
        if manifest_path.exists():
            try:
                m = json.loads(manifest_path.read_text(encoding="utf-8"))
                results.append(m)
            except Exception:
                pass
        if len(results) >= limit:
            break
    return results


def restore_snapshot(snapshot_id: str) -> bool:
    """Restore a snapshot by ID."""
    snap_dir = _get_snapshot_dir() / snapshot_id
    manifest_path = snap_dir / "manifest.json"
    if not manifest_path.exists():
        return False

    from .paths import get_hermes_home
    home = get_hermes_home()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    for fname in manifest.get("files", {}):
        src = snap_dir / fname
        dst = home / fname
        if src.exists():
            shutil.copy2(src, dst)

    return True


def _prune_snapshots(keep: int = MAX_SNAPSHOTS) -> int:
    """Remove old snapshots beyond keep limit. Returns count removed."""
    snap_base = _get_snapshot_dir()
    if not snap_base.exists():
        return 0
    dirs = sorted(
        [d for d in snap_base.iterdir() if d.is_dir() and (d / "manifest.json").exists()],
        key=lambda d: d.name,
        reverse=True,
    )
    removed = 0
    for d in dirs[keep:]:
        shutil.rmtree(d)
        removed += 1
    return removed
