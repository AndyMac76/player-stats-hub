"""
debug_passing_and_shooting_data.py

One-off diagnostic: navigates to a single match report page and inspects
the raw HTML for both the Passing table (Cmp%) and the Shooting table
(to check whether headed shots / shots outside the box are tracked at
match level - soccerdata doesn't expose either, so we need to see the
real structure before writing parsers).

Usage:
    python debug_passing_and_shooting_data.py
"""

import re
import time

import config
import persistent_fbref
import soccerdata as sd
from bs4 import BeautifulSoup, Comment

fbref = sd.FBref(leagues=config.LEAGUE, seasons=config.CURRENT_SEASON, headless=False, path_to_browser=None)

schedule = fbref.read_schedule().reset_index()
completed = schedule.dropna(subset=["score"]) if "score" in schedule.columns else schedule
match_id = completed["game_id"].dropna().iloc[0]
print(f"Testing with match_id={match_id}")

driver = fbref._driver
driver.get(f"https://fbref.com/en/matches/{match_id}/")
time.sleep(2)
html = driver.page_source

soup = BeautifulSoup(html, "html.parser")

print("\n--- Table IDs found in the LIVE rendered DOM ---")
for table in soup.find_all("table"):
    print(f"  {table.get('id')}")

def check_term(term, label):
    print(f"\n--- Does '{term}' appear anywhere in the live DOM? ({label}) ---")
    print("YES" if term in html else "NO")

check_term("Cmp%", "passing completion")
check_term("Head", "headed shots")
check_term("Dist", "shot distance / zone data")
check_term("Body Part", "shot body-part breakdown")

print("\n--- Checking inside HTML comments (FBref sometimes hides tables here) ---")
comments = soup.find_all(string=lambda text: isinstance(text, Comment))
found_any = False
for c in comments:
    if any(term in c for term in ("Cmp%", "shooting", "Shooting")) or "Head" in c:
        found_any = True
        ids_in_comment = re.findall(r'id="([^"]*(?:passing|shooting)[^"]*)"', c, re.IGNORECASE)
        print(f"  Found relevant comment. Table ids inside: {ids_in_comment}")

if not found_any:
    print("  No passing/shooting-related comments found.")

print("\nDone. Paste this output back so we can build the real parsers.")
