"""Observe remote Takeuchi LOG file size/mtime for flush-latency investigation.

See notes/tx1_flush_latency_investigation.md. Stats all 6 known Takeuchi log
types via SMB directly (no robocopy needed). Records each observation to
`log_file_observe`. Used to diagnose why TX1.Log events appear on the SMB
share many minutes after the machine records them internally.
"""

import datetime
import logging
import os
import platform

from parsers.base_parser import get_db_connection

logger = logging.getLogger(__name__)

# Takeuchi control PC writes 6 log files (verified from 2026-04-09 dump).
# Filename pattern: {DD}{Type}.Log, rotating monthly.
LOG_TYPES = ["Drive", "TX1", "MACRO", "TARN", "FILE", "Alarm"]


def _insert_observation(conn, machine_id, log_type, observed_at, size, mtime_iso, error):
    conn.execute(
        "INSERT INTO log_file_observe "
        "(machine_id, log_type, observed_at, file_size, file_mtime, error) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (machine_id, log_type, observed_at, size, mtime_iso, error),
    )


def observe_takeuchi_logs(db_path, machine_id, ip, day_prefix, observed_at=None):
    """Stat each of the 6 Takeuchi log file types on the remote SMB share.

    Args:
        db_path: Path to SQLite database.
        machine_id: Machine identifier (e.g. 'M13').
        ip: Control PC IP (e.g. '10.10.1.23').
        day_prefix: Two-digit day string (e.g. '18').
        observed_at: Optional ISO timestamp. Defaults to now().

    Notes:
        - On non-Windows (Mac dev), SMB paths won't resolve; each stat is
          recorded with error='dev-env: smb unavailable' so the row count
          stays consistent and Mac dev can still verify the instrument flow.
        - Per-file errors are captured so one missing file doesn't abort the
          whole machine's observation cycle.
    """
    if observed_at is None:
        observed_at = datetime.datetime.now().isoformat()

    is_windows = platform.system() == "Windows"
    conn = get_db_connection(db_path)
    try:
        for log_type in LOG_TYPES:
            filename = "{}{}.Log".format(day_prefix, log_type)

            if not is_windows:
                _insert_observation(
                    conn, machine_id, log_type, observed_at,
                    None, None, "dev-env: smb unavailable",
                )
                continue

            smb_path = "\\\\{}\\LOG\\{}".format(ip, filename)
            try:
                st = os.stat(smb_path)
                mtime_iso = datetime.datetime.fromtimestamp(st.st_mtime).isoformat()
                _insert_observation(
                    conn, machine_id, log_type, observed_at,
                    st.st_size, mtime_iso, None,
                )
            except (FileNotFoundError, OSError) as e:
                _insert_observation(
                    conn, machine_id, log_type, observed_at,
                    None, None, str(e)[:200],
                )
        conn.commit()
    finally:
        conn.close()
