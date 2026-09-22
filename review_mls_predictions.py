"""
review_mls_predictions.py

Grades every locked-in MLS prediction (see predict_mls_gameweek.py) whose
match has since been played, against the real team_match_stats result -
the "check for accuracy" half of the loop. Run this AFTER the weekly
scrape has pulled in that gameweek's actual results, so there's something
real to grade against.

Never re-grades an already-reviewed match (mls_prediction_reviews is
keyed by match_id), so this is safe to run every week even if nothing
new has been played yet - it'll just report 0 newly reviewed and print
the running all-time accuracy from whatever's already in the table.

Usage:
    python review_mls_predictions.py
"""

import sqlite3
from datetime import datetime, timezone

import config
import mls_predictions as mp

MARKET_COLS = {
    "corners": "corners",
    "cards_yellow": "cards_yellow",
    "fouls": "fouls",
    "shots_total": "shots_total",
    "shots_on_target": "shots_on_target",
}


def create_tables(conn):
    cols_sql = ", ".join(
        f"actual_home_{m} REAL, actual_away_{m} REAL, error_{m} REAL" for m in mp.MARKETS
    )
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS mls_prediction_reviews (
            match_id TEXT PRIMARY KEY,
            reviewed_at TEXT,
            {cols_sql}
        )
    """)
    conn.commit()


def find_reviewable_matches(conn):
    return conn.execute("""
        SELECT p.match_id, p.home_team, p.away_team,
               p.predicted_home_corners, p.predicted_away_corners,
               p.predicted_home_cards_yellow, p.predicted_away_cards_yellow,
               p.predicted_home_fouls, p.predicted_away_fouls,
               p.predicted_home_shots_total, p.predicted_away_shots_total,
               p.predicted_home_shots_on_target, p.predicted_away_shots_on_target,
               th.corners, ta.corners,
               th.cards_yellow, ta.cards_yellow,
               th.fouls, ta.fouls,
               th.shots_total, ta.shots_total,
               th.shots_on_target, ta.shots_on_target
        FROM mls_predictions p
        JOIN fixtures f ON f.match_id = p.match_id AND f.is_home = 1 AND f.league = p.league
        JOIN team_match_stats th ON th.match_id = p.match_id AND th.is_home = 1
        JOIN team_match_stats ta ON ta.match_id = p.match_id AND ta.is_home = 0
        LEFT JOIN mls_prediction_reviews r ON r.match_id = p.match_id
        WHERE f.is_played = 1 AND r.match_id IS NULL
    """).fetchall()


def main():
    conn = sqlite3.connect(config.DB_PATH)
    create_tables(conn)

    reviewable = find_reviewable_matches(conn)
    reviewed_at = datetime.now(timezone.utc).isoformat()
    new_errors = {m: [] for m in mp.MARKETS}

    for row in reviewable:
        (match_id, home_team, away_team,
         ph_corners, pa_corners, ph_cards, pa_cards, ph_fouls, pa_fouls,
         ph_shots, pa_shots, ph_sot, pa_sot,
         ah_corners, aa_corners, ah_cards, aa_cards, ah_fouls, aa_fouls,
         ah_shots, aa_shots, ah_sot, aa_sot) = row

        actuals = {
            "corners": (ah_corners, aa_corners), "cards_yellow": (ah_cards, aa_cards),
            "fouls": (ah_fouls, aa_fouls), "shots_total": (ah_shots, aa_shots),
            "shots_on_target": (ah_sot, aa_sot),
        }
        predicted = {
            "corners": (ph_corners, pa_corners), "cards_yellow": (ph_cards, pa_cards),
            "fouls": (ph_fouls, pa_fouls), "shots_total": (ph_shots, pa_shots),
            "shots_on_target": (ph_sot, pa_sot),
        }

        errors = {}
        for market in mp.MARKETS:
            actual_home, actual_away = actuals[market]
            pred_home, pred_away = predicted[market]
            error = (abs(pred_home - actual_home) + abs(pred_away - actual_away)) / 2
            errors[market] = error
            new_errors[market].append(error)

        cols = ["match_id", "reviewed_at"]
        vals = [match_id, reviewed_at]
        for market in mp.MARKETS:
            actual_home, actual_away = actuals[market]
            cols += [f"actual_home_{market}", f"actual_away_{market}", f"error_{market}"]
            vals += [actual_home, actual_away, errors[market]]

        placeholders = ",".join("?" for _ in vals)
        conn.execute(f"INSERT OR REPLACE INTO mls_prediction_reviews ({','.join(cols)}) VALUES ({placeholders})", vals)
        print(f"  Reviewed {home_team} vs {away_team}: " +
              ", ".join(f"{mp.MARKET_LABEL[m]} err {errors[m]:.2f}" for m in mp.MARKETS))

    conn.commit()

    n_new = len(reviewable)
    n_total = conn.execute("SELECT COUNT(*) FROM mls_prediction_reviews").fetchone()[0]

    print(f"\n[{mp.LEAGUE}] Reviewed {n_new} newly-played match(es) this run. "
          f"{n_total} match(es) reviewed all-time.")

    if n_total:
        print(f"\n{'Market':<18} {'This run MAE':>13} {'All-time MAE':>13}")
        print("-" * 46)
        for market in mp.MARKETS:
            all_time_avg = conn.execute(f"SELECT AVG(error_{market}) FROM mls_prediction_reviews").fetchone()[0]
            this_run = sum(new_errors[market]) / len(new_errors[market]) if new_errors[market] else None
            this_run_str = f"{this_run:.3f}" if this_run is not None else "-"
            print(f"{mp.MARKET_LABEL[market]:<18} {this_run_str:>13} {all_time_avg:>13.3f}")

    conn.close()


if __name__ == "__main__":
    main()
