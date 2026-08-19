"""
backfill_team_stats.py

Catch-up pass: team_match_stats (possession, corners, cards, fouls, shots,
saves, crosses, interceptions, offsides - the "Team Stats" section of a
match report) was added to scrape_player_match_stats.py after a lot of
matches were already scraped. This script fills it in for every match
that's already in player_match_stats but missing from team_match_stats,
for every active league in config.LEAGUES (or just one, via --league).

Home/away team names come straight from the fixtures table (already
correctly resolved there) rather than re-deriving them from FBref's
schedule page.

Usage:
    python backfill_team_stats.py               # every active league
    python backfill_team_stats.py --league MLS   # just one
"""

import argparse
import sqlite3
import time

import config
import fbref_scrape_common as common
import scrape_player_match_stats as spms

TEST_MATCH_LIMIT = None


def get_match_home_away(conn, league, match_id):
    """(home_team, away_team, match_date) for one match, from fixtures -
    None if the match isn't in fixtures at all (shouldn't happen for a
    match that's already in player_match_stats, but handled defensively)."""
    home_row = conn.execute(
        "SELECT team, opponent, match_date FROM fixtures WHERE league = ? AND match_id = ? AND is_home = 1",
        (league, match_id),
    ).fetchone()
    if not home_row:
        return None
    return home_row  # (home_team, away_team, match_date)


def backfill_league(conn, league_key, league_cfg):
    sd_league = league_cfg["sd_league"]
    season = league_cfg["current_season"]

    fbref = spms.get_scraper(sd_league, season)
    try:
        match_ids = common.matches_needing_team_stats_backfill(conn, league_key, season)

        if TEST_MATCH_LIMIT is not None:
            match_ids = match_ids[:TEST_MATCH_LIMIT]
            print(f"[{league_key}] TEST_MATCH_LIMIT is set to {TEST_MATCH_LIMIT} - only backfilling that many matches.")

        print(f"[{league_key}] {len(match_ids)} matches need team stats backfill.")

        for i, match_id in enumerate(match_ids, start=1):
            print(f"[{league_key}] [{i}/{len(match_ids)}] Backfilling match {match_id}...")

            try:
                match_info = get_match_home_away(conn, league_key, match_id)
                if not match_info:
                    print(f"    No fixtures row found for match {match_id} - skipping.")
                    continue
                home_team, away_team, match_date = match_info

                team_stats = common.get_team_match_stats(fbref, match_id)
                team_stats_rows = common.build_team_match_stats_rows(
                    team_stats, match_id, match_date, season, league_key, home_team, away_team,
                )
                if team_stats_rows:
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO team_match_stats
                        (league, team, opponent, match_id, match_date, season, is_home,
                         possession_pct, shots_on_target, shots_total, saves, shots_faced,
                         cards_yellow, cards_red, fouls, corners, crosses, interceptions, offsides)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        team_stats_rows,
                    )
                    conn.commit()
                    print(f"    Saved team stats for both sides (corners: {team_stats.get('home_corners')}-{team_stats.get('away_corners')}).")
                else:
                    print(f"    No team stats returned for match {match_id}.")

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
