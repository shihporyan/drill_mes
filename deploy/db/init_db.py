"""
Initialize the drill_monitor SQLite database from schema.sql.

Usage:
    python db/init_db.py
    python db/init_db.py --db-path /path/to/custom.db

Reads db_path from config/settings.json by default.
Creates the database file and executes schema.sql to build all tables.
"""

import json
import os
import sqlite3
import sys
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Resolve project root (parent of db/)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_settings():
    """Load settings, supporting DRILL_DEV_CONFIG env var override.

    Returns:
        dict: Parsed settings dictionary.
    """
    settings_path = os.path.join(PROJECT_ROOT, "config", "settings.json")
    env_override = os.environ.get("DRILL_DEV_CONFIG")
    if env_override:
        override_path = env_override if os.path.isabs(env_override) else os.path.join(PROJECT_ROOT, env_override)
        if os.path.exists(override_path):
            settings_path = override_path
    with open(settings_path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_schema_sql():
    """Read the schema.sql file content.

    Returns:
        str: SQL statements for creating all tables and indexes.
    """
    schema_path = os.path.join(PROJECT_ROOT, "db", "schema.sql")
    with open(schema_path, "r", encoding="utf-8") as f:
        return f.read()


def init_database(db_path=None):
    """Create the SQLite database and execute schema.sql.

    Args:
        db_path: Optional path to the database file. If None, reads from settings.json.

    Returns:
        str: Absolute path of the created database.
    """
    if db_path is None:
        settings = load_settings()
        db_path = settings.get("db_path", "drill_monitor.db")

    # Resolve relative paths against project root
    if not os.path.isabs(db_path):
        db_path = os.path.join(PROJECT_ROOT, db_path)

    db_dir = os.path.dirname(db_path)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir)

    schema_sql = get_schema_sql()

    with sqlite3.connect(db_path) as conn:
        conn.executescript(schema_sql)
        logger.info("Database initialized: %s", db_path)

    # Run migrations for existing databases
    _run_migrations(db_path)

    # Verify tables were created
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in cursor.fetchall()]
        logger.info("Tables created: %s", ", ".join(tables))

    return db_path


def _run_migrations(db_path):
    """Apply schema migrations for existing databases.

    Args:
        db_path: Path to the SQLite database file.
    """
    with sqlite3.connect(db_path) as conn:
        # Check existing columns in machine_current_state
        cursor = conn.execute("PRAGMA table_info(machine_current_state)")
        columns = {row[1] for row in cursor.fetchall()}

        if "work_order" not in columns:
            conn.execute("ALTER TABLE machine_current_state ADD COLUMN work_order TEXT")
            logger.info("Migration: added work_order column to machine_current_state")

        if "work_order_side" not in columns:
            conn.execute("ALTER TABLE machine_current_state ADD COLUMN work_order_side TEXT")
            logger.info("Migration: added work_order_side column to machine_current_state")

        # Ensure laser_work_orders table exists (for existing databases)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='laser_work_orders'"
        )
        if not cursor.fetchone():
            conn.execute("""
                CREATE TABLE IF NOT EXISTS laser_work_orders (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    machine_id    TEXT NOT NULL,
                    start_time    TEXT NOT NULL,
                    end_time      TEXT,
                    duration_secs REAL,
                    station       TEXT,
                    work_order    TEXT NOT NULL,
                    lsr_file_path TEXT,
                    hole_count    INTEGER DEFAULT 0,
                    UNIQUE(machine_id, start_time, station)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_laser_wo_machine "
                "ON laser_work_orders(machine_id, start_time)"
            )
            logger.info("Migration: created laser_work_orders table")

        # Convert WD- prefix to O prefix for Takeuchi work orders
        updated = conn.execute(
            "UPDATE machine_current_state SET work_order = 'O' || SUBSTR(work_order, 4) "
            "WHERE work_order LIKE 'WD-%' AND machine_id LIKE 'M%'"
        ).rowcount
        if updated:
            logger.info("Migration: converted %d Takeuchi work orders from WD- to O prefix", updated)


if __name__ == "__main__":
    custom_path = None
    if len(sys.argv) > 1 and sys.argv[1] == "--db-path" and len(sys.argv) > 2:
        custom_path = sys.argv[2]

    result_path = init_database(custom_path)
    print("Database ready at: {}".format(result_path))
