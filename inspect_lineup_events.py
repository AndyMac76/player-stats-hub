"""
inspect_lineup_events.py

One-off diagnostic - runs read_lineup() and read_events() for a single
completed match so we can see the real column names before building the
full scrape/merge logic in scrape_player_match_stats.py.

Uses the same persistent Cloudflare-bypass driver as the main scraper.

Usage:
    python inspect_lineup_events.py
"""

import pandas as pd
import soccerdata as sd

import config
import persistent_fbref  # noqa: F401 - import only, applies the monkey-patch

pd.set_option("display.max_columns", None)
pd.set_option("display.width", 200)

LEAGUE = config.LEAGUE
SEASON = config.CURRENT_SEASON


def main():
    fbref = sd.FBref(leagues=LEAGUE, seasons=SEASON, headless=False, path_to_browser=None)

    schedule = fbref.read_schedule().reset_index()
    completed = schedule.dropna(subset=["score"]) if "score" in schedule.columns else schedule

    if completed.empty:
        print("No completed matches found in schedule - nothing to test against.")
        return

    test_match_id = completed["game_id"].iloc[0]
    print(f"Testing against match_id: {test_match_id}\n")

    print("=" * 70)
    print("read_lineup() output")
    print("=" * 70)
    try:
        lineup = fbref.read_lineup(match_id=test_match_id)
        print("Columns:", list(lineup.columns))
        print("\nIndex names:", lineup.index.names)
        print("\nFirst 10 rows:")
        print(lineup.reset_index().head(10))
        print("\nDtypes:")
        print(lineup.dtypes)
    except Exception as e:
        print(f"read_lineup() failed: {e}")

    print("\n" + "=" * 70)
    print("read_events() output")
    print("=" * 70)
    try:
        events = fbref.read_events(match_id=test_match_id)
        print("Columns:", list(events.columns))
        print("\nIndex names:", events.index.names)
        print("\nFirst 10 rows:")
        print(events.reset_index().head(10))
        print("\nDtypes:")
        print(events.dtypes)
    except Exception as e:
        print(f"read_events() failed: {e}")


if __name__ == "__main__":
    main()