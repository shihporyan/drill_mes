"""
Machine health check: verify SMB connectivity to machine log shares.

Checks if each enabled machine's log share is accessible.
Updates machine_health table.

Usage:
    python collector/health_check.py
"""

import datetime
import logging
import os
import platform
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from parsers.base_parser import (
    load_machines_config,
    load_settings,
    get_enabled_machines,
    get_db_path,
)
from collector.log_collector import update_machine_health

logger = logging.getLogger(__name__)


def check_machine_connectivity(machine):
    """Check if a machine's SMB share is accessible.

    On Windows, uses 'dir' to check the share. On other platforms, uses ping.

    Args:
        machine: Machine config dict with ip field.

    Returns:
        bool: True if machine appears reachable.
    """
    ip = machine["ip"]

    if platform.system() == "Windows":
        share_path = "\\\\{}\\LOG".format(ip)
        try:
            result = subprocess.run(
                ["dir", share_path],
                capture_output=True, text=True, timeout=5, shell=True,
            )
            return result.returncode == 0
        except Exception as e:
            logger.debug("Share check failed for %s: %s", ip, e)
            return False
    else:
        # Dev environment: use ping
        try:
            param = "-c" if platform.system() != "Windows" else "-n"
            result = subprocess.run(
                ["ping", param, "1", "-W", "2", ip],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
        except Exception as e:
            logger.debug("Ping failed for %s: %s", ip, e)
            return False


def run_health_check():
    """Check connectivity for all enabled machines and update database.

    Returns:
        dict: Mapping of machine_id -> is_online.
    """
    machines_config = load_machines_config()
    settings = load_settings()
    db_path = get_db_path(settings)
    enabled = get_enabled_machines(machines_config)

    results = {}
    for machine in enabled:
        mid = machine["id"]
        online = check_machine_connectivity(machine)
        results[mid] = online
        update_machine_health(db_path, mid, online)
        status = "ONLINE" if online else "OFFLINE"
        logger.info("[%s] %s (%s)", mid, status, machine["ip"])

    return results


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    results = run_health_check()
    online = sum(1 for v in results.values() if v)
    print("Health check: {}/{} machines online".format(online, len(results)))
