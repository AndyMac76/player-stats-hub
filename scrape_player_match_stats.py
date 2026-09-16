"""
scrape_player_match_stats.py

Pulls match-by-match player stats from FBref for every active league in
config.LEAGUES (or just one, via --league) and stores them into the shared
player_stats.db, tagged by league.

Per match, in the same loop (reuses the same cached match-report page fetch
where possible):
    - player_match_stats  (goals/assists/cards/etc.)
    - lineups             (starter vs sub, per player)
    - match_events        (goals/cards/subs with minute + players involved)
And once per league per run, cheaply from the schedule already being fetched:
    - fixtures             (full team fixture list, needed for "last 5 team
                             fixtures regardless of played" style dashboard stats)

player_match_stats normally comes from FBref's season-aggregate pages
(4 league-wide page loads per run, shared across every match) instead of a
per-match "summary"+"keepers" fetch - validated in
validate_aggregate_delta_approach.py at 100% accuracy on every reconstructible
column. This only works for a team with exactly ONE unscraped match this run
(a season-aggregate page only ever gives a CURRENT total, so two+ new matches
for the same team can't be split back out) - teams with more than one pending
match (a missed-week catch-up, a rescheduled midweek fixture) transparently
fall back to the old, slower per-match fetch for just those matches. Lineups,
match events, and team stats (corners etc.) always still need the per-match
fetch either way - none of that is on the aggregate pages.

Going-forward only. Resumable per league: on startup, checks which
match_ids are already saved for that league/season and skips them.
Lineups/events are fetched alongside stats for the same match_id, so they
inherit the same resumability - no separate tracking needed.

Usage:
    python scrape_player_match_stats.py              # every active league
    python scrape_player_match_stats.py --league EPL  # just one
"""

import argparse
import sqlite3
import time
from collections import Counter

import pandas as pd
import soccerdata as sd

import config
import fbref_scrape_common as common

TEST_MATCH_LIMIT = None


def get_scraper(sd_league, season):
    return sd.FBref(leagues=sd_league, seasons=season, headless=False, path_to_browser=None)


def get_match_ids(fbref):
    schedule = fbref.read_schedule()
    schedule = schedule.reset_index()
    completed = schedule.dropna(subset=["score"]) if "score" in schedule.columns else schedule
    return completed


def build_schedule_lookup(schedule):
    date_col = None
    for candidate in ("date", "Date", "game_date"):
        if candidate in schedule.columns:
            date_col = candidate
            break

    home_col = None
    for candidate in ("home_team", "Home", "home"):
        if candidate in schedule.columns:
            home_col = candidate
            break

    away_col = None
    for candidate in ("away_team", "Away", "away"):
        if candidate in schedule.columns:
            away_col = candidate
            break

    if not (date_col and home_col and away_col):
        print("    WARNING: could not find expected date/home/away columns in schedule.")
        print(f"    Schedule columns are: {list(schedule.columns)}")
        return {}

    lookup = {}
    for _, row in schedule.iterrows():
        lookup[row["game_id"]] = {
            "date": row[date_col],
            "home_team": row[home_col],
            "away_team": row[away_col],
        }
    return lookup


def write_fixtures_table(conn, league, season, schedule_lookup):
    """Cheap - schedule is already fetched for match_ids, so this writes a
    team-perspective row for both home and away out of data we already have
    in memory. No extra network calls.

    These matches all have a score (get_match_ids() already filtered to
    completed ones), so is_played=1 here unconditionally - pull_full_schedule.py
    is the source of truth for upcoming/unplayed fixtures."""
    rows = []
    for match_id, info in schedule_lookup.items():
        match_date = str(info["date"]) if pd.notna(info["date"]) else None
        rows.append((league, info["home_team"], info["away_team"], match_id, match_date, "Home", season, 1, 1))
        rows.append((league, info["away_team"], info["home_team"], match_id, match_date, "Away", season, 1, 0))

    conn.executemany(
        """
        INSERT OR IGNORE INTO fixtures
            (league, team, opponent, match_id, match_date, venue, season, is_played, is_home)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    print(f"[{league}] Fixtures table updated: {len(rows)} team-match rows (upserted, duplicates ignored).")


def get_already_scraped_match_ids(conn, league, season):
    try:
        rows = conn.execute(
            "SELECT DISTINCT match_id FROM player_match_stats WHERE league = ? AND season = ?",
            (league, season),
        ).fetchall()
        return {row[0] for row in rows}
    except sqlite3.OperationalError:
        return set()


def canonicalize_schedule_team_names(conn, league_key, team_aliases, schedule_lookup):
    """Rewrites schedule_lookup's home_team/away_team in place to match
    whichever spelling player_match_stats has already established for that
    club this league, wherever the two differ.

    Every write that follows (fixtures, and - since the season-aggregate
    fast path added below - player_match_stats itself) ultimately sources
    its team name from schedule_lookup, i.e. FBref's schedule page. That
    page doesn't always spell a club the same way its match-report page
    does ("Brighton" vs "Brighton & Hove Albion"), and match-report naming
    is what every match scraped the old way (and everything before the
    fast path existed) already used - so left alone, the fast path splits
    a club across two different literal strings in the same column,
    silently duplicating it on the dashboard's Teams view."""
    established = {row[0] for row in conn.execute(
        "SELECT DISTINCT team FROM player_match_stats WHERE league = ?", (league_key,)
    )}
    if not established:
        return
    resolved = {}
    for info in schedule_lookup.values():
        for key in ("home_team", "away_team"):
            name = info[key]
            if name in established or name in resolved:
                continue
            match = next((e for e in established if common.names_match(name, e, team_aliases)), None)
            resolved[name] = match if match else name
    for info in schedule_lookup.values():
        for key in ("home_team", "away_team"):
            info[key] = resolved.get(info[key], info[key])


def build_aggregate_fast_path(conn, fbref, league_key, league_cfg, match_ids, schedule_lookup):
    """Figures out which teams have exactly one unscraped match this run
    (the only case a season-aggregate delta can be trusted to attribute to
    the right match_id - see fbref_scrape_common.py's module docstring on
    build_rows_from_aggregate_delta), then fetches the season-aggregate
    pages ONCE for the whole league if any team qualifies. Returns
    (fast_path_teams, current_cumulative, prior_totals) - fast_path_teams is
    empty and the other two are {} if nothing qualified (skips the fetch
    entirely rather than paying for pages nobody will use).

    Reuses the caller's already-alive `fbref` driver rather than spinning
    up a second sd.FBref(...)/Chrome instance - persistent_fbref.py's
    profile directory is a single fixed path only one live Chrome process
    can hold at a time, so a second instance here would fail to launch
    ("cannot connect to chrome") while the caller's own driver is still
    open, silently aborting the entire league's scrape before it even
    reaches the per-match loop (found this exact way: EPL/CHAMP stuck at
    the same "already-scraped" count for 3 runs straight after this fast
    path was added, always failing here first)."""
    team_pending_count = Counter()
    for match_id in match_ids:
        info = schedule_lookup.get(match_id)
        if info:
            team_pending_count[info["home_team"]] += 1
            team_pending_count[info["away_team"]] += 1

    fast_path_teams = {team for team, count in team_pending_count.items() if count == 1}
    if not fast_path_teams:
        return set(), {}, {}

    print(f"[{league_key}] {len(fast_path_teams)} team(s) have exactly one pending match this run - "
          f"fetching season-aggregate pages once instead of per-match for those.")

    season = league_cfg["current_season"]
    original_no_cache = fbref.no_cache
    fbref.no_cache = True
    try:
        frames = common.fetch_season_aggregates(fbref)
    finally:
        fbref.no_cache = original_no_cache

    season_team_aliases = league_cfg.get("season_team_aliases", {})
    current_cumulative = common.build_cumulative_lookup(frames, season_team_aliases)
    prior_totals = common.build_prior_totals(conn, league_key, season)
    return fast_path_teams, current_cumulative, prior_totals


def scrape_league(conn, league_key, league_cfg):
    sd_league = league_cfg["sd_league"]
    season = league_cfg["current_season"]
    team_aliases = league_cfg["team_aliases"]

    fbref = get_scraper(sd_league, season)
    try:
        schedule = get_match_ids(fbref)
        match_ids = schedule["game_id"].dropna().unique().tolist()
        schedule_lookup = build_schedule_lookup(schedule)
        canonicalize_schedule_team_names(conn, league_key, team_aliases, schedule_lookup)

        if schedule_lookup:
            write_fixtures_table(conn, league_key, season, schedule_lookup)

        # Built once up front: covers any player who already has a real ID from
        # a match they featured in, so unused subs in later matches resolve to
        # their canonical ID instead of a name placeholder.
        canonical_lookup = common.build_canonical_player_id_lookup(conn, league_key)

        already_scraped = get_already_scraped_match_ids(conn, league_key, season)
        if already_scraped:
            before = len(match_ids)
            match_ids = [m for m in match_ids if m not in already_scraped]
            print(f"[{league_key}] Resuming: skipping {before - len(match_ids)} already-scraped matches.")

        if TEST_MATCH_LIMIT is not None:
            match_ids = match_ids[:TEST_MATCH_LIMIT]
            print(f"[{league_key}] TEST_MATCH_LIMIT is set to {TEST_MATCH_LIMIT} - only scraping that many matches.")

        print(f"[{league_key}] Found {len(match_ids)} completed matches to scrape for {season}.")

        fast_path_teams, current_cumulative, prior_totals = build_aggregate_fast_path(
            conn, fbref, league_key, league_cfg, match_ids, schedule_lookup,
        )

        for i, match_id in enumerate(match_ids, start=1):
            print(f"[{league_key}] [{i}/{len(match_ids)}] Scraping match {match_id}...")

            try:
                match_info = schedule_lookup.get(match_id)
                # One page fetch covers both the player-ID map and the Team
                # Stats section (corners etc.) - they used to be two
                # independent fetches of the exact same match report URL.
                player_id_map, team_stats = common.get_match_page_data(fbref, match_id)

                home_team = match_info.get("home_team") if match_info else None
                away_team = match_info.get("away_team") if match_info else None
                use_fast_path = (
                    match_info is not None
                    and home_team in fast_path_teams
                    and away_team in fast_path_teams
                )

                merged = None
                if use_fast_path:
                    home_rows = common.build_rows_from_aggregate_delta(
                        match_id, match_info, home_team, season, league_key, team_aliases,
                        current_cumulative, prior_totals, player_id_map, canonical_lookup,
                    )
                    away_rows = common.build_rows_from_aggregate_delta(
                        match_id, match_info, away_team, season, league_key, team_aliases,
                        current_cumulative, prior_totals, player_id_map, canonical_lookup,
                    )
                    if home_rows is not None and away_rows is not None:
                        merged = pd.concat([home_rows, away_rows], ignore_index=True)
                        print(f"    Used season-aggregate fast path (skipped per-match stat fetch).")
                    else:
                        print(f"    Aggregate delta wasn't trustworthy for match {match_id} - falling back to per-match fetch.")

                if merged is None:
                    stat_frames = {}
                    for stat_type in config.STAT_TYPES:
                        df = common.read_stat_with_recovery(fbref, stat_type, match_id)
                        if df is not None:
                            stat_frames[stat_type] = df

                    if not stat_frames:
                        print(f"    No data returned for match {match_id}, skipping.")
                        continue

                    merged = common.merge_stat_frames(
                        stat_frames, match_id, match_info, season, league_key, team_aliases,
                        player_id_map, canonical_lookup,
                    )
                if merged is not None and not merged.empty:
                    merged.to_sql("player_match_stats", conn, if_exists="append", index=False)
                    print(f"    Saved {len(merged)} player rows.")
                    # Keep the lookup current within this run - a player who
                    # got a real ID this match should resolve correctly if they
                    # show up as an unused sub in a later match this same run.
                    for _, r in merged.iterrows():
                        if r["player_id"] and r["player_id"] != r["player_name"]:
                            canonical_lookup[r["player_name"]] = r["player_id"]

                # --- Lineups (starter vs sub) ---
                try:
                    lineup_df = common.read_lineup_with_recovery(fbref, match_id)
                    merged_lineup = common.merge_lineup_frame(
                        lineup_df, match_id, season, league_key, player_id_map, canonical_lookup
                    )
                    if merged_lineup is not None and not merged_lineup.empty:
                        merged_lineup.to_sql("lineups", conn, if_exists="append", index=False)
                        print(f"    Saved {len(merged_lineup)} lineup rows.")
                except sqlite3.IntegrityError as e:
                    print(f"    WARNING: lineup insert skipped duplicate rows for match {match_id}: {e}")
                except Exception as e:
                    print(f"    WARNING: lineup processing failed for match {match_id}: {e}")

                # --- Events (goals/cards/subs) ---
                try:
                    events_df = common.read_events_with_recovery(fbref, match_id)
                    merged_events = common.merge_events_frame(
                        events_df, match_id, season, league_key, player_id_map, canonical_lookup
                    )
                    if merged_events is not None and not merged_events.empty:
                        merged_events.to_sql("match_events", conn, if_exists="append", index=False)
                        print(f"    Saved {len(merged_events)} event rows.")
                except Exception as e:
                    print(f"    WARNING: events processing failed for match {match_id}: {e}")

                # --- Team stats (possession, corners, cards, fouls, etc.) ---
                # (team_stats already fetched above, alongside player_id_map,
                # from the same page load)
                try:
                    if match_info:
                        # match_info["date"] is a pandas Timestamp (straight from the
                        # schedule dataframe) - sqlite3's binder doesn't accept those
                        # directly, same reason write_fixtures_table() above stringifies
                        # it too.
                        raw_date = match_info.get("date")
                        match_date_str = str(raw_date) if pd.notna(raw_date) else None
                        team_stats_rows = common.build_team_match_stats_rows(
                            team_stats, match_id, match_date_str, season, league_key,
                            match_info.get("home_team"), match_info.get("away_team"),
                        )
                        if team_stats_rows:
                            conn.executemany(
                                """
                                INSERT OR REPLACE INTO team_match_stats
                                (league, team, opponent, match_id, match_date, season, is_home,
                                 possession_pct, shots_on_target, shots_total, saves, shots_faced,
                                 cards_yellow, cards_red, fouls, corners, crosses, interceptions, offsides)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                team_stats_rows,
                            )
                            conn.commit()
                            print(f"    Saved team stats for both sides (corners: {team_stats.get('home_corners')}-{team_stats.get('away_corners')}).")
                except Exception as e:
                    print(f"    WARNING: team stats processing failed for match {match_id}: {e}")

            except Exception as e:
                import traceback
                print(f"    ERROR on match {match_id}: {e}")
                traceback.print_exc()

            time.sleep(3)

        print(f"[{league_key}] Done.")
    finally:
        common.quit_driver(fbref)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--league", choices=list(config.LEAGUES.keys()), default=None,
        help="Scrape only this league instead of every active league in config.LEAGUES.",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(config.DB_PATH)
    common.create_tables(conn)

    targets = {args.league: config.LEAGUES[args.league]} if args.league else config.active_leagues()
    if not targets:
        print("No active leagues to scrape (config.LEAGUES has none marked active=True).")
        conn.close()
        return

    for league_key, league_cfg in targets.items():
        try:
            scrape_league(conn, league_key, league_cfg)
        except Exception as e:
            import traceback
            print(f"[{league_key}] ERROR scraping league: {e}")
            traceback.print_exc()

    conn.close()
    print("All leagues done.")


if __name__ == "__main__":
    main()
