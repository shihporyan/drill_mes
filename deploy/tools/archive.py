"""
SQLite database auto-archiver.

Checks if drill_monitor.db exceeds the configured size threshold and
moves records older than 6 months to an archive database.

Usage:
    python tools/archive.py           # Check and archive if needed
    python tools/archive.py --force   # Force archive regardless of size
"""

import logging
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from parsers.base_parser import load_settings, get_db_path, check_db_archive

logger = logging.getLogger(__name__)


def run_archive(force=False):
    """Run the archive check and process.

    Args:
        force: If True, archive regardless of database size.
    """
    settings = load_settings()
    db_path = get_db_path(settings)

    if not os.path.exists(db_path):
        logger.info("Database not found: %s", db_path)
        return

    size_mb = os.path.getsize(db_path) / (1024 * 1024)
    threshold = settings.get("db_archive_threshold_mb", 500)

    logger.info("Database size: %.1f MB (threshold: %d MB)", size_mb, threshold)

    if force or size_mb >= threshold:
        if force and size_mb < threshold:
            logger.info("Force mode: archiving despite being under threshold")
        check_db_archive(settings)
    else:
        logger.info("No archiving needed.")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    force = len(sys.argv) > 1 and sys.argv[1] == "--force"
    run_archive(force=force)
