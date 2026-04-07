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
            "/api/drilling/heatmap": self._handle_heatmap,
            "/api/drilling/transitions": self._handle_transitions,
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
        all_machines = machines_config["machines"]

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

        # Build machine list
        machines = []
        summary = {"running": 0, "idle": 0, "stopped": 0, "offline": 0, "total": len(all_machines)}

        for m in all_machines:
            mid = m["id"]
            state_data = state_rows.get(mid)
            util_data = daily_util.get(mid)
            health_data = health_rows.get(mid)

            is_online = health_data and health_data.get("is_online", 0) == 1

            if state_data:
                state = state_data.get("state", "UNKNOWN")
                since = state_data.get("since", "")
                duration_minutes = 0
                if since:
                    try:
                        since_dt = datetime.datetime.fromisoformat(since)
                        duration_minutes = int((now - since_dt).total_seconds() / 60)
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
                })

                if state == "RUN":
                    summary["running"] += 1
                elif state == "RESET":
                    summary["idle"] += 1
                elif state == "STOP":
                    summary["stopped"] += 1
            else:
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
        settings = self.server.settings
        target = settings.get("utilization_target", 75)

        with self._get_db() as conn:
            if period == "day":
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
                cursor = conn.execute(
                    "SELECT machine_id, "
                    "SUM(run_seconds) as run_seconds, "
                    "SUM(total_seconds) as total_seconds, "
                    "SUM(hole_count) as hole_count "
                    "FROM hourly_utilization "
                    "WHERE date BETWEEN ? AND ? "
                    "GROUP BY machine_id ORDER BY machine_id",
                    (week_start.isoformat(), week_end.isoformat()),
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
                    "WHERE date LIKE ? "
                    "GROUP BY machine_id ORDER BY machine_id",
                    (month_prefix + "%",),
                )
            else:
                self._send_error(400, "Invalid period: {}".format(period))
                return

            rows = cursor.fetchall()

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

    def _handle_heatmap(self, params):
        """Handle GET /api/drilling/heatmap.

        Returns hourly utilization for all machines on a given date.

        Args:
            params: Query parameters (date).
        """
        date_str = params.get("date", datetime.date.today().isoformat())

        with self._get_db() as conn:
            cursor = conn.execute(
                "SELECT machine_id, hour, utilization, hole_count "
                "FROM hourly_utilization WHERE date=? "
                "ORDER BY machine_id, hour",
                (date_str,),
            )
            rows = cursor.fetchall()

        # Group by machine
        machine_data = {}
        for r in rows:
            mid = r["machine_id"]
            if mid not in machine_data:
                machine_data[mid] = []
            machine_data[mid].append({
                "hour": r["hour"],
                "utilization": r["utilization"],
                "hole_count": r["hole_count"],
            })

        machines = [{"id": mid, "hours": hours} for mid, hours in sorted(machine_data.items())]

        self._send_json({
            "date": date_str,
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
