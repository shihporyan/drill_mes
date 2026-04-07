"""
Base parser module with shared logic for all parser types.

Provides common utilities for:
- Loading config (machines, settings)
- Database connection management
- Incremental parse progress tracking
- File overwrite detection
- SQLite auto-archiving
"""

import json
import os
import logging
import sqlite3
import shutil
import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
logger = logging.getLogger(__name__)


def load_machines_config():
    """Load machine list from config/machines.json.

    Returns:
        dict: Full machines config including machine list, SMB settings.
    """
    path = os.path.join(PROJECT_ROOT, "config", "machines.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_settings():
    """Load application settings from config/settings.json.

    Returns:
        dict: Settings dictionary.
    """
    path = os.path.join(PROJECT_ROOT, "config", "settings.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_enabled_machines(machines_config):
    """Filter and return only enabled machines.

    Args:
        machines_config: Full machines config dict.

    Returns:
        list: List of machine dicts where enabled=True.
    """
    return [m for m in machines_config["machines"] if m.get("enabled", False)]


def get_db_path(settings=None):
    """Resolve the database file path.

    Args:
        settings: Optional settings dict. Loads from file if None.

    Returns:
        str: Absolute path to the SQLite database.
    """
    if settings is None:
        settings = load_settings()
    db_path = settings.get("db_path", "drill_monitor.db")
    if not os.path.isabs(db_path):
        db_path = os.path.join(PROJECT_ROOT, db_path)
    return db_path


def get_db_connection(db_path=None):
    """Create a SQLite connection with WAL mode enabled.

    Args:
        db_path: Optional path. Resolves from settings if None.

    Returns:
        sqlite3.Connection: Database connection.
    """
    if db_path is None:
        db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def check_file_overwrite(conn, machine_id, day_prefix, current_file_size):
    """Detect if a log file has been overwritten (monthly cycle).

    When file_size shrinks, the file was overwritten -> reset parse progress.

    Args:
        conn: SQLite connection.
        machine_id: Machine identifier (e.g. 'M13').
        day_prefix: Two-digit day string (e.g. '06').
        current_file_size: Current file size in bytes.

    Returns:
        bool: True if file was overwritten (progress reset needed).
    """
    cursor = conn.execute(
        "SELECT file_size FROM parse_progress WHERE machine_id=? AND day_prefix=?",
        (machine_id, day_prefix),
    )
    row = cursor.fetchone()
    if row and current_file_size < row[0]:
        logger.warning(
            "File overwrite detected for %s day=%s (old=%d, new=%d). Resetting progress.",
            machine_id, day_prefix, row[0], current_file_size,
        )
        conn.execute(
            "DELETE FROM parse_progress WHERE machine_id=? AND day_prefix=?",
            (machine_id, day_prefix),
        )
        # Also clear hourly data for this machine+day since we re-parse from scratch
        # The day_prefix alone is not enough to determine the date, so we leave
        # hourly cleanup to the caller which knows the actual date from the log rows.
        conn.commit()
        return True
    return False


def get_parse_progress(conn, machine_id, day_prefix):
    """Get the last parsed line number and file size for incremental parsing.

    Args:
        conn: SQLite connection.
        machine_id: Machine identifier.
        day_prefix: Two-digit day string.

    Returns:
        tuple: (last_line, file_size) or (0, 0) if no progress recorded.
    """
    cursor = conn.execute(
        "SELECT last_line, file_size FROM parse_progress "
        "WHERE machine_id=? AND day_prefix=?",
        (machine_id, day_prefix),
    )
    row = cursor.fetchone()
    if row:
        return row[0], row[1]
    return 0, 0


def update_parse_progress(conn, machine_id, day_prefix, last_line, last_timestamp, file_size):
    """Update parse progress after processing lines.

    Args:
        conn: SQLite connection.
        machine_id: Machine identifier.
        day_prefix: Two-digit day string.
        last_line: Last processed line number.
        last_timestamp: ISO timestamp of last processed row.
        file_size: Current file size in bytes.
    """
    conn.execute(
        "INSERT INTO parse_progress (machine_id, day_prefix, last_line, last_timestamp, file_size) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(machine_id, day_prefix) DO UPDATE SET "
        "last_line=excluded.last_line, last_timestamp=excluded.last_timestamp, "
        "file_size=excluded.file_size",
        (machine_id, day_prefix, last_line, last_timestamp, file_size),
    )
    conn.commit()


def check_db_archive(settings=None):
    """Archive old data if database exceeds size threshold.

    Moves records older than 6 months to archive_YYYY.db.

    Args:
        settings: Optional settings dict.
    """
    if settings is None:
        settings = load_settings()

    db_path = get_db_path(settings)
    threshold_mb = settings.get("db_archive_threshold_mb", 500)

    if not os.path.exists(db_path):
        return

    size_mb = os.path.getsize(db_path) / (1024 * 1024)
    if size_mb < threshold_mb:
        return

    logger.info("Database size %.1f MB exceeds threshold %d MB. Starting archive...", size_mb, threshold_mb)

    today = datetime.date.today()
    cutoff_date = (today - datetime.timedelta(days=180)).isoformat()
    archive_year = (today - datetime.timedelta(days=180)).year
    archive_db_path = os.path.join(
        os.path.dirname(db_path),
        "archive_{}.db".format(archive_year),
    )

    # Create archive db with same schema
    schema_path = os.path.join(PROJECT_ROOT, "db", "schema.sql")
    with open(schema_path, "r", encoding="utf-8") as f:
        schema_sql = f.read()

    with sqlite3.connect(archive_db_path) as archive_conn:
        archive_conn.executescript(schema_sql)

    with sqlite3.connect(db_path) as conn:
        conn.execute("ATTACH DATABASE ? AS archive", (archive_db_path,))

        # Move old hourly_utilization records
        conn.execute(
            "INSERT OR IGNORE INTO archive.hourly_utilization "
            "SELECT * FROM hourly_utilization WHERE date < ?",
            (cutoff_date,),
        )
        deleted = conn.execute(
            "DELETE FROM hourly_utilization WHERE date < ?",
            (cutoff_date,),
        ).rowcount

        # Move old state_transitions records
        conn.execute(
            "INSERT OR IGNORE INTO archive.state_transitions "
            "SELECT * FROM state_transitions WHERE timestamp < ?",
            (cutoff_date,),
        )
        conn.execute(
            "DELETE FROM state_transitions WHERE timestamp < ?",
            (cutoff_date,),
        )

        conn.execute("DETACH DATABASE archive")
        conn.execute("VACUUM")
        conn.commit()

    logger.info("Archived %d hourly records older than %s to %s", deleted, cutoff_date, archive_db_path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print("Config loaded. Enabled machines:")
    config = load_machines_config()
    for m in get_enabled_machines(config):
        print("  {} ({})".format(m["id"], m["ip"]))
    print("Settings:")
    settings = load_settings()
    for k, v in settings.items():
        print("  {}: {}".format(k, v))
