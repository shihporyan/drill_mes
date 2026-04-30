"""
Drill Monitoring System - Main Entry Point.

Starts all components:
1. Log Collector (robocopy cycle)
2. Drive.Log Parser
3. HTTP API Server + Dashboard

Each component runs in its own thread. The API server is the main thread.
Supports Windows Task Scheduler: run with --once for single cycle, or
default continuous mode.

Usage:
    python main.py                # Start all services (continuous)
    python main.py --once         # Run one collect+parse cycle, then exit
    python main.py --server-only  # Start only the API server
"""

import datetime
import logging
import logging.handlers
import os
import sqlite3
import sys
import threading
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from db.init_db import init_database
from parsers.base_parser import load_settings, load_machines_config, get_db_path
from parsers.drive_log_parser import run_parser_cycle, run_parser_loop
from parsers.tx1_log_parser import run_parser_cycle as run_tx1_parser_cycle
from parsers.laser_log_parser import run_parser_cycle as run_laser_parser_cycle
from parsers.mtime_observer import start_observer_thread as start_mtime_observer
from collector.log_collector import run_collection_cycle, run_collection_loop
from collector.laser_log_collector import run_collection_cycle as run_laser_collection_cycle
from server.api_server import run_server
from tools.cleanup import cleanup_old_backups

logger = logging.getLogger("drill_monitor")


def setup_logging(settings):
    """Configure logging to console and rotating file.

    Args:
        settings: Settings dict with optional log_file path.
    """
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    formatter = logging.Formatter(log_format)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    # Rotating file handler (10 MB per file, keep 5 backups)
    log_file = settings.get("log_file")
    if log_file:
        if not os.path.isabs(log_file):
            log_file = os.path.join(PROJECT_ROOT, log_file)
        log_dir = os.path.dirname(log_file)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


def run_collect_and_parse_loop(settings, machines_config, db_path):
    """Combined collector + parser loop running in a background thread.

    Runs collection, then parsing, then waits for poll_interval.

    Args:
        settings: Settings dict.
        machines_config: Machines config dict.
        db_path: Path to SQLite database.
    """
    interval = settings.get("poll_interval_seconds", 600)
    logger.info("Collector+Parser loop started (interval=%ds)", interval)

    steps = (
        ("collect_takeuchi", lambda: run_collection_cycle(
            settings=settings, machines_config=machines_config, db_path=db_path)),
        ("collect_laser", lambda: run_laser_collection_cycle(
            settings=settings, machines_config=machines_config, db_path=db_path)),
        ("parse_drive", lambda: run_parser_cycle(
            db_path=db_path, settings=settings, machines_config=machines_config)),
        ("parse_tx1", lambda: run_tx1_parser_cycle(
            db_path=db_path, settings=settings, machines_config=machines_config)),
        ("parse_laser", lambda: run_laser_parser_cycle(
            db_path=db_path, settings=settings, machines_config=machines_config)),
        ("cleanup", lambda: cleanup_old_backups(dry_run=False, settings=settings)),
    )

    while True:
        cycle_start = datetime.datetime.now()
        steps_ok = 0
        failed = []

        for step_name, step_fn in steps:
            logger.info("--- %s ---", step_name)
            try:
                step_fn()
                steps_ok += 1
            except Exception as e:
                logger.error("%s failed: %s", step_name, e, exc_info=True)
                failed.append(step_name)

        cycle_end = datetime.datetime.now()
        took_ms = int((cycle_end - cycle_start).total_seconds() * 1000)

        # Write next_cycle_at + cycle_stats together so the dashboard has
        # both the countdown target and a row to chart.
        next_at = (cycle_end + datetime.timedelta(seconds=interval)).isoformat()
        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO system_status(key, value) VALUES(?, ?)",
                    ("next_cycle_at", next_at),
                )
                conn.execute(
                    "INSERT INTO cycle_stats "
                    "(cycle_start, cycle_end, took_ms, interval_secs, "
                    "steps_ok, steps_failed, failed_step_names) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (cycle_start.isoformat(), cycle_end.isoformat(), took_ms,
                     interval, steps_ok, len(failed),
                     ",".join(failed) if failed else None),
                )
        except Exception as e:
            logger.warning("Failed to write cycle status: %s", e)

        if took_ms > interval * 1000:
            logger.warning(
                "Cycle took %dms, longer than poll_interval %ds. "
                "Next cycle starts immediately (no idle time).",
                took_ms, interval,
            )

        logger.info("Cycle done in %dms. Next cycle in %d seconds...", took_ms, interval)
        time.sleep(interval)


def run_once():
    """Execute a single collect + parse cycle and exit.

    Suitable for Windows Task Scheduler.
    """
    settings = load_settings()
    machines_config = load_machines_config()
    db_path = get_db_path(settings)
    setup_logging(settings)

    logger.info("=== Single cycle mode ===")

    # Ensure database exists
    init_database(db_path)

    # Collect (mechanical + laser)
    run_collection_cycle(settings=settings, machines_config=machines_config, db_path=db_path)
    run_laser_collection_cycle(settings=settings, machines_config=machines_config, db_path=db_path)

    # Parse (mechanical + TX1 work order + laser)
    run_parser_cycle(db_path=db_path, settings=settings, machines_config=machines_config)
    run_tx1_parser_cycle(db_path=db_path, settings=settings, machines_config=machines_config)
    run_laser_parser_cycle(db_path=db_path, settings=settings, machines_config=machines_config)

    # Cleanup
    cleanup_old_backups(dry_run=False, settings=settings)

    logger.info("=== Single cycle complete ===")


def run_server_only():
    """Start only the API server without collector/parser."""
    settings = load_settings()
    db_path = get_db_path(settings)
    setup_logging(settings)

    init_database(db_path)

    host = settings.get("http_host", "127.0.0.1")
    port = settings.get("http_port", 8080)

    logger.info("=== Server-only mode ===")
    run_server(host=host, port=port, db_path=db_path)


def run_all():
    """Start all services: collector+parser loop in background, API server in foreground."""
    settings = load_settings()
    machines_config = load_machines_config()
    db_path = get_db_path(settings)
    setup_logging(settings)

    logger.info("=== Drill Monitoring System Starting ===")
    logger.info("Database: %s", db_path)

    # Ensure database exists
    init_database(db_path)

    # Start collector+parser in background thread
    worker = threading.Thread(
        target=run_collect_and_parse_loop,
        args=(settings, machines_config, db_path),
        daemon=True,
        name="collector-parser",
    )
    worker.start()
    logger.info("Collector+Parser thread started")

    # Start high-frequency TX1 mtime observer (investigation layer, see
    # notes/tx1_flush_latency_investigation.md). 30s poll, very light.
    try:
        start_mtime_observer(db_path=db_path, settings=settings, machines_config=machines_config)
        logger.info("TX1 mtime observer thread started (30s interval)")
    except Exception as e:
        logger.warning("Failed to start TX1 mtime observer: %s", e)

    # Run API server in main thread (blocking)
    host = settings.get("http_host", "127.0.0.1")
    port = settings.get("http_port", 8080)
    run_server(host=host, port=port, db_path=db_path)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        mode = sys.argv[1]
        if mode == "--once":
            run_once()
        elif mode == "--server-only":
            run_server_only()
        elif mode == "--help":
            print(__doc__)
        else:
            print("Unknown option: {}".format(mode))
            print("Usage: python main.py [--once | --server-only | --help]")
            sys.exit(1)
    else:
        run_all()
