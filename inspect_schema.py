"""
inspect_schema.py

One-off diagnostic - checks player_match_stats and player_rolling_stats
schemas, and looks for any table that might hold a full fixture list
(needed for "last 5 team fixtures regardless of played").

Run from your MLS project folder:
    python inspect_schema.py
"""

import sqlite3
import config

conn = sqlite3.connect(config.DB_PATH)
cur = conn.cursor()

print("=== Tables in database ===")
tables = cur.execute(
    "SELECT name FROM sqlite_master WHERE type='table'"
).fetchall()
for t in tables:
    print(" -", t[0])

print("\n=== player_match_stats columns ===")
cols = cur.execute("PRAGMA table_info(player_match_stats)").fetchall()
for c in cols:
    print(" -", c[1], f"({c[2]})")

print("\n=== Sample row from player_match_stats ===")
sample = cur.execute("SELECT * FROM player_match_stats LIMIT 1").fetchone()
col_names = [c[1] for c in cols]
if sample:
    for name, val in zip(col_names, sample):
        print(f"   {name}: {val}")

print("\n=== Row count check: does player_match_stats include 0-minute rows? ===")
zero_min = cur.execute(
    "SELECT COUNT(*) FROM player_match_stats WHERE minutes_played = 0"
).fetchone()[0]
total = cur.execute("SELECT COUNT(*) FROM player_match_stats").fetchone()[0]
print(f"   {zero_min} rows with 0 minutes, out of {total} total rows")

conn.close()
