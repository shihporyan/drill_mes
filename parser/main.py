"""Main entry point — incremental log parsing loop.

Runs each parser in priority order every PARSE_INTERVAL seconds.
Can also be invoked for a single pass (e.g., for testing).
"""

import time
import logging
from datetime import datetime
from pathlib import Path

from .config import MACHINES, PARSE_INTERVAL, get_log_path, LOG_TYPES
from .db import (
    init_db, init_machine, get_parse_offsets,
    get_machine_state, DB_PATH,
)
from .tarn_parser import parse_tarn
from .tx1_parser import parse_tx1
from .drive_parser import parse_drive
from .file_parser import parse_file_log
from .alarm_parser import parse_alarm
from .utilization import save_utilization

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

PARSERS = {
    "TX1.Log": parse_tx1,
    "FILE.Log": parse_file_log,
    "TARN.Log": parse_tarn,
    "Drive.Log": parse_drive,
    "Alarm.Log": parse_alarm,
}


def parse_once(machine_id: str, date_str: str, use_dev: bool = False,
               db_path: Path = None) -> dict:
    """Run a single parse pass for one machine and date.

    Args:
        machine_id: e.g. 'DRILL-01'
        date_str: e.g. '20260317' (YYYYMMDD)
        use_dev: if True, use local logs/ directory
        db_path: override database path

    Returns:
        Dict of {log_type: events_processed_count}
    """
    db = db_path or DB_PATH
    offsets = get_parse_offsets(machine_id, date_str, db)
    results = {}

    for log_type in LOG_TYPES:
        if log_type not in PARSERS:
            continue

        file_path = get_log_path(machine_id, date_str, log_type, use_dev)
        if not file_path.exists():
            log.debug(f"  {log_type}: file not found at {file_path}")
            results[log_type] = 0
            continue

        old_offset = offsets.get(log_type, 0)
        file_size = file_path.stat().st_size

        if old_offset >= file_size:
            log.debug(f"  {log_type}: no new data (offset={old_offset}, size={file_size})")
            results[log_type] = 0
            continue

        parser_fn = PARSERS[log_type]
        new_offset = parser_fn(file_path, machine_id, old_offset, db, date_str)
        bytes_parsed = new_offset - old_offset
        results[log_type] = bytes_parsed
        log.info(f"  {log_type}: parsed {bytes_parsed:,} bytes (offset {old_offset} → {new_offset})")

    # Calculate utilization after parsing
    save_utilization(machine_id, date_str, db)

    return results


def run_loop(use_dev: bool = False, db_path: Path = None):
    """Main loop: parse logs every PARSE_INTERVAL seconds."""
    db = db_path or DB_PATH
    init_db(db)

    # Initialize machines
    for mid, info in MACHINES.items():
        init_machine(mid, info.get("ip"), info.get("type"), db)
        log.info(f"Initialized machine: {mid}")

    log.info(f"Starting parse loop (interval={PARSE_INTERVAL}s, dev={use_dev})")

    while True:
        now = datetime.now()
        date_str = now.strftime("%Y%m%d")

        for machine_id in MACHINES:
            log.info(f"Parsing {machine_id} for {date_str}...")
            try:
                results = parse_once(machine_id, date_str, use_dev, db)
                total_bytes = sum(results.values())
                if total_bytes > 0:
                    log.info(f"  Total: {total_bytes:,} bytes parsed")
                else:
                    log.info(f"  No new data")
            except Exception as e:
                log.error(f"  Error parsing {machine_id}: {e}", exc_info=True)

        log.info(f"Next parse in {PARSE_INTERVAL}s...")
        time.sleep(PARSE_INTERVAL)


def run_backfill(machine_id: str, date_str: str, use_dev: bool = True,
                 db_path: Path = None):
    """One-shot parse for a specific date (backfill/testing).

    Resets offsets for the target date before parsing.
    """
    db = db_path or DB_PATH
    init_db(db)

    info = MACHINES.get(machine_id, {})
    init_machine(machine_id, info.get("ip"), info.get("type"), db)

    log.info(f"Backfill: {machine_id} / {date_str}")
    results = parse_once(machine_id, date_str, use_dev, db)

    total = sum(results.values())
    log.info(f"Backfill complete: {total:,} bytes parsed")
    for lt, b in results.items():
        if b > 0:
            log.info(f"  {lt}: {b:,} bytes")

    # Show machine state
    state = get_machine_state(machine_id, db)
    log.info(f"Machine state: status={state.get('current_status')}, "
             f"program={state.get('current_program')}, "
             f"tool={state.get('current_tool')}, "
             f"diameter={state.get('current_diameter')}")

    return results


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 3:
        # Backfill mode: python -m parser.main DRILL-01 20260317
        mid = sys.argv[1]
        ds = sys.argv[2]
        run_backfill(mid, ds)
    else:
        # Loop mode
        run_loop(use_dev=False)
