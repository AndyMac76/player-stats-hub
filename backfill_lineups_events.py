"""
backfill_lineups_events.py

Catch-up pass: scrape_player_match_stats.py only pulls lineups and
match_events for NEW matches (its resumability check is keyed off
player_match_stats, so already-scraped matches get skipped entirely). This
script fills in lineups/match_events for every match that's already in
player_match_stats but missing from those two tables, for every active
league in config.LEAGUES (or just one, via --league).

Useful going forward if a weekly run dies partway through a matchday -
NOT for historical-season backfill, which this project deliberately
doesn't do.

Reuses everything from scrape_player_match_stats.py / fbref_scrape_common -
no duplicated logic. Resumable: re-running skips any match_id already
present in lineups.

Usage:
    python backfill_lineups_events.py               # every active league
    python backfill_lineups_events.py --league EPL   # just one
"""

import argparse
import sqlite3
import time

import config
import fbref_scrape_common as common
import scrape_player_match_stats as spms

TEST_MATCH_LIMIT = None


def backfill_league(conn, league_key, league_cfg):
    sd_league = league_cfg["sd_league"]
    season = league_cfg["current_season"]

    fbref = spms.get_scraper(sd_league, season)
    try:
        canonical_lookup = common.build_canonical_player_id_lookup(conn, league_key)

        match_ids = common.matches_needing_backfill(conn, league_key, season)

        if TEST_MATCH_LIMIT is not None:
            match_ids = match_ids[:TEST_MATCH_LIMIT]
            print(f"[{league_key}] TEST_MATCH_LIMIT is set to {TEST_MATCH_LIMIT} - only backfilling that many matches.")

        print(f"[{league_key}] {len(match_ids)} matches need lineup/events backfill.")

        for i, match_id in enumerate(match_ids, start=1):
            print(f"[{league_key}] [{i}/{len(match_ids)}] Backfilling match {match_id}...")

            try:
                player_id_map = common.get_player_id_map(fbref, match_id)

                lineup_df = common.read_lineup_with_recovery(fbref, match_id)
                merged_lineup = common.merge_lineup_frame(lineup_df, match_id, season, league_key, player_id_map, canonical_lookup)
                if merged_lineup is not None and not merged_lineup.empty:
                    merged_lineup.to_sql("lineups", conn, if_exists="append", index=False)
                    print(f"    Saved {len(merged_lineup)} lineup rows.")
                else:
                    print(f"    No lineup data returned for match {match_id}.")

                events_df = common.read_events_with_recovery(fbref, match_id)
                merged_events = common.merge_events_frame(events_df, match_id, season, league_key, player_id_map, canonical_lookup)
                if merged_events is not None and not merged_events.empty:
                    merged_events.to_sql("match_events", conn, if_exists="append", index=False)
                    print(f"    Saved {len(merged_events)} event rows.")
                else:
                    print(f"    No event data returned for match {match_id}.")

            except Exception as e:
                import traceback
                print(f"    ERROR on match {match_id}: {e}")
                traceback.print_exc()

            time.sleep(3)

        print(f"[{league_key}] Backfill done.")
    finally:
        common.quit_driver(fbref)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--league", choices=list(config.LEAGUES.keys()), default=None,
        help="Backfill only this league instead of every active league in config.LEAGUES.",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(config.DB_PATH)
    common.create_tables(conn)

    targets = {args.league: config.LEAGUES[args.league]} if args.league else config.active_leagues()
    if not targets:
        print("No active leagues to process (config.LEAGUES has none marked active=True).")
        conn.close()
        return

    for league_key, league_cfg in targets.items():
        try:
            backfill_league(conn, league_key, league_cfg)
        except Exception as e:
            import traceback
            print(f"[{league_key}] ERROR backfilling league: {e}")
            traceback.print_exc()

    conn.close()


if __name__ == "__main__":
    main()
