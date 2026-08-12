"""
pull_full_schedule.py

Rebuilds each active league's rows in the shared `fixtures` table from
soccerdata's read_schedule(), which returns the FULL season schedule -
played matches AND upcoming/unplayed ones - unlike scrape_player_match_stats.py's
own fixtures write, which only covers matches it has actually scraped
(i.e. completed ones).

Since soccerdata's exact read_schedule() column names aren't guaranteed
ahead of time, this script prints the raw columns first so any mismatch
can be confirmed/patched against reality rather than guessed.

Fixtures FBref hasn't assigned a match-report ID to yet (typical for
fixtures far enough out - e.g. an entire season that hasn't started) get a
stable synthetic ID instead of NULL - built from a FIXED home/away order so
both rows for that fixture always agree on it, regardless of which team's
row is being written. Without this, match_id ends up NULL for those
fixtures, and SQL's "NULL != NULL" behaviour means any WHERE match_id = ?
lookup on them silently matches nothing downstream (e.g. in
generate_matchup_dashboard.py).

Usage:
    python pull_full_schedule.py               # every active league
    python pull_full_schedule.py --league EPL   # just one
"""

import argparse
import sqlite3

import pandas as pd
import soccerdata as sd

import config
import fbref_scrape_common as common  # noqa: F401 - imported for persistent_fbref side effect


def flatten_columns(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [
            "_".join([str(c) for c in col if c and str(c) != ""]).strip("_")
            for col in df.columns
        ]
    return df


def find_col(df, candidates):
    for c in candidates:
        if c in df.columns:
            return c
    return None


def pull_league_schedule(conn, league_key, league_cfg):
    sd_league = league_cfg["sd_league"]
    season = league_cfg["current_season"]
    season_aliases = league_cfg.get("season_team_aliases", {})

    def normalize_team(team):
        return season_aliases.get(team, team)

    print(f"\n[{league_key}] Pulling full season schedule for {season}...")
    fbref = sd.FBref(leagues=sd_league, seasons=season)
    try:
        schedule = fbref.read_schedule()
    finally:
        common.quit_driver(fbref)
    schedule = flatten_columns(schedule.reset_index())

    print(f"  {len(schedule)} rows, columns: {list(schedule.columns)}")
    if len(schedule):
        print("\nSample row:")
        print(schedule.iloc[0].to_dict())

    col_map = {
        "date": find_col(schedule, ["date", "game_date"]),
        "home_team": find_col(schedule, ["home_team", "home"]),
        "away_team": find_col(schedule, ["away_team", "away"]),
        "venue": find_col(schedule, ["venue"]),
        "game_id": find_col(schedule, ["game_id", "match_id", "game"]),
        "score": find_col(schedule, ["score"]),
    }

    missing = [k for k, v in col_map.items() if v is None and k != "score"]
    if missing:
        print(f"\n[{league_key}] Couldn't confidently resolve columns: {missing}")
        print("   Patch the candidate lists in col_map above to match, then re-run.")
        return

    # Fully regenerated from FBref each run for this league - safe to
    # delete-and-recreate this league's slice rather than the whole table.
    conn.execute("DELETE FROM fixtures WHERE league = ? AND season = ?", (league_key, season))

    rows = []
    pending_count = 0
    for _, r in schedule.iterrows():
        date_val = r.get(col_map["date"])
        home = normalize_team(r.get(col_map["home_team"]))
        away = normalize_team(r.get(col_map["away_team"]))
        venue = r.get(col_map["venue"]) if col_map["venue"] else None
        game_id = r.get(col_map["game_id"])
        score = r.get(col_map["score"]) if col_map["score"] else None

        # A played match has a score; an unplayed one is typically NaN/blank.
        is_played = 1 if pd.notna(score) and str(score).strip() not in ("", "nan") else 0

        date_clean = None if pd.isna(date_val) else str(date_val)

        if pd.isna(game_id):
            # FBref hasn't assigned a match-report ID yet. Build a stable
            # synthetic ID from a FIXED home/away order (and this league's
            # key, so two leagues can never collide on the same synthetic
            # ID) so both rows for this fixture agree on it, regardless of
            # which team's row is being written below.
            game_id_clean = f"pending-{league_key}-{date_clean or 'TBD'}-{home}-{away}"
            pending_count += 1
        else:
            game_id_clean = str(game_id)

        # Two rows per fixture - one per team's perspective.
        rows.append((league_key, home, away, game_id_clean, date_clean, venue, season, is_played, 1))
        rows.append((league_key, away, home, game_id_clean, date_clean, venue, season, is_played, 0))

    conn.executemany(
        """
        INSERT OR REPLACE INTO fixtures
            (league, team, opponent, match_id, match_date, venue, season, is_played, is_home)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()

    total = len(rows)
    played = sum(1 for r in rows if r[7] == 1)
    unplayed = total - played
    print(f"\n[{league_key}] Wrote {total} fixture rows ({played} played, {unplayed} upcoming/unplayed).")
    if pending_count:
        print(f"   {pending_count} fixture(s) had no FBref match-report ID yet - "
              f"assigned synthetic 'pending-...' IDs instead.")

    # Sanity check: do all team names in fixtures now match team names
    # already known from lineups? Catches any team missed by
    # season_team_aliases before it silently breaks a join later. With an
    # empty (or nearly empty) `lineups` table for this league - e.g. before
    # a season has kicked off - this will look alarming ("every team is
    # unmatched") but is expected, not a real problem: lineups only exist
    # once matches have actually been scraped.
    fixture_teams = {r[1] for r in rows}
    lineup_teams = {
        row[0] for row in conn.execute("SELECT DISTINCT team FROM lineups WHERE league = ?", (league_key,))
    }
    unmatched_teams = fixture_teams - lineup_teams
    if unmatched_teams:
        print(
            f"\n[{league_key}] {len(unmatched_teams)} team name(s) in fixtures don't match any "
            f"team in lineups yet - likely missing from season_team_aliases, or simply because no "
            f"matches have been scraped for this league yet:"
        )
        for t in sorted(unmatched_teams):
            print(f"     {t!r}")
    else:
        print(f"[{league_key}] All team names in fixtures match known team names in lineups.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--league", choices=list(config.LEAGUES.keys()), default=None,
        help="Pull only this league's schedule instead of every active league in config.LEAGUES.",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(config.DB_PATH)
    common.create_tables(conn)

    targets = {args.league: config.LEAGUES[args.league]} if args.league else config.active_leagues()
    if not targets:
        print("No active leagues to pull (config.LEAGUES has none marked active=True).")
        conn.close()
        return

    for league_key, league_cfg in targets.items():
        try:
            pull_league_schedule(conn, league_key, league_cfg)
        except Exception as e:
            import traceback
            print(f"[{league_key}] ERROR pulling schedule: {e}")
            traceback.print_exc()

    conn.close()


if __name__ == "__main__":
    main()
