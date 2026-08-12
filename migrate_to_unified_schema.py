"""
migrate_to_unified_schema.py

One-off migration: this player_stats.db predates the multi-league schema
(no `league` column anywhere, and a few primary keys that need `league`
added to stay collision-safe across leagues - see fbref_scrape_common.create_tables()
for the target schema). Every existing row here is MLS data, so this
tags it all league='MLS' and rebuilds each table under the new schema.

SQLite can't ALTER a primary key in place, so each table is renamed aside,
recreated via the shared schema, repopulated from the renamed-aside copy
with league='MLS' filled in, then the old copy is dropped.

Safe to run more than once - tables that already have a `league` column
are skipped.

Usage:
    python migrate_to_unified_schema.py
"""

import sqlite3

import config
import fbref_scrape_common as common

LEGACY_LEAGUE = "MLS"

TABLES = [
    "fixtures", "lineups", "match_events",
    "player_match_stats", "player_rolling_stats", "player_season_stats",
]


def main():
    conn = sqlite3.connect(config.DB_PATH)
    cur = conn.cursor()

    def table_names():
        return {row[0] for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    existing = table_names()
    to_migrate = []

    for table in TABLES:
        if table not in existing:
            print(f"{table}: doesn't exist yet, nothing to migrate.")
            continue
        cols = [row[1] for row in cur.execute(f"PRAGMA table_info({table})")]
        if "league" in cols:
            print(f"{table}: already has a league column, skipping.")
            continue
        cur.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
        to_migrate.append(table)
        print(f"{table}: renamed aside to {table}_old for migration.")

    if not to_migrate:
        print("Nothing to migrate.")
        conn.close()
        return

    # Recreates only the tables we just renamed away (CREATE TABLE IF NOT EXISTS).
    common.create_tables(conn)

    for table in to_migrate:
        old_table = f"{table}_old"
        old_cols = [row[1] for row in cur.execute(f"PRAGMA table_info({old_table})")]
        col_list = ", ".join(old_cols)
        cur.execute(
            f"INSERT INTO {table} (league, {col_list}) "
            f"SELECT ?, {col_list} FROM {old_table}",
            (LEGACY_LEAGUE,),
        )
        moved = cur.rowcount
        cur.execute(f"DROP TABLE {old_table}")
        print(f"{table}: migrated {moved} rows, tagged league='{LEGACY_LEAGUE}'.")

    conn.commit()
    conn.close()
    print("\nMigration complete.")


if __name__ == "__main__":
    main()
