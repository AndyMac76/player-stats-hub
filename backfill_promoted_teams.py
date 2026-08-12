"""
backfill_promoted_teams.py

One-off, targeted historical backfill: Coventry City, Hull City, and
Ipswich Town were promoted to the Premier League for 2026/27, so they
have zero EPL history - but they DO have real 2025/26 Championship
history, which the Championship pipeline never collected (it only started
tracking the CURRENT 2026/27 Championship, going forward, per this
project's no-historical-backfill policy).

This is a deliberate, narrow exception to that policy: not a full
Championship season backfill (552 matches, hours of scraping), just the
~130-ish matches involving these 3 specific clubs from last season - so
Fantasy Football Assistant's quality-score model has real underlying
stats for promoted-team players instead of silently having none.

Writes into the unified player_stats.db tagged league='CHAMP',
season='2526' (distinct from the current league='CHAMP' season='2627'
rows already being collected for this season's actual Championship clubs
- same league key, different season, exactly like how MLS/EPL rows are
already told apart by season within one league).

Usage:
    python backfill_promoted_teams.py
"""

import sqlite3
import time

import pandas as pd
import soccerdata as sd

import config
import fbref_scrape_common as common

LEAGUE_KEY = "CHAMP"
SEASON = "2526"
SD_LEAGUE = config.LEAGUES["CHAMP"]["sd_league"]
TARGET_TEAMS = ["Coventry City", "Hull City", "Ipswich Town"]

# Same idea as config.LEAGUES[...]["team_aliases"] - discovered empirically
# from this pull, not guessed in advance.
TEAM_ALIASES = {}


def get_target_match_ids(fbref):
    print(f"Pulling {SD_LEAGUE} {SEASON} schedule...")
    schedule = fbref.read_schedule().reset_index()
    print(f"  {len(schedule)} total matches in the season.")

    mask = schedule["home_team"].isin(TARGET_TEAMS) | schedule["away_team"].isin(TARGET_TEAMS)
    target_schedule = schedule[mask]
    print(f"  {len(target_schedule)} matches involve {', '.join(TARGET_TEAMS)}.")

    match_ids = target_schedule["game_id"].dropna().unique().tolist()
    lookup = {}
    for _, row in target_schedule.iterrows():
        lookup[row["game_id"]] = {
            "date": row["date"],
            "home_team": row["home_team"],
            "away_team": row["away_team"],
        }
    return match_ids, lookup


def main():
    conn = sqlite3.connect(config.DB_PATH)
    common.create_tables(conn)

    fbref = sd.FBref(leagues=SD_LEAGUE, seasons=SEASON, headless=False, path_to_browser=None)
    match_ids, schedule_lookup = get_target_match_ids(fbref)

    already_scraped = {
        row[0] for row in conn.execute(
            "SELECT DISTINCT match_id FROM player_match_stats WHERE league = ? AND season = ?",
            (LEAGUE_KEY, SEASON),
        )
    }
    if already_scraped:
        before = len(match_ids)
        match_ids = [m for m in match_ids if m not in already_scraped]
        print(f"Resuming: skipping {before - len(match_ids)} already-scraped matches.")

    print(f"Scraping {len(match_ids)} matches...")
    canonical_lookup = common.build_canonical_player_id_lookup(conn, LEAGUE_KEY)

    for i, match_id in enumerate(match_ids, start=1):
        print(f"[{i}/{len(match_ids)}] Scraping match {match_id}...")
        try:
            stat_frames = {}
            for stat_type in config.STAT_TYPES:
                df = common.read_stat_with_recovery(fbref, stat_type, match_id)
                if df is not None:
                    stat_frames[stat_type] = df

            if not stat_frames:
                print("    No data returned, skipping.")
                continue

            match_info = schedule_lookup.get(match_id)
            player_id_map = common.get_player_id_map(fbref, match_id)
            merged = common.merge_stat_frames(
                stat_frames, match_id, match_info, SEASON, LEAGUE_KEY, TEAM_ALIASES,
                player_id_map, canonical_lookup,
            )
            if merged is not None and not merged.empty:
                merged.to_sql("player_match_stats", conn, if_exists="append", index=False)
                print(f"    Saved {len(merged)} player rows.")
                for _, r in merged.iterrows():
                    if r["player_id"] and r["player_id"] != r["player_name"]:
                        canonical_lookup[r["player_name"]] = r["player_id"]

        except Exception as e:
            import traceback
            print(f"    ERROR on match {match_id}: {e}")
            traceback.print_exc()

        time.sleep(3)

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
