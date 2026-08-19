"""
generate_dashboard.py

Builds a single self-contained, offline HTML dashboard for the Player
Stats Hub project (dashboard.html), reading from the shared
player_stats.db across every league that has data.

One page, three views (toggle at the top): Fixtures / Players / Teams.
League tabs sit below that toggle and filter whichever view is active.

  - Fixtures - the current gameweek's matches as cards, per league. Click
    a fixture to expand both squads, each player's aggregated stats over
    their team's last 5 fixtures. Click a player's name to jump straight
    to their row in the Players view (in-page, no new tab/file).
  - Players - sortable/filterable player table. Rolling / Season toggle
    switches the stat columns between actual totals over each player's
    last N matches (shown as a per-match sequence + average, e.g.
    "1 0 2 0 1 (0.8)") and their totals for the current season so far.
    A per-player dot-streak form panel (Attack, Defence, Shooting,
    Goalkeeping) is position-aware: defenders/forwards get different
    thresholds than midfielders.
  - Teams - squad-level summary: goals/assists/cards/etc. totaled per
    team for the current season, plus squad size and matches played.
    Computed from player_match_stats rather than the separate
    player_season_stats table (which lacks goals/assists entirely).

Column headers throughout are abbreviated (G, A, SOT, TW...) with the
full stat name in a hover tooltip.

Supports deep-linking via URL: dashboard.html?view=players&league=EPL&player=<id>
opens straight into that player's pinned row (used internally by the
Fixtures view's squad links, but also shareable directly).

"Current gameweek" (per league) = the earliest upcoming (unplayed)
fixture date, plus any other upcoming fixtures within a few days of it
(window size configurable per league - MLS's more spread-out schedule
uses a wider window than EPL's tighter Sat/Sun rounds). If a league's
season has finished, falls back to the most recent played date cluster.
"Current squad" for a team = the matchday squad from that team's most
recently PLAYED match - for a league with zero matches played yet (e.g.
EPL before its 2026/27 season kicks off), every team's squad is empty
until the first round has been scraped - expected, not a bug.

Note: passing completion, headed shots, and shots-outside-the-box are NOT
included - confirmed via diagnostic scraping that FBref doesn't publish
these at match level for these competitions.

Workflow: edit this script only, then run:
    python generate_dashboard.py
    start dashboard.html

Usage:
    python generate_dashboard.py
"""

import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd

import config
import fbref_scrape_common as common

DB_PATH = config.DB_PATH
OUTPUT_FILE = "dashboard.html"
FORM_WINDOW = config.ROLLING_WINDOW
LAST_N = 5

# The Betting tab's EPL data reads predictions from a sibling project
# (The Corner Kick) rather than duplicating its modeling pipeline here -
# same sys.path-bootstrap pattern used for fbref_common. Corner Kick's
# models are trained on football-data.co.uk's Premier League data
# specifically, not FBref, so this only ever covers EPL - other leagues'
# betting data (if any) comes from this project's own team_stat_predictions
# table instead (see load_own_betting_predictions()).
BETTING_DIR = Path(__file__).resolve().parent.parent / "The Corner Kick"
BETTING_DB_PATH = BETTING_DIR / "corners.db"
if str(BETTING_DIR) not in sys.path:
    sys.path.insert(0, str(BETTING_DIR))

LEAGUE_ORDER = list(config.LEAGUES.keys())
CURRENT_SEASON_BY_LEAGUE = {key: cfg["current_season"] for key, cfg in config.LEAGUES.items()}

# Gameweek window size (days) per league - MLS's schedule spreads matches
# out more than EPL's tighter Sat/Sun rounds. Falls back to 4 for any
# league not listed here.
GAMEWEEK_WINDOW_DAYS = {
    "MLS": 4,
    "EPL": 3,
}

# The curated stat columns shown in the Players table ("last N matches" and
# "season totals" views) and in the Teams summary - a subset of everything
# in player_match_stats, matching what the table has always shown rather
# than dumping all ~20 raw columns. Order here drives display order.
CURATED_STAT_COLUMNS = [
    "goals", "assists", "shots", "shots_on_target",
    "tackles_won", "interceptions", "fouls", "fouls_drawn", "minutes_played",
]
GK_STAT_COLUMNS = ["cards_yellow", "saves", "goals_conceded"]

# Columns excluded from the Fixtures view's dynamically-detected stat list
# (squad tables there show whatever numeric columns exist, not the curated
# subset above) even though they may be INTEGER/REAL typed - these are
# identifiers/metadata, not stats worth displaying per player.
EXCLUDE_FROM_STATS = {
    "player_id", "match_id", "season", "jersey_number", "age", "born",
}


# ---------------------------------------------------------------------------
# Per-match category scoring for the Players view's dot-streak form panel -
# position-aware, since e.g. one tackle means something different for a
# striker than a centre-back.
# ---------------------------------------------------------------------------

def position_category(position):
    if not position or position == "-":
        return "MF"
    pos = str(position).upper()
    if "GK" in pos:
        return "GK"
    if any(tag in pos for tag in ("CB", "LB", "RB", "WB", "DF")):
        return "DF"
    if any(tag in pos for tag in ("FW", "ST", "CF")):
        return "FW"
    return "MF"


def score_attack(row):
    cat = position_category(row.get("position"))
    goals_assists = (row["goals"] or 0) + (row["assists"] or 0)
    shots = row["shots"] or 0
    if cat == "DF":
        if goals_assists >= 1:
            return "green"
        if shots >= 1:
            return "amber"
        return "red"
    if goals_assists >= 1:
        return "green"
    if shots >= 2:
        return "amber"
    return "red"


def score_defence(row):
    cat = position_category(row.get("position"))
    total = (row["tackles_won"] or 0) + (row["interceptions"] or 0)
    if cat == "FW":
        return "green" if total >= 1 else "red"
    if cat == "DF":
        if total >= 4:
            return "green"
        if total >= 2:
            return "amber"
        return "red"
    # MF (and GK, rarely scored here)
    if total >= 3:
        return "green"
    if total >= 1:
        return "amber"
    return "red"


def score_shooting(row):
    cat = position_category(row.get("position"))
    sot = row["shots_on_target"] or 0
    if cat == "DF":
        if sot >= 1:
            return "green"
        if (row["shots"] or 0) >= 1:
            return "amber"
        return "red"
    if sot >= 2:
        return "green"
    if sot == 1:
        return "amber"
    return "red"


def score_goalkeeping(row):
    if row.get("position") != "GK":
        return None
    saves = row["saves"] or 0
    conceded = row["goals_conceded"] or 0
    if conceded == 0 or saves >= 3:
        return "green"
    if saves >= 1:
        return "amber"
    return "red"


CATEGORY_SCORERS = {
    "attack": score_attack,
    "defence": score_defence,
    "shooting": score_shooting,
    "goalkeeping": score_goalkeeping,
}


# ---------------------------------------------------------------------------
# Percentile bars (Players view "Scouting" panel) - mirrors FBref's own
# scouting-report bars: each stat is converted to a per-90 rate and ranked
# against positional peers in the same league this season. No new data
# needed, just a different lens on the season totals already computed
# above. A minimum-minutes gate keeps a player who's featured for 10
# minutes from looking like an elite volume scorer on one lucky shot.
# ---------------------------------------------------------------------------

PERCENTILE_MIN_MINUTES = 270  # ~3 full matches

PERCENTILE_STATS = {
    "outfield": [
        ("season_goals", "Goals"),
        ("season_assists", "Assists"),
        ("season_shots", "Shots"),
        ("season_shots_on_target", "Shots on Target"),
        ("season_tackles_won", "Tackles Won"),
        ("season_interceptions", "Interceptions"),
        ("season_fouls_drawn", "Fouls Drawn"),
        ("season_fouls", "Fouls Committed"),
        ("season_cards_yellow", "Yellow Cards"),
    ],
    "GK": [
        ("season_saves", "Saves"),
        ("season_goals_conceded", "Goals Conceded"),
        ("season_cards_yellow", "Yellow Cards"),
    ],
}


def add_percentiles(players):
    """Mutates each player dict in `players`, adding:
      - percentile_labels: {stat_key: display label} for this player's cat
      - percentiles: {stat_key: {per90, percentile}} or None if the player
        hasn't reached PERCENTILE_MIN_MINUTES this season yet
      - percentile_cohort_size: how many peers they were ranked against

    Cohorts are (league, position category) among the current season's
    data only - comparing a player to the right season's peers, not a
    blend of this season and last."""
    if not players:
        return players

    df = pd.DataFrame(players)
    df["pct_cat"] = df["position"].apply(lambda p: "GK" if position_category(p) == "GK" else "outfield")
    eligible = df[df["season_minutes_played"] >= PERCENTILE_MIN_MINUTES]

    percentiles_by_index = {}
    cohort_size_by_index = {}
    for (_league, cat), group in eligible.groupby(["league", "pct_cat"]):
        stat_defs = PERCENTILE_STATS[cat]
        per90 = pd.DataFrame(index=group.index)
        for stat_key, _label in stat_defs:
            per90[stat_key] = group[stat_key] / group["season_minutes_played"] * 90
        ranks = per90.rank(pct=True) * 100

        for idx in group.index:
            percentiles_by_index[idx] = {
                stat_key: {"per90": round(per90.loc[idx, stat_key], 2), "percentile": round(ranks.loc[idx, stat_key], 1)}
                for stat_key, _label in stat_defs
            }
            cohort_size_by_index[idx] = len(group)

    for i, p in enumerate(players):
        cat = df.loc[i, "pct_cat"]
        p["percentile_labels"] = dict(PERCENTILE_STATS[cat])
        p["percentiles"] = percentiles_by_index.get(i)
        p["percentile_cohort_size"] = cohort_size_by_index.get(i, 0)

    return players


def build_form_streaks(match_df):
    streaks = {}
    match_df = match_df.sort_values(["league", "player_id", "match_date"])

    for (league, player_id), group in match_df.groupby(["league", "player_id"]):
        recent = group.tail(FORM_WINDOW)
        player_streaks = {cat: [] for cat in CATEGORY_SCORERS}
        for _, row in recent.iterrows():
            for cat, scorer in CATEGORY_SCORERS.items():
                player_streaks[cat].append(scorer(row))
        streaks[(league, player_id)] = player_streaks

    return streaks


# ---------------------------------------------------------------------------
# Stat totals - "last N matches" (as a per-match series + average) and
# "season so far" (players), plus team summary - all computed from
# player_match_stats rather than the separate player_rolling_stats/
# player_season_stats tables (which either average instead of listing each
# match, or lack goals/assists entirely).
# ---------------------------------------------------------------------------

def filter_to_current_season(match_df):
    current_season = match_df["league"].map(CURRENT_SEASON_BY_LEAGUE)
    return match_df[current_season == match_df["season"]]


def _zero_stat_row():
    return {"matches_played": 0, **{c: 0 for c in CURATED_STAT_COLUMNS + GK_STAT_COLUMNS}}


def _zero_rolling_row():
    return {"matches_played": 0, **{c: {"values": [], "avg": 0} for c in CURATED_STAT_COLUMNS + GK_STAT_COLUMNS}}


def _sum_stats(group):
    row = {"matches_played": int(len(group))}
    for c in CURATED_STAT_COLUMNS + GK_STAT_COLUMNS:
        row[c] = round(float(group[c].fillna(0).sum()), 1)
    return row


def _clean_number(v):
    """Whole numbers as int (e.g. 2, not 2.0), fractional as 1dp float -
    keeps the per-match series readable (shots/goals/cards are always
    whole numbers per match; this only matters if a stat is ever
    unexpectedly fractional)."""
    f = float(v) if pd.notna(v) else 0.0
    return int(f) if f.is_integer() else round(f, 1)


def build_rolling_series(match_df):
    """Per-player, per-stat list of the actual value in each of their last
    FORM_WINDOW matches (oldest to newest, same convention as the form
    dots), plus the average of those values - e.g. goals: {values: [1, 0,
    2, 0, 1], avg: 0.8}, so the table can show "1 0 2 0 1 (0.8)" instead of
    collapsing straight to a single number."""
    series = {}
    sorted_df = match_df.sort_values(["league", "player_id", "match_date"])
    for (league, player_id), group in sorted_df.groupby(["league", "player_id"]):
        recent = group.tail(FORM_WINDOW)
        row = {"matches_played": int(len(recent))}
        for c in CURATED_STAT_COLUMNS + GK_STAT_COLUMNS:
            values = [_clean_number(v) for v in recent[c].tolist()]
            avg = _clean_number(sum(values) / len(values)) if values else 0
            row[c] = {"values": values, "avg": avg}
        series[(league, player_id)] = row
    return series


def get_team_last_n_fixtures(fixtures_df, n=LAST_N):
    """(league, team) -> that team's last n PLAYED match_ids, oldest to
    newest. `fixtures_df` must already be filtered to is_played=1."""
    result = {}
    for (league, team), group in fixtures_df.sort_values(["league", "team", "match_date"]).groupby(["league", "team"]):
        result[(league, team)] = group["match_id"].tail(n).tolist()
    return result


def build_team_fixture_series(match_df, fixtures_df):
    """Per-player, per-stat list of values over their TEAM's last
    FORM_WINDOW played fixtures - unlike build_rolling_series() above,
    this counts a fixture the player didn't feature in too (rotation,
    injury, suspension), recording 0 for it but flagging `played: False`
    so the dashboard can render that 0 distinctly from a real
    played-and-recorded-zero, instead of the current "last N matches
    played" view silently skipping squad-rotation gaps altogether."""
    team_fixtures = get_team_last_n_fixtures(fixtures_df, FORM_WINDOW)

    stat_lookup = {}
    for row in match_df.itertuples(index=False):
        stat_lookup[(row.league, row.player_id, row.match_id)] = row

    last_team = (
        match_df.sort_values(["league", "player_id", "match_date"])
        .groupby(["league", "player_id"])["team"].last()
    )

    series = {}
    for (league, player_id), team in last_team.items():
        match_ids = team_fixtures.get((league, team), [])
        row = {"fixtures_in_window": len(match_ids)}
        matches_played = sum(1 for mid in match_ids if (league, player_id, mid) in stat_lookup)
        row["matches_played"] = matches_played
        for c in CURATED_STAT_COLUMNS + GK_STAT_COLUMNS:
            values, played_flags = [], []
            for mid in match_ids:
                stat_row = stat_lookup.get((league, player_id, mid))
                if stat_row is not None:
                    values.append(_clean_number(getattr(stat_row, c)))
                    played_flags.append(True)
                else:
                    values.append(0)
                    played_flags.append(False)
            avg = _clean_number(sum(values) / len(values)) if values else 0
            row[c] = {"values": values, "played": played_flags, "avg": avg}
        series[(league, player_id)] = row
    return series


def build_season_totals(season_df):
    totals = {}
    for (league, player_id), group in season_df.groupby(["league", "player_id"]):
        totals[(league, player_id)] = _sum_stats(group)
    return totals


def _team_match_averages(match_df, team_fixtures):
    """(league, team) -> {stat: avg}, averaging each stat's team-total
    over the given (league, team) -> match_ids mapping."""
    rows = {}
    for (league, team), match_ids in team_fixtures.items():
        if not match_ids:
            continue
        subset = match_df[
            (match_df["league"] == league) & (match_df["team"] == team) & (match_df["match_id"].isin(match_ids))
        ]
        n = len(match_ids)
        row = {}
        for c in CURATED_STAT_COLUMNS + GK_STAT_COLUMNS:
            total = float(subset[c].fillna(0).sum())
            row[c] = round(total / n, 2) if n else 0.0
        rows[(league, team)] = row
    return rows


def build_team_rolling_summary(match_df, fixtures_df):
    """Per (league, team), average team-total stats over the team's last
    FORM_WINDOW HOME fixtures, and separately its last FORM_WINDOW AWAY
    fixtures - kept apart rather than blended, since home/away form can
    differ a lot. Returns (home_rows, away_rows)."""
    home_fixtures = get_team_last_n_fixtures(fixtures_df[fixtures_df["is_home"] == 1], FORM_WINDOW)
    away_fixtures = get_team_last_n_fixtures(fixtures_df[fixtures_df["is_home"] == 0], FORM_WINDOW)
    return _team_match_averages(match_df, home_fixtures), _team_match_averages(match_df, away_fixtures)


def build_team_summary(season_df, match_df, fixtures_df):
    """Team-level summary for the Teams view: matches played, plus each
    stat as four per-game averages (not totals) - season-so-far and
    last-FORM_WINDOW-games, each split into home and away rather than
    blended into one number."""
    rows = []
    if season_df.empty:
        return rows
    last5_home, last5_away = build_team_rolling_summary(match_df, fixtures_df)

    for (league, team), group in season_df.groupby(["league", "team"]):
        matches_played = int(group["match_id"].nunique())
        row = {"league": league, "team": team, "matches_played": matches_played}

        home_group = group[group["venue"] == "Home"]
        away_group = group[group["venue"] == "Away"]
        home_matches = home_group["match_id"].nunique()
        away_matches = away_group["match_id"].nunique()
        l5h = last5_home.get((league, team), {})
        l5a = last5_away.get((league, team), {})

        for c in CURATED_STAT_COLUMNS + GK_STAT_COLUMNS:
            home_total = float(home_group[c].fillna(0).sum())
            away_total = float(away_group[c].fillna(0).sum())
            row[f"season_avg_home_{c}"] = round(home_total / home_matches, 2) if home_matches else 0.0
            row[f"season_avg_away_{c}"] = round(away_total / away_matches, 2) if away_matches else 0.0
            row[f"last5_avg_home_{c}"] = l5h.get(c, 0.0)
            row[f"last5_avg_away_{c}"] = l5a.get(c, 0.0)
        rows.append(row)
    return rows


def build_league_table(fixtures_df):
    """Standard standings per league, current season only: played / won /
    drawn / lost / goals for / against / difference / points, sorted by
    points then goal difference then goals for (usual tiebreak order).
    Needs goals_for/goals_against on fixtures (populated by
    pull_full_schedule.py's score parsing) - a match missing that (e.g.
    scraped before that column existed) is simply excluded rather than
    guessed at."""
    if fixtures_df.empty:
        return []

    current_season = fixtures_df["league"].map(CURRENT_SEASON_BY_LEAGUE)
    played = fixtures_df[fixtures_df["goals_for"].notna() & (current_season == fixtures_df["season"])]

    rows = []
    for (league, team), group in played.groupby(["league", "team"]):
        won = int((group["goals_for"] > group["goals_against"]).sum())
        drawn = int((group["goals_for"] == group["goals_against"]).sum())
        lost = int((group["goals_for"] < group["goals_against"]).sum())
        gf = int(group["goals_for"].sum())
        ga = int(group["goals_against"].sum())
        rows.append({
            "league": league, "team": team, "played": int(len(group)),
            "won": won, "drawn": drawn, "lost": lost,
            "goals_for": gf, "goals_against": ga, "goal_diff": gf - ga,
            "points": won * 3 + drawn,
        })

    rows.sort(key=lambda r: (r["league"], -r["points"], -r["goal_diff"], -r["goals_for"]))

    position_by_league = {}
    for row in rows:
        position_by_league[row["league"]] = position_by_league.get(row["league"], 0) + 1
        row["position"] = position_by_league[row["league"]]

    return rows


def _zero_team_fixture_row():
    return {
        "matches_played": 0, "fixtures_in_window": 0,
        **{c: {"values": [], "played": [], "avg": 0} for c in CURATED_STAT_COLUMNS + GK_STAT_COLUMNS},
    }


def build_player_payload(match_df, fixtures_df):
    if match_df.empty:
        print("No data in player_match_stats yet - run the scraper first.")
        return []

    streaks = build_form_streaks(match_df)
    season_df = filter_to_current_season(match_df)
    season_totals = build_season_totals(season_df)
    rolling_series = build_rolling_series(match_df)
    team_fixture_series = build_team_fixture_series(match_df, fixtures_df)

    sorted_df = match_df.sort_values(["league", "player_id", "match_date"])

    players = []
    for (league, player_id), group in sorted_df.groupby(["league", "player_id"]):
        last = group.iloc[-1]
        position = last.get("position")
        position = position if pd.notna(position) else "-"
        rolling_row = rolling_series.get((league, player_id), _zero_rolling_row())
        season_row = season_totals.get((league, player_id), _zero_stat_row())
        teamfx_row = team_fixture_series.get((league, player_id), _zero_team_fixture_row())

        players.append({
            "player_id": player_id,
            "league": league,
            "player_name": last["player_name"],
            "team": last["team"],
            "position": position,
            "rolling_matches_played": rolling_row["matches_played"],
            "rolling_goals": rolling_row["goals"],
            "rolling_assists": rolling_row["assists"],
            "rolling_shots": rolling_row["shots"],
            "rolling_shots_on_target": rolling_row["shots_on_target"],
            "rolling_tackles_won": rolling_row["tackles_won"],
            "rolling_interceptions": rolling_row["interceptions"],
            "rolling_fouls": rolling_row["fouls"],
            "rolling_fouls_drawn": rolling_row["fouls_drawn"],
            "rolling_minutes_played": rolling_row["minutes_played"],
            "rolling_cards_yellow": rolling_row["cards_yellow"],
            "rolling_saves": rolling_row["saves"],
            "rolling_goals_conceded": rolling_row["goals_conceded"],
            "season_matches_played": season_row["matches_played"],
            "season_goals": season_row["goals"],
            "season_assists": season_row["assists"],
            "season_shots": season_row["shots"],
            "season_shots_on_target": season_row["shots_on_target"],
            "season_tackles_won": season_row["tackles_won"],
            "season_interceptions": season_row["interceptions"],
            "season_fouls": season_row["fouls"],
            "season_fouls_drawn": season_row["fouls_drawn"],
            "season_minutes_played": season_row["minutes_played"],
            "season_cards_yellow": season_row["cards_yellow"],
            "season_saves": season_row["saves"],
            "season_goals_conceded": season_row["goals_conceded"],
            "teamfx_matches_played": teamfx_row["matches_played"],
            "teamfx_fixtures_in_window": teamfx_row["fixtures_in_window"],
            "teamfx_goals": teamfx_row["goals"],
            "teamfx_assists": teamfx_row["assists"],
            "teamfx_shots": teamfx_row["shots"],
            "teamfx_shots_on_target": teamfx_row["shots_on_target"],
            "teamfx_tackles_won": teamfx_row["tackles_won"],
            "teamfx_interceptions": teamfx_row["interceptions"],
            "teamfx_fouls": teamfx_row["fouls"],
            "teamfx_fouls_drawn": teamfx_row["fouls_drawn"],
            "teamfx_minutes_played": teamfx_row["minutes_played"],
            "teamfx_cards_yellow": teamfx_row["cards_yellow"],
            "teamfx_saves": teamfx_row["saves"],
            "teamfx_goals_conceded": teamfx_row["goals_conceded"],
            "form": streaks.get((league, player_id), {cat: [] for cat in CATEGORY_SCORERS}),
        })

    add_percentiles(players)
    return players


# ---------------------------------------------------------------------------
# Fixtures view - current gameweek per league, squads with last-5-fixture
# aggregated stats.
# ---------------------------------------------------------------------------

def get_numeric_stat_columns(conn):
    cols = conn.execute("PRAGMA table_info(player_match_stats)").fetchall()
    numeric = []
    for _cid, name, coltype, _notnull, _dflt, _pk in cols:
        if name.lower() in EXCLUDE_FROM_STATS:
            continue
        if coltype.upper() in ("INTEGER", "REAL", "NUM", "NUMERIC", "FLOAT", "INT"):
            numeric.append(name)
    return numeric


def get_fixture_details(conn, league, match_id):
    home = conn.execute(
        "SELECT team, opponent, match_date, venue FROM fixtures "
        "WHERE league = ? AND match_id = ? AND is_home = 1",
        (league, match_id),
    ).fetchone()
    away = conn.execute(
        "SELECT team, opponent, match_date, venue FROM fixtures "
        "WHERE league = ? AND match_id = ? AND is_home = 0",
        (league, match_id),
    ).fetchone()
    if not home or not away:
        return None
    home_team, _, match_date, venue = home
    away_team, _, _, _ = away
    return {
        "match_id": match_id,
        "home_team": home_team,
        "away_team": away_team,
        "match_date": match_date,
        "venue": venue,
    }


def get_current_squad(conn, league, team):
    last_match = conn.execute(
        "SELECT match_id FROM fixtures WHERE league = ? AND team = ? AND is_played = 1 "
        "ORDER BY match_date DESC LIMIT 1",
        (league, team),
    ).fetchone()
    if not last_match:
        return [], None
    last_match_id = last_match[0]

    # lineups.team comes from the match-report page (e.g. "Heart of
    # Midlothian") while `team` here comes from the schedule page via
    # fixtures (e.g. "Hearts") - the same club, different FBref page, not
    # always the same literal string. team_matches() (registered per-league
    # in build_fixtures_payload) resolves both through the league's alias
    # dict instead of requiring an exact match.
    squad = conn.execute(
        "SELECT player_id, player_name, position, is_starter FROM lineups "
        "WHERE league = ? AND team_matches(team, ?) AND match_id = ? "
        "ORDER BY is_starter DESC, jersey_number ASC",
        (league, team, last_match_id),
    ).fetchall()
    return squad, last_match_id


def get_last_n_match_ids(conn, league, team, n=LAST_N):
    rows = conn.execute(
        "SELECT match_id, match_date FROM fixtures "
        "WHERE league = ? AND team = ? AND is_played = 1 ORDER BY match_date DESC LIMIT ?",
        (league, team, n),
    ).fetchall()
    return [r[0] for r in rows]


def get_player_stats_over_matches(conn, league, player_id, match_ids, stat_columns):
    if not match_ids:
        return {"games_played": 0, "stats": {c: 0 for c in stat_columns}}

    placeholders = ",".join("?" for _ in match_ids)
    cols_sql = ", ".join(stat_columns)
    rows = conn.execute(
        f"SELECT {cols_sql} FROM player_match_stats "
        f"WHERE league = ? AND player_id = ? AND match_id IN ({placeholders})",
        (league, player_id, *match_ids),
    ).fetchall()

    totals = {c: 0 for c in stat_columns}
    for row in rows:
        for col, val in zip(stat_columns, row):
            if val is not None:
                totals[col] += val

    return {"games_played": len(rows), "stats": totals}


def build_squad_payload(conn, league, team, stat_columns):
    squad, _ = get_current_squad(conn, league, team)
    last_n_ids = get_last_n_match_ids(conn, league, team, LAST_N)

    players = []
    for player_id, player_name, position, is_starter in squad:
        stat_line = get_player_stats_over_matches(conn, league, player_id, last_n_ids, stat_columns)
        players.append(
            {
                "player_id": player_id,
                "player_name": player_name,
                "position": position,
                "was_last_starter": bool(is_starter),
                "games_played": stat_line["games_played"],
                "games_window": len(last_n_ids),
                "stats": stat_line["stats"],
            }
        )
    return players


def build_fixtures_payload(conn, league):
    team_aliases = config.LEAGUES.get(league, {}).get("team_aliases", {})
    conn.create_function("team_matches", 2, lambda a, b: common.names_match(a, b, team_aliases))

    stat_columns = get_numeric_stat_columns(conn)
    window_days = GAMEWEEK_WINDOW_DAYS.get(league, 4)

    match_ids, window_start, window_end = common.get_current_gameweek_match_ids(conn, league, window_days)
    print(f"[{league}] Current gameweek window: {window_start} to {window_end} ({len(match_ids)} fixtures)")

    if not match_ids:
        print(f"[{league}] No fixtures found for current gameweek - skipping.")
        return None

    fixtures_payload = []
    for match_id in match_ids:
        details = get_fixture_details(conn, league, match_id)
        if not details:
            print(f"[{league}] Skipping match_id={match_id} - couldn't resolve home/away pair.")
            continue

        print(f"[{league}]   Building squads for {details['home_team']} vs {details['away_team']}...")
        details["home_squad"] = build_squad_payload(conn, league, details["home_team"], stat_columns)
        details["away_squad"] = build_squad_payload(conn, league, details["away_team"], stat_columns)
        fixtures_payload.append(details)

    return {
        "league": league,
        "window_start": window_start,
        "window_end": window_end,
        "last_n": LAST_N,
        "stat_columns": stat_columns,
        "fixtures": fixtures_payload,
    }


# ---------------------------------------------------------------------------
# Betting data - keyed by league, since the two sources have different
# shapes: EPL comes from The Corner Kick (a separate project, richer
# prediction set including goals/result/BTTS/over-under, trained on 7
# seasons of football-data.co.uk history); any other league (currently
# just MLS) comes from THIS project's own team_stat_predictions table
# (corners/cards/fouls/shots/SOT only, trained on FBref-scraped team
# stats - see train_betting_models.py/predict_betting_stats.py). A
# league with neither source simply has no key in the returned dict.
# ---------------------------------------------------------------------------

def load_corner_kick_data():
    """EPL only. None if The Corner Kick isn't available on this machine
    (different machine, corners.db not built yet, etc.) - degrade
    gracefully rather than crash, since this is a separate project's
    data, not something Player Stats Hub owns."""
    if not BETTING_DB_PATH.exists():
        print(f"[Betting] {BETTING_DB_PATH} not found - no EPL betting data.")
        return None

    try:
        import dashboard_data as betting_data
        conn = sqlite3.connect(str(BETTING_DB_PATH))
        round_number = betting_data.get_current_gameweek(conn)
        if round_number is None:
            conn.close()
            return None
        fixtures = betting_data.fetch_gameweek_data(conn, round_number)
        for fx in fixtures:
            fx["match_date_display"] = betting_data.format_date(fx["match_date"])
        conn.close()
        return {"source": "corner_kick", "source_label": "The Corner Kick",
                "window_label": f"Round {round_number}", "fixtures": fixtures}
    except Exception as e:
        print(f"[Betting] Couldn't load Corner Kick data: {e}")
        return None


def load_own_betting_predictions(conn, league):
    """corners/cards/fouls/shots/SOT predictions for one league's current
    gameweek, from this project's own team_stat_predictions table.
    Normalizes field names to match The Corner Kick's schema (e.g.
    predicted_home_cards -> predicted_home_yellows) so the dashboard's
    fixture-card renderer needs no per-source branching for the markets
    it has in common - only the goals/result section (which this source
    doesn't produce) differs."""
    table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='team_stat_predictions'"
    ).fetchone()
    if not table_exists:
        return None

    window_days = GAMEWEEK_WINDOW_DAYS.get(league, 4)
    match_ids, window_start, window_end = common.get_current_gameweek_match_ids(conn, league, window_days)
    if not match_ids:
        return None

    placeholders = ",".join("?" for _ in match_ids)
    rows = conn.execute(f"""
        SELECT match_id, match_date, home_team, away_team,
               predicted_home_corners, predicted_away_corners,
               predicted_home_cards, predicted_away_cards,
               predicted_home_fouls, predicted_away_fouls,
               predicted_home_shots, predicted_away_shots,
               predicted_home_sot, predicted_away_sot,
               cold_start
        FROM team_stat_predictions
        WHERE league = ? AND match_id IN ({placeholders})
        ORDER BY match_date ASC
    """, (league, *match_ids)).fetchall()
    if not rows:
        return None

    cols = ["match_id", "match_date", "home_team", "away_team",
            "predicted_home_corners", "predicted_away_corners",
            "predicted_home_yellows", "predicted_away_yellows",
            "predicted_home_fouls", "predicted_away_fouls",
            "predicted_home_shots", "predicted_away_shots",
            "predicted_home_sot", "predicted_away_sot",
            "any_cold_start"]
    fixtures = [dict(zip(cols, row)) for row in rows]
    for fx in fixtures:
        fx["match_date_display"] = format_window_date(fx["match_date"])
        fx["predicted_total_corners"] = round((fx["predicted_home_corners"] or 0) + (fx["predicted_away_corners"] or 0), 2)
        fx["predicted_total_fouls"] = round((fx["predicted_home_fouls"] or 0) + (fx["predicted_away_fouls"] or 0), 2)
        fx["any_cold_start"] = bool(fx["any_cold_start"])
        fx["any_warm_start"] = False

    return {"source": "own_model", "source_label": "our own model (single-season, FBref-trained)",
            "window_label": f"{format_window_date(window_start)} to {format_window_date(window_end)}",
            "fixtures": fixtures}


_MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def format_window_date(raw):
    """'2026-08-23 00:00:00' -> '23 Aug 2026' - day before month throughout
    this dashboard, not the raw ISO string or US-style month-first."""
    if not raw:
        return raw
    date_part = raw.split(" ")[0]
    parts = date_part.split("-")
    if len(parts) != 3:
        return date_part
    year, month, day = parts
    try:
        month_name = _MONTH_ABBR[int(month) - 1]
    except (ValueError, IndexError):
        return date_part
    return f"{int(day)} {month_name} {year}"


def load_betting_data():
    data = {}

    corner_kick_data = load_corner_kick_data()
    if corner_kick_data:
        data["EPL"] = corner_kick_data

    conn = sqlite3.connect(DB_PATH)
    for league in config.LEAGUES:
        if league == "EPL":
            continue
        own_data = load_own_betting_predictions(conn, league)
        if own_data:
            data[league] = own_data
    conn.close()

    return data if data else None


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_all_data():
    conn = sqlite3.connect(DB_PATH)

    match_df = pd.read_sql("SELECT * FROM player_match_stats", conn)
    fixtures_df = pd.read_sql("SELECT * FROM fixtures WHERE is_played = 1", conn)
    players = build_player_payload(match_df, fixtures_df)
    season_df = filter_to_current_season(match_df)
    team_rows = build_team_summary(season_df, match_df, fixtures_df)
    league_table_rows = build_league_table(fixtures_df)

    present_fixture_leagues = {row[0] for row in conn.execute("SELECT DISTINCT league FROM fixtures")}
    fixture_leagues = [l for l in LEAGUE_ORDER if l in present_fixture_leagues] + \
        sorted(present_fixture_leagues - set(LEAGUE_ORDER))

    fixture_payloads = []
    for league in fixture_leagues:
        payload = build_fixtures_payload(conn, league)
        if payload:
            fixture_payloads.append(payload)

    conn.close()

    betting_data = load_betting_data()

    return players, team_rows, fixture_payloads, league_table_rows, betting_data


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def generate_html(players, team_rows, fixture_payloads, league_table_rows, betting_data):
    player_leagues = {p["league"] for p in players}
    fixture_leagues = {fp["league"] for fp in fixture_payloads}
    present = player_leagues | fixture_leagues
    leagues = [l for l in LEAGUE_ORDER if l in present] + sorted(present - set(LEAGUE_ORDER))
    default_league = leagues[0] if leagues else None
    default_view = "fixtures" if fixture_payloads else ("players" if players else "teams")

    players_json = json.dumps(players)
    teams_json = json.dumps(team_rows)
    fixtures_json = json.dumps(fixture_payloads)
    league_table_json = json.dumps(league_table_rows)
    betting_json = json.dumps(betting_data)
    leagues_json = json.dumps(leagues)
    default_league_json = json.dumps(default_league)
    default_view_json = json.dumps(default_view)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Player Stats Hub</title>
<style>
    :root {{
        --bg: #0f1420;
        --card-bg: #171d2c;
        --border: #2e3a52;
        --input-bg: #1a2233;
        --text: #e8e8e8;
        --text-dim: #9fb3d9;
        --muted: #6d7891;
        --accent: #3b82f6;
        --accent-dim: #1e3a5f;
        --green: #3fbf5f;
        --amber: #e0a83c;
        --red: #d95c5c;
    }}
    * {{ box-sizing: border-box; }}
    body {{
        font-family: -apple-system, Segoe UI, Arial, sans-serif;
        background: var(--bg);
        color: var(--text);
        margin: 0;
        padding: 24px;
    }}
    h1 {{
        margin: 0 0 16px 0;
        font-size: 22px;
    }}
    .toolbar {{
        display: flex;
        flex-wrap: wrap;
        gap: 16px;
        align-items: center;
        margin-bottom: 16px;
    }}
    .toggle-group {{
        display: flex;
        gap: 8px;
    }}
    .toggle-btn {{
        background: var(--input-bg);
        color: var(--text-dim);
        border: 1px solid var(--border);
        border-radius: 6px;
        padding: 6px 16px;
        font-size: 13px;
        cursor: pointer;
    }}
    .toggle-btn:hover {{
        color: #ffffff;
    }}
    .toggle-btn.active {{
        background: var(--border);
        color: #ffffff;
        border-color: var(--green);
    }}
    .pinned-chip {{
        display: none;
        align-items: center;
        gap: 8px;
        background: var(--accent-dim);
        border: 1px solid var(--accent);
        border-radius: 6px;
        padding: 5px 12px;
        font-size: 12px;
        color: #cfe0ff;
    }}
    .pinned-chip.visible {{ display: flex; }}
    .pinned-chip button {{
        background: none;
        border: none;
        color: #cfe0ff;
        cursor: pointer;
        font-size: 12px;
        text-decoration: underline;
        padding: 0;
    }}
    table {{
        border-collapse: collapse;
        width: 100%;
        font-size: 13px;
    }}
    th, td {{
        padding: 6px 10px;
        text-align: left;
        border-bottom: 1px solid var(--border);
        white-space: nowrap;
        font-variant-numeric: tabular-nums;
    }}
    th {{
        cursor: pointer;
        user-select: none;
        color: var(--text-dim);
        position: sticky;
        top: 0;
        background: var(--bg);
    }}
    th[title] {{
        text-decoration: underline dotted #3a4666;
        text-underline-offset: 3px;
    }}
    th.no-sort {{
        cursor: default;
    }}
    th:hover:not(.no-sort) {{
        color: #ffffff;
    }}
    tr:hover {{
        background: #161e30;
    }}
    .filter-row th {{
        cursor: default;
        position: sticky;
        top: 29px;
        background: var(--bg);
        padding: 4px 6px;
    }}
    .filter-row input, .filter-row select {{
        width: 100%;
        box-sizing: border-box;
        background: var(--input-bg);
        color: var(--text);
        border: 1px solid var(--border);
        border-radius: 4px;
        padding: 4px 6px;
        font-size: 12px;
    }}
    .dot-row {{
        display: flex;
        gap: 3px;
    }}
    .dot {{
        width: 10px;
        height: 10px;
        border-radius: 50%;
        background: #333c50;
    }}
    .dot.green {{ background: var(--green); }}
    .dot.amber {{ background: var(--amber); }}
    .dot.red {{ background: var(--red); }}
    .muted {{
        color: var(--muted);
        font-size: 12px;
        margin: 0 0 16px 0;
    }}
    .series {{
        font-size: 12px;
        color: #b7c2d9;
    }}
    .series .avg {{
        color: var(--text);
        font-weight: 600;
    }}
    .gk-col {{ display: none; }}
    .gk-col.visible {{ display: table-cell; }}
    #playerView, #teamView, #fixturesView {{ display: none; }}

    .fixture-list {{
        display: flex;
        flex-direction: column;
        gap: 10px;
        max-width: 720px;
    }}
    .fixture-card {{
        background: var(--card-bg);
        border: 1px solid var(--border);
        border-radius: 10px;
        padding: 14px 18px;
        cursor: pointer;
        display: flex;
        justify-content: space-between;
        align-items: center;
        transition: border-color 0.15s;
    }}
    .fixture-card:hover {{ border-color: var(--accent); }}
    .fixture-card.active {{ border-color: var(--accent); background: var(--accent-dim); }}
    .fixture-teams {{ font-size: 15px; font-weight: 500; }}
    .fixture-meta {{ font-size: 12px; color: var(--text-dim); text-align: right; }}
    .squad-panel {{
        display: none;
        margin-top: 14px;
        background: var(--card-bg);
        border: 1px solid var(--border);
        border-radius: 10px;
        padding: 18px;
        max-width: 100%;
        overflow-x: auto;
    }}
    .squad-panel.open {{ display: block; }}
    .squad-columns {{
        display: flex;
        gap: 24px;
        flex-wrap: wrap;
    }}
    .squad-block {{ flex: 1; min-width: 420px; }}
    .squad-block h3 {{ font-size: 14px; margin: 0 0 10px; color: var(--accent); }}
    .player-name {{ font-weight: 500; }}
    .player-name button {{
        background: none;
        border: none;
        padding: 0;
        color: var(--text);
        font: inherit;
        font-weight: 500;
        cursor: pointer;
        text-align: left;
    }}
    .player-name button:hover {{ color: var(--accent); text-decoration: underline; }}
    .games-badge {{ font-size: 11px; color: var(--text-dim); }}
    .window-note {{ font-size: 11px; color: var(--text-dim); margin-bottom: 12px; }}
    .spark {{ vertical-align: middle; margin-right: 6px; }}
    .dnp {{ color: var(--red); font-weight: 700; }}
    .league-picker {{ max-width: 720px; }}
    .league-picker h2 {{ font-size: 16px; font-weight: 500; color: var(--text-dim); margin: 0 0 16px; }}
    .league-grid {{
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
        gap: 14px;
    }}
    .league-card {{
        background: var(--card-bg);
        border: 1px solid var(--border);
        border-radius: 10px;
        padding: 24px 18px;
        font-size: 18px;
        font-weight: 600;
        cursor: pointer;
        text-align: center;
        transition: border-color 0.15s, background 0.15s;
    }}
    .league-card:hover {{ border-color: var(--accent); background: var(--accent-dim); }}
    .league-indicator {{
        display: flex;
        align-items: center;
        gap: 10px;
        font-size: 15px;
        font-weight: 600;
    }}
    .league-indicator button {{
        background: none;
        border: 1px solid var(--border);
        border-radius: 6px;
        padding: 4px 10px;
        color: var(--text-dim);
        font-size: 12px;
        font-weight: 400;
        cursor: pointer;
    }}
    .league-indicator button:hover {{ border-color: var(--accent); color: var(--accent); }}
    .team-row {{ cursor: pointer; }}
    .team-row:hover {{ background: var(--accent-dim); }}
    .team-expand-row td {{
        background: var(--card-bg);
        padding: 14px 18px;
    }}
    .team-roster-table {{ width: 100%; }}
    .team-roster-table th {{ font-size: 11px; color: var(--text-dim); text-align: left; padding: 4px 8px; }}
    .team-roster-table td {{ padding: 4px 8px; }}
    .betting-note {{ font-size: 12px; color: var(--text-dim); margin-bottom: 16px; }}
    .betting-grid {{
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
        gap: 16px;
    }}
    .betting-card {{
        background: var(--card-bg);
        border: 1px solid var(--border);
        border-radius: 10px;
        padding: 16px 18px;
    }}
    .betting-card-head {{
        display: flex;
        justify-content: space-between;
        align-items: baseline;
        margin-bottom: 4px;
    }}
    .betting-teams {{ font-size: 15px; font-weight: 600; }}
    .betting-date {{ font-size: 11px; color: var(--text-dim); }}
    .confidence-badge {{
        display: inline-block;
        font-size: 10px;
        font-weight: 600;
        padding: 2px 7px;
        border-radius: 10px;
        margin-bottom: 10px;
    }}
    .confidence-badge.low {{ background: rgba(217, 92, 92, 0.18); color: var(--red); }}
    .confidence-badge.estimated {{ background: rgba(224, 168, 60, 0.18); color: var(--amber); }}
    .betting-xg {{ font-size: 13px; color: var(--text-dim); margin-bottom: 10px; }}
    .betting-xg strong {{ color: var(--text); }}
    .result-bar {{
        display: flex;
        height: 20px;
        border-radius: 5px;
        overflow: hidden;
        margin-bottom: 4px;
        font-size: 10px;
        font-weight: 600;
    }}
    .result-bar span {{
        display: flex;
        align-items: center;
        justify-content: center;
        color: #ffffff;
        white-space: nowrap;
        overflow: hidden;
    }}
    .result-bar .rb-home {{ background: var(--green); }}
    .result-bar .rb-draw {{ background: var(--muted); }}
    .result-bar .rb-away {{ background: var(--accent); }}
    .betting-legend {{ display: flex; justify-content: space-between; font-size: 10px; color: var(--text-dim); margin-bottom: 12px; }}
    .betting-markets {{
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 6px 18px;
        font-size: 12px;
    }}
    .betting-market-row {{ display: flex; justify-content: space-between; }}
    .betting-market-label {{ color: var(--text-dim); }}
    .scouting-panel {{
        display: none;
        margin-bottom: 16px;
        background: var(--card-bg);
        border: 1px solid var(--border);
        border-radius: 10px;
        padding: 18px;
        max-width: 640px;
    }}
    .scouting-panel.open {{ display: block; }}
    .scouting-panel h3 {{ font-size: 14px; margin: 0 0 4px; color: var(--accent); }}
    .pct-row {{
        display: grid;
        grid-template-columns: 130px 1fr 60px 34px;
        align-items: center;
        gap: 10px;
        margin: 7px 0;
    }}
    .pct-label {{ font-size: 12px; color: var(--text-dim); }}
    .pct-bar-track {{
        background: var(--border);
        border-radius: 4px;
        height: 8px;
        overflow: hidden;
    }}
    .pct-bar-fill {{ height: 100%; border-radius: 4px; }}
    .pct-value {{ font-size: 11px; color: var(--text-dim); text-align: right; }}
    .pct-num {{ font-size: 12px; font-weight: 600; text-align: right; }}
</style>
</head>
<body>

<h1>Player Stats Hub</h1>
<p class="muted" id="subhead"></p>

<div class="league-picker" id="leaguePickerView">
    <h2>Select a league</h2>
    <div class="league-grid" id="leagueGrid"></div>
</div>

<div id="dashboardArea">
<div class="toolbar">
    <div class="league-indicator" id="leagueIndicator">
        <span id="leagueIndicatorLabel"></span>
        <button id="changeLeagueBtn">Change league</button>
    </div>
    <div class="toggle-group" id="viewToggle">
        <button class="toggle-btn" data-view="fixtures">Fixtures</button>
        <button class="toggle-btn" data-view="players">Players</button>
        <button class="toggle-btn" data-view="teams">Teams</button>
        <button class="toggle-btn" data-view="table">Table</button>
        <button class="toggle-btn" data-view="betting">Betting</button>
    </div>
    <div class="toggle-group" id="statModeToggle">
        <button class="toggle-btn active" data-mode="rolling">Last {FORM_WINDOW} Matches</button>
        <button class="toggle-btn" data-mode="teamfx">Last {FORM_WINDOW} Team Fixtures</button>
        <button class="toggle-btn" data-mode="season">Season Totals</button>
    </div>
    <div class="pinned-chip" id="pinnedChip">
        <span id="pinnedLabel"></span>
        <button id="clearPin">clear</button>
    </div>
</div>

<div id="fixturesView">
    <div class="fixture-list" id="fixtureList"></div>
    <div class="squad-panel" id="squadPanel"></div>
</div>

<div id="playerView">
<div class="scouting-panel" id="scoutingPanel"></div>
<table id="playerTable">
    <thead id="playerTableHead"></thead>
    <tbody id="tableBody"></tbody>
</table>
</div>

<div id="teamView">
<table id="teamTable">
    <thead id="teamTableHead"></thead>
    <tbody id="teamTableBody"></tbody>
</table>
</div>

<div id="leagueTableView">
<table id="leagueTable">
    <thead>
        <tr>
            <th>#</th>
            <th>Team</th>
            <th title="Played">P</th>
            <th title="Won">W</th>
            <th title="Drawn">D</th>
            <th title="Lost">L</th>
            <th title="Goals For">GF</th>
            <th title="Goals Against">GA</th>
            <th title="Goal Difference">GD</th>
            <th title="Points">Pts</th>
        </tr>
    </thead>
    <tbody id="leagueTableBody"></tbody>
</table>
</div>

<div id="bettingView">
    <div id="bettingContent"></div>
</div>
</div>

<script>
    const players = {players_json};
    const teamRows = {teams_json};
    const fixturesData = {fixtures_json};
    const leagueTableData = {league_table_json};
    const bettingData = {betting_json};
    const leagues = {leagues_json};

    let currentLeague = null;
    let currentView = {default_view_json};
    const DEFAULT_LEAGUE = {default_league_json};
    let statMode = "rolling";
    let pinnedPlayerId = null;
    let sortKey = "rolling_goals";
    let sortDir = -1;
    let teamSortKey = "season_avg_home_goals";
    let teamSortDir = -1;
    let activeMatchId = null;
    let expandedTeam = null;

    const TEXT_FILTER_KEYS = new Set(["player_name"]);
    const EXACT_FILTER_KEYS = new Set(["team", "position"]);

    // ---- shared helpers ----

    function statValue(p, key) {{
        const v = p[key];
        if (v && typeof v === 'object' && 'avg' in v) return v.avg;
        return v;
    }}

    function renderSparkline(values, playedFlags) {{
        if (!values || values.length < 2) return '';
        const max = Math.max(1, ...values);
        const w = 50, h = 16, pad = 2;
        const step = w / (values.length - 1);
        const points = values.map((v, i) => {{
            const x = i * step;
            const y = h - pad - (v / max) * (h - pad * 2);
            return {{ x, y }};
        }});
        const polyline = points.map(p => `${{p.x.toFixed(1)}},${{p.y.toFixed(1)}}`).join(' ');
        const dots = points.map((p, i) => {{
            const played = !playedFlags || playedFlags[i];
            const color = played ? 'var(--accent)' : 'var(--red)';
            return `<circle cx="${{p.x.toFixed(1)}}" cy="${{p.y.toFixed(1)}}" r="1.7" fill="${{color}}" />`;
        }}).join('');
        return `<svg class="spark" width="${{w}}" height="${{h}}" viewBox="0 0 ${{w}} ${{h}}">` +
            `<polyline points="${{polyline}}" fill="none" stroke="var(--text-dim)" stroke-width="1" />${{dots}}</svg>`;
    }}

    function dotsHtml(colours) {{
        if (!colours || colours.length === 0) {{
            return '<div class="dot-row"><span class="muted">-</span></div>';
        }}
        return '<div class="dot-row">' +
            colours.map(c => `<span class="dot ${{c || ''}}"></span>`).join('') +
            '</div>';
    }}

    // ---- view / league / stat-mode toggling ----

    function setupViewToggle() {{
        document.querySelectorAll('#viewToggle .toggle-btn').forEach(btn => {{
            btn.classList.toggle('active', btn.dataset.view === currentView);
            btn.addEventListener('click', () => switchView(btn.dataset.view));
        }});
    }}

    function switchView(view) {{
        currentView = view;
        document.querySelectorAll('#viewToggle .toggle-btn').forEach(b => b.classList.toggle('active', b.dataset.view === view));
        document.getElementById('fixturesView').style.display = view === 'fixtures' ? 'block' : 'none';
        document.getElementById('playerView').style.display = view === 'players' ? 'block' : 'none';
        document.getElementById('teamView').style.display = view === 'teams' ? 'block' : 'none';
        document.getElementById('leagueTableView').style.display = view === 'table' ? 'block' : 'none';
        document.getElementById('bettingView').style.display = view === 'betting' ? 'block' : 'none';
        document.getElementById('statModeToggle').style.display = view === 'players' ? 'flex' : 'none';
        renderCurrentView();
    }}

    function renderCurrentView() {{
        if (currentView === 'fixtures') renderFixtureList();
        else if (currentView === 'players') renderTable();
        else if (currentView === 'teams') renderTeamTable();
        else if (currentView === 'table') renderLeagueTable();
        else renderBettingView();
    }}

    function renderLeaguePicker() {{
        const gridEl = document.getElementById('leagueGrid');
        gridEl.innerHTML = leagues.map(l => `<div class="league-card" data-league="${{l}}">${{l}}</div>`).join('');
        gridEl.querySelectorAll('.league-card').forEach(card => {{
            card.addEventListener('click', () => enterLeague(card.dataset.league));
        }});
    }}

    function showLeaguePicker() {{
        document.getElementById('leaguePickerView').style.display = 'block';
        document.getElementById('dashboardArea').style.display = 'none';
        const url = new URL(window.location);
        url.searchParams.delete('league');
        url.searchParams.delete('view');
        url.searchParams.delete('player');
        window.history.replaceState({{}}, '', url);
    }}

    function enterLeague(league) {{
        currentLeague = league;
        activeMatchId = null;
        expandedTeam = null;
        clearPin();
        closeScouting();
        document.getElementById('leaguePickerView').style.display = 'none';
        document.getElementById('dashboardArea').style.display = 'block';
        document.getElementById('leagueIndicatorLabel').textContent = league;
        setupDropdownFilters();
        renderCurrentView();
    }}

    function setupStatModeToggle() {{
        document.querySelectorAll('#statModeToggle .toggle-btn').forEach(btn => {{
            btn.addEventListener('click', () => {{
                if (btn.dataset.mode === statMode) return;
                statMode = btn.dataset.mode;
                sortKey = COLUMN_SETS[statMode].defaultSort;
                sortDir = -1;
                document.querySelectorAll('#statModeToggle .toggle-btn').forEach(b => b.classList.toggle('active', b === btn));
                renderSubhead();
                renderPlayerTableHead();
                renderTable();
            }});
        }});
    }}

    function renderSubhead() {{
        if (currentView === 'fixtures') {{
            const data = fixturesData.find(d => d.league === currentLeague);
            document.getElementById('subhead').textContent = data
                ? `Window: ${{formatDate(data.window_start)}} to ${{formatDate(data.window_end)}} - stats below are each player's totals over their team's last ${{data.last_n}} fixtures, whether they featured or not. Click a player's name to jump to their row in Players.`
                : `No fixtures found for this league's current gameweek.`;
        }} else if (currentView === 'players') {{
            const subheadText = {{
                rolling: `Actual totals over each player's last {FORM_WINDOW} matches THEY FEATURED IN (not an average, and not counting fixtures they missed). Form dots show most recent {FORM_WINDOW} matches, oldest to newest. Minimum filters show players AT OR ABOVE the value entered. Hover a column header for its full name.`,
                teamfx: `Values over their TEAM's last {FORM_WINDOW} played fixtures, whether the player featured or not - a fixture they missed (rotation, injury, suspension) counts as one of the {FORM_WINDOW} and shows as a red 0, so a quiet run doesn't get hidden by simply not being in the squad. Minimum filters show players AT OR ABOVE the value entered. Hover a column header for its full name.`,
                season: `Totals for this league's current season so far. A league whose season hasn't started yet will show all zeros here until matches are scraped. Hover a column header for its full name.`,
            }}[statMode];
            document.getElementById('subhead').textContent = subheadText;
        }} else if (currentView === 'teams') {{
            document.getElementById('subhead').textContent = `Season-to-date squad totals per team. Hover a column header for its full name.`;
        }} else if (currentView === 'table') {{
            document.getElementById('subhead').textContent = `Current-season standings, sorted by points then goal difference. Click a team on the Fixtures/Players/Teams views for more detail.`;
        }} else {{
            document.getElementById('subhead').textContent = `Match result, corners, cards, fouls and shots predictions from a separate modeling project - not derived from the stats data on this page.`;
        }}
    }}

    // ---- Fixtures view ----

    function currentFixturesData() {{
        return fixturesData.find(d => d.league === currentLeague);
    }}

    const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

    function formatDate(d) {{
        if (!d) return '';
        // Parsed from the plain "YYYY-MM-DD" string, not via `new Date()` -
        // that would parse as UTC midnight and can shift a day off in the
        // browser's local timezone. UK convention: day before month.
        const [year, month, day] = d.split(' ')[0].split('-');
        const monthName = MONTH_NAMES[parseInt(month, 10) - 1];
        if (!monthName) return d.split(' ')[0];
        return `${{parseInt(day, 10)}} ${{monthName}} ${{year}}`;
    }}

    function renderFixtureList() {{
        renderSubhead();
        const listEl = document.getElementById('fixtureList');
        const data = currentFixturesData();
        if (!data) {{
            listEl.innerHTML = '<p class="muted">No fixtures for this league right now.</p>';
            document.getElementById('squadPanel').classList.remove('open');
            document.getElementById('squadPanel').innerHTML = '';
            return;
        }}
        listEl.innerHTML = '';
        data.fixtures.forEach(fx => {{
            const card = document.createElement('div');
            card.className = 'fixture-card' + (fx.match_id === activeMatchId ? ' active' : '');
            card.innerHTML = `
                <div class="fixture-teams">${{fx.home_team}} vs ${{fx.away_team}}</div>
                <div class="fixture-meta">${{formatDate(fx.match_date)}}<br>${{fx.venue || ''}}</div>
            `;
            card.addEventListener('click', () => toggleFixture(fx.match_id));
            listEl.appendChild(card);
        }});
    }}

    function renderSquadTable(squadPlayers, teamLabel, statColumns) {{
        const rows = squadPlayers.map(p => {{
            const statCells = statColumns.map(c => {{
                const v = p.stats[c];
                return `<td>${{v === undefined || v === null ? '-' : v}}</td>`;
            }}).join('');
            return `
                <tr>
                    <td class="player-name"><button data-player="${{p.player_id}}">${{p.player_name}}</button></td>
                    <td>${{p.position || ''}}</td>
                    <td class="games-badge">${{p.games_played}}/${{p.games_window}}</td>
                    ${{statCells}}
                </tr>
            `;
        }}).join('');

        const headerCells = statColumns.map(c => `<th>${{c}}</th>`).join('');

        return `
            <div class="squad-block">
                <h3>${{teamLabel}}</h3>
                <table>
                    <thead>
                        <tr><th>Player</th><th>Pos</th><th>Games</th>${{headerCells}}</tr>
                    </thead>
                    <tbody>${{rows}}</tbody>
                </table>
            </div>
        `;
    }}

    function toggleFixture(matchId) {{
        const panelEl = document.getElementById('squadPanel');
        if (activeMatchId === matchId) {{
            activeMatchId = null;
            panelEl.classList.remove('open');
            panelEl.innerHTML = '';
            renderFixtureList();
            return;
        }}

        activeMatchId = matchId;
        const data = currentFixturesData();
        const fx = data.fixtures.find(f => f.match_id === matchId);
        panelEl.innerHTML = `
            <div class="window-note">Last ${{data.last_n}} team fixtures - totals, not per-game averages.</div>
            <div class="squad-columns">
                ${{renderSquadTable(fx.home_squad, fx.home_team, data.stat_columns)}}
                ${{renderSquadTable(fx.away_squad, fx.away_team, data.stat_columns)}}
            </div>
        `;
        panelEl.querySelectorAll('button[data-player]').forEach(btn => {{
            btn.addEventListener('click', () => showPlayer(btn.dataset.player, currentLeague));
        }});
        panelEl.classList.add('open');
        renderFixtureList();
    }}

    // ---- Players view ----

    // Abbreviation + full name (shown as a hover tooltip) for each raw stat,
    // shared between the "last N matches" and "season totals" column sets
    // so both modes label things identically.
    const STAT_INFO = {{
        goals: {{ abbr: "G", full: "Goals" }},
        assists: {{ abbr: "A", full: "Assists" }},
        shots: {{ abbr: "S", full: "Shots" }},
        shots_on_target: {{ abbr: "SOT", full: "Shots on Target" }},
        tackles_won: {{ abbr: "TW", full: "Tackles Won" }},
        interceptions: {{ abbr: "I", full: "Interceptions" }},
        fouls: {{ abbr: "F", full: "Fouls Committed" }},
        fouls_drawn: {{ abbr: "FD", full: "Fouls Drawn" }},
        minutes_played: {{ abbr: "MIN", full: "Minutes Played" }},
        cards_yellow: {{ abbr: "YC", full: "Yellow Cards" }},
        saves: {{ abbr: "SV", full: "Saves" }},
        goals_conceded: {{ abbr: "GC", full: "Goals Conceded" }},
    }};
    const STAT_ORDER = ["goals", "assists", "shots", "shots_on_target", "tackles_won",
                         "interceptions", "fouls", "fouls_drawn", "minutes_played"];
    const GK_ORDER = ["cards_yellow", "saves", "goals_conceded"];

    function buildColumnSet(prefix, matchesKey, matchesFull) {{
        return {{
            main: [
                {{ key: matchesKey, abbr: "M", full: matchesFull }},
                ...STAT_ORDER.map(s => ({{ key: `${{prefix}}_${{s}}`, abbr: STAT_INFO[s].abbr, full: STAT_INFO[s].full }})),
            ],
            gk: GK_ORDER.map(s => ({{ key: `${{prefix}}_${{s}}`, abbr: STAT_INFO[s].abbr, full: STAT_INFO[s].full }})),
            defaultSort: `${{prefix}}_goals`,
        }};
    }}

    const COLUMN_SETS = {{
        rolling: buildColumnSet("rolling", "rolling_matches_played", `Matches (last {FORM_WINDOW})`),
        teamfx: buildColumnSet("teamfx", "teamfx_matches_played", `Matches (last {FORM_WINDOW} team fixtures)`),
        season: buildColumnSet("season", "season_matches_played", "Matches (season)"),
    }};

    function getActiveFilters() {{
        const filters = {{}};
        document.querySelectorAll('#playerTableHead [data-filter]').forEach(el => {{
            const key = el.dataset.filter;
            const val = el.value;
            if (val !== '' && val !== null) {{
                filters[key] = val;
            }}
        }});
        return filters;
    }}

    function updateGkColumnVisibility() {{
        const posEl = document.getElementById('positionFilter');
        const isGk = posEl && posEl.value === 'GK';
        document.querySelectorAll('.gk-col').forEach(el => {{
            el.classList.toggle('visible', isGk);
        }});
    }}

    function renderPlayerTableHead() {{
        const cols = COLUMN_SETS[statMode];
        const headEl = document.getElementById('playerTableHead');

        const mainHeaders = cols.main.map(c => `<th data-key="${{c.key}}" title="${{c.full}}">${{c.abbr}}</th>`).join('');
        const gkHeaders = cols.gk.map(c => `<th class="gk-col" data-key="${{c.key}}" title="${{c.full}}">${{c.abbr}}</th>`).join('');
        const mainFilters = cols.main.map(c => `<th><input type="number" data-filter="${{c.key}}" placeholder="Min" step="1" title="${{c.full}}"></th>`).join('');
        const gkFilters = cols.gk.map(c => `<th class="gk-col"><input type="number" data-filter="${{c.key}}" placeholder="Min" step="1" title="${{c.full}}"></th>`).join('');

        headEl.innerHTML = `
            <tr>
                <th data-key="player_name" title="Player Name">Player</th>
                <th data-key="team" title="Team">Team</th>
                <th data-key="position" title="Position">Pos</th>
                ${{mainHeaders}}
                ${{gkHeaders}}
                <th class="no-sort">Attack</th>
                <th class="no-sort">Defence</th>
                <th class="no-sort">Shooting</th>
                <th class="no-sort">GK</th>
            </tr>
            <tr class="filter-row">
                <th><input type="text" data-filter="player_name" placeholder="Search..."></th>
                <th><select data-filter="team"><option value="">All</option></select></th>
                <th><select id="positionFilter" data-filter="position"><option value="">All</option></select></th>
                ${{mainFilters}}
                ${{gkFilters}}
                <th></th><th></th><th></th><th></th>
            </tr>
        `;

        setupDropdownFilters();
        setupFilterListeners();
        setupSorting();
    }}

    function renderTable() {{
        renderSubhead();
        updateGkColumnVisibility();
        const cols = COLUMN_SETS[statMode];
        const allCols = cols.main.concat(cols.gk);
        const filters = getActiveFilters();

        let rows;
        if (pinnedPlayerId) {{
            rows = players.filter(p => p.player_id === pinnedPlayerId && p.league === currentLeague);
        }} else {{
            rows = players.filter(p => {{
                if (p.league !== currentLeague) return false;
                for (const [key, val] of Object.entries(filters)) {{
                    if (TEXT_FILTER_KEYS.has(key)) {{
                        if (!String(p[key]).toLowerCase().includes(val.toLowerCase())) return false;
                    }} else if (EXACT_FILTER_KEYS.has(key)) {{
                        if (p[key] !== val) return false;
                    }} else {{
                        if ((statValue(p, key) || 0) < parseFloat(val)) return false;
                    }}
                }}
                return true;
            }});
        }}

        rows.sort((a, b) => {{
            let av = statValue(a, sortKey), bv = statValue(b, sortKey);
            if (typeof av === "string") {{
                return sortDir * av.localeCompare(bv);
            }}
            return sortDir * ((av || 0) - (bv || 0));
        }});

        const statCellsFor = p => allCols.map(c => {{
            const cls = cols.gk.includes(c) ? ' class="gk-col"' : '';
            const raw = p[c.key];
            let display;
            if (raw && typeof raw === 'object' && 'values' in raw) {{
                if (raw.values.length) {{
                    const valuesHtml = raw.values.map((v, i) => {{
                        const played = !raw.played || raw.played[i];
                        return played ? v : `<span class="dnp" title="Didn't feature in this fixture">${{v}}</span>`;
                    }}).join(' ');
                    display = `${{renderSparkline(raw.values, raw.played)}}<span class="series">${{valuesHtml}} (<span class="avg">${{raw.avg}}</span>)</span>`;
                }} else {{
                    display = '-';
                }}
            }} else {{
                display = raw;
            }}
            return `<td${{cls}}>${{display}}</td>`;
        }}).join('');

        const tbody = document.getElementById('tableBody');
        tbody.innerHTML = rows.map(p => `
            <tr>
                <td class="player-name"><button data-player="${{p.player_id}}" data-league="${{p.league}}">${{p.player_name}}</button></td>
                <td>${{p.team}}</td>
                <td>${{p.position}}</td>
                ${{statCellsFor(p)}}
                <td>${{dotsHtml(p.form.attack)}}</td>
                <td>${{dotsHtml(p.form.defence)}}</td>
                <td>${{dotsHtml(p.form.shooting)}}</td>
                <td>${{dotsHtml(p.form.goalkeeping)}}</td>
            </tr>
        `).join('');
        tbody.querySelectorAll('button[data-player]').forEach(btn => {{
            btn.addEventListener('click', () => toggleScouting(btn.dataset.player, btn.dataset.league));
        }});

        updateGkColumnVisibility();
    }}

    // ---- Scouting panel (percentile bars vs positional peers) ----

    let activeScoutingPlayerId = null;

    function pctColor(pct) {{
        if (pct >= 70) return 'var(--green)';
        if (pct >= 40) return 'var(--amber)';
        return 'var(--red)';
    }}

    function closeScouting() {{
        activeScoutingPlayerId = null;
        const panelEl = document.getElementById('scoutingPanel');
        panelEl.classList.remove('open');
        panelEl.innerHTML = '';
    }}

    function toggleScouting(playerId, league) {{
        if (activeScoutingPlayerId === playerId) {{
            closeScouting();
            return;
        }}
        activeScoutingPlayerId = playerId;
        const p = players.find(pl => pl.player_id === playerId && pl.league === league);
        const panelEl = document.getElementById('scoutingPanel');
        if (!p) {{
            closeScouting();
            return;
        }}

        if (!p.percentiles) {{
            panelEl.innerHTML = `
                <h3>${{p.player_name}}</h3>
                <div class="window-note">Not enough minutes played this season yet (needs {PERCENTILE_MIN_MINUTES}+) for a reliable percentile comparison.</div>
            `;
            panelEl.classList.add('open');
            return;
        }}

        const bars = Object.entries(p.percentile_labels).map(([key, label]) => {{
            const stat = p.percentiles[key];
            return `
                <div class="pct-row">
                    <span class="pct-label">${{label}}</span>
                    <div class="pct-bar-track"><div class="pct-bar-fill" style="width:${{stat.percentile}}%; background:${{pctColor(stat.percentile)}}"></div></div>
                    <span class="pct-value">${{stat.per90}}/90</span>
                    <span class="pct-num">${{Math.round(stat.percentile)}}</span>
                </div>
            `;
        }}).join('');

        panelEl.innerHTML = `
            <h3>${{p.player_name}} - ${{p.position}}, ${{p.team}}</h3>
            <div class="window-note">Percentile vs ${{p.percentile_cohort_size}} ${{league}} ${{p.position}} peers with {PERCENTILE_MIN_MINUTES}+ minutes this season, by per-90 rate. Higher isn't automatically "better" for every stat (e.g. fouls) - read each on its own terms.</div>
            ${{bars}}
        `;
        panelEl.classList.add('open');
    }}

    function setupDropdownFilters() {{
        const leaguePlayers = players.filter(p => p.league === currentLeague);
        const teams = [...new Set(leaguePlayers.map(p => p.team).filter(Boolean))].sort();
        const positions = [...new Set(leaguePlayers.map(p => p.position).filter(pos => pos && pos !== '-'))].sort();

        const teamSelect = document.querySelector('#playerTableHead select[data-filter="team"]');
        if (teamSelect) {{
            teamSelect.innerHTML = '<option value="">All</option>';
            teams.forEach(t => {{
                const opt = document.createElement('option');
                opt.value = t;
                opt.textContent = t;
                teamSelect.appendChild(opt);
            }});
        }}

        const posSelect = document.getElementById('positionFilter');
        if (posSelect) {{
            posSelect.innerHTML = '<option value="">All</option>';
            positions.forEach(pos => {{
                const opt = document.createElement('option');
                opt.value = pos;
                opt.textContent = pos;
                posSelect.appendChild(opt);
            }});
        }}
    }}

    function setupFilterListeners() {{
        document.querySelectorAll('#playerTableHead [data-filter]').forEach(el => {{
            el.addEventListener('input', renderTable);
            el.addEventListener('change', renderTable);
        }});
    }}

    function setupSorting() {{
        document.querySelectorAll('#playerTableHead th[data-key]').forEach(th => {{
            th.addEventListener('click', () => {{
                const key = th.dataset.key;
                if (sortKey === key) {{
                    sortDir *= -1;
                }} else {{
                    sortKey = key;
                    sortDir = -1;
                }}
                renderTable();
            }});
        }});
    }}

    function showPlayer(playerId, league) {{
        const match = players.find(p => p.player_id === playerId && p.league === league);
        if (!match) return;
        pinnedPlayerId = match.player_id;
        currentLeague = match.league;
        document.getElementById('pinnedLabel').textContent = `Showing: ${{match.player_name}} (${{match.team}})`;
        document.getElementById('pinnedChip').classList.add('visible');
        document.getElementById('leagueIndicatorLabel').textContent = match.league;
        switchView('players');
    }}

    function clearPin() {{
        pinnedPlayerId = null;
        document.getElementById('pinnedChip').classList.remove('visible');
        const url = new URL(window.location);
        url.searchParams.delete('player');
        window.history.replaceState({{}}, '', url);
    }}

    document.getElementById('clearPin').addEventListener('click', () => {{
        clearPin();
        renderTable();
    }});

    // ---- Teams view ----

    // Each stat shows four per-game averages, not a season total: season
    // home avg, season away avg, last-{FORM_WINDOW}-games home avg, and
    // last-{FORM_WINDOW}-games away avg - kept separate, not blended.
    const TEAM_STAT_ORDER = ["goals", "assists", "shots", "shots_on_target", "tackles_won",
                              "interceptions", "fouls", "fouls_drawn", "cards_yellow", "saves", "goals_conceded"];

    const TEAM_COLUMNS = [{{ key: "matches_played", abbr: "M", full: "Matches Played" }}];
    TEAM_STAT_ORDER.forEach(s => {{
        const abbr = STAT_INFO[s].abbr, full = STAT_INFO[s].full;
        TEAM_COLUMNS.push({{ key: `season_avg_home_${{s}}`, abbr: `${{abbr}}H`, full: `${{full}} - season average per HOME game` }});
        TEAM_COLUMNS.push({{ key: `season_avg_away_${{s}}`, abbr: `${{abbr}}A`, full: `${{full}} - season average per AWAY game` }});
        TEAM_COLUMNS.push({{ key: `last5_avg_home_${{s}}`, abbr: `${{abbr}}5H`, full: `${{full}} - average over last {FORM_WINDOW} HOME games` }});
        TEAM_COLUMNS.push({{ key: `last5_avg_away_${{s}}`, abbr: `${{abbr}}5A`, full: `${{full}} - average over last {FORM_WINDOW} AWAY games` }});
    }});

    function renderTeamTableHead() {{
        const headEl = document.getElementById('teamTableHead');
        const cells = TEAM_COLUMNS.map(c => `<th data-key="${{c.key}}" title="${{c.full}}">${{c.abbr}}</th>`).join('');
        headEl.innerHTML = `<tr><th data-key="team">Team</th>${{cells}}</tr>`;
        setupTeamSorting();
    }}

    function renderTeamRoster(team) {{
        const roster = players
            .filter(p => p.league === currentLeague && p.team === team)
            .slice()
            .sort((a, b) => (b.season_minutes_played || 0) - (a.season_minutes_played || 0));

        if (!roster.length) {{
            return `<p class="muted">No player data for ${{team}} yet this season.</p>`;
        }}

        const cols = ["goals", "assists", "shots", "shots_on_target", "tackles_won",
                       "interceptions", "fouls", "fouls_drawn", "minutes_played"];
        const headerCells = cols.map(c => `<th title="${{STAT_INFO[c].full}}">${{STAT_INFO[c].abbr}}</th>`).join('');
        const rows = roster.map(p => {{
            const cells = cols.map(c => `<td>${{p[`season_${{c}}`]}}</td>`).join('');
            return `
                <tr>
                    <td class="player-name"><button data-player="${{p.player_id}}" data-league="${{p.league}}">${{p.player_name}}</button></td>
                    <td>${{p.position}}</td>
                    ${{cells}}
                </tr>
            `;
        }}).join('');

        return `
            <table class="team-roster-table">
                <thead><tr><th>Player</th><th>Pos</th>${{headerCells}}</tr></thead>
                <tbody>${{rows}}</tbody>
            </table>
        `;
    }}

    function renderTeamTable() {{
        renderSubhead();
        let rows = teamRows.filter(t => t.league === currentLeague);
        rows.sort((a, b) => {{
            let av = a[teamSortKey], bv = b[teamSortKey];
            if (typeof av === "string") {{
                return teamSortDir * av.localeCompare(bv);
            }}
            return teamSortDir * ((av || 0) - (bv || 0));
        }});
        const tbody = document.getElementById('teamTableBody');
        if (!rows.length) {{
            tbody.innerHTML = `<tr><td colspan="${{TEAM_COLUMNS.length + 1}}" class="muted">No current-season data yet for this league.</td></tr>`;
            return;
        }}
        tbody.innerHTML = rows.map(t => {{
            const mainRow = `
                <tr class="team-row" data-team="${{t.team}}">
                    <td>${{t.team}}</td>
                    ${{TEAM_COLUMNS.map(c => `<td>${{t[c.key]}}</td>`).join('')}}
                </tr>
            `;
            if (t.team !== expandedTeam) return mainRow;
            return mainRow + `
                <tr class="team-expand-row">
                    <td colspan="${{TEAM_COLUMNS.length + 1}}">${{renderTeamRoster(t.team)}}</td>
                </tr>
            `;
        }}).join('');

        tbody.querySelectorAll('tr.team-row').forEach(tr => {{
            tr.addEventListener('click', () => {{
                expandedTeam = expandedTeam === tr.dataset.team ? null : tr.dataset.team;
                renderTeamTable();
            }});
        }});
        tbody.querySelectorAll('button[data-player]').forEach(btn => {{
            btn.addEventListener('click', (e) => {{
                e.stopPropagation();
                showPlayer(btn.dataset.player, btn.dataset.league);
            }});
        }});
    }}

    function setupTeamSorting() {{
        document.querySelectorAll('#teamTable th[data-key]').forEach(th => {{
            th.addEventListener('click', () => {{
                const key = th.dataset.key;
                if (teamSortKey === key) {{
                    teamSortDir *= -1;
                }} else {{
                    teamSortKey = key;
                    teamSortDir = -1;
                }}
                renderTeamTable();
            }});
        }});
    }}

    // ---- League Table view ----

    function renderLeagueTable() {{
        renderSubhead();
        const rows = leagueTableData.filter(t => t.league === currentLeague);
        const tbody = document.getElementById('leagueTableBody');
        if (!rows.length) {{
            tbody.innerHTML = `<tr><td colspan="10" class="muted">No played matches with a recorded score yet this season for this league.</td></tr>`;
            return;
        }}
        tbody.innerHTML = rows.map(t => `
            <tr>
                <td>${{t.position}}</td>
                <td>${{t.team}}</td>
                <td>${{t.played}}</td>
                <td>${{t.won}}</td>
                <td>${{t.drawn}}</td>
                <td>${{t.lost}}</td>
                <td>${{t.goals_for}}</td>
                <td>${{t.goals_against}}</td>
                <td>${{t.goal_diff > 0 ? '+' : ''}}${{t.goal_diff}}</td>
                <td><strong>${{t.points}}</strong></td>
            </tr>
        `).join('');
    }}

    // ---- Betting view (The Corner Kick) ----

    function pct(v) {{
        return (v === null || v === undefined) ? '—' : Math.round(v * 100) + '%';
    }}

    function num(v, decimals = 1) {{
        return (v === null || v === undefined) ? '—' : v.toFixed(decimals);
    }}

    function renderBettingCard(fx) {{
        const hasResultModel = fx.expected_home_goals !== undefined && fx.expected_home_goals !== null;

        let badge = '';
        if (fx.any_cold_start) {{
            badge = `<div class="confidence-badge low">Low confidence - team(s) new to our data</div>`;
        }} else if (fx.any_warm_start) {{
            badge = `<div class="confidence-badge estimated">Estimated - early-season form</div>`;
        }}

        let resultSection = '';
        if (hasResultModel) {{
            const homePct = Math.max(0, Math.round((fx.home_win_prob || 0) * 100));
            const drawPct = Math.max(0, Math.round((fx.draw_prob || 0) * 100));
            const awayPct = Math.max(0, 100 - homePct - drawPct);
            resultSection = `
                <div class="betting-xg">Expected goals: <strong>${{num(fx.expected_home_goals, 2)}} - ${{num(fx.expected_away_goals, 2)}}</strong></div>
                <div class="result-bar">
                    <span class="rb-home" style="width:${{homePct}}%">${{homePct >= 12 ? homePct + '%' : ''}}</span>
                    <span class="rb-draw" style="width:${{drawPct}}%">${{drawPct >= 12 ? drawPct + '%' : ''}}</span>
                    <span class="rb-away" style="width:${{awayPct}}%">${{awayPct >= 12 ? awayPct + '%' : ''}}</span>
                </div>
                <div class="betting-legend"><span>Home win</span><span>Draw</span><span>Away win</span></div>
                <div class="betting-market-row"><span class="betting-market-label">BTTS</span><span>${{pct(fx.btts_yes_prob)}}</span></div>
                <div class="betting-market-row"><span class="betting-market-label">Over/Under 2.5</span><span>${{pct(fx.over_2_5_prob)}} / ${{pct(fx.under_2_5_prob)}}</span></div>
            `;
        }}

        return `
            <div class="betting-card">
                <div class="betting-card-head">
                    <span class="betting-teams">${{fx.home_team}} vs ${{fx.away_team}}</span>
                    <span class="betting-date">${{fx.match_date_display || formatDate(fx.match_date)}}</span>
                </div>
                ${{badge}}
                ${{resultSection}}
                <div class="betting-markets">
                    <div class="betting-market-row"><span class="betting-market-label">Corners</span><span>${{num(fx.predicted_home_corners)}} - ${{num(fx.predicted_away_corners)}} (${{num(fx.predicted_total_corners)}})</span></div>
                    <div class="betting-market-row"><span class="betting-market-label">Yellow cards</span><span>${{num(fx.predicted_home_yellows)}} - ${{num(fx.predicted_away_yellows)}}</span></div>
                    <div class="betting-market-row"><span class="betting-market-label">Fouls</span><span>${{num(fx.predicted_home_fouls)}} - ${{num(fx.predicted_away_fouls)}} (${{num(fx.predicted_total_fouls)}})</span></div>
                    <div class="betting-market-row"><span class="betting-market-label">Shots</span><span>${{num(fx.predicted_home_shots)}} - ${{num(fx.predicted_away_shots)}}</span></div>
                    <div class="betting-market-row"><span class="betting-market-label">Shots on target</span><span>${{num(fx.predicted_home_sot)}} - ${{num(fx.predicted_away_sot)}}</span></div>
                </div>
            </div>
        `;
    }}

    function renderBettingView() {{
        renderSubhead();
        const contentEl = document.getElementById('bettingContent');
        const leagueData = bettingData ? bettingData[currentLeague] : null;

        if (!leagueData || !leagueData.fixtures || !leagueData.fixtures.length) {{
            const available = bettingData ? Object.keys(bettingData) : [];
            const availText = available.length ? ` Currently available for: ${{available.join(', ')}}.` : '';
            contentEl.innerHTML = `<p class="muted">No betting predictions available for ${{currentLeague}}.${{availText}}</p>`;
            return;
        }}

        contentEl.innerHTML = `
            <div class="betting-note">${{leagueData.window_label}} predictions from ${{leagueData.source_label}}${{leagueData.source === 'own_model' ? ' - a lighter model than The Corner Kick\\'s EPL one, trained on a single in-progress season' : ', a separate modeling project, not derived from the FBref data elsewhere on this page'}}. Not betting advice.</div>
            <div class="betting-grid">${{leagueData.fixtures.map(renderBettingCard).join('')}}</div>
        `;
    }}

    // ---- URL deep-linking ----

    function applyUrlParams() {{
        const params = new URLSearchParams(window.location.search);
        const leagueParam = params.get('league');
        const playerParam = params.get('player');
        const viewParam = params.get('view');

        if (leagueParam && leagues.includes(leagueParam)) {{
            currentLeague = leagueParam;
        }}
        if (viewParam && ['fixtures', 'players', 'teams', 'table', 'betting'].includes(viewParam)) {{
            currentView = viewParam;
        }}
        if (playerParam) {{
            const match = players.find(p => p.player_id === playerParam && (!leagueParam || p.league === leagueParam));
            if (match) {{
                pinnedPlayerId = match.player_id;
                currentLeague = match.league;
                currentView = 'players';
                document.getElementById('pinnedLabel').textContent = `Showing: ${{match.player_name}} (${{match.team}})`;
                document.getElementById('pinnedChip').classList.add('visible');
            }}
        }}
        return currentLeague !== null;
    }}

    // ---- init ----

    const hasLeagueFromUrl = applyUrlParams();
    renderLeaguePicker();
    document.getElementById('changeLeagueBtn').addEventListener('click', showLeaguePicker);
    setupViewToggle();
    setupStatModeToggle();
    renderPlayerTableHead();
    renderTeamTableHead();
    document.getElementById('fixturesView').style.display = currentView === 'fixtures' ? 'block' : 'none';
    document.getElementById('playerView').style.display = currentView === 'players' ? 'block' : 'none';
    document.getElementById('teamView').style.display = currentView === 'teams' ? 'block' : 'none';
    document.getElementById('leagueTableView').style.display = currentView === 'table' ? 'block' : 'none';
    document.getElementById('bettingView').style.display = currentView === 'betting' ? 'block' : 'none';
    document.getElementById('statModeToggle').style.display = currentView === 'players' ? 'flex' : 'none';

    if (hasLeagueFromUrl) {{
        enterLeague(currentLeague);
    }} else {{
        showLeaguePicker();
    }}
</script>

</body>
</html>
"""
    return html


def main():
    players, team_rows, fixture_payloads, league_table_rows, betting_data = load_all_data()

    if not players and not fixture_payloads:
        print("No data found - nothing to build.")
        return

    html = generate_html(players, team_rows, fixture_payloads, league_table_rows, betting_data)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    total_fixtures = sum(len(p["fixtures"]) for p in fixture_payloads)
    if betting_data:
        counts = ", ".join(f"{lg}: {len(d['fixtures'])}" for lg, d in betting_data.items())
        betting_note = f", betting predictions ({counts})"
    else:
        betting_note = ", no betting data"
    print(f"Wrote {OUTPUT_FILE}: {len(players)} players, {len(team_rows)} team rows, "
          f"{len(fixture_payloads)} leagues with fixtures ({total_fixtures} fixtures total){betting_note}.")


if __name__ == "__main__":
    main()
