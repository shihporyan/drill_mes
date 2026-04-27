"""
Backup cleanup: delete local log backups older than retention period.

Scans backup_root for date-stamped subdirectories and removes those
exceeding backup_retention_days from settings.

Usage:
    python tools/cleanup.py           # Dry run (show what would be deleted)
    python tools/cleanup.py --execute # Actually delete
"""

import logging
import os
import shutil
import stat
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from parsers.base_parser import load_settings

logger = logging.getLogger(__name__)


def _force_writable_then_retry(func, path, exc_info):
    """rmtree onerror callback: clear read-only bit and retry.

    SMB-copied .Log files often arrive with the read-only attribute, which
    causes shutil.rmtree to fail with WinError 5 on Windows even though the
    user has full perms. Strip the bit and retry.
    """
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception as e:
        logger.error("Failed to delete %s after chmod: %s", path, e)


def cleanup_old_backups(dry_run=True, settings=None):
    """Delete backup directories older than retention period.

    Args:
        dry_run: If True, only log what would be deleted.
        settings: Optional settings dict.

    Returns:
        list: Paths that were (or would be) deleted.
    """
    if settings is None:
        settings = load_settings()

    backup_root = settings.get("backup_root", "C:\\DrillLogs")
    max_age_days = settings.get("backup_retention_days", 90)
    cutoff = time.time() - max_age_days * 86400

    deleted = []

    if not os.path.isdir(backup_root):
        logger.info("Backup root does not exist: %s", backup_root)
        return deleted

    for machine_dir in os.listdir(backup_root):
        machine_path = os.path.join(backup_root, machine_dir)
        if not os.path.isdir(machine_path):
            continue

        for date_dir in os.listdir(machine_path):
            date_path = os.path.join(machine_path, date_dir)
            if not os.path.isdir(date_path):
                continue

            try:
                mtime = os.path.getmtime(date_path)
            except OSError:
                continue

            if mtime < cutoff:
                age_days = int((time.time() - mtime) / 86400)
                if dry_run:
                    logger.info("[DRY RUN] Would delete: %s (%d days old)", date_path, age_days)
                else:
                    try:
                        shutil.rmtree(date_path, onerror=_force_writable_then_retry)
                        if os.path.exists(date_path):
                            logger.error("Failed to delete %s (still exists after rmtree)", date_path)
                            continue
                        logger.info("Deleted: %s (%d days old)", date_path, age_days)
                    except Exception as e:
                        logger.error("Failed to delete %s: %s", date_path, e)
                        continue
                deleted.append(date_path)

    logger.info("Cleanup %s: %d directories %s",
                "dry run" if dry_run else "complete",
                len(deleted),
                "would be deleted" if dry_run else "deleted")
    return deleted


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    execute = len(sys.argv) > 1 and sys.argv[1] == "--execute"
    if not execute:
        print("Dry run mode. Use --execute to actually delete files.")
    cleanup_old_backups(dry_run=not execute)
