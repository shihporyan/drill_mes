"""
Takeuchi O100.txt parser — current board identification via M98P sub-program list.

O100.txt sits at D:\\Takeuchi\\NcProgram\\O100.txt on each machine. It is a
routing table the operator manually edits each time they change boards.

Structure:
    O100              ← entry section (the "currently drilling" subset)
    M98P127           ← call sub-program O127
    M98P128
    M98P102
    M99               ← return

    O200              ← manual-mode section (single tool, not board-specific)
    M98P100
    M02

    O300 / O400 / O500 / O990  ← other auxiliary modes

Only the O100 section is board-identifying. Other sections are operator
tooling/setup macros.

The active sub-program numbers (M98P###) cross-reference the WD-specific
NC table Excel (top-nc / bot-nc sheets), which maps each sub to a board
(A/B/C/D/E/F/G).

Two extraction sources:
    - M14-style:  FILE.Log dumps full O100.txt content on each LoadProgram
    - M13-style:  FILE.Log doesn't dump; must read live SMB file at
                  TX1 LoadProgram event time.

This module is pure parsing — no DB I/O. See tools/dev_extract_o100.py
for the dev-mode extraction driver.

Reference: notes/mech_drill_board_identification.md
"""

import hashlib
import re
import datetime


# Section header on its own line: "O100", "O200", ..., "O990"
SECTION_RE = re.compile(r"^O(\d+)\s*$")

# Sub-program call. Captures the sub number; ignores L## repeat suffix.
M98P_RE = re.compile(r"M98P(\d+)")

# FILE.Log LoadProgram header (M14-style content dump).
# Example: 2026/05/02 16:01:56.135 LoadProgram "D:\Takeuchi\NcProgram\O100.txt "
FILE_LOG_LOAD_RE = re.compile(
    r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+LoadProgram\s+\"([^\"]+)\"\s*$"
)

# TX1.Log LoadProgram event (machine-agnostic signal).
# Example: 2026/05/02 07:51:50.104 ｙReadProgramｚLoadProgram(D:\Takeuchi\NcProgram\O100.txt )
# The ReadProgram tag is in CP932; we match by the bracketed LoadProgram(...) pattern instead.
TX1_LOAD_RE = re.compile(
    r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+.*LoadProgram\(([^)]+)\)"
)


def parse_o100_content(text):
    """Parse O100.txt content into structured sections.

    Args:
        text: Full O100.txt content as a single string.

    Returns:
        dict with:
            active_subs: list of int — M98P numbers under O100 entry section
            sections: dict[str, list[int]] — all O### sections → sub lists
            content_hash: sha256 hex of normalized content (for dedup)
            raw_lines: int line count
    """
    sections = {}
    current_section = None
    raw_lines = 0

    for line in text.splitlines():
        raw_lines += 1
        stripped = line.strip()
        if not stripped:
            continue

        m_section = SECTION_RE.match(stripped)
        if m_section:
            current_section = "O" + m_section.group(1)
            sections.setdefault(current_section, [])
            continue

        if current_section is None:
            continue

        for m_sub in M98P_RE.finditer(stripped):
            sections[current_section].append(int(m_sub.group(1)))

    active_subs = sections.get("O100", [])

    # Hash on normalized form so cosmetic differences (trailing whitespace,
    # CRLF vs LF, trailing blank lines) don't create spurious distinct snapshots.
    # Critical for cross-validating M14 FILE.Log dumps against live SMB reads —
    # the live file often has an extra trailing newline the dump doesn't capture.
    normalized = "\n".join(line.rstrip() for line in text.splitlines()).rstrip()
    content_hash = hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()

    return {
        "active_subs": active_subs,
        "sections": sections,
        "content_hash": content_hash,
        "raw_lines": raw_lines,
    }


def extract_dumps_from_file_log(file_log_path):
    """Extract all O100.txt dumps from M14-style FILE.Log.

    M14 FILE.Log dumps the full O100.txt content after each LoadProgram
    event, terminated by a "[EOF]" marker line.

    Args:
        file_log_path: path to FILE.Log

    Returns:
        list of dicts with: timestamp (datetime), content (str), parsed (dict from parse_o100_content)
        Sorted by timestamp ascending. Only O100.txt loads are included
        (other file loads are filtered out).
    """
    with open(file_log_path, "rb") as f:
        raw = f.read()
    # FILE.Log mixes CP932 prose with ASCII content. Decode permissively.
    text = raw.decode("cp932", errors="replace")

    dumps = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = FILE_LOG_LOAD_RE.match(lines[i])
        if not m:
            i += 1
            continue

        ts_str, path = m.group(1), m.group(2).strip()
        # Filter to O100.txt loads only (path may end with " " due to trailing space in log)
        if not path.rstrip().lower().endswith("o100.txt"):
            i += 1
            continue

        try:
            timestamp = datetime.datetime.strptime(ts_str, "%Y/%m/%d %H:%M:%S.%f")
        except ValueError:
            i += 1
            continue

        # Collect content until [EOF] marker or next LoadProgram line
        content_lines = []
        i += 1
        while i < len(lines):
            line = lines[i]
            if line.strip() == "[EOF]":
                i += 1
                break
            if FILE_LOG_LOAD_RE.match(line):
                # Next dump started without explicit [EOF] — back off so outer loop sees it
                break
            content_lines.append(line)
            i += 1

        content = "\n".join(content_lines)
        parsed = parse_o100_content(content)
        dumps.append({
            "timestamp": timestamp,
            "content": content,
            "parsed": parsed,
        })

    dumps.sort(key=lambda d: d["timestamp"])
    return dumps


def find_load_events_in_tx1(tx1_path):
    """Find all O100.txt LoadProgram events in TX1.Log (machine-agnostic).

    Used by M13-style flow where FILE.Log doesn't dump content — the TX1
    event is the trigger to read the live SMB file.

    Args:
        tx1_path: path to TX1.Log

    Returns:
        list of dicts with: timestamp (datetime), path (str)
        Sorted by timestamp ascending.
    """
    with open(tx1_path, "rb") as f:
        raw = f.read()
    text = raw.decode("cp932", errors="replace")

    events = []
    for line in text.splitlines():
        m = TX1_LOAD_RE.match(line)
        if not m:
            continue
        ts_str, path = m.group(1), m.group(2).strip()
        if not path.rstrip().lower().endswith("o100.txt"):
            continue
        try:
            timestamp = datetime.datetime.strptime(ts_str, "%Y/%m/%d %H:%M:%S.%f")
        except ValueError:
            continue
        events.append({"timestamp": timestamp, "path": path})

    events.sort(key=lambda e: e["timestamp"])
    return events
