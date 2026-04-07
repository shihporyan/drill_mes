"""
Golden test for drive_log_parser.py.

Uses verified data from M02 (DRILL-01) on 2026/03/17.
The fixture file tests/fixtures/17Drive.Log must be manually placed.
This test can also run with a synthetic fixture generated in-memory.

Golden data source: DEV_GUIDE.md Section 12.
"""

import os
import sys
import sqlite3
import tempfile
import unittest

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from parsers.drive_log_parser import parse_log_file
from db.init_db import init_database

VALID_STATES = {"RUN", "RESET", "STOP"}

# Golden data: hourly RUN/RESET/STOP seconds for M02 on 2026-03-17
GOLDEN_HOURLY = {
    0:  {"run": 3103, "reset": 436,  "stop": 61,  "total": 3600, "util": 86.2},
    1:  {"run": 0,    "reset": 0,    "stop": 0,   "total": 0,    "util": 0.0},
    2:  {"run": 0,    "reset": 0,    "stop": 0,   "total": 0,    "util": 0.0},
    3:  {"run": 0,    "reset": 0,    "stop": 0,   "total": 0,    "util": 0.0},
    4:  {"run": 0,    "reset": 0,    "stop": 0,   "total": 0,    "util": 0.0},
    5:  {"run": 0,    "reset": 0,    "stop": 0,   "total": 0,    "util": 0.0},
    6:  {"run": 0,    "reset": 0,    "stop": 0,   "total": 0,    "util": 0.0},
    7:  {"run": 274,  "reset": 3319, "stop": 7,   "total": 3600, "util": 7.6},
    8:  {"run": 2302, "reset": 1284, "stop": 14,  "total": 3600, "util": 63.9},
    9:  {"run": 2733, "reset": 860,  "stop": 7,   "total": 3600, "util": 75.9},
    10: {"run": 3319, "reset": 257,  "stop": 24,  "total": 3600, "util": 92.2},
    11: {"run": 1898, "reset": 1594, "stop": 108, "total": 3600, "util": 52.7},
    12: {"run": 0,    "reset": 3600, "stop": 0,   "total": 3600, "util": 0.0},
    13: {"run": 0,    "reset": 3600, "stop": 0,   "total": 3600, "util": 0.0},
    14: {"run": 0,    "reset": 3600, "stop": 0,   "total": 3600, "util": 0.0},
    15: {"run": 0,    "reset": 3600, "stop": 0,   "total": 3600, "util": 0.0},
    16: {"run": 0,    "reset": 2797, "stop": 0,   "total": 2797, "util": 0.0},
    17: {"run": 0,    "reset": 1303, "stop": 0,   "total": 1303, "util": 0.0},
}

GOLDEN_DAILY_TOTAL = {
    "run": 13629,
    "reset": 26250,
    "stop": 221,
    "total": 40100,
}

GOLDEN_TRANSITION_COUNT = 63


def generate_synthetic_log(golden_data, date_str="2026/03/17"):
    """Generate a synthetic Drive.Log that matches golden hourly data.

    Each second within a recorded hour produces one CSV line.
    The state distribution matches golden RUN/RESET/STOP seconds exactly.

    Args:
        golden_data: Dict mapping hour -> {run, reset, stop, total}.
        date_str: Date string in YYYY/MM/DD format.

    Returns:
        str: CSV content matching Drive.Log format.
    """
    lines = []
    counter = 173400000  # Starting counter value
    prev_state = None

    for hour in range(24):
        data = golden_data.get(hour)
        if not data or data["total"] == 0:
            continue

        total = data["total"]
        run_secs = data["run"]
        reset_secs = data["reset"]
        stop_secs = data["stop"]

        # Build second-by-second state sequence for this hour
        states = []
        states.extend(["RUN"] * run_secs)
        states.extend(["RESET"] * reset_secs)
        states.extend(["STOP"] * stop_secs)

        # Ensure we have exactly `total` entries
        # If rounding issues, pad with RESET
        while len(states) < total:
            states.append("RESET")
        states = states[:total]

        # Sort to create realistic blocks (RUN, then RESET, then STOP)
        # but keep transitions natural
        for sec_offset, state in enumerate(states):
            second = sec_offset % 60
            minute = sec_offset // 60
            time_str = "{:02d}:{:02d}:{:02d}".format(hour, minute, second)

            mode = "AUTO" if state == "RUN" else "MAN"
            program = "O100.txt" if state == "RUN" else ""
            tool = "084" if state == "RUN" else "000"
            drill_dia = "1.000" if state == "RUN" else "0.150"

            if state == "RUN":
                counter += 1

            line = "{},{},{},{},{},{},{},{},{},{},{},1,0,0,0,0,0,{},{},{},{},{},{}".format(
                date_str, time_str, mode, state, program,
                "   20.142", "   276.228", tool,
                " " + drill_dia, "0000", counter,
                "  0.000", "  0.000", "  0.000", "  0.000", "  0.000", "  0.000",
            )
            lines.append(line)
            prev_state = state

    return "\n".join(lines) + "\n"


class TestParserWithSyntheticData(unittest.TestCase):
    """Test parser accuracy using synthetic log data matching golden values."""

    def setUp(self):
        """Create temp db and synthetic log file."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        init_database(self.db_path)

        self.log_content = generate_synthetic_log(GOLDEN_HOURLY)
        self.log_path = os.path.join(self.temp_dir, "17Drive.Log")
        with open(self.log_path, "w", encoding="utf-8") as f:
            f.write(self.log_content)

    def tearDown(self):
        """Clean up temp files."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_hourly_utilization(self):
        """Verify hourly RUN/RESET/STOP seconds match golden data."""
        parse_log_file(self.db_path, "M02", self.log_path, "17")

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT hour, run_seconds, reset_seconds, stop_seconds, total_seconds, utilization "
                "FROM hourly_utilization WHERE machine_id='M02' AND date='2026-03-17' "
                "ORDER BY hour"
            )
            rows = {r[0]: r for r in cursor.fetchall()}

        for hour, expected in GOLDEN_HOURLY.items():
            if expected["total"] == 0:
                # Hours with no data may or may not have a row
                if hour in rows:
                    self.assertEqual(rows[hour][1], 0, "Hour {} RUN should be 0".format(hour))
                continue

            self.assertIn(hour, rows, "Missing data for hour {}".format(hour))
            row = rows[hour]
            self.assertEqual(row[1], expected["run"],
                             "Hour {} RUN: got {} expected {}".format(hour, row[1], expected["run"]))
            self.assertEqual(row[2], expected["reset"],
                             "Hour {} RESET: got {} expected {}".format(hour, row[2], expected["reset"]))
            self.assertEqual(row[3], expected["stop"],
                             "Hour {} STOP: got {} expected {}".format(hour, row[3], expected["stop"]))
            self.assertEqual(row[4], expected["total"],
                             "Hour {} TOTAL: got {} expected {}".format(hour, row[4], expected["total"]))
            self.assertAlmostEqual(row[5], expected["util"], places=1,
                                   msg="Hour {} UTIL: got {} expected {}".format(hour, row[5], expected["util"]))

    def test_daily_totals(self):
        """Verify daily aggregate matches golden totals."""
        parse_log_file(self.db_path, "M02", self.log_path, "17")

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT SUM(run_seconds), SUM(reset_seconds), SUM(stop_seconds), SUM(total_seconds) "
                "FROM hourly_utilization WHERE machine_id='M02' AND date='2026-03-17'"
            )
            row = cursor.fetchone()

        self.assertEqual(row[0], GOLDEN_DAILY_TOTAL["run"], "Daily RUN total mismatch")
        self.assertEqual(row[1], GOLDEN_DAILY_TOTAL["reset"], "Daily RESET total mismatch")
        self.assertEqual(row[2], GOLDEN_DAILY_TOTAL["stop"], "Daily STOP total mismatch")
        self.assertEqual(row[3], GOLDEN_DAILY_TOTAL["total"], "Daily TOTAL mismatch")

    def test_state_transitions(self):
        """Verify state transitions are recorded with valid structure.

        Note: Exact transition count (63) can only be verified with the real
        fixture, since synthetic data uses contiguous state blocks.
        """
        parse_log_file(self.db_path, "M02", self.log_path, "17")

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM state_transitions WHERE machine_id='M02'"
            )
            count = cursor.fetchone()[0]

            # Synthetic data has fewer transitions than real data, but should have some
            self.assertGreater(count, 0, "Should have at least one transition")

            # Verify transition structure
            cursor = conn.execute(
                "SELECT from_state, to_state FROM state_transitions "
                "WHERE machine_id='M02' LIMIT 1"
            )
            row = cursor.fetchone()
            self.assertIn(row[0], VALID_STATES, "from_state should be valid")
            self.assertIn(row[1], VALID_STATES, "to_state should be valid")
            self.assertNotEqual(row[0], row[1], "Transition should be between different states")

    def test_incremental_parse(self):
        """Verify that running parser twice does not double-count."""
        parse_log_file(self.db_path, "M02", self.log_path, "17")
        parse_log_file(self.db_path, "M02", self.log_path, "17")

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT SUM(run_seconds) FROM hourly_utilization "
                "WHERE machine_id='M02' AND date='2026-03-17'"
            )
            total_run = cursor.fetchone()[0]

        self.assertEqual(total_run, GOLDEN_DAILY_TOTAL["run"],
                         "Double-parse should not double-count RUN seconds")

    def test_machine_current_state(self):
        """Verify machine_current_state is populated after parsing."""
        parse_log_file(self.db_path, "M02", self.log_path, "17")

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT machine_id, state FROM machine_current_state WHERE machine_id='M02'"
            )
            row = cursor.fetchone()

        self.assertIsNotNone(row, "machine_current_state should have M02 entry")
        self.assertIn(row[1], ("RUN", "RESET", "STOP"), "State should be valid")


class TestParserWithFixture(unittest.TestCase):
    """Test parser accuracy using real fixture file (if available)."""

    FIXTURE_PATH = os.path.join(PROJECT_ROOT, "tests", "fixtures", "17Drive.Log")

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        init_database(self.db_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @unittest.skipUnless(
        os.path.exists(os.path.join(PROJECT_ROOT, "tests", "fixtures", "17Drive.Log")),
        "Real fixture 17Drive.Log not available"
    )
    def test_real_fixture_hourly(self):
        """Verify parser against real M02 17Drive.Log fixture."""
        parse_log_file(self.db_path, "M02", self.FIXTURE_PATH, "17")

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT hour, run_seconds, reset_seconds, stop_seconds, total_seconds "
                "FROM hourly_utilization WHERE machine_id='M02' AND date='2026-03-17' "
                "ORDER BY hour"
            )
            rows = {r[0]: r for r in cursor.fetchall()}

        for hour, expected in GOLDEN_HOURLY.items():
            if expected["total"] == 0:
                continue
            self.assertIn(hour, rows, "Missing hour {}".format(hour))
            row = rows[hour]
            self.assertEqual(row[1], expected["run"],
                             "Hour {} RUN mismatch".format(hour))

    @unittest.skipUnless(
        os.path.exists(os.path.join(PROJECT_ROOT, "tests", "fixtures", "17Drive.Log")),
        "Real fixture 17Drive.Log not available"
    )
    def test_real_fixture_transitions(self):
        """Verify exact transition count against real fixture."""
        parse_log_file(self.db_path, "M02", self.FIXTURE_PATH, "17")

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM state_transitions WHERE machine_id='M02'"
            )
            count = cursor.fetchone()[0]

        self.assertEqual(count, GOLDEN_TRANSITION_COUNT,
                         "Transition count: got {} expected {}".format(count, GOLDEN_TRANSITION_COUNT))


if __name__ == "__main__":
    unittest.main(verbosity=2)
