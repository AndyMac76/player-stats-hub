"""
validate_mls_predictions.py

Standalone backtest for mls_predictions.py, run BEFORE wiring anything
into the live weekly pipeline - same "prove it before it touches
production" approach as validate_aggregate_delta_approach.py earlier this
project.

MLS has one in-progress season (no prior completed season to hold out the
way The Corner Kick does for EPL), so this uses a CHRONOLOGICAL 80/20
split instead: train the per-market regressions on the first 80% of the
season's played matches, evaluate everything - regressions AND the
Poisson goals/result/BTTS/over-under model - against the most recent 20%,
using each test match's own pre-match form snapshot (already lookahead-
free by construction - see mls_predictions.FormTracker).

Reports:
  - Each of the 5 regression markets: model MAE vs a naive baseline
    (just predicting the team's own rolling average, no regression) -
    the model is only worth using if it clearly beats this.
  - The goals model: MAE on expected goals vs actual, plus hit rates for
    match result / BTTS / over-2.5, each compared against a naive
    baseline (result: always guess "Home Win"; BTTS/over-2.5: always
    guess whichever was more common in the training data).

Usage:
    python validate_mls_predictions.py
"""

import sqlite3

import config
import mls_predictions as mp

TEST_FRACTION = 0.2


def mae(pairs):
    return sum(abs(a - b) for a, b in pairs) / len(pairs) if pairs else float("nan")


def main():
    conn = sqlite3.connect(config.DB_PATH)
    rows = mp.fetch_match_rows(conn, mp.LEAGUE)
    conn.close()

    print(f"[{mp.LEAGUE}] {len(rows)} played matches with full team_match_stats coverage.")
    if len(rows) < 40:
        print("Not enough played matches yet for a meaningful backtest - try again once more of the season is in.")
        return

    dataset, _tracker = mp.build_dataset(rows)
    split_idx = int(len(dataset) * (1 - TEST_FRACTION))
    train_rows, test_rows = dataset[:split_idx], dataset[split_idx:]
    print(f"Chronological split: {len(train_rows)} train matches (up to {train_rows[-1]['match_date']}), "
          f"{len(test_rows)} test matches ({test_rows[0]['match_date']} to {test_rows[-1]['match_date']}).\n")

    # ---- per-market regressions ----
    print(f"{'Market':<16} {'Model MAE':>10} {'Baseline MAE':>13}   Verdict")
    print("-" * 60)
    for market in mp.MARKETS:
        home_model, away_model = mp.train_market_models(train_rows, market)

        model_pairs, baseline_pairs = [], []
        for row in test_rows:
            hf, af = row["home_form"][market], row["away_form"][market]
            actual_home, actual_away = row["actual"][market]

            pred_home, pred_away = mp.predict_market(home_model, away_model, row["home_form"], row["away_form"], market)
            model_pairs.append((pred_home, actual_home))
            model_pairs.append((pred_away, actual_away))

            baseline_pairs.append((hf["home_for"], actual_home))
            baseline_pairs.append((af["away_for"], actual_away))

        model_mae, baseline_mae = mae(model_pairs), mae(baseline_pairs)
        verdict = "beats baseline" if model_mae < baseline_mae else "NOT better than baseline"
        print(f"{mp.MARKET_LABEL[market]:<16} {model_mae:>10.3f} {baseline_mae:>13.3f}   {verdict}")

    # ---- goals / result / BTTS / over-under ----
    rho = mp.fit_rho(train_rows)
    print(f"\nFitted Dixon-Coles rho: {rho:+.3f} (0 = no low-score correlation adjustment)")

    goal_pairs = []
    correct_result = correct_result_baseline = 0
    correct_btts = correct_btts_baseline = 0
    correct_ou = correct_ou_baseline = 0
    brier_result_model = brier_result_base = 0.0
    brier_btts_model = brier_btts_base = 0.0
    brier_ou_model = brier_ou_base = 0.0

    n_train = len(train_rows)
    train_home_rate = sum(1 for r in train_rows if r["actual"]["goals"][0] > r["actual"]["goals"][1]) / n_train
    train_draw_rate = sum(1 for r in train_rows if r["actual"]["goals"][0] == r["actual"]["goals"][1]) / n_train
    train_away_rate = 1 - train_home_rate - train_draw_rate
    train_btts_rate = sum(1 for r in train_rows if r["actual"]["goals"][0] >= 1 and r["actual"]["goals"][1] >= 1) / n_train
    train_over_rate = sum(1 for r in train_rows if sum(r["actual"]["goals"]) > 2.5) / n_train
    baseline_btts = train_btts_rate >= 0.5
    baseline_ou = train_over_rate >= 0.5

    for row in test_rows:
        home_xg, away_xg = mp.predict_goals(row["home_form"], row["away_form"])
        actual_home, actual_away = row["actual"]["goals"]
        goal_pairs.append((home_xg, actual_home))
        goal_pairs.append((away_xg, actual_away))

        scorelines = mp.scoreline_probabilities(home_xg, away_xg, rho=rho)
        outcomes = mp.outcome_probabilities(scorelines)
        predicted_result = max(("home_win", "draw", "away_win"), key=lambda k: outcomes[k])
        actual_result = "home_win" if actual_home > actual_away else ("away_win" if actual_away > actual_home else "draw")
        correct_result += predicted_result == actual_result
        correct_result_baseline += actual_result == "home_win"

        actual_vec = {"home_win": float(actual_home > actual_away), "draw": float(actual_home == actual_away),
                      "away_win": float(actual_away > actual_home)}
        base_vec = {"home_win": train_home_rate, "draw": train_draw_rate, "away_win": train_away_rate}
        brier_result_model += sum((outcomes[k] - actual_vec[k]) ** 2 for k in actual_vec)
        brier_result_base += sum((base_vec[k] - actual_vec[k]) ** 2 for k in actual_vec)

        actual_btts = actual_home >= 1 and actual_away >= 1
        correct_btts += (outcomes["btts_yes"] >= 0.5) == actual_btts
        correct_btts_baseline += baseline_btts == actual_btts
        brier_btts_model += (outcomes["btts_yes"] - float(actual_btts)) ** 2
        brier_btts_base += (train_btts_rate - float(actual_btts)) ** 2

        actual_over = (actual_home + actual_away) > 2.5
        correct_ou += (outcomes["over_2_5"] >= 0.5) == actual_over
        correct_ou_baseline += baseline_ou == actual_over
        brier_ou_model += (outcomes["over_2_5"] - float(actual_over)) ** 2
        brier_ou_base += (train_over_rate - float(actual_over)) ** 2

    n = len(test_rows)
    print(f"\n{'Market':<16} {'Model MAE':>10}")
    print("-" * 30)
    print(f"{'Goals (xG)':<16} {mae(goal_pairs):>10.3f}")

    print(f"\n{'Market':<20} {'Model hit-rate':>15} {'Baseline hit-rate':>18}   Verdict")
    print("-" * 75)
    for label, model_hits, baseline_hits in [
        ("Result (H/D/A)", correct_result, correct_result_baseline),
        ("BTTS", correct_btts, correct_btts_baseline),
        ("Over/Under 2.5", correct_ou, correct_ou_baseline),
    ]:
        model_rate, baseline_rate = model_hits / n * 100, baseline_hits / n * 100
        verdict = "beats baseline" if model_rate > baseline_rate else "NOT better than baseline"
        print(f"{label:<20} {model_rate:>14.1f}% {baseline_rate:>17.1f}%   {verdict}")

    print("\nBrier scores (lower is better - hit-rate alone can be misleading for probability forecasts):")
    print(f"{'Market':<20} {'Model Brier':>12} {'Baseline Brier':>15}   Verdict")
    print("-" * 62)
    for label, model_b, base_b in [
        ("Result (3-way)", brier_result_model, brier_result_base),
        ("BTTS", brier_btts_model, brier_btts_base),
        ("Over/Under 2.5", brier_ou_model, brier_ou_base),
    ]:
        model_score, base_score = model_b / n, base_b / n
        verdict = "beats baseline" if model_score < base_score else "NOT better than baseline"
        print(f"{label:<20} {model_score:>12.4f} {base_score:>15.4f}   {verdict}")


if __name__ == "__main__":
    main()
