"""
Recompute machine_current_state.since from state_transitions.

Companion to the parser fix that derives `since` from state_transitions
instead of the (sometimes stuck) prior `machine_current_state.since`. The
fix only takes effect on the next parse cycle that actually processes new
rows for a given machine; this script repairs stale values immediately so
the dashboard does not display wildly inflated idle durations until the
machine produces new data.

Logic: for each row in machine_current_state, look up the most recent
row in state_transitions with matching machine_id and to_state == state,
and set since to that timestamp. If no transition has ever been recorded
into the current state, leave the row alone (the parser will set it
correctly on the next batch).

Safety:
- Run with DrillMonitor stopped (parser writes can race the UPDATE).
- Default is dry-run; pass --apply to actually write.
- --apply takes a .bak_before_since_fix copy of the DB first.

Usage:
    python tools/fix_stale_since.py            # dry-run, prints diffs
    python tools/fix_stale_since.py --apply    # write changes
"""

import argparse
import logging
import os
import shutil
import sqlite3
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from parsers.base_parser import load_settings, get_db_path

logger = logging.getLogger("fix_stale_since")


def fix(db_path, apply):
    rows_changed = 0
    rows_kept = 0
    rows_no_history = 0

    with sqlite3.connect(db_path) as conn:
        machines = conn.execute(
            "SELECT machine_id, state, since FROM machine_current_state ORDER BY machine_id"
        ).fetchall()

        for machine_id, state, current_since in machines:
            row = conn.execute(
                "SELECT timestamp FROM state_transitions "
                "WHERE machine_id=? AND to_state=? "
                "ORDER BY timestamp DESC LIMIT 1",
                (machine_id, state),
            ).fetchone()

            if row is None or row[0] is None:
                logger.info("[%s] state=%s no transitions to %s on record — leaving since=%s",
                            machine_id, state, state, current_since)
                rows_no_history += 1
                continue

            new_since = row[0]
            if new_since == current_since:
                rows_kept += 1
                continue

            logger.info("[%s] state=%s since: %s -> %s",
                        machine_id, state, current_since, new_since)
            rows_changed += 1

            if apply:
                conn.execute(
                    "UPDATE machine_current_state SET since=? WHERE machine_id=?",
                    (new_since, machine_id),
                )

        if apply:
            conn.commit()

    logger.info("Done. changed=%d unchanged=%d no_transitions=%d",
                rows_changed, rows_kept, rows_no_history)
    return rows_changed


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="Write the recomputed since values (default: dry-run).")
    ap.add_argument("--db", help="Override DB path (default: from settings).")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    db_path = args.db or get_db_path(load_settings())
    logger.info("DB:    %s", db_path)
    logger.info("Mode:  %s", "APPLY" if args.apply else "DRY-RUN")

    if args.apply:
        backup = db_path + ".bak_before_since_fix"
        if os.path.exists(backup):
            logger.error("Backup file already exists: %s — refuse to overwrite. "
                         "Move or delete it, then retry.", backup)
            sys.exit(1)
        shutil.copy2(db_path, backup)
        logger.info("DB backup: %s", backup)

    fix(db_path, args.apply)


if __name__ == "__main__":
    main()
