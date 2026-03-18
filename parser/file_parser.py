"""FILE.Log parser — extract M98P calls from O100.txt and sub-program hole counts.

FILE.Log is latin-1 encoded. Each LoadProgram event records the full content
of the loaded file. Key data:
  - When O100.txt is loaded: extract M98P calls (sub-program references)
  - When .B/.T is loaded: extract sub-program definitions and hole counts
"""

import re
from pathlib import Path

from .db import get_conn, update_parse_offset

LOAD_HEADER = re.compile(
    r'^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) LoadProgram "(.+?)"'
)

M98P_PATTERN = re.compile(r"M98P(\d+)")

# Sub-program definition: line is just O followed by digits
O_NUMBER_PATTERN = re.compile(r"^O(\d+)$")

# Drilling coordinate: line starts with X (and may have Y)
DRILL_COORD_PATTERN = re.compile(r"^X-?\d+")

# G81 starts drill cycle, G80 ends it
G81_PATTERN = re.compile(r"^G81\b")
G80_PATTERN = re.compile(r"^G80\b")


def parse_timestamp(ts_str: str) -> str:
    """Convert '2026/03/17 00:04:47.090' to ISO8601."""
    return ts_str.replace("/", "-", 2).replace(" ", "T", 1)


def parse_file_log(file_path: Path, machine_id: str, byte_offset: int = 0,
                   db_path: Path = None, date_str: str = None) -> int:
    """Parse FILE.Log incrementally.

    For each LoadProgram event:
    - If O100.txt: extract M98P calls, update corresponding program_loads record
    - If .B/.T: extract sub-program O-numbers and hole counts

    Returns new byte offset after parsing.
    """
    if not file_path.exists():
        return byte_offset

    with open(file_path, "rb") as f:
        f.seek(byte_offset)
        raw = f.read()
        new_offset = byte_offset + len(raw)

    if not raw:
        return byte_offset

    text = raw.decode("latin-1", errors="replace")
    lines = text.splitlines()

    # Split into segments by LoadProgram headers
    segments = []
    current_header = None
    current_lines = []

    for line in lines:
        stripped = line.strip()
        m = LOAD_HEADER.match(stripped)
        if m:
            if current_header:
                segments.append((current_header, current_lines))
            current_header = {"ts": m.group(1), "path": m.group(2).strip()}
            current_lines = []
        elif current_header:
            current_lines.append(stripped)

    if current_header:
        segments.append((current_header, current_lines))

    with get_conn(db_path) as conn:
        for header, content_lines in segments:
            ts = parse_timestamp(header["ts"])
            prog_path = header["path"]
            prog_name = prog_path.replace("\\", "/").split("/")[-1].strip()

            if prog_name.upper() == "O100.TXT" or prog_name.upper().endswith(".TXT"):
                # Extract M98P calls from O100.txt
                m98p_calls = set()
                for cl in content_lines:
                    for m in M98P_PATTERN.finditer(cl):
                        m98p_calls.add(int(m.group(1)))

                if m98p_calls:
                    import json
                    calls_json = json.dumps(sorted(m98p_calls))

                    # Find the most recent program_loads for O100.txt at this timestamp
                    # and update its m98p_calls
                    conn.execute(
                        "UPDATE program_loads SET m98p_calls = ? "
                        "WHERE machine_id = ? AND load_time = ? AND program_name = ?",
                        (calls_json, machine_id, ts, prog_name),
                    )

                    # If no matching record (O100.txt may not be in TX1 LoadProgram),
                    # find the closest previous production program and update it
                    if conn.total_changes == 0:
                        row = conn.execute(
                            "SELECT id FROM program_loads "
                            "WHERE machine_id = ? AND load_time <= ? "
                            "AND program_name != 'O100.txt' "
                            "ORDER BY load_time DESC LIMIT 1",
                            (machine_id, ts),
                        ).fetchone()
                        if row:
                            conn.execute(
                                "UPDATE program_loads SET m98p_calls = ? WHERE id = ?",
                                (calls_json, row["id"]),
                            )

            elif prog_name.upper().endswith(".B") or prog_name.upper().endswith(".T"):
                # Extract sub-program definitions and hole counts
                _parse_subprograms(content_lines, prog_name, conn)

    update_parse_offset(machine_id, "FILE.Log", new_offset, date_str, db_path)
    return new_offset


def _parse_subprograms(lines: list, prog_name: str, conn) -> dict:
    """Parse sub-program definitions from .B/.T file content.

    Returns dict of {o_number: hole_count}.
    """
    results = {}
    current_o = None
    in_drill_cycle = False
    hole_count = 0

    for line in lines:
        if not line:
            continue

        m = O_NUMBER_PATTERN.match(line)
        if m:
            # Save previous sub-program
            if current_o is not None:
                results[current_o] = hole_count
            current_o = int(m.group(1))
            in_drill_cycle = False
            hole_count = 0
            continue

        if G81_PATTERN.match(line):
            in_drill_cycle = True
            continue

        if G80_PATTERN.match(line):
            in_drill_cycle = False
            continue

        if in_drill_cycle and DRILL_COORD_PATTERN.match(line):
            hole_count += 1

    # Save last sub-program
    if current_o is not None:
        results[current_o] = hole_count

    return results
