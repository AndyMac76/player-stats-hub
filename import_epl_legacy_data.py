"""
import_epl_legacy_data.py

One-off: imports the old "Player Stats" (EPL) project's existing
player_match_stats data (the fully-scraped 2025/26 season, 380 matches)
into this unified player_stats.db, tagged league='EPL'.

That project's schema predates the `position` column (added later, in
MLS's version) - imported rows get position=NULL, consistent with this
project's no-historical-backfill decision (nothing else - fixtures,
lineups, match_events, rolling/season stats - gets backfilled for that
season either; those start fresh from 2026/27 onward).

Safe to run more than once - uses INSERT OR IGNORE keyed on the unified
table's (league, player_id, match_id) primary key.

Usage:
    python import_epl_legacy_data.py
"""

import sqlite3

import config
import fbref_scrape_common as common

LEGACY_DB_PATH = r"C:\Users\andym\OneDrive\Projects\Player Stats\player_stats.db"
LEAGUE_KEY = "EPL"

# Column order in the OLD EPL project's player_match_stats table.
LEGACY_COLUMNS = [
    "player_id", "player_name", "team", "match_id", "match_date", "opponent",
    "venue", "minutes_played", "fouls", "tackles_won", "shots", "shots_on_target",
    "cards_yellow", "cards_red", "saves", "goals_conceded", "season", "goals",
    "assists", "penalty_goals", "penalty_attempts", "fouls_drawn", "offsides",
    "crosses", "interceptions", "own_goals", "pk_won", "pk_conceded",
]


def main():
    legacy_conn = sqlite3.connect(LEGACY_DB_PATH)
    legacy_cur = legacy_conn.cursor()

    legacy_cols = [r[1] for r in legacy_cur.execute("PRAGMA table_info(player_match_stats)")]
    missing = set(LEGACY_COLUMNS) - set(legacy_cols)
    if missing:
        raise SystemExit(f"Legacy schema is missing expected columns {missing} - aborting, check LEGACY_COLUMNS.")

    col_list_sql = ", ".join(LEGACY_COLUMNS)
    rows = legacy_cur.execute(f"SELECT {col_list_sql} FROM player_match_stats").fetchall()
    legacy_conn.close()

    print(f"Read {len(rows)} rows from legacy EPL db ({LEGACY_DB_PATH}).")

    conn = sqlite3.connect(config.DB_PATH)
    common.create_tables(conn)

    insert_rows = [(LEAGUE_KEY, *row, None) for row in rows]  # (league, <legacy cols in order>, position=NULL)

    conn.executemany(
        f"""
        INSERT OR IGNORE INTO player_match_stats (league, {col_list_sql}, position)
        VALUES (?, {", ".join(["?"] * len(LEGACY_COLUMNS))}, ?)
        """,
        insert_rows,
    )
    conn.commit()

    imported = conn.execute(
        "SELECT COUNT(*) FROM player_match_stats WHERE league = ?", (LEAGUE_KEY,)
    ).fetchone()[0]
    print(f"player_match_stats now has {imported} rows for league='{LEAGUE_KEY}'.")

    conn.close()


if __name__ == "__main__":
    main()
