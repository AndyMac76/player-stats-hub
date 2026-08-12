"""
backfill_epl_position.py

One-off: the old EPL project's scraper never captured the `position`
column (added later, in MLS's version of the schema), so all 11,492
imported 2025/26 player_match_stats rows have position=NULL. FBref's
summary page has always had this data - it just wasn't extracted.

Re-fetches the summary stat page for each of the 380 already-scraped EPL
2025/26 matches and UPDATEs just the position column in place. Same
pattern as the old project's backfill_new_stats.py (column backfill, not
a full re-scrape).

Resumable: skips matches where every row already has a non-null position.

Usage:
    python backfill_epl_position.py
"""

import sqlite3
import time

import config
import fbref_scrape_common as common
import scrape_player_match_stats as spms

LEAGUE_KEY = "EPL"
LEGACY_SEASON = "2526"


def matches_needing_position(conn):
    rows = conn.execute(
        """
        SELECT match_id FROM player_match_stats
        WHERE league = ? AND season = ?
        GROUP BY match_id
        HAVING SUM(CASE WHEN position IS NULL THEN 1 ELSE 0 END) > 0
        """,
        (LEAGUE_KEY, LEGACY_SEASON),
    ).fetchall()
    return sorted(r[0] for r in rows)


def main():
    conn = sqlite3.connect(config.DB_PATH)

    match_ids = matches_needing_position(conn)
    print(f"{len(match_ids)} EPL {LEGACY_SEASON} matches need position backfilled.")
    if not match_ids:
        conn.close()
        return

    league_cfg = config.LEAGUES[LEAGUE_KEY]
    fbref = spms.get_scraper(league_cfg["sd_league"], LEGACY_SEASON)

    updated_total = 0
    for i, match_id in enumerate(match_ids, start=1):
        print(f"[{i}/{len(match_ids)}] {match_id}...")
        try:
            summary_df = common.read_stat_with_recovery(fbref, "summary", match_id)
            if summary_df is None:
                print("    No summary data returned, skipping.")
                continue

            df = common.flatten_columns(summary_df.reset_index())
            pos_col = next((c for c in ("pos", "Pos", "position", "Position") if c in df.columns), None)
            name_col = "player" if "player" in df.columns else None
            if not pos_col or not name_col:
                print(f"    Couldn't find position/player columns. Available: {list(df.columns)}")
                continue

            updates = [(row[pos_col], LEAGUE_KEY, match_id, row[name_col]) for _, row in df.iterrows() if row[pos_col]]
            conn.executemany(
                "UPDATE player_match_stats SET position = ? "
                "WHERE league = ? AND match_id = ? AND player_name = ? AND position IS NULL",
                updates,
            )
            conn.commit()
            updated_total += len(updates)
            print(f"    Updated {len(updates)} rows.")

        except Exception as e:
            import traceback
            print(f"    ERROR on match {match_id}: {e}")
            traceback.print_exc()

        time.sleep(3)

    conn.close()
    print(f"\nDone. Updated {updated_total} rows total.")


if __name__ == "__main__":
    main()
