"""
mls_predictions.py

Shared prediction library for MLS: goals/result/BTTS/over-under via a
Poisson attack-defense model, and corners/cards(yellow)/fouls/shots/
shots-on-target via simple per-market linear regression. Both are built
from rolling team form that only ever looks at matches BEFORE the one
being predicted - a team's "form" going into a match never includes that
match or any later one, in training data or in a live prediction.

This project's earlier in-house betting models (and The Corner Kick's EPL
goals model) computed attack/defense strength from a team's FULL season of
matches, future ones included - fine for a pure historical writeup, but a
real lookahead leak for a model meant to predict an upcoming match using
only what's known at that point. Fixed here by carrying running totals
forward match by match and always reading the pre-match snapshot.

MLS-only for now. Reads from `team_match_stats` (corners/cards/fouls/
shots/SOT, always sourced from the match-report page - unaffected by the
season-aggregate fast-path team-name bug fixed 2026-09-16) and `fixtures`
(goals, joined by match_id + is_home rather than team-name string, so it
can't be tripped up by that table's own separate, unrelated naming
inconsistency).

Usage: imported by validate_mls_predictions.py (backtest) and, once that
looks trustworthy, predict_mls_gameweek.py / review_mls_predictions.py
(live weekly loop).
"""

import math
import sqlite3
from collections import defaultdict

from sklearn.linear_model import LinearRegression

import config

LEAGUE = "MLS"

MARKETS = ["corners", "cards_yellow", "fouls", "shots_total", "shots_on_target"]
MARKET_LABEL = {
    "corners": "Corners", "cards_yellow": "Yellow cards", "fouls": "Fouls",
    "shots_total": "Shots", "shots_on_target": "Shots on target",
}


# ---------------------------------------------------------------------------
# Poisson goals model
# ---------------------------------------------------------------------------
def poisson_probability(expected, actual):
    return (expected ** actual) * math.exp(-expected) / math.factorial(actual)


def dixon_coles_tau(home_goals, away_goals, home_xg, away_xg, rho):
    """Low-score correlation correction (Dixon & Coles, 1997). Independent
    Poisson systematically under-predicts 0-0/1-1 draws and over-predicts
    1-0/0-1 - real matches aren't independent at low scores (a team
    leading 1-0 late plays differently than 0-0). Only touches those four
    cells; everything else is untouched (tau=1)."""
    if home_goals == 0 and away_goals == 0:
        return 1 - home_xg * away_xg * rho
    if home_goals == 0 and away_goals == 1:
        return 1 + home_xg * rho
    if home_goals == 1 and away_goals == 0:
        return 1 + away_xg * rho
    if home_goals == 1 and away_goals == 1:
        return 1 - rho
    return 1.0


def scoreline_probabilities(home_xg, away_xg, rho=0.0, max_goals=8):
    scorelines = []
    total = 0.0
    for hg in range(max_goals):
        for ag in range(max_goals):
            p = poisson_probability(home_xg, hg) * poisson_probability(away_xg, ag)
            p *= dixon_coles_tau(hg, ag, home_xg, away_xg, rho)
            scorelines.append([hg, ag, p])
            total += p
    if total > 0:
        for s in scorelines:
            s[2] /= total
    return [tuple(s) for s in scorelines]


def fit_rho(dataset_rows, rho_range=(-0.3, 0.3), step=0.005):
    """Grid-searches rho to maximize the Dixon-Coles-adjusted
    log-likelihood of `dataset_rows`' actual scorelines, given each row's
    already-computed pre-match expected goals. Only the tau term depends
    on rho (the plain Poisson terms don't), so that's all that needs
    maximizing. Fit on TRAINING rows only - this is a single global
    parameter, not something re-fit per match."""
    fixed = []
    for row in dataset_rows:
        lam, mu = predict_goals(row["home_form"], row["away_form"])
        x, y = row["actual"]["goals"]
        fixed.append((lam, mu, x, y))

    lo, hi = rho_range
    n_steps = int(round((hi - lo) / step)) + 1
    best_rho, best_ll = 0.0, float("-inf")
    for i in range(n_steps):
        rho = lo + i * step
        ll, valid = 0.0, True
        for lam, mu, x, y in fixed:
            tau = dixon_coles_tau(x, y, lam, mu, rho)
            if tau <= 0:
                valid = False
                break
            ll += math.log(tau)
        if valid and ll > best_ll:
            best_ll, best_rho = ll, rho
    return best_rho


def outcome_probabilities(scorelines):
    home_win = draw = away_win = btts_yes = over_2_5 = 0.0
    for hg, ag, p in scorelines:
        if hg > ag: home_win += p
        elif ag > hg: away_win += p
        else: draw += p
        if hg >= 1 and ag >= 1: btts_yes += p
        if hg + ag > 2.5: over_2_5 += p
    return {"home_win": home_win, "draw": draw, "away_win": away_win,
            "btts_yes": btts_yes, "over_2_5": over_2_5}


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def fetch_match_rows(conn, league=LEAGUE):
    """Every played match, oldest first. Joined by match_id + is_home (not
    team-name string) so it's immune to fixtures.team's own separate,
    pre-existing naming inconsistency (found 2026-09-16 while fixing the
    season-aggregate fast-path bug - unrelated to this model, but the same
    join done via team names would silently drop affected matches)."""
    rows = conn.execute("""
        SELECT f.match_id, f.match_date, f.team AS home_team, f.opponent AS away_team,
               f.goals_for AS home_goals, f.goals_against AS away_goals,
               th.corners AS home_corners, ta.corners AS away_corners,
               th.cards_yellow AS home_cards_yellow, ta.cards_yellow AS away_cards_yellow,
               th.fouls AS home_fouls, ta.fouls AS away_fouls,
               th.shots_total AS home_shots_total, ta.shots_total AS away_shots_total,
               th.shots_on_target AS home_shots_on_target, ta.shots_on_target AS away_shots_on_target
        FROM fixtures f
        JOIN team_match_stats th ON th.match_id = f.match_id AND th.is_home = 1
        JOIN team_match_stats ta ON ta.match_id = f.match_id AND ta.is_home = 0
        WHERE f.league = ? AND f.is_home = 1 AND f.is_played = 1
        ORDER BY f.match_date ASC
    """, (league,)).fetchall()
    cols = ["match_id", "match_date", "home_team", "away_team", "home_goals", "away_goals"]
    for m in MARKETS:
        cols += ["home_" + m, "away_" + m]
    return [dict(zip(cols, row)) for row in rows]


# ---------------------------------------------------------------------------
# Running "form as of this match" state - shared by the goals model and the
# per-market regressions, since they're the same underlying idea (a team's
# average output/concession, home and away, before this point in the
# season).
# ---------------------------------------------------------------------------
class FormTracker:
    """Call snapshot(team) to get a team's form BEFORE any match update()
    has been applied for that match, then update(row) after using it -
    that ordering is what keeps every prediction lookahead-free."""

    def __init__(self, stat_keys):
        self.stat_keys = stat_keys  # e.g. ["goals", "corners", ...]
        self.home_for = defaultdict(lambda: defaultdict(list))
        self.home_against = defaultdict(lambda: defaultdict(list))
        self.away_for = defaultdict(lambda: defaultdict(list))
        self.away_against = defaultdict(lambda: defaultdict(list))
        self.league_home_avg = defaultdict(list)
        self.league_away_avg = defaultdict(list)

    def _avg(self, series, fallback):
        return sum(series) / len(series) if series else fallback

    def snapshot(self, team):
        """{"goals": {"home_for":.., "home_against":.., "away_for":.., "away_against":..}, ...}
        Before any match has been seen at all, every average falls back to
        1.0 - a neutral placeholder that only ever applies to the first
        match or two of the whole dataset, before league_home_avg/
        league_away_avg have any data to average."""
        out = {}
        for key in self.stat_keys:
            lg_home = self._avg(self.league_home_avg[key], 1.0)
            lg_away = self._avg(self.league_away_avg[key], 1.0)
            out[key] = {
                "home_for": self._avg(self.home_for[team][key], lg_home),
                "home_against": self._avg(self.home_against[team][key], lg_away),
                "away_for": self._avg(self.away_for[team][key], lg_away),
                "away_against": self._avg(self.away_against[team][key], lg_home),
                "league_home_avg": lg_home,
                "league_away_avg": lg_away,
            }
        return out

    def update(self, home_team, away_team, values):
        """values: {"goals": (home_val, away_val), "corners": (...), ...}"""
        for key, (home_val, away_val) in values.items():
            self.home_for[home_team][key].append(home_val)
            self.home_against[home_team][key].append(away_val)
            self.away_for[away_team][key].append(away_val)
            self.away_against[away_team][key].append(home_val)
            self.league_home_avg[key].append(home_val)
            self.league_away_avg[key].append(away_val)


def build_dataset(rows):
    """Walks every match chronologically, capturing each team's pre-match
    form snapshot as the feature row, then updates the tracker with that
    match's actual values. Returns (dataset, tracker) - dataset is a list
    of dicts, one per match, with both the snapshot (for features) and the
    actual outcome (for labels); tracker is left holding the form snapshot
    as of AFTER the last match, i.e. current form for a live prediction -
    reusing it avoids walking the whole history a second time."""
    stat_keys = ["goals"] + MARKETS
    tracker = FormTracker(stat_keys)
    dataset = []

    for row in rows:
        home_snap = tracker.snapshot(row["home_team"])
        away_snap = tracker.snapshot(row["away_team"])

        dataset.append({
            "match_id": row["match_id"], "match_date": row["match_date"],
            "home_team": row["home_team"], "away_team": row["away_team"],
            "home_form": home_snap, "away_form": away_snap,
            "actual": {key: (row["home_" + key], row["away_" + key]) for key in stat_keys},
        })

        tracker.update(row["home_team"], row["away_team"],
                        {key: (row["home_" + key], row["away_" + key]) for key in stat_keys})

    return dataset, tracker


# ---------------------------------------------------------------------------
# Goals model: Poisson expected goals from attack/defense strength ratios,
# using each side's pre-match form snapshot.
# ---------------------------------------------------------------------------
def predict_goals(home_form, away_form):
    g = "goals"
    league_home = home_form[g]["league_home_avg"] or 1.0
    league_away = home_form[g]["league_away_avg"] or 1.0

    home_attack = home_form[g]["home_for"] / league_home if league_home else 1.0
    home_defence = home_form[g]["home_against"] / league_away if league_away else 1.0
    away_attack = away_form[g]["away_for"] / league_away if league_away else 1.0
    away_defence = away_form[g]["away_against"] / league_home if league_home else 1.0

    home_xg = max(0.05, league_home * home_attack * away_defence)
    away_xg = max(0.05, league_away * away_attack * home_defence)
    return home_xg, away_xg


# ---------------------------------------------------------------------------
# Per-market regressions: two features per side (that team's own for-rate,
# the opponent's allowed-rate), separate model for home and away.
# ---------------------------------------------------------------------------
def build_market_training_rows(dataset, market):
    home_X, home_y, away_X, away_y = [], [], [], []
    for row in dataset:
        hf, af = row["home_form"][market], row["away_form"][market]
        home_X.append([hf["home_for"], af["away_against"]])
        home_y.append(row["actual"][market][0])
        away_X.append([af["away_for"], hf["home_against"]])
        away_y.append(row["actual"][market][1])
    return home_X, home_y, away_X, away_y


def train_market_models(dataset, market):
    home_X, home_y, away_X, away_y = build_market_training_rows(dataset, market)
    home_model = LinearRegression().fit(home_X, home_y)
    away_model = LinearRegression().fit(away_X, away_y)
    return home_model, away_model


def predict_market(home_model, away_model, home_form, away_form, market):
    hf, af = home_form[market], away_form[market]
    home_pred = max(0.0, home_model.predict([[hf["home_for"], af["away_against"]]])[0])
    away_pred = max(0.0, away_model.predict([[af["away_for"], hf["home_against"]]])[0])
    return home_pred, away_pred


# ---------------------------------------------------------------------------
# Full prediction for one match, given the dataset built so far.
# ---------------------------------------------------------------------------
def predict_match(models, home_form, away_form, rho=0.0):
    home_xg, away_xg = predict_goals(home_form, away_form)
    scorelines = scoreline_probabilities(home_xg, away_xg, rho=rho)
    outcomes = outcome_probabilities(scorelines)

    prediction = {"home_xg": home_xg, "away_xg": away_xg, **outcomes}
    for market in MARKETS:
        home_model, away_model = models[market]
        home_pred, away_pred = predict_market(home_model, away_model, home_form, away_form, market)
        prediction["home_" + market] = home_pred
        prediction["away_" + market] = away_pred
    return prediction


def train_all_market_models(dataset):
    return {market: train_market_models(dataset, market) for market in MARKETS}
