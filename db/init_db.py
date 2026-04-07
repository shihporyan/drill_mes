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
    """Load settings from config/settings.json.

    Returns:
        dict: Parsed settings dictionary.
    """
    settings_path = os.path.join(PROJECT_ROOT, "config", "settings.json")
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

    # Verify tables were created
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in cursor.fetchall()]
        logger.info("Tables created: %s", ", ".join(tables))

    return db_path


if __name__ == "__main__":
    custom_path = None
    if len(sys.argv) > 1 and sys.argv[1] == "--db-path" and len(sys.argv) > 2:
        custom_path = sys.argv[2]

    result_path = init_database(custom_path)
    print("Database ready at: {}".format(result_path))
