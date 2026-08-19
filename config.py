"""
config.py

Shared settings for the Player Stats Hub project - a single database and
pipeline covering multiple leagues (MLS, EPL, Scottish Premiership...).

Each entry in LEAGUES is a self-contained per-league config:
    sd_league       the identifier passed to soccerdata's sd.FBref(leagues=...)
    current_season  the season string to scrape (format depends on the
                     league - MLS is a plain calendar year like "2026";
                     everyone else uses soccerdata's two-year code like "2627")
    team_aliases    FBref name-variant -> canonical name, used by
                     fbref_scrape_common.normalize_team()/names_match()
    active          whether the master scraper processes this league by
                     default - flip to True once a league's config (aliases,
                     current_season) is filled in; no script changes needed
    register        present only for leagues soccerdata doesn't ship
                     built-in (only the Big-5 European leagues + World Cup
                     are built-in) - written into soccerdata's own
                     config/league_dict.json as an import-time side effect,
                     the same way the original MLS and Scottish Premiership
                     projects each did this individually before merging here

Pipeline scripts loop over every `active` league by default, or target one
via --league.
"""

import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Make the shared fbref_common package (sibling directory) importable.
# ---------------------------------------------------------------------------
_SHARED_DIR = Path(__file__).resolve().parent.parent / "fbref_common"
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

DB_PATH = "player_stats.db"

# FBref's read_player_match_stats only supports these two at match level.
STAT_TYPES = ["summary", "keepers"]

ROLLING_WINDOW = 5

# ---------------------------------------------------------------------------
# Team name aliases
# ---------------------------------------------------------------------------
# MLS: exhaustive dict. MLS full team names don't reliably contain their
# short forms as substrings (e.g. "sporting kc" is not a substring of
# "sporting kansas city"), so names_match()'s substring fallback can't be
# relied on the way it can for EPL - every variant needs its own entry.
_MLS_TEAM_ALIASES = {
    "atlanta united": "atlanta united", "atlanta united fc": "atlanta united",
    "atlanta": "atlanta united", "atlanta utd": "atlanta united",
    "austin fc": "austin fc", "austin": "austin fc",
    "charlotte fc": "charlotte fc", "charlotte": "charlotte fc",
    "chicago fire": "chicago fire", "chicago fire fc": "chicago fire",
    "chicago": "chicago fire",
    "fc cincinnati": "fc cincinnati", "cincinnati": "fc cincinnati",
    "colorado rapids": "colorado rapids", "rapids": "colorado rapids",
    "colorado": "colorado rapids",
    "columbus crew": "columbus crew", "crew": "columbus crew",
    "columbus": "columbus crew",
    "dc united": "dc united", "d.c. united": "dc united", "d.c.": "dc united",
    "fc dallas": "fc dallas", "dallas": "fc dallas",
    "houston dynamo": "houston dynamo", "houston dynamo fc": "houston dynamo",
    "dynamo": "houston dynamo", "houston": "houston dynamo",
    "sporting kansas city": "sporting kansas city", "sporting kc": "sporting kansas city",
    "kansas city": "sporting kansas city",
    "la galaxy": "la galaxy", "los angeles galaxy": "la galaxy", "galaxy": "la galaxy",
    "los angeles fc": "los angeles fc", "lafc": "los angeles fc",
    "inter miami": "inter miami", "inter miami cf": "inter miami", "miami": "inter miami",
    "minnesota united": "minnesota united", "minnesota united fc": "minnesota united",
    "minnesota": "minnesota united", "minnesota utd": "minnesota united",
    "cf montreal": "cf montreal", "cf montréal": "cf montreal", "montreal": "cf montreal",
    "nashville sc": "nashville sc", "nashville": "nashville sc",
    "new england revolution": "new england revolution", "new england": "new england revolution",
    "revolution": "new england revolution", "ne revolution": "new england revolution",
    "new york city fc": "new york city fc", "nycfc": "new york city fc",
    "new york city": "new york city fc",
    # FBref's match-report pages use "Red Bull New York" - that's the
    # canonical form here (not "New York Red Bulls", despite that being
    # the club's more commonly-known name), so every variant needs to
    # resolve to the same string the actual scraped data uses.
    "new york red bulls": "red bull new york", "ny red bulls": "red bull new york",
    "red bulls": "red bull new york", "red bull new york": "red bull new york",
    "rb new york": "red bull new york",
    "orlando city": "orlando city", "orlando city sc": "orlando city", "orlando": "orlando city",
    "philadelphia union": "philadelphia union", "philadelphia": "philadelphia union",
    "portland timbers": "portland timbers", "timbers": "portland timbers",
    "portland": "portland timbers",
    "real salt lake": "real salt lake", "rsl": "real salt lake",
    "san diego fc": "san diego fc", "san diego": "san diego fc",
    "san jose earthquakes": "san jose earthquakes", "earthquakes": "san jose earthquakes",
    "san jose": "san jose earthquakes", "sj earthquakes": "san jose earthquakes",
    "seattle sounders": "seattle sounders", "seattle sounders fc": "seattle sounders",
    "sounders": "seattle sounders", "seattle": "seattle sounders",
    "st. louis city": "st louis city", "st louis city": "st louis city",
    "st louis city sc": "st louis city", "st. louis": "st louis city",
    "toronto fc": "toronto fc", "tfc": "toronto fc", "toronto": "toronto fc",
    "vancouver whitecaps": "vancouver whitecaps", "vancouver whitecaps fc": "vancouver whitecaps",
    "whitecaps": "vancouver whitecaps", "vancouver": "vancouver whitecaps",
}

# Also used for pull_full_schedule.py / pull_season_aggregates.py's
# season-level FBref pages, which show shorter team names than the
# match-level pages (e.g. "Atlanta Utd" instead of "Atlanta United").
_MLS_SEASON_TEAM_ALIASES = {
    "Atlanta Utd": "Atlanta United",
    "LAFC": "Los Angeles FC",
    "Houston": "Houston Dynamo",
    "RB New York": "Red Bull New York",
    "NYCFC": "New York City FC",
    "Charlotte": "Charlotte FC",
    "Vancouver": "Vancouver Whitecaps",
    "SJ Earthquakes": "San Jose Earthquakes",
    "Minnesota Utd": "Minnesota United",
    "Sporting KC": "Sporting Kansas City",
    "Philadelphia": "Philadelphia Union",
    "NE Revolution": "New England Revolution",
}

# EPL: kept deliberately light. names_match()'s substring fallback covers
# most short-form/full-name pairs on its own (e.g. "newcastle" is a
# substring of "newcastle united"), so only the exceptions need an entry
# here. EPL's 20-club roster changes every season (3 up, 3 down), so a
# MLS-style exhaustive dict would need re-verifying every year - this
# needs re-checking each promoted/relegated team, not rebuilding from
# scratch.
_EPL_TEAM_ALIASES = {
    "wolves": "wolverhampton",
    "manchester utd": "manchester united",
}

LEAGUES = {
    "MLS": {
        "sd_league": "USA-Major League Soccer",
        "current_season": "2026",  # calendar-year season, in progress since 21 Feb 2026
        "team_aliases": _MLS_TEAM_ALIASES,
        "season_team_aliases": _MLS_SEASON_TEAM_ALIASES,
        "active": True,
        "register": {
            "fbref_name": "Major League Soccer",
            "season_start": "Feb",
            "season_end": "Nov",
            "season_code": "single-year",
        },
    },
    "EPL": {
        "sd_league": "ENG-Premier League",
        "current_season": "2627",  # 2026/27 season, kicks off 21 Aug 2026
        "team_aliases": _EPL_TEAM_ALIASES,
        "season_team_aliases": _EPL_TEAM_ALIASES,
        "active": True,
        # No `register` - ENG-Premier League is a soccerdata built-in league.
    },
    "SPFL": {
        "sd_league": "SCO-Premiership",
        "current_season": "2627",  # 2026/27 season, started 31 July 2026
        # "Dundee" and "Dundee United" are two DIFFERENT clubs - without
        # these, names_match()'s substring fallback would wrongly match
        # "dundee" against "dundee united" (it's a literal substring) in a
        # Dundee derby, misattributing opponent/venue. Mapped to distinct
        # canonical forms so neither is a substring of the other.
        # "Hearts" (schedule short form) vs "Heart of Midlothian" (the
        # match-page/legacy-data full name) also isn't a substring match
        # either direction, so needs an explicit alias too.
        "team_aliases": {
            "dundee": "dundee fc",
            "dundee united": "dundee united fc",
            "hearts": "heart of midlothian",
        },
        "season_team_aliases": {},
        "active": True,
        "register": {
            "fbref_name": "Scottish Premiership",
            "season_start": "Aug",
            "season_end": "May",
        },
    },
    "CHAMP": {
        "sd_league": "ENG-Championship",
        "current_season": "2627",  # 2026/27 season, kicks off 14 Aug 2026
        # Discovered via the live schedule pull's 24-team list. "Wolves"
        # isn't a substring of "Wolverhampton (Wanderers)" and "QPR" isn't
        # a substring of "Queens Park Rangers", so names_match()'s
        # substring fallback can't resolve either on its own. No Sheffield
        # Wednesday this season (only Sheffield United is up), so no
        # Dundee-style derby collision to worry about here.
        "team_aliases": {
            "wolves": "wolverhampton",
            "qpr": "queens park rangers",
        },
        "season_team_aliases": {},
        "active": True,
        "register": {
            # FBref's own competitions index lists this as "EFL Championship"
            # (confirmed against fbref.com/en/comps/ - the display name has
            # to match exactly or soccerdata's league lookup silently
            # returns zero rows instead of erroring clearly).
            "fbref_name": "EFL Championship",
            "season_start": "Aug",
            "season_end": "May",
        },
    },
    "L1": {
        "sd_league": "ENG-League One",
        "current_season": "2627",  # 2026/27 season - same EFL calendar as Championship
        # Discovered by comparing the schedule page's 24-team list against
        # actual match-report team names after scraping matchday 1. Every
        # other short/full pairing (e.g. "Cambridge" / "Cambridge United")
        # resolves fine via names_match()'s substring fallback - only these
        # two don't share a substring relationship either direction.
        "team_aliases": {
            "mk dons": "milton keynes dons",
            "sheffield weds": "sheffield wednesday",
        },
        "season_team_aliases": {},
        "active": True,
        "register": {
            # Confirmed against a live fetch of fbref.com/en/comps/ (comp
            # ID 15, /en/comps/15/history/League-One-Seasons) - "EFL League
            # One", not just "League One", matching the "EFL Championship"
            # naming convention already established for CHAMP.
            "fbref_name": "EFL League One",
            "season_start": "Aug",
            "season_end": "May",
        },
    },
}


def active_leagues():
    return {k: v for k, v in LEAGUES.items() if v.get("active")}


# ---------------------------------------------------------------------------
# Register any league soccerdata doesn't know natively.
# ---------------------------------------------------------------------------
SOCCERDATA_CONFIG_DIR = Path.home() / "soccerdata" / "config"
LEAGUE_DICT_PATH = SOCCERDATA_CONFIG_DIR / "league_dict.json"


def _ensure_leagues_registered():
    to_register = {cfg["sd_league"]: cfg["register"] for cfg in LEAGUES.values() if "register" in cfg}
    if not to_register:
        return

    SOCCERDATA_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    existing = {}
    if LEAGUE_DICT_PATH.is_file():
        with LEAGUE_DICT_PATH.open(encoding="utf8") as f:
            existing = json.load(f)

    changed = False
    for sd_league, reg in to_register.items():
        if sd_league not in existing:
            entry = {
                "FBref": reg["fbref_name"],
                "season_start": reg["season_start"],
                "season_end": reg["season_end"],
            }
            if "season_code" in reg:
                entry["season_code"] = reg["season_code"]
            existing[sd_league] = entry
            changed = True
            print(f"    [config] Registered '{sd_league}' in soccerdata's league_dict.json")

    if changed:
        with LEAGUE_DICT_PATH.open("w", encoding="utf8") as f:
            json.dump(existing, f, indent=2)


_ensure_leagues_registered()
