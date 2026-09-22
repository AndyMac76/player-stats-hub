"""
predict_mls_gameweek.py

Locks in predictions for MLS's upcoming gameweek, for the 5 markets that
actually beat a naive baseline in validate_mls_predictions.py: corners,
yellow cards, fouls, shots, shots on target. Goals/result/BTTS/over-under
are deliberately NOT predicted here - that side of the model didn't beat
baseline in backtesting, even after trying a Dixon-Coles low-score
correction (the fit came back as no adjustment at all, a genuine null
result rather than a bug - see mls_predictions.fit_rho), so it isn't
shipped until it does.

Trains fresh on every played MLS match each run (cheap at this data size,
and means predictions always use the fullest history available), then
predicts the upcoming gameweek's fixtures. Predictions are locked in with
INSERT OR IGNORE, keyed by match_id - once made, a prediction is never
silently revised as more recent form comes in, so review_mls_predictions.py
is always grading what was genuinely knowable in advance, not a moving
target.

Usage:
    python predict_mls_gameweek.py
"""

import sqlite3
from datetime import datetime, timezone

import config
import fbref_scrape_common as common
import mls_predictions as mp

GAMEWEEK_WINDOW_DAYS = 4


def create_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mls_predictions (
            match_id TEXT PRIMARY KEY,
            league TEXT,
            match_date TEXT,
            home_team TEXT,
            away_team TEXT,
            predicted_at TEXT,
            predicted_home_corners REAL, predicted_away_corners REAL,
            predicted_home_cards_yellow REAL, predicted_away_cards_yellow REAL,
            predicted_home_fouls REAL, predicted_away_fouls REAL,
            predicted_home_shots_total REAL, predicted_away_shots_total REAL,
            predicted_home_shots_on_target REAL, predicted_away_shots_on_target REAL
        )
    """)
    conn.commit()


def find_unpredicted_upcoming_matches(conn):
    match_ids, window_start, window_end = common.get_current_gameweek_match_ids(
        conn, mp.LEAGUE, GAMEWEEK_WINDOW_DAYS
    )
    if not match_ids:
        return [], window_start, window_end

    upcoming = []
    for match_id in match_ids:
        row = conn.execute(
            "SELECT team, opponent, match_date, is_played FROM fixtures "
            "WHERE league = ? AND match_id = ? AND is_home = 1",
            (mp.LEAGUE, match_id),
        ).fetchone()
        if row and row[3] == 0:
            upcoming.append({"match_id": match_id, "home_team": row[0], "away_team": row[1], "match_date": row[2]})

    if not upcoming:
        return [], window_start, window_end

    placeholders = ",".join("?" for _ in upcoming)
    already = {r[0] for r in conn.execute(
        f"SELECT match_id FROM mls_predictions WHERE match_id IN ({placeholders})",
        [m["match_id"] for m in upcoming],
    ).fetchall()}
    return [m for m in upcoming if m["match_id"] not in already], window_start, window_end


def main():
    conn = sqlite3.connect(config.DB_PATH)
    create_tables(conn)

    to_predict, window_start, window_end = find_unpredicted_upcoming_matches(conn)
    if not to_predict:
        print(f"[{mp.LEAGUE}] No new upcoming fixtures to predict "
              f"(window {window_start} to {window_end})." if window_start else
              f"[{mp.LEAGUE}] No upcoming fixtures found - nothing to predict.")
        conn.close()
        return

    rows = mp.fetch_match_rows(conn, mp.LEAGUE)
    if not rows:
        print(f"[{mp.LEAGUE}] No played matches with stats yet - can't train.")
        conn.close()
        return

    dataset, tracker = mp.build_dataset(rows)
    models = mp.train_all_market_models(dataset)

    predicted_at = datetime.now(timezone.utc).isoformat()
    saved = 0
    for m in to_predict:
        home_form = tracker.snapshot(m["home_team"])
        away_form = tracker.snapshot(m["away_team"])
        pred = {}
        for market in mp.MARKETS:
            home_model, away_model = models[market]
            pred["home_" + market], pred["away_" + market] = mp.predict_market(
                home_model, away_model, home_form, away_form, market
            )

        conn.execute("""
            INSERT OR IGNORE INTO mls_predictions
            (match_id, league, match_date, home_team, away_team, predicted_at,
             predicted_home_corners, predicted_away_corners,
             predicted_home_cards_yellow, predicted_away_cards_yellow,
             predicted_home_fouls, predicted_away_fouls,
             predicted_home_shots_total, predicted_away_shots_total,
             predicted_home_shots_on_target, predicted_away_shots_on_target)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            m["match_id"], mp.LEAGUE, m["match_date"], m["home_team"], m["away_team"], predicted_at,
            pred["home_corners"], pred["away_corners"],
            pred["home_cards_yellow"], pred["away_cards_yellow"],
            pred["home_fouls"], pred["away_fouls"],
            pred["home_shots_total"], pred["away_shots_total"],
            pred["home_shots_on_target"], pred["away_shots_on_target"],
        ))
        saved += 1
        print(f"  {m['home_team']} vs {m['away_team']} ({m['match_date']}): "
              f"corners {pred['home_corners']:.1f}-{pred['away_corners']:.1f}, "
              f"yellows {pred['home_cards_yellow']:.1f}-{pred['away_cards_yellow']:.1f}, "
              f"fouls {pred['home_fouls']:.1f}-{pred['away_fouls']:.1f}, "
              f"shots {pred['home_shots_total']:.1f}-{pred['away_shots_total']:.1f}, "
              f"SOT {pred['home_shots_on_target']:.1f}-{pred['away_shots_on_target']:.1f}")

    conn.commit()
    conn.close()
    print(f"[{mp.LEAGUE}] Locked in {saved} new prediction(s) for {window_start} to {window_end}.")


if __name__ == "__main__":
    main()
