"""
validate_aggregate_delta_approach.py

Tests whether FBref's season-aggregate "Squad & Player Stats" pages
(Standard, Shooting, Playing Time, Miscellaneous, Goalkeeping - the same
5 tabs soccerdata exposes as stat_type "standard"/"shooting"/
"playing_time"/"misc"/"keeper") could replace per-match scraping for
player_match_stats' box-score columns, WITHOUT actually changing the
production scraper yet.

The idea: instead of one page load per match for player stats (2 of the
~5 fetches per match), fetch these 5 league-wide pages once per week and
compute each player's contribution as (this week's cumulative total) -
(last week's cumulative total). We can't literally ask FBref for "totals
as of last week" - it only ever shows the current snapshot - so this
validates the approach differently: using data we've ALREADY scraped the
slow, trusted way.

For the most recently-played matchday in this league:
    delta_derived = (FBref's CURRENT season-aggregate total)
                     - (sum of our own player_match_stats for every
                        EARLIER match this season)
    actual        = sum of our own player_match_stats for JUST the
                     most recent matchday

If delta_derived == actual consistently, that proves the aggregate-page
approach would correctly reconstruct a week's contribution once we start
keeping our own running snapshot to diff against going forward - without
ever touching the real pipeline until this comes back clean.

Usage:
    python validate_aggregate_delta_approach.py               # MLS by default
    python validate_aggregate_delta_approach.py --league EPL
"""

import argparse
import sqlite3

import pandas as pd
import soccerdata as sd

import config
import fbref_scrape_common as common  # noqa: F401 - imported for persistent_fbref side effect

# player_match_stats column -> (season-aggregate stat_type, flattened column name)
STAT_SOURCE = {
    "goals": ("standard", "Performance_Gls"),
    "assists": ("standard", "Performance_Ast"),
    "penalty_goals": ("standard", "Performance_PK"),
    "penalty_attempts": ("standard", "Performance_PKatt"),
    "minutes_played": ("standard", "Playing Time_Min"),
    "shots": ("shooting", "Standard_Sh"),
    "shots_on_target": ("shooting", "Standard_SoT"),
    "cards_yellow": ("misc", "Performance_CrdY"),
    "cards_red": ("misc", "Performance_CrdR"),
    "fouls": ("misc", "Performance_Fls"),
    "fouls_drawn": ("misc", "Performance_Fld"),
    "offsides": ("misc", "Performance_Off"),
    "crosses": ("misc", "Performance_Crs"),
    "interceptions": ("misc", "Performance_Int"),
    "tackles_won": ("misc", "Performance_TklW"),
    "pk_won": ("misc", "Performance_PKwon"),
    "pk_conceded": ("misc", "Performance_PKcon"),
    "own_goals": ("misc", "Performance_OG"),
    "saves": ("keeper", "Performance_Saves"),
    "goals_conceded": ("keeper", "Performance_GA"),
}

STAT_TYPES_NEEDED = sorted({src for src, _ in STAT_SOURCE.values()})


def flatten_columns(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [
            "_".join([str(c) for c in col if c and str(c) != ""]).strip("_")
            for col in df.columns
        ]
    return df


def fetch_current_aggregates(fbref):
    """{stat_type: flattened DataFrame} for every tab STAT_SOURCE needs."""
    frames = {}
    for stat_type in STAT_TYPES_NEEDED:
        print(f"  Fetching '{stat_type}' season aggregates...")
        df = fbref.read_player_season_stats(stat_type=stat_type)
        frames[stat_type] = flatten_columns(df.reset_index())
    return frames


def build_current_cumulative_lookup(frames, season_team_aliases):
    """(player_name, team) -> {player_match_stats_col: current cumulative
    value}, reading each stat from whichever tab actually has it. Season-
    aggregate pages use the same shorter team names as pull_season_
    aggregates.py already has to handle (e.g. "NE Revolution" vs
    player_match_stats' "New England Revolution") - same season_team_aliases
    dict, applied the same way."""
    lookup = {}
    for col, (stat_type, source_col) in STAT_SOURCE.items():
        df = frames[stat_type]
        if source_col not in df.columns:
            print(f"  WARNING: expected column '{source_col}' not found in '{stat_type}' - skipping {col}.")
            continue
        for _, row in df.iterrows():
            team = season_team_aliases.get(row["team"], row["team"])
            key = (row["player"], team)
            lookup.setdefault(key, {})[col] = row[source_col]
    return lookup


def build_name_team_to_id_lookup(conn, league):
    rows = conn.execute(
        "SELECT DISTINCT player_name, team, player_id FROM player_match_stats WHERE league = ?",
        (league,),
    ).fetchall()
    return {(name, team): pid for name, team, pid in rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--league", default="MLS", choices=list(config.LEAGUES.keys()))
    parser.add_argument("--tolerance", type=float, default=0.01, help="Allowed numeric difference before flagging a mismatch (float rounding).")
    args = parser.parse_args()
    league = args.league
    league_cfg = config.LEAGUES[league]
    season = league_cfg["current_season"]

    conn = sqlite3.connect(config.DB_PATH)

    latest_date_row = conn.execute(
        "SELECT MAX(match_date) FROM player_match_stats WHERE league = ? AND season = ?",
        (league, season),
    ).fetchone()
    latest_date = latest_date_row[0] if latest_date_row else None
    if not latest_date:
        print(f"[{league}] No scraped matches for season {season} yet - nothing to validate against.")
        conn.close()
        return

    latest_match_ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT match_id FROM player_match_stats WHERE league = ? AND season = ? AND match_date = ?",
        (league, season, latest_date),
    ).fetchall()]
    print(f"[{league}] Most recent matchday: {latest_date} ({len(latest_match_ids)} match(es): {latest_match_ids})")

    placeholders = ",".join("?" for _ in latest_match_ids)
    all_rows = pd.read_sql(
        f"SELECT player_id, player_name, team, match_id, "
        f"{', '.join(STAT_SOURCE.keys())} "
        f"FROM player_match_stats WHERE league = ? AND season = ?",
        conn, params=(league, season),
    )
    id_lookup = build_name_team_to_id_lookup(conn, league)
    conn.close()

    print(f"[{league}] Fetching current season-aggregate pages ({len(STAT_TYPES_NEEDED)} tabs)...")
    # no_cache=True: soccerdata otherwise reuses whatever season-aggregate
    # page it last fetched (found stuck 3 weeks stale here, from Aug 8, while
    # our own scraped matches went through Aug 30) - a stale read here would
    # make every derived delta wrong in a way that looks like a real problem
    # with the approach but is actually just an unrefreshed cache.
    fbref = sd.FBref(leagues=league_cfg["sd_league"], seasons=season, no_cache=True)
    try:
        frames = fetch_current_aggregates(fbref)
    finally:
        common.quit_driver(fbref)

    season_team_aliases = league_cfg.get("season_team_aliases", {})
    current_cumulative = build_current_cumulative_lookup(frames, season_team_aliases)

    latest_set = set(latest_match_ids)
    prior_rows = all_rows[~all_rows["match_id"].isin(latest_set)]
    actual_rows = all_rows[all_rows["match_id"].isin(latest_set)]

    # Scoped by (player_id, team), not just player_id: a mid-season transfer
    # keeps the same player_id across clubs, but FBref's season-aggregate
    # page for the OLD team stops accumulating the moment they leave - a
    # plain player_id sum would make "prior" bigger than the aggregate
    # page's "current" total for the old team (found via Eduard Löwen:
    # St. Louis City -> San Jose Earthquakes mid-season, while restructuring
    # the production scraper to use this approach - this script's own blind
    # spot is that it only checks players who appear in the latest
    # matchday, so a transferred-out player's stale prior total never got
    # exercised here).
    prior_totals = prior_rows.groupby(["player_id", "team"])[list(STAT_SOURCE.keys())].sum(numeric_only=True)
    actual_totals = actual_rows.groupby("player_id")[list(STAT_SOURCE.keys())].sum(numeric_only=True)

    # Team as of the latest matchday itself (not a season-wide first
    # occurrence, which could be a transferred player's OLD team).
    name_team_by_id = actual_rows.drop_duplicates("player_id").set_index("player_id")[["player_name", "team"]]

    results = []
    unmatched = []
    for player_id in actual_totals.index:
        name, team = name_team_by_id.loc[player_id, "player_name"], name_team_by_id.loc[player_id, "team"]
        current = current_cumulative.get((name, team))
        if current is None:
            unmatched.append((name, team))
            continue

        prior_key = (player_id, team)
        prior = prior_totals.loc[prior_key] if prior_key in prior_totals.index else pd.Series(0, index=STAT_SOURCE.keys())
        actual = actual_totals.loc[player_id]

        for stat in STAT_SOURCE:
            if stat not in current:
                continue
            derived = current[stat] - (prior[stat] if stat in prior else 0)
            actual_val = actual[stat] if stat in actual else 0
            diff = abs(derived - actual_val)
            results.append({
                "player": name, "team": team, "stat": stat,
                "derived": derived, "actual": actual_val, "diff": diff,
                "match": diff <= args.tolerance,
            })

    results_df = pd.DataFrame(results)
    if results_df.empty:
        print(f"[{league}] No comparable player-stat rows produced - can't validate.")
        return

    print(f"\n[{league}] {len(unmatched)} player(s) from the latest matchday had no name+team match in the "
          f"season-aggregate pages (likely a name-matching gap, not an aggregate-approach problem):")
    for name, team in unmatched[:10]:
        print(f"    {name} ({team})")

    print(f"\n[{league}] Validation results across {results_df['player'].nunique()} players, "
          f"{len(STAT_SOURCE)} stats each ({len(results_df)} cells compared):\n")

    per_stat = results_df.groupby("stat").agg(
        cells=("match", "size"),
        matched=("match", "sum"),
        mean_diff=("diff", "mean"),
        max_diff=("diff", "max"),
    )
    per_stat["match_rate"] = (per_stat["matched"] / per_stat["cells"] * 100).round(1)
    print(per_stat[["cells", "matched", "match_rate", "mean_diff", "max_diff"]].to_string())

    overall_rate = results_df["match"].mean() * 100
    print(f"\nOverall match rate: {overall_rate:.1f}% ({results_df['match'].sum()}/{len(results_df)} cells)")

    mismatches = results_df[results_df["match"] != True].sort_values("diff", ascending=False)  # noqa: E712 - catches NaN too, not just False
    if len(mismatches):
        print(f"\nTop mismatches (largest difference first):")
        print(mismatches.head(20)[["player", "team", "stat", "derived", "actual", "diff"]].to_string(index=False))
    else:
        print("\nNo mismatches at all - the aggregate-delta approach reconstructed this matchday exactly.")


if __name__ == "__main__":
    main()
