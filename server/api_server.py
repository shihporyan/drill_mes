"""
HTTP API server for drill monitoring dashboard.

Provides 4 endpoints using stdlib http.server:
- GET /api/drilling/overview
- GET /api/drilling/utilization?period=day|week|month&date=...
- GET /api/drilling/heatmap?date=YYYY-MM-DD
- GET /api/drilling/transitions?machine=...&date=...

Binds to office NIC IP from settings.json (never 0.0.0.0).
Serves static files from web/ directory.

Usage:
    python server/api_server.py
"""

import datetime
import json
import logging
import os
import sqlite3
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from parsers.base_parser import load_settings, load_machines_config, get_db_path, get_enabled_machines

logger = logging.getLogger(__name__)

# Maximum non-current-state gap that still counts as a "flicker" when computing
# effective_since. Operators routinely tap RESET/STOP for a few seconds during
# tool changes or pauses, then resume the same RUN; without this collapse the
# dashboard duration re-anchors on every tap and never reflects the actual
# continuous run length (M14/M18 incident, 2026-04-28: 18h true run displayed
# as 2h-3h because of 4-32s STOP transients).
SINCE_FLICKER_SECONDS = 60

# Earliest date with trustworthy production data. Anything before is residual
# from pre-cutover dev runs (sparse rows on 2026-03-25..27 from initial laser
# testing) and creates visual noise in trend / month views. All trend, heatmap,
# and utilization queries filter on date >= this value. If the production
# baseline shifts (e.g. another fresh cutover), update this constant.
DATA_START_DATE = "2026-04-21"


def _bool_param(params, key, default):
    """Parse a query-string param as bool. Accepts '1'/'0'/'true'/'false'/'yes'/'no'."""
    v = params.get(key)
    if v is None:
        return default
    return str(v).lower() in ("1", "true", "yes")


def _weekend_clause(include_weekends, date_col="date"):
    """SQL fragment to exclude Sat/Sun from aggregate queries.

    SQLite strftime('%w', text) returns 0 for Sunday, 6 for Saturday.
    Returns empty string when weekends are included (no filter).
    """
    if include_weekends:
        return ""
    return " AND CAST(strftime('%w', {}) AS INTEGER) NOT IN (0, 6)".format(date_col)


def compute_effective_since(conn, machine_id, state, db_since):
    """Return the start of the current uninterrupted state run, ignoring
    flickers (non-current-state gaps lasting <= SINCE_FLICKER_SECONDS).

    Walks state_transitions DESC starting from the most recent ?->state row;
    for each immediately-prior state->? exit, if the gap to the next ?->state
    re-entry is short enough, treats the gap as a flicker and continues
    walking back. Stops on the first sustained gap or when transitions
    run out.

    Returns the iso timestamp string. Falls back to db_since if state_transitions
    has no row for this machine + state (fresh DB / new machine).
    """
    cursor = conn.execute(
        "SELECT timestamp, from_state, to_state FROM state_transitions "
        "WHERE machine_id=? ORDER BY timestamp DESC LIMIT 500",
        (machine_id,),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    if not rows:
        return db_since

    # Index of the most recent transition into the current state.
    i = 0
    while i < len(rows) and rows[i]["to_state"] != state:
        i += 1
    if i >= len(rows):
        return db_since
    since = rows[i]["timestamp"]

    while True:
        # Find the most recent state->? exit older than `since`. The gap
        # between that exit and `since` is the time we spent outside the
        # current state. (Skipping rows where from_state != state lets us
        # collapse multi-hop flickers like RUN->STOP->RESET->STOP->RUN.)
        k = i + 1
        while k < len(rows) and rows[k]["from_state"] != state:
            k += 1
        if k >= len(rows):
            break
        try:
            gap = (datetime.datetime.fromisoformat(since)
                   - datetime.datetime.fromisoformat(rows[k]["timestamp"])
                   ).total_seconds()
        except (ValueError, TypeError):
            break
        if gap > SINCE_FLICKER_SECONDS:
            break

        # Walk back to the ?->state entry that started this earlier run.
        j = k + 1
        while j < len(rows) and rows[j]["to_state"] != state:
            j += 1
        if j >= len(rows):
            break
        since = rows[j]["timestamp"]
        i = j

    return since


class DrillAPIHandler(BaseHTTPRequestHandler):
    """HTTP request handler for drill monitoring API."""

    def log_message(self, format, *args):
        """Route HTTP access logs through Python logging."""
        logger.info("HTTP %s", format % args)

    def _send_json(self, data, status=200):
        """Send a JSON response with CORS headers.

        Args:
            data: Dict to serialize as JSON.
            status: HTTP status code.
        """
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status, message):
        """Send a JSON error response.

        Args:
            status: HTTP status code.
            message: Error description.
        """
        self._send_json({"error": message}, status)

    def _get_db(self):
        """Get a database connection from server context.

        Returns:
            sqlite3.Connection
        """
        conn = sqlite3.connect(self.server.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def do_OPTIONS(self):
        """Handle CORS preflight requests."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        """Route GET requests to appropriate handler."""
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        # Flatten single-value params
        params = {k: v[0] if len(v) == 1 else v for k, v in params.items()}

        routes = {
            "/api/drilling/overview": self._handle_overview,
            "/api/drilling/utilization": self._handle_utilization,
            "/api/drilling/utilization/trend": self._handle_utilization_trend,
            "/api/drilling/heatmap": self._handle_heatmap,
            "/api/drilling/transitions": self._handle_transitions,
            "/api/drilling/work_orders": self._handle_work_orders,
        }

        handler = routes.get(path)
        if handler:
            try:
                handler(params)
            except Exception as e:
                logger.error("API error on %s: %s", path, e, exc_info=True)
                self._send_error(500, "Internal server error")
            return

        # Serve static files from web/
        if path == "/" or path == "":
            path = "/dashboard.html"

        static_path = os.path.join(PROJECT_ROOT, "web", path.lstrip("/"))
        if os.path.isfile(static_path):
            self._serve_static(static_path)
            return

        # Browser DevTools auto-fetches *.map source maps for minified JS.
        # We don't ship them — quiet 204 keeps the access log clean.
        if path.endswith(".map"):
            self.send_response(204)
            self.end_headers()
            return

        self._send_error(404, "Not found")

    def _serve_static(self, file_path):
        """Serve a static file with appropriate content type.

        Args:
            file_path: Absolute path to the file.
        """
        ext = os.path.splitext(file_path)[1].lower()
        content_types = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".jsx": "text/babel; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".png": "image/png",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }
        content_type = content_types.get(ext, "application/octet-stream")

        try:
            mode = "rb"
            with open(file_path, mode) as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            logger.error("Static file error: %s", e)
            self._send_error(500, "Failed to read file")

    def _handle_overview(self, params):
        """Handle GET /api/drilling/overview.

        Returns machine states, daily utilization summary, and health status.

        Args:
            params: Query parameters (unused).
        """
        now = datetime.datetime.now()
        today = now.strftime("%Y-%m-%d")

        machines_config = self.server.machines_config
        all_machines = [m for m in machines_config["machines"] if m.get("enabled", False)]

        with self._get_db() as conn:
            # Get current state for all machines
            cursor = conn.execute("SELECT * FROM machine_current_state")
            state_rows = {r["machine_id"]: dict(r) for r in cursor.fetchall()}

            # Get today's utilization
            cursor = conn.execute(
                "SELECT machine_id, SUM(run_seconds) as run, SUM(total_seconds) as total, "
                "SUM(hole_count) as holes "
                "FROM hourly_utilization WHERE date=? GROUP BY machine_id",
                (today,),
            )
            daily_util = {r["machine_id"]: dict(r) for r in cursor.fetchall()}

            # Get health status
            cursor = conn.execute("SELECT * FROM machine_health")
            health_rows = {r["machine_id"]: dict(r) for r in cursor.fetchall()}

            # Get next cycle time for frontend sync
            next_cycle_at = None
            try:
                cursor = conn.execute(
                    "SELECT value FROM system_status WHERE key='next_cycle_at'"
                )
                row = cursor.fetchone()
                if row:
                    next_cycle_at = row[0]
            except Exception:
                pass

            # Pre-compute flicker-collapsed since for each machine in current
            # state — done inside the with block so the connection stays open.
            effective_since = {}
            for mid, sd in state_rows.items():
                st = sd.get("state")
                db_since = sd.get("since")
                if st in ("RUN", "RESET", "STOP") and db_since:
                    effective_since[mid] = compute_effective_since(
                        conn, mid, st, db_since,
                    )

        # Build machine list
        machines = []
        summary = {"running": 0, "idle": 0, "stopped": 0, "offline": 0, "total": len(all_machines)}

        for m in all_machines:
            mid = m["id"]
            machine_type = m.get("type", "takeuchi")
            state_data = state_rows.get(mid)
            util_data = daily_util.get(mid)
            health_data = health_rows.get(mid)

            is_online = health_data and health_data.get("is_online", 0) == 1

            if state_data:
                state = state_data.get("state", "UNKNOWN")
                # `since` here is the flicker-collapsed start of the current
                # state run (ignores <=60s operator pauses); falls back to
                # the raw machine_current_state.since when transitions
                # haven't been recorded yet.
                since = effective_since.get(mid) or state_data.get("since", "")
                duration_minutes = 0
                if since:
                    try:
                        since_dt = datetime.datetime.fromisoformat(since)
                        # clamp: machine control PCs are offline (no NTP),
                        # their clocks drift vs server — `since` may briefly
                        # land in the server's future after a state transition
                        duration_minutes = max(0, int((now - since_dt).total_seconds() / 60))
                    except (ValueError, TypeError):
                        pass

                util_today = 0.0
                hole_count_today = 0
                if util_data:
                    total_secs = util_data.get("total", 0)
                    run_secs = util_data.get("run", 0)
                    util_today = round(run_secs / total_secs * 100, 1) if total_secs > 0 else 0.0
                    hole_count_today = util_data.get("holes", 0) or 0

                machines.append({
                    "id": mid,
                    "type": machine_type,
                    "state": state,
                    "mode": state_data.get("mode", ""),
                    "program": state_data.get("program", ""),
                    "tool_num": state_data.get("tool_num", ""),
                    "drill_dia": state_data.get("drill_dia", 0.0),
                    "since": since,
                    "duration_minutes": duration_minutes,
                    "util_today": util_today,
                    "hole_count_today": hole_count_today,
                    "counter": state_data.get("counter", 0),
                    "work_order": state_data.get("work_order"),
                    "work_order_side": state_data.get("work_order_side"),
                })

                if state == "RUN":
                    summary["running"] += 1
                elif state == "RESET":
                    summary["idle"] += 1
                elif state == "STOP":
                    summary["stopped"] += 1
            else:
                offline_minutes = None
                if health_data and health_data.get("offline_since"):
                    try:
                        off_dt = datetime.datetime.fromisoformat(health_data["offline_since"])
                        offline_minutes = int((now - off_dt).total_seconds() / 60)
                    except (ValueError, TypeError):
                        pass

                machines.append({
                    "id": mid,
                    "type": machine_type,
                    "state": "OFFLINE",
                    "mode": "",
                    "program": "",
                    "tool_num": "",
                    "drill_dia": 0.0,
                    "since": health_data.get("offline_since") if health_data else None,
                    "duration_minutes": offline_minutes or 0,
                    "util_today": 0.0,
                    "hole_count_today": 0,
                    "counter": 0,
                    "work_order": None,
                    "work_order_side": None,
                })
                summary["offline"] += 1

        # Build health list
        health = []
        for m in all_machines:
            mid = m["id"]
            h = health_rows.get(mid, {})
            health.append({
                "id": mid,
                "is_online": bool(h.get("is_online", 0)),
                "last_seen": h.get("last_seen"),
                "offline_since": h.get("offline_since"),
            })

        self._send_json({
            "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S"),
            "next_cycle_at": next_cycle_at,
            "machines": machines,
            "summary": summary,
            "health": health,
        })

    def _handle_utilization(self, params):
        """Handle GET /api/drilling/utilization.

        Supports period=day|week|month with corresponding date parameter.

        Args:
            params: Query parameters (period, date).
        """
        period = params.get("period", "day")
        date_str = params.get("date", datetime.date.today().isoformat())
        # include_weekends defaults to False so weekday KPIs aren't diluted by
        # mostly-idle Sat/Sun. Frontend exposes a toggle for analysts who want
        # to see whether weekend overtime is producing.
        include_weekends = _bool_param(params, "include_weekends", False)
        settings = self.server.settings
        target = settings.get("utilization_target", 75)

        with self._get_db() as conn:
            if period == "day":
                # Single-day query: weekend filter is meaningless (caller chose the day).
                if date_str < DATA_START_DATE:
                    rows = []
                    cursor = None
                else:
                    cursor = conn.execute(
                        "SELECT machine_id, "
                        "SUM(run_seconds) as run_seconds, "
                        "SUM(total_seconds) as total_seconds, "
                        "SUM(hole_count) as hole_count "
                        "FROM hourly_utilization WHERE date=? "
                        "GROUP BY machine_id ORDER BY machine_id",
                        (date_str,),
                    )
            elif period == "week":
                # Calculate week range (Mon-Sun)
                try:
                    ref_date = datetime.date.fromisoformat(date_str)
                except ValueError:
                    self._send_error(400, "Invalid date format")
                    return
                week_start = ref_date - datetime.timedelta(days=ref_date.weekday())
                week_end = week_start + datetime.timedelta(days=6)
                effective_start = max(week_start.isoformat(), DATA_START_DATE)
                cursor = conn.execute(
                    "SELECT machine_id, "
                    "SUM(run_seconds) as run_seconds, "
                    "SUM(total_seconds) as total_seconds, "
                    "SUM(hole_count) as hole_count "
                    "FROM hourly_utilization "
                    "WHERE date BETWEEN ? AND ?" + _weekend_clause(include_weekends) +
                    " GROUP BY machine_id ORDER BY machine_id",
                    (effective_start, week_end.isoformat()),
                )
            elif period == "month":
                # date format: YYYY-MM
                month_prefix = date_str[:7] if len(date_str) >= 7 else date_str
                cursor = conn.execute(
                    "SELECT machine_id, "
                    "SUM(run_seconds) as run_seconds, "
                    "SUM(total_seconds) as total_seconds, "
                    "SUM(hole_count) as hole_count "
                    "FROM hourly_utilization "
                    "WHERE date LIKE ? AND date >= ?" + _weekend_clause(include_weekends) +
                    " GROUP BY machine_id ORDER BY machine_id",
                    (month_prefix + "%", DATA_START_DATE),
                )
            else:
                self._send_error(400, "Invalid period: {}".format(period))
                return

            rows = cursor.fetchall() if cursor is not None else []

        # Map machine_id -> skip_info flag so the dashboard can grey out the
        # hole_count column for machines that are knowingly without per-hole
        # data (e.g. L1 has no INFO share — hole events exist in raw logs but
        # without ProcTime{Start,End} we can't attribute them to work orders).
        machines_config = self.server.machines_config
        skip_info_ids = {
            m["id"] for m in machines_config.get("machines", [])
            if m.get("skip_info")
        }

        machines = []
        total_run = 0
        total_secs = 0
        for r in rows:
            run = r["run_seconds"] or 0
            total = r["total_seconds"] or 0
            util = round(run / total * 100, 1) if total > 0 else 0.0
            machines.append({
                "id": r["machine_id"],
                "utilization": util,
                "run_seconds": run,
                "total_seconds": total,
                "hole_count": r["hole_count"] or 0,
                "has_hole_data": r["machine_id"] not in skip_info_ids,
            })
            total_run += run
            total_secs += total

        fleet_avg = round(total_run / total_secs * 100, 1) if total_secs > 0 else 0.0

        self._send_json({
            "period": period,
            "date": date_str,
            "machines": machines,
            "fleet_average": fleet_avg,
            "target": target,
        })

    def _handle_utilization_trend(self, params):
        """Handle GET /api/drilling/utilization/trend.

        Returns time-series fleet-average utilization for drill-down charts.
        Supports three levels:
        - level=year&year=2026  -> 12 monthly data points
        - level=month&year=2026&month=3  -> weekly data points
        - level=week&year=2026&month=3&week=2  -> 7 daily data points

        Optional filter:
        - type=takeuchi|kataoka  -> restrict to machines of given type

        Args:
            params: Query parameters (level, year, month, week, type).
        """
        level = params.get("level", "year")
        year = params.get("year", str(datetime.date.today().year))
        machine_type = params.get("type")
        include_weekends = _bool_param(params, "include_weekends", False)
        weekend_sql = _weekend_clause(include_weekends)
        settings = self.server.settings
        target = settings.get("utilization_target", 75)

        # Build machine_id filter from type parameter
        type_filter_sql = ""
        type_filter_params = ()
        filtered_machine_ids = None
        if machine_type in ("takeuchi", "kataoka"):
            machines_config = self.server.machines_config
            filtered_machine_ids = [
                m["id"] for m in machines_config["machines"]
                if m.get("enabled", False) and m.get("type") == machine_type
            ]
            if filtered_machine_ids:
                placeholders = ",".join("?" * len(filtered_machine_ids))
                type_filter_sql = " AND machine_id IN ({})".format(placeholders)
                type_filter_params = tuple(filtered_machine_ids)
            else:
                # No machines match the filter - return empty result
                filtered_machine_ids = []

        with self._get_db() as conn:
            if level == "year":
                if filtered_machine_ids == []:
                    rows = []
                else:
                    # Return 12 months for the given year
                    cursor = conn.execute(
                        "SELECT substr(date,6,2) as month, "
                        "SUM(run_seconds) as run, SUM(total_seconds) as total, "
                        "SUM(hole_count) as holes "
                        "FROM hourly_utilization WHERE date LIKE ? AND date >= ?" + weekend_sql + type_filter_sql +
                        " GROUP BY substr(date,6,2) ORDER BY month",
                        (year + "%", DATA_START_DATE) + type_filter_params,
                    )
                    rows = cursor.fetchall()
                month_names = ["1月","2月","3月","4月","5月","6月","7月","8月","9月","10月","11月","12月"]
                data_map = {}
                for r in rows:
                    m = int(r["month"])
                    run = r["run"] or 0
                    total = r["total"] or 0
                    util = round(run / total * 100, 1) if total > 0 else 0
                    data_map[m] = {"util": util, "holes": r["holes"] or 0}

                now = datetime.date.today()
                current_month = now.month if str(now.year) == year else None
                data = []
                for i in range(12):
                    m = i + 1
                    entry = {"label": month_names[i], "month": m, "isCurrent": m == current_month}
                    if m in data_map:
                        entry["util"] = data_map[m]["util"]
                        entry["holes"] = data_map[m]["holes"]
                    else:
                        entry["util"] = None
                        entry["holes"] = None
                    data.append(entry)

                # Period summary uses fleet weighted-average (sum run / sum total),
                # not simple-mean of monthly buckets — otherwise an early month
                # with only a few seconds of data weighs the same as a full month.
                with_data = [d for d in data if d["util"] is not None]
                total_run = sum((r["run"] or 0) for r in rows)
                total_secs = sum((r["total"] or 0) for r in rows)
                avg_util = round(total_run / total_secs * 100, 1) if total_secs > 0 else 0
                if filtered_machine_ids is not None:
                    machine_count = len(filtered_machine_ids)
                else:
                    machine_count = conn.execute(
                        "SELECT COUNT(DISTINCT machine_id) as cnt FROM hourly_utilization WHERE date LIKE ? AND date >= ?",
                        (year + "%", DATA_START_DATE),
                    ).fetchone()["cnt"]

                self._send_json({
                    "level": "year", "year": int(year), "type": machine_type, "data": data,
                    "summary": {
                        "avg_util": avg_util, "target": target,
                        "machine_count": machine_count,
                        "max_util": max((d["util"] for d in with_data), default=0),
                        "min_util": min((d["util"] for d in with_data), default=0),
                        "max_label": max(with_data, key=lambda d: d["util"])["label"] if with_data else None,
                        "min_label": min(with_data, key=lambda d: d["util"])["label"] if with_data else None,
                    },
                })

            elif level == "month":
                month = params.get("month", str(datetime.date.today().month))
                month_prefix = "{}-{}".format(year, str(month).zfill(2))

                if filtered_machine_ids == []:
                    rows = []
                else:
                    # Get all dates in this month with data
                    cursor = conn.execute(
                        "SELECT date, SUM(run_seconds) as run, SUM(total_seconds) as total, "
                        "SUM(hole_count) as holes "
                        "FROM hourly_utilization WHERE date LIKE ? AND date >= ?" + weekend_sql + type_filter_sql +
                        " GROUP BY date ORDER BY date",
                        (month_prefix + "%", DATA_START_DATE) + type_filter_params,
                    )
                    rows = cursor.fetchall()

                # Build the calendar weeks for this month: W1 starts on the
                # Monday of the week containing day 1; successive Mondays are
                # W2, W3... up to (and including) the Monday whose week still
                # touches this month. Empty weeks are returned with util=null
                # so the label sequence stays aligned with the calendar — the
                # chart renders gaps for weeks without data instead of relabeling
                # the surviving weeks W1, W2 (the cause of the "今天 4/28 卻
                # 顯示 W2" bug).
                first_day = datetime.date(int(year), int(month), 1)
                if int(month) == 12:
                    last_day = datetime.date(int(year) + 1, 1, 1) - datetime.timedelta(days=1)
                else:
                    last_day = datetime.date(int(year), int(month) + 1, 1) - datetime.timedelta(days=1)
                w1_start = first_day - datetime.timedelta(days=first_day.weekday())

                weeks = []  # list of (week_num, week_start)
                cursor_ws = w1_start
                wn = 1
                while cursor_ws <= last_day:
                    weeks.append((wn, cursor_ws))
                    cursor_ws += datetime.timedelta(days=7)
                    wn += 1

                week_data = {ws.isoformat(): {"run": 0, "total": 0, "holes": 0}
                             for _, ws in weeks}
                for r in rows:
                    d = datetime.date.fromisoformat(r["date"])
                    ws_iso = (d - datetime.timedelta(days=d.weekday())).isoformat()
                    if ws_iso in week_data:
                        week_data[ws_iso]["run"] += (r["run"] or 0)
                        week_data[ws_iso]["total"] += (r["total"] or 0)
                        week_data[ws_iso]["holes"] += (r["holes"] or 0)

                now = datetime.date.today()
                current_week_start = now - datetime.timedelta(days=now.weekday())

                data = []
                for wn, ws in weeks:
                    wd = week_data[ws.isoformat()]
                    util = round(wd["run"] / wd["total"] * 100, 1) if wd["total"] > 0 else None
                    data.append({
                        "label": "W{}".format(wn), "week": wn,
                        "week_start": ws.isoformat(),
                        "util": util, "holes": wd["holes"] if wd["total"] > 0 else None,
                        "isCurrent": ws == current_week_start,
                    })

                # See year-level comment: weighted average across the rows so
                # an incomplete week doesn't get equal weight as a full one.
                with_data = [d for d in data if d["util"] is not None]
                total_run = sum((r["run"] or 0) for r in rows)
                total_secs = sum((r["total"] or 0) for r in rows)
                avg_util = round(total_run / total_secs * 100, 1) if total_secs > 0 else 0

                machine_count = len(filtered_machine_ids) if filtered_machine_ids is not None else None
                self._send_json({
                    "level": "month", "year": int(year), "month": int(month), "type": machine_type, "data": data,
                    "summary": {
                        "avg_util": avg_util, "target": target,
                        "machine_count": machine_count,
                        "max_util": max((d["util"] for d in with_data), default=0),
                        "min_util": min((d["util"] for d in with_data), default=0),
                        "max_label": max(with_data, key=lambda d: d["util"])["label"] if with_data else None,
                        "min_label": min(with_data, key=lambda d: d["util"])["label"] if with_data else None,
                    },
                })

            elif level == "week":
                month = params.get("month", str(datetime.date.today().month))
                week = int(params.get("week", "1"))
                week_start_str = params.get("week_start")

                if week_start_str:
                    week_start = datetime.date.fromisoformat(week_start_str)
                else:
                    # Fallback: calculate from year/month/week
                    first_day = datetime.date(int(year), int(month), 1)
                    first_monday = first_day - datetime.timedelta(days=first_day.weekday())
                    week_start = first_monday + datetime.timedelta(weeks=week - 1)

                week_end = week_start + datetime.timedelta(days=6)
                day_names = ["一","二","三","四","五","六","日"]

                if filtered_machine_ids == []:
                    rows = []
                else:
                    effective_week_start = max(week_start.isoformat(), DATA_START_DATE)
                    cursor = conn.execute(
                        "SELECT date, SUM(run_seconds) as run, SUM(total_seconds) as total, "
                        "SUM(hole_count) as holes "
                        "FROM hourly_utilization WHERE date BETWEEN ? AND ?" + weekend_sql + type_filter_sql +
                        " GROUP BY date ORDER BY date",
                        (effective_week_start, week_end.isoformat()) + type_filter_params,
                    )
                    rows = cursor.fetchall()
                date_map = {r["date"]: r for r in rows}

                now = datetime.date.today()
                data = []
                for i in range(7):
                    d = week_start + datetime.timedelta(days=i)
                    ds = d.isoformat()
                    r = date_map.get(ds)
                    if r:
                        run = r["run"] or 0
                        total = r["total"] or 0
                        util = round(run / total * 100, 1) if total > 0 else 0
                        holes = r["holes"] or 0
                    else:
                        util = None
                        holes = None
                    data.append({
                        "label": day_names[i], "date": ds,
                        "util": util, "holes": holes,
                        "isCurrent": d == now,
                    })

                # See year-level comment: weighted average across the rows so
                # a partial day doesn't get equal weight as a full one.
                with_data = [d for d in data if d["util"] is not None]
                total_run = sum((r["run"] or 0) for r in rows)
                total_secs = sum((r["total"] or 0) for r in rows)
                avg_util = round(total_run / total_secs * 100, 1) if total_secs > 0 else 0

                machine_count = len(filtered_machine_ids) if filtered_machine_ids is not None else None
                self._send_json({
                    "level": "week", "year": int(year), "month": int(month), "week": week, "type": machine_type,
                    "week_start": week_start.isoformat(), "data": data,
                    "summary": {
                        "avg_util": avg_util, "target": target,
                        "machine_count": machine_count,
                        "max_util": max((d["util"] for d in with_data), default=0),
                        "min_util": min((d["util"] for d in with_data), default=0),
                        "max_label": max(with_data, key=lambda d: d["util"])["label"] if with_data else None,
                        "min_label": min(with_data, key=lambda d: d["util"])["label"] if with_data else None,
                    },
                })
            else:
                self._send_error(400, "Invalid level: {}".format(level))

    def _handle_heatmap(self, params):
        """Handle GET /api/drilling/heatmap.

        Returns utilization heatmap aggregated at three granularities:
        - range=day  (default): 24 cells per machine (1 hour each)
        - range=week:           7 cells per machine (1 day each, Mon..Sun)
        - range=month:          N cells per machine (1 day each, day-of-month)

        For all ranges also returns per-machine shift averages computed
        from hour-level data using hour-start ownership rule:
            day   = hours [8..14]
            mid   = hours [15..22]
            night = hours [0..7, 23]

        Args:
            params: Query parameters (range, date).
                date format: YYYY-MM-DD (day/week) or YYYY-MM[-DD] (month).
        """
        range_ = params.get("range", "day")
        date_str = params.get("date", datetime.date.today().isoformat())
        # Weekend filter: only meaningful for week/month range. For single-day
        # range we leave Sat/Sun rows alone (caller asked for that specific
        # date).
        include_weekends = _bool_param(params, "include_weekends", False)

        # Resolve [start, end] date range and bucket assignment for this granularity.
        if range_ == "week":
            try:
                ref = datetime.date.fromisoformat(date_str)
            except ValueError:
                self._send_error(400, "Invalid date format")
                return
            start = ref - datetime.timedelta(days=ref.weekday())
            end = start + datetime.timedelta(days=6)
            bucket_count = 7
            bucket_labels = ["一", "二", "三", "四", "五", "六", "日"]
            def bucket_idx(date_iso, hour):
                d = datetime.date.fromisoformat(date_iso)
                return d.weekday()  # 0=Mon..6=Sun
        elif range_ == "month":
            ym = date_str[:7]
            try:
                year = int(ym[:4])
                month = int(ym[5:7])
            except (ValueError, IndexError):
                self._send_error(400, "Invalid date format")
                return
            start = datetime.date(year, month, 1)
            if month == 12:
                end = datetime.date(year + 1, 1, 1) - datetime.timedelta(days=1)
            else:
                end = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
            bucket_count = end.day
            bucket_labels = [str(d) for d in range(1, bucket_count + 1)]
            def bucket_idx(date_iso, hour):
                return int(date_iso.split("-")[2]) - 1
        else:  # day
            try:
                ref = datetime.date.fromisoformat(date_str)
            except ValueError:
                self._send_error(400, "Invalid date format")
                return
            start = ref
            end = ref
            bucket_count = 24
            bucket_labels = ["{:02d}".format(h) for h in range(24)]
            def bucket_idx(date_iso, hour):
                return hour

        with self._get_db() as conn:
            effective_start = max(start.isoformat(), DATA_START_DATE)
            # Apply weekend filter only for multi-day ranges; a day-mode caller
            # has already chosen the date so we honour their request even if
            # it falls on Sat/Sun.
            heat_weekend_sql = _weekend_clause(include_weekends) if range_ != "day" else ""
            cursor = conn.execute(
                "SELECT machine_id, date, hour, run_seconds, total_seconds, hole_count "
                "FROM hourly_utilization "
                "WHERE date BETWEEN ? AND ?" + heat_weekend_sql +
                " ORDER BY machine_id, date, hour",
                (effective_start, end.isoformat()),
            )
            rows = cursor.fetchall()

        cells_acc = {}   # mid -> [{run, total, holes}, ...] of bucket_count
        shifts_acc = {}  # mid -> {day|mid|night: {run, total}}

        def shift_of(h):
            if 8 <= h <= 14:
                return "day"
            if 15 <= h <= 22:
                return "mid"
            return "night"

        for r in rows:
            mid = r["machine_id"]
            if mid not in cells_acc:
                cells_acc[mid] = [{"run": 0, "total": 0, "holes": 0} for _ in range(bucket_count)]
                shifts_acc[mid] = {
                    "day":   {"run": 0, "total": 0},
                    "mid":   {"run": 0, "total": 0},
                    "night": {"run": 0, "total": 0},
                }
            run = r["run_seconds"] or 0
            total = r["total_seconds"] or 0
            holes = r["hole_count"] or 0
            idx = bucket_idx(r["date"], r["hour"])
            if 0 <= idx < bucket_count:
                cells_acc[mid][idx]["run"] += run
                cells_acc[mid][idx]["total"] += total
                cells_acc[mid][idx]["holes"] += holes
            sh = shift_of(r["hour"])
            shifts_acc[mid][sh]["run"] += run
            shifts_acc[mid][sh]["total"] += total

        machines = []
        for mid in sorted(cells_acc.keys()):
            cells = []
            for i, c in enumerate(cells_acc[mid]):
                util = round(c["run"] / c["total"] * 100, 1) if c["total"] > 0 else 0
                cells.append({"idx": i, "utilization": util, "hole_count": c["holes"]})
            shifts = {}
            for sh in ("day", "mid", "night"):
                s = shifts_acc[mid][sh]
                shifts[sh] = round(s["run"] / s["total"] * 100, 1) if s["total"] > 0 else 0
            machines.append({"id": mid, "cells": cells, "shifts": shifts})

        # Backward-compat: in day mode also expose machines[].hours with the
        # legacy {hour, utilization, hole_count} shape so older clients/tests
        # don't break. New code should consume `cells`.
        if range_ == "day":
            for m in machines:
                m["hours"] = [
                    {"hour": c["idx"], "utilization": c["utilization"], "hole_count": c["hole_count"]}
                    for c in m["cells"]
                ]

        self._send_json({
            "range": range_,
            "date": date_str,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "bucket_labels": bucket_labels,
            "machines": machines,
        })

    def _handle_transitions(self, params):
        """Handle GET /api/drilling/transitions.

        Returns state transition events for a machine on a given date.

        Args:
            params: Query parameters (machine, date).
        """
        machine_id = params.get("machine")
        date_str = params.get("date", datetime.date.today().isoformat())

        if not machine_id:
            self._send_error(400, "Missing 'machine' parameter")
            return

        with self._get_db() as conn:
            cursor = conn.execute(
                "SELECT timestamp, from_state, to_state, program, tool_num, drill_dia "
                "FROM state_transitions "
                "WHERE machine_id=? AND timestamp LIKE ? "
                "ORDER BY timestamp",
                (machine_id, date_str + "%"),
            )
            rows = cursor.fetchall()

        transitions = []
        for r in rows:
            transitions.append({
                "timestamp": r["timestamp"],
                "from": r["from_state"],
                "to": r["to_state"],
                "program": r["program"],
            })

        self._send_json({
            "machine_id": machine_id,
            "date": date_str,
            "transitions": transitions,
        })


    def _handle_work_orders(self, params):
        """Handle GET /api/drilling/work_orders.

        Returns laser work orders for a machine, optionally filtered by date.

        Args:
            params: Query parameters (machine, date).
        """
        machine_id = params.get("machine")
        date_str = params.get("date")

        with self._get_db() as conn:
            if machine_id and date_str:
                cursor = conn.execute(
                    "SELECT * FROM laser_work_orders "
                    "WHERE machine_id=? AND start_time LIKE ? "
                    "ORDER BY start_time",
                    (machine_id, date_str + "%"),
                )
            elif machine_id:
                cursor = conn.execute(
                    "SELECT * FROM laser_work_orders "
                    "WHERE machine_id=? ORDER BY start_time DESC LIMIT 50",
                    (machine_id,),
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM laser_work_orders "
                    "ORDER BY start_time DESC LIMIT 50"
                )

            rows = cursor.fetchall()

        work_orders = []
        for r in rows:
            work_orders.append({
                "machine_id": r["machine_id"],
                "start_time": r["start_time"],
                "end_time": r["end_time"],
                "duration_secs": r["duration_secs"],
                "station": r["station"],
                "work_order": r["work_order"],
                "lsr_file_path": r["lsr_file_path"],
                "hole_count": r["hole_count"],
            })

        self._send_json({
            "machine_id": machine_id,
            "date": date_str,
            "work_orders": work_orders,
        })


def create_server(host=None, port=None, db_path=None):
    """Create and configure the HTTP server.

    Args:
        host: Bind address. Reads from settings if None.
        port: Bind port. Reads from settings if None.
        db_path: Database path. Reads from settings if None.

    Returns:
        HTTPServer: Configured server instance.
    """
    settings = load_settings()
    machines_config = load_machines_config()

    if host is None:
        host = settings.get("http_host", "127.0.0.1")
    if port is None:
        port = settings.get("http_port", 8080)
    if db_path is None:
        db_path = get_db_path(settings)

    # Security: never bind to 0.0.0.0
    if host == "0.0.0.0":
        logger.error("Binding to 0.0.0.0 is prohibited. Using 127.0.0.1 instead.")
        host = "127.0.0.1"

    server = HTTPServer((host, port), DrillAPIHandler)
    server.db_path = db_path
    server.settings = settings
    server.machines_config = machines_config

    return server


def run_server(host=None, port=None, db_path=None):
    """Start the HTTP API server.

    Args:
        host: Bind address override.
        port: Bind port override.
        db_path: Database path override.
    """
    server = create_server(host, port, db_path)
    addr = server.server_address
    logger.info("API server starting on http://%s:%d", addr[0], addr[1])
    logger.info("Dashboard: http://%s:%d/dashboard.html", addr[0], addr[1])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server shutting down...")
        server.shutdown()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # For local development, use 127.0.0.1
    host = "127.0.0.1"
    port = 8080

    if len(sys.argv) > 1:
        host = sys.argv[1]
    if len(sys.argv) > 2:
        port = int(sys.argv[2])

    # Ensure database exists
    from db.init_db import init_database
    init_database()

    run_server(host=host, port=port)
