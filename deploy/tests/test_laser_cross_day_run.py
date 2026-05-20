"""Regression tests for laser_log_parser cross-midnight RUN handling.

Covers the failure mode where a RUN_START on day N-1 with no matching
RUN_END (machine still drilling) leaves day N's ClsLaserCom with zero
AUTO_RUN events: detect_current_state previously fell back to
RESET/pm_last, machine_current_state silently flipped, and
state_transitions never recorded the original start — causing the
dashboard to report multi-day phantom idle time even while
hourly_utilization.hole_count showed active drilling.
"""

import datetime
import os
import sys
import sqlite3
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from parsers.laser_log_parser import (
    AUTO_RUN_START,
    AUTO_RUN_END,
    parse_laser_machine,
    find_active_cross_day_run_start,
)
from db.init_db import init_database


def _fmt_ts(dt):
    return dt.strftime("%Y/%m/%d %H:%M:%S") + ":000"


def _write_laser_com(path, events):
    """events: list of (datetime, 'START'|'END')."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for dt, kind in events:
            tag = AUTO_RUN_START if kind == "START" else AUTO_RUN_END
            f.write("{} {}\n".format(_fmt_ts(dt), tag))


def _write_physical_memory(path, timestamps):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for dt in timestamps:
            f.write("{} heartbeat\n".format(_fmt_ts(dt)))


def _write_plc_trd(path, hole_events):
    """hole_events: list of (datetime, station_number)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for dt, station in hole_events:
            f.write(
                "{} 本加工データ取得 加工基盤番号:{}\n".format(
                    dt.strftime("%Y/%m/%d %H:%M:%S"), station,
                )
            )


def _make_layout(root, machine_id, date, files):
    """files: dict of component_name -> writer-fn that gets the path."""
    date_str = date.strftime("%Y%m%d")
    day_dir = os.path.join(root, machine_id, date_str)
    os.makedirs(day_dir, exist_ok=True)
    for component, writer in files.items():
        path = os.path.join(
            day_dir, "{}_{}.log".format(date_str, component),
        )
        writer(path)


class CrossDayRunCarryoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="drill_laser_test_")
        self.backup_root = os.path.join(self.tmp, "DrillLogs")
        self.db_path = os.path.join(self.tmp, "test.db")
        init_database(self.db_path)
        self.machine_id = "L4"
        self.today = datetime.date(2026, 5, 19)
        self.yesterday = self.today - datetime.timedelta(days=1)
        self.run_start = datetime.datetime(2026, 5, 18, 18, 42, 10)

    def _layout_yesterday_unclosed_run(self):
        """Yesterday: a brief closed run, then an AUTO_RUN_START with no END."""
        events = [
            (datetime.datetime(2026, 5, 18, 18, 38, 31), "START"),
            (datetime.datetime(2026, 5, 18, 18, 40, 45), "END"),
            (self.run_start, "START"),  # still open at file tail
        ]
        _make_layout(
            self.backup_root, self.machine_id, self.yesterday,
            {
                "ClsLaserCom": lambda p: _write_laser_com(p, events),
                "PhysicalMemory": lambda p: _write_physical_memory(
                    p,
                    [datetime.datetime(2026, 5, 18, h, 0) for h in range(8, 24)],
                ),
                "ClsPLCTrd": lambda p: _write_plc_trd(p, []),
            },
        )

    def _layout_today_silent_but_drilling(self):
        """Today: ClsLaserCom has zero AUTO_RUN events; PLC log has hole events
        every minute (machine still drilling), PhysicalMemory has heartbeats."""
        _make_layout(
            self.backup_root, self.machine_id, self.today,
            {
                "ClsLaserCom": lambda p: _write_laser_com(p, []),
                "PhysicalMemory": lambda p: _write_physical_memory(
                    p,
                    [
                        datetime.datetime(2026, 5, 19, h, m)
                        for h in range(0, 18) for m in (0, 30)
                    ],
                ),
                "ClsPLCTrd": lambda p: _write_plc_trd(
                    p,
                    [
                        (datetime.datetime(2026, 5, 19, h, m, 0), "3")
                        for h in range(0, 18) for m in range(0, 60, 5)
                    ],
                ),
            },
        )

    def test_walk_back_finds_original_run_start(self):
        self._layout_yesterday_unclosed_run()
        start = find_active_cross_day_run_start(
            self.backup_root, self.machine_id, self.today,
        )
        self.assertEqual(start, self.run_start)

    def test_walk_back_stops_when_yesterday_closed(self):
        events = [
            (datetime.datetime(2026, 5, 18, 8, 0), "START"),
            (datetime.datetime(2026, 5, 18, 9, 0), "END"),
        ]
        _make_layout(
            self.backup_root, self.machine_id, self.yesterday,
            {"ClsLaserCom": lambda p: _write_laser_com(p, events)},
        )
        start = find_active_cross_day_run_start(
            self.backup_root, self.machine_id, self.today,
        )
        self.assertIsNone(start)

    def test_walk_back_traverses_empty_middle_day(self):
        """N-2 has the START, N-1 has zero AUTO_RUN events (middle of batch)."""
        n_minus_2 = self.today - datetime.timedelta(days=2)
        run_start_n2 = datetime.datetime(2026, 5, 17, 16, 40, 33)
        _make_layout(
            self.backup_root, self.machine_id, n_minus_2,
            {"ClsLaserCom": lambda p: _write_laser_com(
                p, [(run_start_n2, "START")],
            )},
        )
        _make_layout(
            self.backup_root, self.machine_id, self.yesterday,
            {"ClsLaserCom": lambda p: _write_laser_com(p, [])},
        )
        start = find_active_cross_day_run_start(
            self.backup_root, self.machine_id, self.today,
        )
        self.assertEqual(start, run_start_n2)

    def test_walk_back_traverses_missing_middle_day(self):
        """N-1 has NO ClsLaserCom file at all (a continuously-running machine
        emits no state events, so the controller writes no ClsLaserCom that
        day) but DOES have a PhysicalMemory heartbeat. This is the production
        L4 5/18->5/20 case the original empty-file test missed: walk-back must
        treat the missing file as a silent middle day and keep going to N-2,
        not stop at the gap."""
        n_minus_2 = self.today - datetime.timedelta(days=2)
        run_start_n2 = datetime.datetime(2026, 5, 17, 16, 40, 33)
        _make_layout(
            self.backup_root, self.machine_id, n_minus_2,
            {"ClsLaserCom": lambda p: _write_laser_com(
                p, [(run_start_n2, "START")],
            )},
        )
        # Yesterday: NO ClsLaserCom, only a PhysicalMemory heartbeat.
        _make_layout(
            self.backup_root, self.machine_id, self.yesterday,
            {"PhysicalMemory": lambda p: _write_physical_memory(
                p, [datetime.datetime(2026, 5, 18, h, 0) for h in range(0, 24)],
            )},
        )
        start = find_active_cross_day_run_start(
            self.backup_root, self.machine_id, self.today,
        )
        self.assertEqual(start, run_start_n2)

    def test_walk_back_stops_when_machine_was_down(self):
        """N-1 has neither ClsLaserCom nor PhysicalMemory (machine was off):
        the RUN could not have spanned it, so walk-back must stop."""
        n_minus_2 = self.today - datetime.timedelta(days=2)
        run_start_n2 = datetime.datetime(2026, 5, 17, 16, 40, 33)
        _make_layout(
            self.backup_root, self.machine_id, n_minus_2,
            {"ClsLaserCom": lambda p: _write_laser_com(
                p, [(run_start_n2, "START")],
            )},
        )
        # Yesterday: nothing at all (no dir contents).
        os.makedirs(
            os.path.join(self.backup_root, self.machine_id,
                         self.yesterday.strftime("%Y%m%d")),
            exist_ok=True,
        )
        start = find_active_cross_day_run_start(
            self.backup_root, self.machine_id, self.today,
        )
        self.assertIsNone(start)

    def test_parse_laser_machine_recovers_state_and_since(self):
        self._layout_yesterday_unclosed_run()
        self._layout_today_silent_but_drilling()

        log_dir = os.path.join(
            self.backup_root, self.machine_id, self.today.strftime("%Y%m%d"),
        )
        parse_laser_machine(
            self.db_path, self.machine_id, log_dir,
            programs_dir=None, date_str=self.today.strftime("%Y%m%d"),
            backup_root=self.backup_root,
        )

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT state, since FROM machine_current_state WHERE machine_id=?",
            (self.machine_id,),
        ).fetchone()
        self.assertIsNotNone(row, "machine_current_state row missing")
        self.assertEqual(row["state"], "RUN")
        self.assertEqual(row["since"], "2026-05-18T18:42:10")

        # state_transitions must include RESET->RUN at the original start
        # (yesterday) — this is what compute_effective_since walks for the
        # dashboard "since" timer.
        tx = conn.execute(
            "SELECT timestamp, from_state, to_state FROM state_transitions "
            "WHERE machine_id=? ORDER BY timestamp",
            (self.machine_id,),
        ).fetchall()
        transitions = [(r["timestamp"], r["from_state"], r["to_state"])
                       for r in tx]
        self.assertIn(
            ("2026-05-18T18:42:10", "RESET", "RUN"), transitions,
            "missing the original cross-day RESET->RUN transition: {}".format(
                transitions,
            ),
        )

        # Hourly utilization for today must show non-zero RUN seconds even
        # though today's ClsLaserCom is silent — the carryover interval should
        # be clipped to today's pm_first/pm_last range.
        total_run = conn.execute(
            "SELECT SUM(run_seconds) FROM hourly_utilization "
            "WHERE machine_id=? AND date='2026-05-19'",
            (self.machine_id,),
        ).fetchone()[0]
        self.assertGreater(
            total_run, 0,
            "today's run_seconds should be > 0 when yesterday's RUN is still open",
        )

    def test_no_override_without_plc_hole_corroboration(self):
        """Stale cross-day evidence (yesterday log truncated) but no drilling
        today → must NOT force state=RUN (machine genuinely idle)."""
        self._layout_yesterday_unclosed_run()
        # Today: silent ClsLaserCom, heartbeats present, but ZERO hole events.
        _make_layout(
            self.backup_root, self.machine_id, self.today,
            {
                "ClsLaserCom": lambda p: _write_laser_com(p, []),
                "PhysicalMemory": lambda p: _write_physical_memory(
                    p, [datetime.datetime(2026, 5, 19, 0, 0)],
                ),
                "ClsPLCTrd": lambda p: _write_plc_trd(p, []),
            },
        )

        log_dir = os.path.join(
            self.backup_root, self.machine_id, self.today.strftime("%Y%m%d"),
        )
        parse_laser_machine(
            self.db_path, self.machine_id, log_dir,
            programs_dir=None, date_str=self.today.strftime("%Y%m%d"),
            backup_root=self.backup_root,
        )

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT state FROM machine_current_state WHERE machine_id=?",
            (self.machine_id,),
        ).fetchone()
        self.assertNotEqual(
            row["state"], "RUN",
            "must not force RUN without PLC hole corroboration",
        )

    def test_open_interval_writes_reset_to_run_transition(self):
        """A same-day AUTO_RUN_START with no matching END should still produce
        a RESET->RUN transition row — previously these were skipped, leaving
        compute_effective_since to fall back to an older transition."""
        run_start_today = datetime.datetime(2026, 5, 19, 9, 0, 0)
        _make_layout(
            self.backup_root, self.machine_id, self.today,
            {
                "ClsLaserCom": lambda p: _write_laser_com(
                    p, [(run_start_today, "START")],
                ),
                "PhysicalMemory": lambda p: _write_physical_memory(
                    p,
                    [datetime.datetime(2026, 5, 19, 9, 0),
                     datetime.datetime(2026, 5, 19, 10, 0)],
                ),
                "ClsPLCTrd": lambda p: _write_plc_trd(p, []),
            },
        )

        log_dir = os.path.join(
            self.backup_root, self.machine_id, self.today.strftime("%Y%m%d"),
        )
        parse_laser_machine(
            self.db_path, self.machine_id, log_dir,
            programs_dir=None, date_str=self.today.strftime("%Y%m%d"),
            backup_root=self.backup_root,
        )

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        tx = conn.execute(
            "SELECT timestamp, from_state, to_state FROM state_transitions "
            "WHERE machine_id=?",
            (self.machine_id,),
        ).fetchall()
        self.assertIn(
            ("2026-05-19T09:00:00", "RESET", "RUN"),
            [(r["timestamp"], r["from_state"], r["to_state"]) for r in tx],
        )


if __name__ == "__main__":
    unittest.main()
