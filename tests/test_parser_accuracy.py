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

# Source data for synthetic log generation: what the machine actually logged.
# Hours with total=0 produce no rows, creating gaps in the synthetic data.
SOURCE_HOURLY = {
    0:  {"run": 3103, "reset": 436,  "stop": 61,  "total": 3600},
    7:  {"run": 274,  "reset": 3319, "stop": 7,   "total": 3600},
    8:  {"run": 2302, "reset": 1284, "stop": 14,  "total": 3600},
    9:  {"run": 2733, "reset": 860,  "stop": 7,   "total": 3600},
    10: {"run": 3319, "reset": 257,  "stop": 24,  "total": 3600},
    11: {"run": 1898, "reset": 1594, "stop": 108, "total": 3600},
    12: {"run": 0,    "reset": 3600, "stop": 0,   "total": 3600},
    13: {"run": 0,    "reset": 3600, "stop": 0,   "total": 3600},
    14: {"run": 0,    "reset": 3600, "stop": 0,   "total": 3600},
    15: {"run": 0,    "reset": 3600, "stop": 0,   "total": 3600},
    16: {"run": 0,    "reset": 2797, "stop": 0,   "total": 2797},
    17: {"run": 0,    "reset": 1303, "stop": 0,   "total": 1303},
}

# Golden data: expected parser output (timestamp-delta based counting).
#
# The parser attributes the time delta between consecutive rows to the
# current row's state, capped at GAP_CAP_SECONDS (120).  When the synthetic
# data has a gap between active blocks, the last row's state is attributed
# up to the cap into the gap:
#   - Hour 0 (ends 00:59:59) → Hour 7 (starts 07:00:00): 1s to hour 0,
#     119s STOP spills into hour 1.
#   - Hour 16 (ends 16:46:36) → Hour 17 (starts 17:00:00): 120s extra
#     RESET attributed to hour 16.
GOLDEN_HOURLY = {
    0:  {"run": 3103, "reset": 436,  "stop": 61,   "total": 3600, "util": 86.2},
    1:  {"run": 0,    "reset": 0,    "stop": 119,  "total": 119,  "util": 0.0},
    7:  {"run": 274,  "reset": 3319, "stop": 7,    "total": 3600, "util": 7.6},
    8:  {"run": 2302, "reset": 1284, "stop": 14,   "total": 3600, "util": 63.9},
    9:  {"run": 2733, "reset": 860,  "stop": 7,    "total": 3600, "util": 75.9},
    10: {"run": 3319, "reset": 257,  "stop": 24,   "total": 3600, "util": 92.2},
    11: {"run": 1898, "reset": 1594, "stop": 108,  "total": 3600, "util": 52.7},
    12: {"run": 0,    "reset": 3600, "stop": 0,    "total": 3600, "util": 0.0},
    13: {"run": 0,    "reset": 3600, "stop": 0,    "total": 3600, "util": 0.0},
    14: {"run": 0,    "reset": 3600, "stop": 0,    "total": 3600, "util": 0.0},
    15: {"run": 0,    "reset": 3600, "stop": 0,    "total": 3600, "util": 0.0},
    16: {"run": 0,    "reset": 2916, "stop": 0,    "total": 2916, "util": 0.0},
    17: {"run": 0,    "reset": 1303, "stop": 0,    "total": 1303, "util": 0.0},
}

GOLDEN_DAILY_TOTAL = {
    "run": 13629,
    "reset": 26369,
    "stop": 340,
    "total": 40338,
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

        self.log_content = generate_synthetic_log(SOURCE_HOURLY)
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


class TestParserWithGappedData(unittest.TestCase):
    """Test that the parser correctly handles M13-style firmware gaps.

    Simulates:
    - 14-second gaps between rows (firmware skipping logging cycles)
    - Time reversals (peek-ahead rows from ~6 minutes in the future)
    - Cross-hour boundary gaps
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        init_database(self.db_path)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _generate_gapped_log(self):
        """Generate log with 14-second gaps and a time reversal.

        Pattern (hour 10, 10:00:00-10:59:59):
        - 28 rows at 1s intervals (RUN), then 14s gap, repeat
        - One peek-ahead row injected from ~6 min future
        - Some RESET rows at end of hour

        Expected: ~86 cycles of (28 rows + 14s gap) = ~86*42s = 3612s.
        Each cycle covers 42 real seconds with 28 logged rows.
        """
        lines = []
        counter = 50000000
        date_str = "2026/04/10"

        # Hour 10: RUN with 14s gaps, 28 rows per cycle
        current_second = 10 * 3600  # 10:00:00
        end_second = 10 * 3600 + 3000  # 10:50:00 → RUN
        rows_in_cycle = 0

        while current_second < end_second:
            h = current_second // 3600
            m = (current_second % 3600) // 60
            s = current_second % 60
            time_str = "{:02d}:{:02d}:{:02d}".format(h, m, s)
            counter += 1
            line = "{},{},AUTO,RUN,O100.txt,20.142,276.228,084,1.000,0000,{},1,0,0,0,0,0,0.000,0.000,0.000,0.000,0.000,0.000".format(
                date_str, time_str, counter)
            lines.append(line)
            rows_in_cycle += 1
            current_second += 1

            if rows_in_cycle >= 28:
                current_second += 14  # firmware gap
                rows_in_cycle = 0

        # Inject a peek-ahead row (from ~6 min in the future, then normal resumes)
        # This simulates the M13 firmware bug: one row from the future inserted
        # between normal rows
        future_second = current_second + 355
        h = future_second // 3600
        m = (future_second % 3600) // 60
        s = future_second % 60
        peek_time = "{:02d}:{:02d}:{:02d}".format(h, m, s)
        counter_peek = counter + 120
        lines.append("{},{},AUTO,RUN,O100.txt,20.142,276.228,084,1.000,0000,{},1,0,0,0,0,0,0.000,0.000,0.000,0.000,0.000,0.000".format(
            date_str, peek_time, counter_peek))

        # Continue normal rows after the peek (RESET for remaining time)
        reset_start = current_second
        reset_end = 10 * 3600 + 3600  # 11:00:00
        while reset_start < reset_end:
            h = reset_start // 3600
            m = (reset_start % 3600) // 60
            s = reset_start % 60
            time_str = "{:02d}:{:02d}:{:02d}".format(h, m, s)
            line = "{},{},MAN,RESET,,20.142,276.228,000,0.150,0000,{},1,0,0,0,0,0,0.000,0.000,0.000,0.000,0.000,0.000".format(
                date_str, time_str, counter)
            lines.append(line)
            reset_start += 1

        return "\n".join(lines) + "\n"

    def test_gap_seconds_attribution(self):
        """Verify that 14-second gaps are attributed to the correct state."""
        content = self._generate_gapped_log()
        log_path = os.path.join(self.temp_dir, "10Drive.Log")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(content)

        parse_log_file(self.db_path, "M13", log_path, "10")

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT run_seconds, reset_seconds, stop_seconds, total_seconds "
                "FROM hourly_utilization WHERE machine_id='M13' AND hour=10"
            )
            row = cursor.fetchone()

        self.assertIsNotNone(row, "Should have data for hour 10")
        run, reset, stop, total = row

        # With gaps properly attributed, total should be close to 3600
        # (not just the row count, which would be much less)
        self.assertGreater(total, 3500,
                           "Total should be close to 3600, got {} "
                           "(gaps were not attributed)".format(total))
        self.assertLessEqual(total, 3600,
                             "Total should not exceed 3600, got {}".format(total))

        # RUN should account for most of the hour (we put RUN in first 50 min)
        self.assertGreater(run, 2800,
                           "RUN should include gap time, got {}".format(run))

    def test_time_reversal_handling(self):
        """Verify that time reversals (peek-ahead rows) don't corrupt data."""
        content = self._generate_gapped_log()
        log_path = os.path.join(self.temp_dir, "10Drive.Log")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(content)

        parse_log_file(self.db_path, "M13", log_path, "10")

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT total_seconds FROM hourly_utilization "
                "WHERE machine_id='M13' AND hour=10"
            )
            total = cursor.fetchone()[0]

        # The peek-ahead row should be sorted into its correct chronological
        # position and not create a negative-time artifact
        self.assertGreater(total, 0, "Total should be positive despite reversal")

    def test_cross_hour_gap_split(self):
        """Verify that a gap spanning an hour boundary splits correctly."""
        # Generate simple data: one row at 10:59:55 (RUN) and one at 11:00:09 (RUN)
        date_str = "2026/04/10"
        lines = [
            "{},10:59:55,AUTO,RUN,O100.txt,20.142,276.228,084,1.000,0000,100,1,0,0,0,0,0,0.000,0.000,0.000,0.000,0.000,0.000".format(date_str),
            "{},11:00:09,AUTO,RUN,O100.txt,20.142,276.228,084,1.000,0000,101,1,0,0,0,0,0,0.000,0.000,0.000,0.000,0.000,0.000".format(date_str),
        ]
        log_path = os.path.join(self.temp_dir, "10Drive.Log")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        parse_log_file(self.db_path, "M13", log_path, "10")

        with sqlite3.connect(self.db_path) as conn:
            rows = {}
            cursor = conn.execute(
                "SELECT hour, run_seconds FROM hourly_utilization "
                "WHERE machine_id='M13' ORDER BY hour"
            )
            for r in cursor:
                rows[r[0]] = r[1]

        # 14s gap: 5s should go to hour 10 (10:59:55 to 11:00:00)
        # and 9s to hour 11 (11:00:00 to 11:00:09)
        self.assertEqual(rows.get(10, 0), 5,
                         "Hour 10 should get 5 seconds (to boundary)")
        # Hour 11: 9s from gap + 1s for last row = 10
        self.assertEqual(rows.get(11, 0), 10,
                         "Hour 11 should get 9s (from gap) + 1s (last row)")


class TestParserPeekAheadReplay(unittest.TestCase):
    """Test that cross-batch peek-ahead replay does not double-count.

    Scenario: firmware writes a future-timestamped "peek-ahead" row to the
    log (e.g. at 10:00:30), then in a later flush writes the real rows that
    fill in the earlier timestamps (10:00:05, :06, :07, :08). When the
    parser runs twice (once before and once after the fill-in), the second
    batch's rows have timestamps earlier than the last_timestamp recorded
    by the first batch. Without handling this, hourly_utilization is
    over-counted and state_transitions contain duplicates.
    """

    DATE_STR = "2026/04/10"

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        init_database(self.db_path)
        self.log_path = os.path.join(self.temp_dir, "10Drive.Log")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _row(self, time_str, state, counter):
        """Format one Drive.Log CSV row."""
        mode = "AUTO" if state == "RUN" else "MAN"
        prog = "O100.txt" if state == "RUN" else ""
        tool = "084" if state == "RUN" else "000"
        dia = "1.000" if state == "RUN" else "0.150"
        return ("{date},{t},{m},{s},{p},20.142,276.228,{tl},{d},0000,"
                "{c},1,0,0,0,0,0,0.000,0.000,0.000,0.000,0.000,0.000").format(
            date=self.DATE_STR, t=time_str, m=mode, s=state, p=prog,
            tl=tool, d=dia, c=counter,
        )

    def _write(self, rows):
        with open(self.log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")

    def test_peek_ahead_replay_does_not_double_count(self):
        """Two parse calls with peek-ahead fill-in should total same as one parse."""
        # Batch 1: real rows 10:00:00-04 (RUN) + peek-ahead at 10:00:30 (RUN).
        # Counter increments monotonically: 100..104 for real rows, 130 for
        # peek-ahead (simulating firmware projecting forward).
        batch1_rows = [
            self._row("10:00:00", "RUN", 100),
            self._row("10:00:01", "RUN", 101),
            self._row("10:00:02", "RUN", 102),
            self._row("10:00:03", "RUN", 103),
            self._row("10:00:04", "RUN", 104),
            self._row("10:00:30", "RUN", 130),
        ]
        self._write(batch1_rows)
        parse_log_file(self.db_path, "M13", self.log_path, "10")

        with sqlite3.connect(self.db_path) as conn:
            after_batch1 = conn.execute(
                "SELECT run_seconds, hole_count FROM hourly_utilization "
                "WHERE machine_id='M13' AND hour=10"
            ).fetchone()
        self.assertIsNotNone(after_batch1, "Batch 1 should produce hour-10 row")

        # Batch 2: same file + filler rows 10:00:05-08 appended AFTER the
        # peek-ahead line (firmware writes real rows later) + next real row
        # at 10:00:31. The filler timestamps are earlier than the peek-ahead.
        batch2_rows = batch1_rows + [
            self._row("10:00:05", "RUN", 105),
            self._row("10:00:06", "RUN", 106),
            self._row("10:00:07", "RUN", 107),
            self._row("10:00:08", "RUN", 108),
            self._row("10:00:31", "RUN", 131),
        ]
        self._write(batch2_rows)
        parse_log_file(self.db_path, "M13", self.log_path, "10")

        with sqlite3.connect(self.db_path) as conn:
            run_sec, holes = conn.execute(
                "SELECT run_seconds, hole_count FROM hourly_utilization "
                "WHERE machine_id='M13' AND hour=10"
            ).fetchone()
            trans_count = conn.execute(
                "SELECT COUNT(*) FROM state_transitions WHERE machine_id='M13'"
            ).fetchone()[0]
            dup_count = conn.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM state_transitions "
                "WHERE machine_id='M13' GROUP BY timestamp HAVING COUNT(*) > 1)"
            ).fetchone()[0]

        # Oracle: parse the final file once from a clean DB.
        oracle_db = os.path.join(self.temp_dir, "oracle.db")
        init_database(oracle_db)
        parse_log_file(oracle_db, "M13", self.log_path, "10")
        with sqlite3.connect(oracle_db) as conn:
            oracle_run, oracle_holes = conn.execute(
                "SELECT run_seconds, hole_count FROM hourly_utilization "
                "WHERE machine_id='M13' AND hour=10"
            ).fetchone()

        self.assertEqual(run_sec, oracle_run,
                         "Two-batch parse with replay should match single-pass parse "
                         "(got run_seconds={}, oracle={})".format(run_sec, oracle_run))
        self.assertEqual(holes, oracle_holes,
                         "Hole count should match single-pass parse "
                         "(got {}, oracle {})".format(holes, oracle_holes))
        self.assertEqual(dup_count, 0,
                         "No duplicate state_transitions should exist after replay "
                         "(found {} duplicated timestamps)".format(dup_count))
        self.assertLessEqual(run_sec, 3600,
                             "Hour total cannot exceed 3600s, got {}".format(run_sec))


class TestParserCrossMidnightReplay(unittest.TestCase):
    """Test that peek-ahead replay in a file with cross-midnight rows does
    NOT wipe prior-day data.

    Scenario: 23Drive.Log starts with a handful of 22-dated rows (firmware
    buffer flushing past midnight) followed by 23-dated rows. If a peek-
    ahead replay triggers a full re-parse of 23Drive.Log, we must only
    rebuild 23's hourly_utilization and leave 22's data (owned by
    22Drive.Log's own parse) untouched.

    Bug: before this fix, the DELETE step wiped hourly rows for every date
    appearing in 23Drive.Log's parsed rows. That destroyed 22's data, then
    re-attributed only a few stray cross-midnight seconds back — severe
    undercount on the prior day.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        init_database(self.db_path)
        self.log_path = os.path.join(self.temp_dir, "23Drive.Log")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _row(self, date_str, time_str, state, counter):
        mode = "AUTO" if state == "RUN" else "MAN"
        prog = "O100.txt" if state == "RUN" else ""
        tool = "084" if state == "RUN" else "000"
        dia = "1.000" if state == "RUN" else "0.150"
        return ("{date},{t},{m},{s},{p},20.142,276.228,{tl},{d},0000,"
                "{c},1,0,0,0,0,0,0.000,0.000,0.000,0.000,0.000,0.000").format(
            date=date_str, t=time_str, m=mode, s=state, p=prog,
            tl=tool, d=dia, c=counter,
        )

    def _write(self, rows):
        with open(self.log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")

    def _seed_prior_day(self):
        """Put realistic 22nd-of-month hourly data into DB (as if 22Drive.Log
        had already been parsed). Hour 23 has 3500s RUN, 100 holes."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO hourly_utilization "
                "(machine_id, date, hour, run_seconds, reset_seconds, "
                "stop_seconds, total_seconds, utilization, hole_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("M13", "2026-04-22", 23, 3500, 100, 0, 3600, 97.2, 100),
            )
            conn.commit()

    def test_replay_preserves_prior_day_hourly(self):
        # Simulate real production: 22's hourly row is already in DB.
        self._seed_prior_day()

        # 23Drive.Log: 3 cross-midnight rows from 22 (23:59:57/58/59), then
        # 23-dated rows with a peek-ahead at 00:00:30, then a filler batch.
        batch1 = [
            self._row("2026/04/22", "23:59:57", "RUN", 50),
            self._row("2026/04/22", "23:59:58", "RUN", 51),
            self._row("2026/04/22", "23:59:59", "RUN", 52),
            self._row("2026/04/23", "00:00:00", "RUN", 53),
            self._row("2026/04/23", "00:00:01", "RUN", 54),
            self._row("2026/04/23", "00:00:02", "RUN", 55),
            self._row("2026/04/23", "00:00:03", "RUN", 56),
            self._row("2026/04/23", "00:00:04", "RUN", 57),
            self._row("2026/04/23", "00:00:30", "RUN", 83),  # peek-ahead
        ]
        self._write(batch1)
        parse_log_file(self.db_path, "M13", self.log_path, "23")

        # Snapshot 22's hour-23 after batch 1 (cross-midnight rows were
        # legitimately attributed to 22 via UPSERT += in the normal
        # incremental path). This is the value the replay must NOT disturb.
        with sqlite3.connect(self.db_path) as conn:
            prior_after_b1 = conn.execute(
                "SELECT run_seconds, reset_seconds, stop_seconds, "
                "total_seconds, hole_count FROM hourly_utilization "
                "WHERE machine_id='M13' AND date='2026-04-22' AND hour=23"
            ).fetchone()
        self.assertIsNotNone(prior_after_b1, "Batch 1 should leave 22 hour 23 intact")
        self.assertGreaterEqual(prior_after_b1[0], 3500,
                                "Sanity: 22 hour 23 RUN should be at least seed value")

        # Batch 2: append fill-in rows earlier than the peek-ahead → replay.
        batch2 = batch1 + [
            self._row("2026/04/23", "00:00:05", "RUN", 58),
            self._row("2026/04/23", "00:00:06", "RUN", 59),
            self._row("2026/04/23", "00:00:07", "RUN", 60),
            self._row("2026/04/23", "00:00:08", "RUN", 61),
            self._row("2026/04/23", "00:00:31", "RUN", 84),
        ]
        self._write(batch2)
        parse_log_file(self.db_path, "M13", self.log_path, "23")

        with sqlite3.connect(self.db_path) as conn:
            prior_after_b2 = conn.execute(
                "SELECT run_seconds, reset_seconds, stop_seconds, "
                "total_seconds, hole_count FROM hourly_utilization "
                "WHERE machine_id='M13' AND date='2026-04-22' AND hour=23"
            ).fetchone()
            today_hour0 = conn.execute(
                "SELECT run_seconds, hole_count FROM hourly_utilization "
                "WHERE machine_id='M13' AND date='2026-04-23' AND hour=0"
            ).fetchone()

        # The replay-driven full re-parse of 23Drive.Log must NOT touch 22's
        # hourly row (which is owned by 22Drive.Log's parse). Without the
        # fix, the DELETE step wiped all dates in parsed_rows → 22's 3500+
        # seed would be destroyed and only the 3 cross-midnight seconds
        # would remain.
        self.assertIsNotNone(prior_after_b2, "Prior day (22) hour-23 row must still exist")
        self.assertEqual(prior_after_b2, prior_after_b1,
                         "Replay must not modify prior-day hourly row "
                         "(before={}, after={})".format(prior_after_b1, prior_after_b2))

        # Today's hour-0 data should match single-pass oracle on same file.
        oracle_db = os.path.join(self.temp_dir, "oracle.db")
        init_database(oracle_db)
        parse_log_file(oracle_db, "M13", self.log_path, "23")
        with sqlite3.connect(oracle_db) as conn:
            oracle = conn.execute(
                "SELECT run_seconds, hole_count FROM hourly_utilization "
                "WHERE machine_id='M13' AND date='2026-04-23' AND hour=0"
            ).fetchone()

        self.assertIsNotNone(today_hour0, "Today's hour-0 row must exist")
        self.assertEqual(today_hour0[0], oracle[0],
                         "Today RUN after replay should match oracle "
                         "(got {}, oracle {})".format(today_hour0[0], oracle[0]))
        self.assertEqual(today_hour0[1], oracle[1],
                         "Today hole_count after replay should match oracle "
                         "(got {}, oracle {})".format(today_hour0[1], oracle[1]))


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
