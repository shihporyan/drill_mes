"""TX1.Log parser — LoadProgram events and operator actions.

TX1.Log is CP932 encoded. Key events:
  LoadProgram(D:/Takeuchi/NcProgram/O2603044.B)
  OpeLog : BUTTON PUSH SCREEN:[TOUCH PANEL] BUTTON:[START]
"""

import re
from pathlib import Path

from .db import get_conn, update_parse_offset, update_machine_state

LOAD_PROGRAM_PATTERN = re.compile(
    r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d{3}).*LoadProgram\((.+?)\s*\)"
)


def parse_timestamp(ts_str: str) -> str:
    """Convert '2026/03/17 00:04:47.090' to ISO8601."""
    return ts_str.replace("/", "-", 2).replace(" ", "T", 1)


def extract_program_info(program_path: str) -> dict:
    """Extract program name, work order, and side from path.

    'D:\\Takeuchi\\NcProgram\\O2603044.B' →
        name='O2603044.B', work_order='WD-2603044', side='B'

    'D:\\Takeuchi\\NcProgram\\O100.txt' →
        name='O100.txt', work_order=None, side=None
    """
    name = program_path.replace("\\", "/").split("/")[-1].strip()
    info = {"name": name, "work_order": None, "side": None}

    # Check for production program pattern: O{digits}.B or O{digits}.T
    m = re.match(r"^O(\d+)\.(B|T)$", name, re.IGNORECASE)
    if m:
        info["work_order"] = f"WD-{m.group(1)}"
        info["side"] = m.group(2).upper()

    return info


def parse_tx1(file_path: Path, machine_id: str, byte_offset: int = 0,
              db_path: Path = None, date_str: str = None) -> int:
    """Parse TX1.Log incrementally from byte_offset.

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

    text = raw.decode("cp932", errors="replace")
    lines = text.splitlines()

    program_events = []
    last_production_program = None

    for line in lines:
        line = line.strip()
        if not line:
            continue

        m = LOAD_PROGRAM_PATTERN.match(line)
        if m:
            ts = parse_timestamp(m.group(1))
            prog_path = m.group(2).strip()
            info = extract_program_info(prog_path)

            program_events.append((
                machine_id, ts, prog_path, info["name"],
                info["work_order"], info["side"], None,  # m98p_calls filled by file_parser
            ))

            # Track last production program (not O100.txt)
            if info["work_order"]:
                last_production_program = info["name"]

    # Write to database
    with get_conn(db_path) as conn:
        if program_events:
            conn.executemany(
                "INSERT INTO program_loads "
                "(machine_id, load_time, program_path, program_name, work_order, side, m98p_calls) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                program_events,
            )

    # Update parse offset
    update_parse_offset(machine_id, "TX1.Log", new_offset, date_str, db_path)

    # Update machine state with last production program
    if last_production_program:
        update_machine_state(
            machine_id, db_path,
            current_program=last_production_program,
        )

    return new_offset
