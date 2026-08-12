"""
debug_hidden_tables.py

One-off diagnostic, round 2: the previous run showed 12 tables with no
'id' attribute alongside the 4 named summary/keeper tables. That's the
right count for 5 extra stat categories (Passing, Pass Types, Defense,
Possession, Misc) x 2 teams = 10, plus 2 unexplained - worth checking
whether these already contain real data (no extra clicking needed) before
trying anything heavier.

For each unnamed table, prints:
  - Any 'id' on its parent element (FBref sometimes puts the id on a
    wrapping <div> rather than the <table> itself)
  - The header row text, so we can identify what each table actually is
  - Whether 'Cmp%' or 'Head' appears specifically within that table

Usage:
    python debug_hidden_tables.py
"""

import time

import config
import persistent_fbref
import soccerdata as sd
from bs4 import BeautifulSoup

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

unnamed_tables = [t for t in soup.find_all("table") if not t.get("id")]
print(f"\nFound {len(unnamed_tables)} tables with no id attribute.\n")

for i, table in enumerate(unnamed_tables, start=1):
    parent = table.find_parent(id=True)
    parent_id = parent.get("id") if parent else None

    header_row = table.find("tr")
    header_text = header_row.get_text(" | ", strip=True) if header_row else "(no header row found)"

    table_html = str(table)
    has_cmp_pct = "Cmp%" in table_html
    has_head = "Head" in table_html

    print(f"--- Table {i} ---")
    print(f"  Nearest parent id: {parent_id}")
    print(f"  Header row: {header_text[:200]}")
    print(f"  Contains 'Cmp%': {has_cmp_pct}   Contains 'Head': {has_head}")
    print()

print("Done. Paste this output back so we know what these tables actually are.")