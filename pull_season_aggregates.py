"""
pull_season_aggregates.py

Pulls season-level aggregates from FBref via soccerdata for every active
league in config.LEAGUES (or just one, via --league):
  - read_player_season_stats(stat_type='playing_time')
  - read_player_season_stats(stat_type='misc')

Writes/refreshes that league's slice of the shared `player_season_stats`
table.

Cheap relative to the match-level scrape: two calls total per league, not
per-match.

Usage:
    python pull_season_aggregates.py               # every active league
    python pull_season_aggregates.py --league EPL   # just one
"""

import argparse
import sqlite3

import pandas as pd
import soccerdata as sd

import config
import fbref_scrape_common as common  # noqa: F401 - imported for persistent_fbref side effect


def clean_value(v):
    """Convert pandas NaN/NA/NaT to a plain None so sqlite3 can bind it."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def flatten_columns(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [
            "_".join([str(c) for c in col if c and str(c) != ""]).strip("_")
            for col in df.columns
        ]
    return df


def pull_playing_time(fbref):
    print("  Pulling playing_time season stats...")
    df = fbref.read_player_season_stats(stat_type="playing_time")
    df = flatten_columns(df.reset_index())
    print(f"    {len(df)} rows, columns: {list(df.columns)}")
    return df


def pull_misc(fbref):
    print("  Pulling misc season stats...")
    df = fbref.read_player_season_stats(stat_type="misc")
    df = flatten_columns(df.reset_index())
    print(f"    {len(df)} rows, columns: {list(df.columns)}")
    return df


def build_name_team_to_id_lookup(conn, league):
    """Season stats don't include player_id (FBref only exposes it on match
    pages). Build a (player_name, team) -> player_id lookup from this
    league's match-level data already in the DB, so season rows can be
    joined back to the same player_id used everywhere else."""
    rows = conn.execute(
        "SELECT DISTINCT player_name, team, player_id FROM player_match_stats WHERE league = ?",
        (league,),
    ).fetchall()

    lookup = {}
    for name, team, player_id in rows:
        lookup[(name, team)] = player_id
    return lookup


def merge_and_write(conn, league, playing_time_df, misc_df, season, season_aliases):
    def normalize_team(team):
        return season_aliases.get(team, team)

    # Neither frame has player_id (FBref only exposes it on match pages),
    # so join on player name - team columns get suffixed by pandas since
    # both frames have one.
    merged = pd.merge(
        playing_time_df,
        misc_df,
        on="player",
        how="outer",
        suffixes=("_pt", "_misc"),
    )

    name_team_lookup = build_name_team_to_id_lookup(conn, league)

    out_rows = []
    unmatched = 0
    for _, row in merged.iterrows():
        player_name = row.get("player")
        raw_team = row.get("team_pt") if pd.notna(row.get("team_pt")) else row.get("team_misc")
        team = normalize_team(raw_team)
        player_id = name_team_lookup.get((player_name, team))
        if player_id is None:
            unmatched += 1

        out_rows.append(
            tuple(
                clean_value(v)
                for v in (
                    league,
                    player_id,
                    player_name,
                    team,
                    season,
                    row.get("Starts_Starts"),
                    row.get("Subs_Subs"),
                    row.get("Playing Time_MP"),
                    row.get("Playing Time_Min"),
                    row.get("Performance_Fls"),
                    row.get("Performance_Fld"),
                    row.get("Performance_CrdY"),
                    row.get("Performance_CrdR"),
                )
            )
        )

    total = len(out_rows)
    match_pct = (total - unmatched) / total * 100 if total else 0
    print(
        f"\n  [{league}] player_id match rate: {total - unmatched}/{total} ({match_pct:.1f}%) "
        f"resolved via name+team lookup against player_match_stats."
    )
    if match_pct < 90 and total:
        print(
            f"  [{league}] Match rate below 90% - this is EXPECTED if no matches have been "
            f"scraped for this league/season yet (the lookup is built from player_match_stats, "
            f"which starts empty until scrape_player_match_stats.py has run at least once)."
        )

    conn.executemany(
        """
        INSERT INTO player_season_stats (
            league, player_id, player_name, team, season,
            starts, subs, matches_played, minutes,
            fouls_committed, fouls_drawn, yellow_cards, red_cards
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(league, player_name, team, season) DO UPDATE SET
            player_id=excluded.player_id,
            starts=excluded.starts,
            subs=excluded.subs,
            matches_played=excluded.matches_played,
            minutes=excluded.minutes,
            fouls_committed=excluded.fouls_committed,
            fouls_drawn=excluded.fouls_drawn,
            yellow_cards=excluded.yellow_cards,
            red_cards=excluded.red_cards
        """,
        out_rows,
    )
    conn.commit()
    return total, unmatched


def pull_league(conn, league_key, league_cfg):
    sd_league = league_cfg["sd_league"]
    season = league_cfg["current_season"]
    season_aliases = league_cfg.get("season_team_aliases", {})

    print(f"\n[{league_key}] Pulling season aggregates for {season}...")
    fbref = sd.FBref(leagues=sd_league, seasons=season)

    try:
        try:
            playing_time_df = pull_playing_time(fbref)
            misc_df = pull_misc(fbref)
        except ValueError as e:
            # FBref doesn't render the season-stats tables at all until at least
            # one match has been played - not a bug, just means this season
            # hasn't started yet. read_player_season_stats() raises a bare
            # ValueError (xpath match count mismatch) in that case rather than
            # returning an empty frame, so this is the only way to detect it.
            print(f"[{league_key}] No season-aggregate stats available from FBref yet for {season} "
                  f"(likely because the season hasn't started) - skipping. ({e})")
            return
    finally:
        common.quit_driver(fbref)

    total, unmatched = merge_and_write(conn, league_key, playing_time_df, misc_df, season, season_aliases)
    print(f"[{league_key}] Wrote/updated {total} rows in player_season_stats for season {season} "
          f"({unmatched} unmatched player_id).")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--league", choices=list(config.LEAGUES.keys()), default=None,
        help="Pull only this league instead of every active league in config.LEAGUES.",
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
            pull_league(conn, league_key, league_cfg)
        except Exception as e:
            import traceback
            print(f"[{league_key}] ERROR pulling season aggregates: {e}")
            traceback.print_exc()

    conn.close()


if __name__ == "__main__":
    main()
