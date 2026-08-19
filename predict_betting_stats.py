"""
predict_betting_stats.py

Generates predicted corners/cards/fouls/shots/shots-on-target for a
league's upcoming fixtures, using the models train_betting_models.py
trains. Each team's CURRENT form is today's real rolling-{ROLLING_WINDOW}
average - replays every played match in team_match_stats chronologically
and takes the state at the end, same logic train_betting_models.py uses
to build training rows, just run once to completion instead of yielding
a row per match.

A team with no match history at all yet falls back to the league-wide
average (flagged separately) rather than being skipped outright - should
be rare given MLS's season is already well underway, but kept as a safety
net rather than assuming it can't happen.

Usage:
    python predict_betting_stats.py               # MLS by default
    python predict_betting_stats.py --league SPFL
"""

import argparse
import sqlite3
from collections import defaultdict, deque

import joblib
import pandas as pd

import config
from train_betting_models import ROLLING_WINDOW, MARKETS, load_played_matches


def compute_current_form(df):
    """Final rolling state after replaying every played match in
    chronological order - each team's real current form, plus the
    league-wide average of each stat as a fallback for a team with zero
    history. Returns (team_form, league_avg)."""
    corners_for = defaultdict(lambda: deque(maxlen=ROLLING_WINDOW))
    corners_against = defaultdict(lambda: deque(maxlen=ROLLING_WINDOW))
    cards_for = defaultdict(lambda: deque(maxlen=ROLLING_WINDOW))
    fouls_for = defaultdict(lambda: deque(maxlen=ROLLING_WINDOW))
    shots_for = defaultdict(lambda: deque(maxlen=ROLLING_WINDOW))
    sot_for = defaultdict(lambda: deque(maxlen=ROLLING_WINDOW))

    for match_id, group in df.groupby("match_id", sort=False):
        if len(group) != 2:
            continue
        home = group[group["is_home"] == 1].iloc[0]
        away = group[group["is_home"] == 0].iloc[0]
        for side, opp_side in ((home, away), (away, home)):
            corners_for[side["team"]].append(side["corners"])
            corners_against[side["team"]].append(opp_side["corners"])
            cards_for[side["team"]].append(side["cards_yellow"])
            fouls_for[side["team"]].append(side["fouls"])
            shots_for[side["team"]].append(side["shots_total"])
            sot_for[side["team"]].append(side["shots_on_target"])

    def avg(d):
        return round(sum(d) / len(d), 2) if d else None

    teams = set(corners_for) | set(cards_for) | set(shots_for)
    team_form = {}
    for team in teams:
        team_form[team] = {
            "corners_for_avg": avg(corners_for[team]),
            "corners_against_avg": avg(corners_against[team]),
            "cards_avg": avg(cards_for[team]),
            "fouls_avg": avg(fouls_for[team]),
            "shots_for_avg": avg(shots_for[team]),
            "sot_for_avg": avg(sot_for[team]),
        }

    def league_avg(getter):
        values = [getter(v) for v in team_form.values() if getter(v) is not None]
        return round(sum(values) / len(values), 2) if values else None

    league_average = {
        "corners_for_avg": league_avg(lambda v: v["corners_for_avg"]),
        "corners_against_avg": league_avg(lambda v: v["corners_against_avg"]),
        "cards_avg": league_avg(lambda v: v["cards_avg"]),
        "fouls_avg": league_avg(lambda v: v["fouls_avg"]),
        "shots_for_avg": league_avg(lambda v: v["shots_for_avg"]),
        "sot_for_avg": league_avg(lambda v: v["sot_for_avg"]),
    }

    return team_form, league_average


def build_feature_row(home_team, away_team, team_form, league_avg, features):
    home = team_form.get(home_team, league_avg)
    away = team_form.get(away_team, league_avg)
    if home is None or away is None:
        return None

    values = {
        "home_corners_for_avg": home["corners_for_avg"], "home_corners_against_avg": home["corners_against_avg"],
        "away_corners_for_avg": away["corners_for_avg"], "away_corners_against_avg": away["corners_against_avg"],
        "home_cards_avg": home["cards_avg"], "away_cards_avg": away["cards_avg"],
        "home_fouls_avg": home["fouls_avg"], "away_fouls_avg": away["fouls_avg"],
        "home_shots_for_avg": home["shots_for_avg"], "away_shots_for_avg": away["shots_for_avg"],
        "home_sot_for_avg": home["sot_for_avg"], "away_sot_for_avg": away["sot_for_avg"],
    }
    if any(values.get(c) is None for c in features):
        return None
    return [values[c] for c in features]


def predict_league(league):
    conn = sqlite3.connect(config.DB_PATH)

    played_df = load_played_matches(conn, league)
    if played_df["match_id"].nunique() < 30:
        print(f"[{league}] Not enough played-match data yet - skipping for now.")
        conn.close()
        return

    team_form, league_avg = compute_current_form(played_df)

    models = {}
    for market_key in MARKETS:
        try:
            models[market_key] = {
                "home": joblib.load(f"{league.lower()}_{market_key}_home_model.joblib"),
                "away": joblib.load(f"{league.lower()}_{market_key}_away_model.joblib"),
            }
        except FileNotFoundError:
            print(f"[{league}] No trained model found for '{market_key}' - run train_betting_models.py first.")
            conn.close()
            return

    fixtures = conn.execute("""
        SELECT match_id, match_date, team, opponent
        FROM fixtures
        WHERE league = ? AND is_played = 0 AND is_home = 1
        ORDER BY match_date ASC
    """, (league,)).fetchall()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS team_stat_predictions (
            league TEXT,
            match_id TEXT,
            match_date TEXT,
            home_team TEXT,
            away_team TEXT,
            predicted_home_corners REAL, predicted_away_corners REAL,
            predicted_home_cards REAL, predicted_away_cards REAL,
            predicted_home_fouls REAL, predicted_away_fouls REAL,
            predicted_home_shots REAL, predicted_away_shots REAL,
            predicted_home_sot REAL, predicted_away_sot REAL,
            cold_start INTEGER,
            PRIMARY KEY (league, match_id)
        )
    """)
    conn.execute("DELETE FROM team_stat_predictions WHERE league = ?", (league,))

    results = []
    skipped = 0
    cold_started = set()

    for match_id, match_date, home_team, away_team in fixtures:
        row_result = {"match_id": match_id, "match_date": match_date, "home_team": home_team, "away_team": away_team}
        any_missing = False

        for market_key, market_cfg in MARKETS.items():
            row = build_feature_row(home_team, away_team, team_form, league_avg, market_cfg["features"])
            if row is None:
                any_missing = True
                break
            X = pd.DataFrame([row], columns=market_cfg["features"])
            home_pred = float(models[market_key]["home"].predict(X)[0])
            away_pred = float(models[market_key]["away"].predict(X)[0])
            row_result[f"predicted_home_{market_key}"] = round(home_pred, 2)
            row_result[f"predicted_away_{market_key}"] = round(away_pred, 2)

        if any_missing:
            skipped += 1
            continue

        if home_team not in team_form:
            cold_started.add(home_team)
        if away_team not in team_form:
            cold_started.add(away_team)
        row_result["cold_start"] = 1 if (home_team not in team_form or away_team not in team_form) else 0

        results.append((
            league, row_result["match_id"], row_result["match_date"], row_result["home_team"], row_result["away_team"],
            row_result.get("predicted_home_corners"), row_result.get("predicted_away_corners"),
            row_result.get("predicted_home_cards"), row_result.get("predicted_away_cards"),
            row_result.get("predicted_home_fouls"), row_result.get("predicted_away_fouls"),
            row_result.get("predicted_home_shots"), row_result.get("predicted_away_shots"),
            row_result.get("predicted_home_sot"), row_result.get("predicted_away_sot"),
            row_result["cold_start"],
        ))

    conn.executemany("""
        INSERT OR REPLACE INTO team_stat_predictions
        (league, match_id, match_date, home_team, away_team,
         predicted_home_corners, predicted_away_corners,
         predicted_home_cards, predicted_away_cards,
         predicted_home_fouls, predicted_away_fouls,
         predicted_home_shots, predicted_away_shots,
         predicted_home_sot, predicted_away_sot,
         cold_start)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, results)
    conn.commit()
    conn.close()

    if cold_started:
        print(f"[{league}] {len(cold_started)} team(s) using league-average fallback (no match history yet): {sorted(cold_started)}")

    print(f"[{league}] Predicted {len(results)} fixtures. Saved to 'team_stat_predictions' table.\n")
    for r in results[:10]:
        print(f"  {r[3]} vs {r[4]}  ->  corners {r[5]}-{r[6]} | cards {r[7]}-{r[8]} | "
              f"fouls {r[9]}-{r[10]} | shots {r[11]}-{r[12]} | SOT {r[13]}-{r[14]}")
    if skipped:
        print(f"\n[{league}] Skipped {skipped} fixtures (no data even with league-average fallback).")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--league", choices=list(config.LEAGUES.keys()), default=None,
        help="Predict only this league instead of every active league except EPL "
             "(EPL already has its own richer model via The Corner Kick).",
    )
    args = parser.parse_args()

    if args.league:
        targets = [args.league]
    else:
        targets = [lg for lg in config.active_leagues() if lg != "EPL"]

    for league in targets:
        try:
            predict_league(league)
        except Exception as e:
            import traceback
            print(f"[{league}] ERROR predicting betting stats: {e}")
            traceback.print_exc()


if __name__ == "__main__":
    main()
