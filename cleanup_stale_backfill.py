"""
cleanup_stale_backfill.py

One-off: wipes lineups and match_events entirely for this season, since
only a handful of matches were backfilled before the canonical-ID lookup
fix existed. Cheaper to redo those few matches than to track down exactly
which ones need fixing. Re-run backfill_lineups_events.py afterwards to
repopulate everything with correct IDs.

Usage:
    python cleanup_stale_backfill.py
"""

import sqlite3
import config

conn = sqlite3.connect(config.DB_PATH)

for table in ("lineups", "match_events"):
    cur = conn.execute(f"DELETE FROM {table} WHERE season = ?", (config.CURRENT_SEASON,))
    print(f"Deleted {cur.rowcount} rows from {table}.")

conn.commit()
conn.close()
print("Done - re-run backfill_lineups_events.py to repopulate from scratch.")