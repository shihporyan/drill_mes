"""Configuration for drill log parser."""

import os
from pathlib import Path

# Project root
PROJECT_ROOT = Path(__file__).parent.parent

# Database
DB_PATH = PROJECT_ROOT / "db" / "drill_mes.db"

# Log source: where robocopy deposits logs
LOG_ARCHIVE_ROOT = Path(r"C:\LogArchive")

# For development/testing: use local logs
LOG_DEV_ROOT = PROJECT_ROOT / "logs"

# Machine definitions
MACHINES = {
    "DRILL-01": {
        "ip": "192.168.1.2",
        "type": "Takeuchi",
        "log_share": r"\\192.168.1.2\log",
    },
}

# Parse interval (seconds)
PARSE_INTERVAL = 600  # 10 minutes

# File encodings (verified from actual log data)
ENCODINGS = {
    "Drive.Log": "latin-1",
    "FILE.Log": "latin-1",
    "TARN.Log": "cp932",
    "TX1.Log": "cp932",
    "Alarm.Log": "latin-1",
    "MACRO.Log": "cp932",
}

# Log file types to parse (in order of priority)
LOG_TYPES = ["TX1.Log", "FILE.Log", "TARN.Log", "Drive.Log", "Alarm.Log"]


def get_log_dir(machine_id: str, date_str: str, use_dev: bool = False) -> Path:
    """Get log directory for a machine and date.

    Args:
        machine_id: e.g. 'DRILL-01'
        date_str: e.g. '20260317'
        use_dev: if True, use local logs/ directory instead of LogArchive
    """
    if use_dev:
        return LOG_DEV_ROOT / machine_id / date_str
    return LOG_ARCHIVE_ROOT / machine_id / date_str


def get_log_path(machine_id: str, date_str: str, log_type: str,
                 use_dev: bool = False) -> Path:
    """Get full path for a specific log file.

    Args:
        machine_id: e.g. 'DRILL-01'
        date_str: e.g. '20260317'
        log_type: e.g. 'Drive.Log'
        use_dev: if True, use local logs/ directory
    """
    day = date_str[6:8]  # extract DD from YYYYMMDD
    filename = f"{day}{log_type}"
    return get_log_dir(machine_id, date_str, use_dev) / filename
