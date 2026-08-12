"""
check_future_fixtures.py

Quick check: does the `fixtures` table include upcoming/unplayed matches,
or only matches already scraped into player_match_stats?

Usage:
    python check_future_fixtures.py
"""

import sqlite3
from datetime import date
from pathlib import Path

DB_PATH = Path("player_stats.db")


def main():
    conn = sqlite3.connect(DB_PATH)

    total_fixtures = conn.execute("SELECT COUNT(*) FROM fixtures").fetchone()[0]
    distinct_matches = conn.execute(
        "SELECT COUNT(DISTINCT match_id) FROM fixtures"
    ).fetchone()[0]
    scraped_matches = conn.execute(
        "SELECT COUNT(DISTINCT match_id) FROM player_match_stats"
    ).fetchone()[0]

    print(f"Total fixtures rows:              {total_fixtures}")
    print(f"Distinct match_ids in fixtures:   {distinct_matches}")
    print(f"Distinct match_ids scraped:       {scraped_matches}")

    if distinct_matches <= scraped_matches:
        print(
            "\n⚠️  fixtures appears to only cover matches already scraped — "
            "no future/unplayed fixtures present."
        )
    else:
        print(
            f"\n✅ fixtures has {distinct_matches - scraped_matches} more match_ids "
            f"than player_match_stats — likely includes future fixtures."
        )

    # Show date range and how many fall after today
    today = date.today().isoformat()
    date_range = conn.execute(
        "SELECT MIN(match_date), MAX(match_date) FROM fixtures"
    ).fetchone()
    print(f"\nDate range in fixtures: {date_range[0]} to {date_range[1]}")
    print(f"Today's date: {today}")

    future_count = conn.execute(
        "SELECT COUNT(DISTINCT match_id) FROM fixtures WHERE match_date > ?",
        (today,),
    ).fetchone()[0]
    print(f"Distinct match_ids with match_date after today: {future_count}")

    # Sample a few rows to eyeball match_id presence/absence for future dates
    sample = conn.execute(
        "SELECT team, opponent, match_id, match_date FROM fixtures "
        "WHERE match_date > ? ORDER BY match_date LIMIT 10",
        (today,),
    ).fetchall()
    if sample:
        print("\nSample upcoming rows in fixtures:")
        for row in sample:
            print(f"   {row}")
    else:
        print("\nNo rows found with match_date after today.")

    conn.close()


if __name__ == "__main__":
    main()