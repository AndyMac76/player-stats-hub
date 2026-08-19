"""
train_betting_models.py

Trains simple linear-regression models predicting each team's next-match
corners, yellow cards, fouls, shots, and shots-on-target - same idea as
The Corner Kick's EPL models (predict_corners.py etc.), adapted for a
single in-progress season. Source data is team_match_stats (FBref's "Team
Stats" match-report section - see fbref_common's
parse_team_match_stats()), not a separate provider, so this works for any
league Player Stats Hub tracks once it has enough played matches. EPL
doesn't need this - The Corner Kick already covers it from a much richer
7-season history via football-data.co.uk.

Differences from The Corner Kick's approach, both deliberate given a
single ~280-match season instead of 7 seasons:
  - One OVERALL rolling-{ROLLING_WINDOW} form per team per stat, not
    split into separate home-venue/away-venue averages - venue-splitting
    this thin a sample (roughly half as many matches per venue) would be
    noisy.
  - No league-position/goal-difference features (team_match_stats doesn't
    track goals - that lives in fixtures.goals_for/against instead, and
    wiring in a second table's history isn't worth the complexity for
    what would only be a marginal feature).
  - No train/test split by season (only one exists) - holds out the most
    recently-played TEST_HOLDOUT_FRACTION of matches chronologically
    instead, training on the rest.
  - No cross-season warm-start needed - the current MLS season is already
    well underway (every team already has several matches of real
    current-season form), unlike EPL's fresh 2026/27 season when The
    Corner Kick's warm-start logic was built. A simple league-wide-average
    fallback covers the rare case of a team with no history at all yet.

Usage:
    python train_betting_models.py               # MLS by default
    python train_betting_models.py --league SPFL  # any league with enough team_match_stats rows
"""

import argparse
import json
import sqlite3
from collections import defaultdict, deque

import pandas as pd
import joblib
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error

import config

ROLLING_WINDOW = 5
TEST_HOLDOUT_FRACTION = 0.2

MARKETS = {
    "corners": {
        "target_col": "corners",
        "features": ["home_corners_for_avg", "home_corners_against_avg", "home_shots_for_avg",
                     "away_corners_for_avg", "away_corners_against_avg", "away_shots_for_avg"],
    },
    "cards": {
        "target_col": "cards_yellow",
        "features": ["home_cards_avg", "home_fouls_avg", "away_cards_avg", "away_fouls_avg"],
    },
    "fouls": {
        "target_col": "fouls",
        "features": ["home_fouls_avg", "home_cards_avg", "away_fouls_avg", "away_cards_avg"],
    },
    "shots": {
        "target_col": "shots_total",
        "features": ["home_shots_for_avg", "home_sot_for_avg", "away_shots_for_avg", "away_sot_for_avg"],
    },
    "sot": {
        "target_col": "shots_on_target",
        "features": ["home_sot_for_avg", "home_shots_for_avg", "away_sot_for_avg", "away_shots_for_avg"],
    },
}


def load_played_matches(conn, league):
    rows = conn.execute("""
        SELECT match_id, match_date, team, opponent, is_home,
               corners, cards_yellow, fouls, shots_total, shots_on_target
        FROM team_match_stats
        WHERE league = ?
        ORDER BY match_date ASC, match_id ASC
    """, (league,)).fetchall()
    cols = ["match_id", "match_date", "team", "opponent", "is_home",
            "corners", "cards_yellow", "fouls", "shots_total", "shots_on_target"]
    return pd.DataFrame(rows, columns=cols)


def build_training_rows(df):
    """One row per match (not per team-perspective) - each match's
    home/away teams' PRE-match rolling form, paired with the actual
    result. Matches are processed in chronological order so no row ever
    sees a stat from a match that hasn't happened yet - the same
    no-lookahead discipline The Corner Kick's training scripts use."""
    corners_for = defaultdict(lambda: deque(maxlen=ROLLING_WINDOW))
    corners_against = defaultdict(lambda: deque(maxlen=ROLLING_WINDOW))
    cards_for = defaultdict(lambda: deque(maxlen=ROLLING_WINDOW))
    fouls_for = defaultdict(lambda: deque(maxlen=ROLLING_WINDOW))
    shots_for = defaultdict(lambda: deque(maxlen=ROLLING_WINDOW))
    sot_for = defaultdict(lambda: deque(maxlen=ROLLING_WINDOW))

    league_totals = defaultdict(list)  # stat_name -> all values seen so far, for early-match fallback

    def avg_or_fallback(rolling_dict, team, stat_name):
        values = rolling_dict[team]
        if values:
            return round(sum(values) / len(values), 2)
        pool = league_totals[stat_name]
        return round(sum(pool) / len(pool), 2) if pool else 0.0

    rows = []
    for match_id, group in df.groupby("match_id", sort=False):
        if len(group) != 2:
            continue
        home = group[group["is_home"] == 1].iloc[0]
        away = group[group["is_home"] == 0].iloc[0]

        rows.append({
            "match_id": match_id,
            "match_date": home["match_date"],
            "home_team": home["team"], "away_team": away["team"],

            "home_corners_for_avg": avg_or_fallback(corners_for, home["team"], "corners"),
            "home_corners_against_avg": avg_or_fallback(corners_against, home["team"], "corners"),
            "away_corners_for_avg": avg_or_fallback(corners_for, away["team"], "corners"),
            "away_corners_against_avg": avg_or_fallback(corners_against, away["team"], "corners"),

            "home_cards_avg": avg_or_fallback(cards_for, home["team"], "cards"),
            "away_cards_avg": avg_or_fallback(cards_for, away["team"], "cards"),

            "home_fouls_avg": avg_or_fallback(fouls_for, home["team"], "fouls"),
            "away_fouls_avg": avg_or_fallback(fouls_for, away["team"], "fouls"),

            "home_shots_for_avg": avg_or_fallback(shots_for, home["team"], "shots"),
            "away_shots_for_avg": avg_or_fallback(shots_for, away["team"], "shots"),

            "home_sot_for_avg": avg_or_fallback(sot_for, home["team"], "sot"),
            "away_sot_for_avg": avg_or_fallback(sot_for, away["team"], "sot"),

            "corners": home["corners"], "corners_away": away["corners"],
            "cards_yellow": home["cards_yellow"], "cards_yellow_away": away["cards_yellow"],
            "fouls": home["fouls"], "fouls_away": away["fouls"],
            "shots_total": home["shots_total"], "shots_total_away": away["shots_total"],
            "shots_on_target": home["shots_on_target"], "shots_on_target_away": away["shots_on_target"],
        })

        for side, opp_side in ((home, away), (away, home)):
            corners_for[side["team"]].append(side["corners"])
            corners_against[side["team"]].append(opp_side["corners"])
            cards_for[side["team"]].append(side["cards_yellow"])
            fouls_for[side["team"]].append(side["fouls"])
            shots_for[side["team"]].append(side["shots_total"])
            sot_for[side["team"]].append(side["shots_on_target"])
            league_totals["corners"].append(side["corners"])
            league_totals["cards"].append(side["cards_yellow"])
            league_totals["fouls"].append(side["fouls"])
            league_totals["shots"].append(side["shots_total"])
            league_totals["sot"].append(side["shots_on_target"])

    return pd.DataFrame(rows)


def train_market(training_df, market_key, market_cfg, league_key):
    features = market_cfg["features"]
    target_col = market_cfg["target_col"]

    n = len(training_df)
    split = int(n * (1 - TEST_HOLDOUT_FRACTION))
    train_df = training_df.iloc[:split]
    test_df = training_df.iloc[split:]

    X_train = train_df[features]
    X_test = test_df[features]

    away_target_col = f"{target_col}_away" if f"{target_col}_away" in training_df.columns else target_col

    home_model = LinearRegression().fit(X_train, train_df[target_col])
    away_model = LinearRegression().fit(X_train, train_df[away_target_col])

    home_pred = home_model.predict(X_test)
    away_pred = away_model.predict(X_test)
    home_mae = mean_absolute_error(test_df[target_col], home_pred)
    away_mae = mean_absolute_error(test_df[away_target_col], away_pred)

    # Every market's feature list is laid out symmetrically - all home
    # features first, then the same features for away - so the away
    # side's own rolling "for" average always sits at the midpoint,
    # regardless of how many features that particular market has.
    home_baseline_col = features[0]
    away_baseline_col = features[len(features) // 2]
    home_baseline_mae = mean_absolute_error(test_df[target_col], test_df[home_baseline_col])
    away_baseline_mae = mean_absolute_error(test_df[away_target_col], test_df[away_baseline_col])

    print(f"  [{market_key}] train={len(train_df)} test={len(test_df)} | "
          f"home MAE {home_mae:.2f} (baseline {home_baseline_mae:.2f}) | "
          f"away MAE {away_mae:.2f} (baseline {away_baseline_mae:.2f})")

    joblib.dump(home_model, f"{league_key.lower()}_{market_key}_home_model.joblib")
    joblib.dump(away_model, f"{league_key.lower()}_{market_key}_away_model.joblib")


def train_league(league):
    conn = sqlite3.connect(config.DB_PATH)
    df = load_played_matches(conn, league)
    conn.close()

    match_count = df["match_id"].nunique()
    print(f"[{league}] {match_count} played matches with team stats available.")
    if match_count < 30:
        print(f"[{league}] Not enough data to train on yet (need at least ~30 matches) - skipping for now.")
        return

    training_df = build_training_rows(df)
    print(f"[{league}] Built {len(training_df)} training rows.\n")

    for market_key, market_cfg in MARKETS.items():
        train_market(training_df, market_key, market_cfg, league)

    with open(f"{league.lower()}_betting_features.json", "w") as f:
        json.dump({k: v["features"] for k, v in MARKETS.items()}, f, indent=2)

    print(f"[{league}] Done. Models saved as {league.lower()}_<market>_<home|away>_model.joblib\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--league", choices=list(config.LEAGUES.keys()), default=None,
        help="Train only this league instead of every active league except EPL "
             "(EPL already has its own richer model via The Corner Kick).",
    )
    args = parser.parse_args()

    if args.league:
        targets = [args.league]
    else:
        targets = [lg for lg in config.active_leagues() if lg != "EPL"]

    for league in targets:
        try:
            train_league(league)
        except Exception as e:
            import traceback
            print(f"[{league}] ERROR training betting models: {e}")
            traceback.print_exc()


if __name__ == "__main__":
    main()
