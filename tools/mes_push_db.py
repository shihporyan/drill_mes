#!/usr/bin/env python3
"""Manual MES DB push.

Use when MES needs the latest drill data immediately and the daily
scheduled push hasn't fired (or doesn't exist yet). Single-shot:

    1. (optional) run `main.py --once` to make sure DB has the latest LOGs
    2. VACUUM INTO an atomic .db snapshot (won't conflict with running main.py)
    3. gzip the snapshot
    4. HTTP POST it to the MES receiver at /upload/<filename>
    5. keep last N snapshots locally for audit/replay, delete older

Spec: notes/drill_push_dev_spec.md (§3 API, §6 test plan)
Config: settings.json `mes_push` block (url, token_file, snapshot_dir, ...)

Usage:
    python tools/mes_push_db.py                     # full flow
    python tools/mes_push_db.py --no-refresh        # skip main.py --once
    python tools/mes_push_db.py --dry-run           # snapshot + gzip, no push
    python tools/mes_push_db.py --label hotfix-wo-A # add label to filename

Exit codes:
    0   success
    10  main.py --once failed
    11  main.py --once timed out
    20  VACUUM INTO failed
    21  gzip failed
    30  token file missing
    31  token file empty
    40  HTTP error (server reachable, returned non-2xx)
    41  network/transport error
    42  MES response not parseable as JSON
    43  MES returned status != ok
"""

import argparse
import gzip
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from parsers.base_parser import load_settings, get_db_path  # noqa: E402


def resolve_path(value: str) -> Path:
    """Resolve a path relative to PROJECT_ROOT if not absolute."""
    p = Path(value)
    return p if p.is_absolute() else PROJECT_ROOT / p


def vacuum_snapshot(src_db: Path, dest_db: Path) -> None:
    """Atomic SQLite snapshot via `VACUUM INTO`.

    Safe to run while main.py is writing — VACUUM INTO takes a shared
    read lock and waits for any in-flight write to finish.
    """
    if dest_db.exists():
        dest_db.unlink()
    # VACUUM INTO does not support parameter binding; escape single quotes
    # in the path defensively even though our paths come from config.
    escaped = str(dest_db).replace("'", "''")
    conn = sqlite3.connect(str(src_db))
    try:
        conn.execute(f"VACUUM INTO '{escaped}'")
    finally:
        conn.close()


def gzip_file(src: Path, dest: Path, level: int = 6) -> None:
    with open(src, "rb") as fin, gzip.open(dest, "wb", compresslevel=level) as fout:
        shutil.copyfileobj(fin, fout, length=1024 * 1024)


def cleanup_old_snapshots(snapshot_dir: Path, keep: int) -> list:
    files = sorted(snapshot_dir.glob("drill_snapshot_*.db.gz"))
    if len(files) <= keep:
        return []
    to_delete = files[: len(files) - keep]
    for f in to_delete:
        try:
            f.unlink()
        except OSError:
            pass
    return to_delete


def push_file(file_path: Path, base_url: str, token: str, timeout: int):
    body = file_path.read_bytes()
    full_url = f"{base_url.rstrip('/')}/upload/{file_path.name}"
    req = urllib.request.Request(
        full_url,
        data=body,
        method="POST",
        headers={"X-Token": token, "Content-Type": "application/gzip"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def setup_logger(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    log = logging.getLogger("mes_push_db")
    log.setLevel(logging.INFO)
    log.handlers = []  # idempotent if run multiple times in same process
    fh = logging.FileHandler(str(log_path), encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt))
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter(fmt))
    log.addHandler(sh)
    return log


def main() -> int:
    ap = argparse.ArgumentParser(description="Manual MES DB push")
    ap.add_argument("--no-refresh", action="store_true",
                    help="Skip running main.py --once before snapshot")
    ap.add_argument("--dry-run", action="store_true",
                    help="Snapshot + gzip only, do not push")
    ap.add_argument("--label", default="",
                    help="Append label to filename, e.g. 'hotfix-wo-A'")
    args = ap.parse_args()

    settings = load_settings()
    mes_cfg = settings.get("mes_push") or {}
    if not args.dry_run and not mes_cfg.get("url"):
        print("ERROR: settings.mes_push.url not configured", file=sys.stderr)
        return 30

    snapshot_dir = resolve_path(mes_cfg.get("snapshot_dir", "snapshots"))
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    log = setup_logger(snapshot_dir / "push.log")

    db_path = Path(get_db_path(settings))
    if not db_path.exists():
        log.error("DB not found: %s", db_path)
        return 20

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    label_part = ""
    if args.label:
        safe_label = "".join(c for c in args.label if c.isalnum() or c in "-_")
        if safe_label:
            label_part = f"_{safe_label}"
    base_name = f"drill_snapshot_{timestamp}{label_part}"
    raw_snap = snapshot_dir / f"{base_name}.db"
    gz_snap = snapshot_dir / f"{base_name}.db.gz"

    log.info("=== push start === target=%s dry_run=%s refresh=%s",
             gz_snap.name, args.dry_run, not args.no_refresh)

    # 1. Refresh DB
    if not args.no_refresh:
        log.info("running main.py --once to refresh DB...")
        env = os.environ.copy()
        t0 = time.time()
        try:
            subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "main.py"), "--once"],
                cwd=str(PROJECT_ROOT),
                env=env,
                check=True,
                timeout=900,
            )
        except subprocess.CalledProcessError as e:
            log.error("main.py --once failed: rc=%d", e.returncode)
            return 10
        except subprocess.TimeoutExpired:
            log.error("main.py --once timed out (>900s)")
            return 11
        log.info("refresh OK in %.1fs", time.time() - t0)

    # 2. VACUUM INTO snapshot
    log.info("VACUUM INTO %s (src=%s)", raw_snap.name, db_path)
    t0 = time.time()
    try:
        vacuum_snapshot(db_path, raw_snap)
    except sqlite3.Error as e:
        log.error("VACUUM INTO failed: %s", e)
        return 20
    raw_size = raw_snap.stat().st_size
    log.info("VACUUM done: %d bytes (%.1fs)", raw_size, time.time() - t0)

    # 3. gzip
    log.info("gzip → %s", gz_snap.name)
    t0 = time.time()
    try:
        gzip_file(raw_snap, gz_snap)
    except OSError as e:
        log.error("gzip failed: %s", e)
        return 21
    finally:
        raw_snap.unlink(missing_ok=True)
    gz_size = gz_snap.stat().st_size
    ratio = (gz_size / raw_size * 100) if raw_size else 0
    log.info("gzip done: %d bytes (%.1f%% of raw, %.1fs)",
             gz_size, ratio, time.time() - t0)

    if args.dry_run:
        log.info("=== dry-run OK; snapshot kept at %s ===", gz_snap)
        return 0

    # 4. Load token
    token_path = resolve_path(mes_cfg.get("token_file", ""))
    if not token_path.exists():
        log.error("token file missing: %s", token_path)
        return 30
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        log.error("token file empty: %s", token_path)
        return 31

    # 5. Push
    url = mes_cfg["url"]
    timeout = int(mes_cfg.get("timeout_seconds", 120))
    log.info("POST %s/upload/%s (timeout=%ds)", url, gz_snap.name, timeout)
    t0 = time.time()
    try:
        status, body = push_file(gz_snap, url, token, timeout)
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        log.error("HTTP %d %s: %s", e.code, e.reason, err_body[:500])
        return 40
    except urllib.error.URLError as e:
        log.error("network error: %s", e.reason)
        return 41
    except Exception as e:
        log.error("transport error: %r", e)
        return 41

    elapsed = time.time() - t0
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        log.error("non-JSON response (status=%d): %s", status, body[:500])
        return 42

    if status != 200 or payload.get("status") != "ok":
        log.error("MES rejected: status=%d payload=%s", status, payload)
        return 43

    log.info("push OK: bytes=%s saved_to=%s (%.1fs)",
             payload.get("bytes"), payload.get("saved_to"), elapsed)

    # 6. Cleanup old snapshots
    keep = int(mes_cfg.get("keep_snapshots", 3))
    deleted = cleanup_old_snapshots(snapshot_dir, keep)
    if deleted:
        log.info("cleaned %d old snapshot(s): %s",
                 len(deleted), [f.name for f in deleted])

    log.info("=== push end OK ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
