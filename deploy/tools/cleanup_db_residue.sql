-- One-shot DB cleanup for residual / corrupt rows.
--
-- Run on production with:
--   sqlite3 C:\DrillMonitor\drill_monitor.db < tools\cleanup_db_residue.sql
--
-- Or:
--   sqlite3 C:\DrillMonitor\drill_monitor.db ".read tools/cleanup_db_residue.sql"
--
-- The dashboard already filters anything before 2026-04-01 via DATA_START_DATE
-- in api_server.py, so this script is optional for visual cleanup. Running it
-- shrinks the DB and removes confusing rows from ad-hoc SQL queries.
--
-- All deletes are scoped — no schema changes, no full-table wipes.

BEGIN TRANSACTION;

-- 1. Pre-cutover residual (sparse rows on 2026-03-25..27 from initial laser
--    testing — 1 row + 144 rows + 72 rows). Confirmed in usb_sample on
--    2026-04-28: those dates predate the 2026-04-22 production cutover.
DELETE FROM hourly_utilization WHERE date < '2026-04-01';
DELETE FROM state_transitions  WHERE timestamp < '2026-04-01';

-- 2. Backfill peek-ahead bug residue. Two single-hour outliers with
--    physically impossible counts (10^8 holes drilled in one hour).
--    Created before commit 5faeabe (peek-ahead replay over-count fix,
--    2026-04-23). Each row blows up the daily / weekly / monthly sums.
--
--    M03 / 2026-04-22 / 15:00 : 347,132,624 holes (util 0%)
--    M07 / 2026-04-21 / 20:00 : 197,216,829 holes (util 16.4%)
DELETE FROM hourly_utilization
WHERE machine_id = 'M03' AND date = '2026-04-22' AND hour = 15;

DELETE FROM hourly_utilization
WHERE machine_id = 'M07' AND date = '2026-04-21' AND hour = 20;

-- 3. Defensive sweep — any remaining row with hole_count > 50,000,000
--    (a single machine cannot drill > 50M holes in one hour; physical
--    limit is ~10K-50K). Catches future occurrences of the same bug.
--    If this is empty, nothing changes.
DELETE FROM hourly_utilization WHERE hole_count > 50000000;

COMMIT;

-- Reclaim disk after the deletes.
VACUUM;

-- Verify after running:
--   SELECT MIN(date), MAX(date), COUNT(*) FROM hourly_utilization;
--   SELECT machine_id, MAX(hole_count) FROM hourly_utilization GROUP BY machine_id;
