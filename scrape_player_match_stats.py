"""
scrape_player_match_stats.py

Pulls match-by-match player stats from FBref for every active league in
config.LEAGUES (or just one, via --league) and stores them into the shared
player_stats.db, tagged by league.

Per match, in the same loop (reuses the same cached match-report page fetch
where possible):
    - player_match_stats  (goals/assists/cards/etc.)
    - lineups             (starter vs sub, per player)
    - match_events        (goals/cards/subs with minute + players involved)
And once per league per run, cheaply from the schedule already being fetched:
    - fixtures             (full team fixture list, needed for "last 5 team
                             fixtures regardless of played" style dashboard stats)

Going-forward only. Resumable per league: on startup, checks which
match_ids are already saved for that league/season and skips them.
Lineups/events are fetched alongside stats for the same match_id, so they
inherit the same resumability - no separate tracking needed.

Usage:
    python scrape_player_match_stats.py              # every active league
    python scrape_player_match_stats.py --league EPL  # just one
"""

import argparse
import sqlite3
import time

import pandas as pd
import soccerdata as sd

import config
import fbref_scrape_common as common

TEST_MATCH_LIMIT = None


def get_scraper(sd_league, season):
    return sd.FBref(leagues=sd_league, seasons=season, headless=False, path_to_browser=None)


def get_match_ids(fbref):
    schedule = fbref.read_schedule()
    schedule = schedule.reset_index()
    completed = schedule.dropna(subset=["score"]) if "score" in schedule.columns else schedule
    return completed


def build_schedule_lookup(schedule):
    date_col = None
    for candidate in ("date", "Date", "game_date"):
        if candidate in schedule.columns:
            date_col = candidate
            break

    home_col = None
    for candidate in ("home_team", "Home", "home"):
        if candidate in schedule.columns:
            home_col = candidate
            break

    away_col = None
    for candidate in ("away_team", "Away", "away"):
        if candidate in schedule.columns:
            away_col = candidate
            break

    if not (date_col and home_col and away_col):
        print("    WARNING: could not find expected date/home/away columns in schedule.")
        print(f"    Schedule columns are: {list(schedule.columns)}")
        return {}

    lookup = {}
    for _, row in schedule.iterrows():
        lookup[row["game_id"]] = {
            "date": row[date_col],
            "home_team": row[home_col],
            "away_team": row[away_col],
        }
    return lookup


def write_fixtures_table(conn, league, season, schedule_lookup):
    """Cheap - schedule is already fetched for match_ids, so this writes a
    team-perspective row for both home and away out of data we already have
    in memory. No extra network calls.

    These matches all have a score (get_match_ids() already filtered to
    completed ones), so is_played=1 here unconditionally - pull_full_schedule.py
    is the source of truth for upcoming/unplayed fixtures."""
    rows = []
    for match_id, info in schedule_lookup.items():
        match_date = str(info["date"]) if pd.notna(info["date"]) else None
        rows.append((league, info["home_team"], info["away_team"], match_id, match_date, "Home", season, 1, 1))
        rows.append((league, info["away_team"], info["home_team"], match_id, match_date, "Away", season, 1, 0))

    conn.executemany(
        """
        INSERT OR IGNORE INTO fixtures
            (league, team, opponent, match_id, match_date, venue, season, is_played, is_home)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    print(f"[{league}] Fixtures table updated: {len(rows)} team-match rows (upserted, duplicates ignored).")


def get_already_scraped_match_ids(conn, league, season):
    try:
        rows = conn.execute(
            "SELECT DISTINCT match_id FROM player_match_stats WHERE league = ? AND season = ?",
            (league, season),
        ).fetchall()
        return {row[0] for row in rows}
    except sqlite3.OperationalError:
        return set()


def scrape_league(conn, league_key, league_cfg):
    sd_league = league_cfg["sd_league"]
    season = league_cfg["current_season"]
    team_aliases = league_cfg["team_aliases"]

    fbref = get_scraper(sd_league, season)
    try:
        schedule = get_match_ids(fbref)
        match_ids = schedule["game_id"].dropna().unique().tolist()
        schedule_lookup = build_schedule_lookup(schedule)

        if schedule_lookup:
            write_fixtures_table(conn, league_key, season, schedule_lookup)

        # Built once up front: covers any player who already has a real ID from
        # a match they featured in, so unused subs in later matches resolve to
        # their canonical ID instead of a name placeholder.
        canonical_lookup = common.build_canonical_player_id_lookup(conn, league_key)

        already_scraped = get_already_scraped_match_ids(conn, league_key, season)
        if already_scraped:
            before = len(match_ids)
            match_ids = [m for m in match_ids if m not in already_scraped]
            print(f"[{league_key}] Resuming: skipping {before - len(match_ids)} already-scraped matches.")

        if TEST_MATCH_LIMIT is not None:
            match_ids = match_ids[:TEST_MATCH_LIMIT]
            print(f"[{league_key}] TEST_MATCH_LIMIT is set to {TEST_MATCH_LIMIT} - only scraping that many matches.")

        print(f"[{league_key}] Found {len(match_ids)} completed matches to scrape for {season}.")

        for i, match_id in enumerate(match_ids, start=1):
            print(f"[{league_key}] [{i}/{len(match_ids)}] Scraping match {match_id}...")

            try:
                stat_frames = {}
                for stat_type in config.STAT_TYPES:
                    df = common.read_stat_with_recovery(fbref, stat_type, match_id)
                    if df is not None:
                        stat_frames[stat_type] = df

                if not stat_frames:
                    print(f"    No data returned for match {match_id}, skipping.")
                    continue

                match_info = schedule_lookup.get(match_id)
                player_id_map = common.get_player_id_map(fbref, match_id)
                merged = common.merge_stat_frames(
                    stat_frames, match_id, match_info, season, league_key, team_aliases,
                    player_id_map, canonical_lookup,
                )
                if merged is not None and not merged.empty:
                    merged.to_sql("player_match_stats", conn, if_exists="append", index=False)
                    print(f"    Saved {len(merged)} player rows.")
                    # Keep the lookup current within this run - a player who
                    # got a real ID this match should resolve correctly if they
                    # show up as an unused sub in a later match this same run.
                    for _, r in merged.iterrows():
                        if r["player_id"] and r["player_id"] != r["player_name"]:
                            canonical_lookup[r["player_name"]] = r["player_id"]

                # --- Lineups (starter vs sub) ---
                try:
                    lineup_df = common.read_lineup_with_recovery(fbref, match_id)
                    merged_lineup = common.merge_lineup_frame(
                        lineup_df, match_id, season, league_key, player_id_map, canonical_lookup
                    )
                    if merged_lineup is not None and not merged_lineup.empty:
                        merged_lineup.to_sql("lineups", conn, if_exists="append", index=False)
                        print(f"    Saved {len(merged_lineup)} lineup rows.")
                except sqlite3.IntegrityError as e:
                    print(f"    WARNING: lineup insert skipped duplicate rows for match {match_id}: {e}")
                except Exception as e:
                    print(f"    WARNING: lineup processing failed for match {match_id}: {e}")

                # --- Events (goals/cards/subs) ---
                try:
                    events_df = common.read_events_with_recovery(fbref, match_id)
                    merged_events = common.merge_events_frame(
                        events_df, match_id, season, league_key, player_id_map, canonical_lookup
                    )
                    if merged_events is not None and not merged_events.empty:
                        merged_events.to_sql("match_events", conn, if_exists="append", index=False)
                        print(f"    Saved {len(merged_events)} event rows.")
                except Exception as e:
                    print(f"    WARNING: events processing failed for match {match_id}: {e}")

            except Exception as e:
                import traceback
                print(f"    ERROR on match {match_id}: {e}")
                traceback.print_exc()

            time.sleep(3)

        print(f"[{league_key}] Done.")
    finally:
        common.quit_driver(fbref)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--league", choices=list(config.LEAGUES.keys()), default=None,
        help="Scrape only this league instead of every active league in config.LEAGUES.",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(config.DB_PATH)
    common.create_tables(conn)

    targets = {args.league: config.LEAGUES[args.league]} if args.league else config.active_leagues()
    if not targets:
        print("No active leagues to scrape (config.LEAGUES has none marked active=True).")
        conn.close()
        return

    for league_key, league_cfg in targets.items():
        try:
            scrape_league(conn, league_key, league_cfg)
        except Exception as e:
            import traceback
            print(f"[{league_key}] ERROR scraping league: {e}")
            traceback.print_exc()

    conn.close()
    print("All leagues done.")


if __name__ == "__main__":
    main()
