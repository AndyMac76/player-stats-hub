"""
rolling_stats.py

Calculates each player's rolling average over their last N matches
(N = config.ROLLING_WINDOW, default 5) and writes the result into the
shared player_rolling_stats table, for every active league in
config.LEAGUES (or just one, via --league).

We store every individual match row (player_match_stats) and calculate
the rolling averages on the fly here, rather than storing only the
averages - this keeps the raw data intact and lets you change the window
size (5, 3, 10...) later without re-scraping anything.

Run this any time after scrape_player_match_stats.py to refresh the
rolling table with the latest matches.

Usage:
    python rolling_stats.py               # every active league
    python rolling_stats.py --league EPL   # just one
"""

import argparse
import sqlite3

import config
import fbref_scrape_common as common


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--league", choices=list(config.LEAGUES.keys()), default=None,
        help="Recalculate only this league instead of every active league in config.LEAGUES.",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(config.DB_PATH)
    common.create_tables(conn)

    targets = {args.league: config.LEAGUES[args.league]} if args.league else config.active_leagues()
    if not targets:
        print("No active leagues to process (config.LEAGUES has none marked active=True).")
        conn.close()
        return

    for league_key in targets:
        rolling_df = common.calculate_rolling_stats(conn, league_key, config.ROLLING_WINDOW)
        common.save_rolling_stats(conn, league_key, rolling_df)

    conn.close()


if __name__ == "__main__":
    main()
