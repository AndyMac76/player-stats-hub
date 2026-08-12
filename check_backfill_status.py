"""
check_backfill_status.py

Quick go/no-go check on the lineups_events backfill.

Run this anytime — while the backfill is still going (to see progress)
or once it looks finished (to spot-check for sanity).

Usage:
    python check_backfill_status.py
"""

import sqlite3
from pathlib import Path

# ---- adjust if your db lives somewhere else ----
DB_PATH = Path("player_stats.db")
TOTAL_MATCHES_EXPECTED = 268
# --------------------------------------------------


def get_conn():
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Couldn't find {DB_PATH.resolve()} — update DB_PATH at the top of this script."
        )
    return sqlite3.connect(DB_PATH)


def section(title):
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def progress_check(conn):
    section("PROGRESS")

    lineup_matches = conn.execute(
        "SELECT COUNT(DISTINCT match_id) FROM lineups"
    ).fetchone()[0]
    event_matches = conn.execute(
        "SELECT COUNT(DISTINCT match_id) FROM match_events"
    ).fetchone()[0]
    pms_matches = conn.execute(
        "SELECT COUNT(DISTINCT match_id) FROM player_match_stats"
    ).fetchone()[0]

    print(f"player_match_stats matches: {pms_matches} / {TOTAL_MATCHES_EXPECTED} expected")
    print(f"lineups matches:            {lineup_matches} / {TOTAL_MATCHES_EXPECTED} expected")
    print(f"match_events matches:       {event_matches} / {TOTAL_MATCHES_EXPECTED} expected")

    if lineup_matches < pms_matches or event_matches < pms_matches:
        missing_lineups = pms_matches - lineup_matches
        missing_events = pms_matches - event_matches
        print(
            f"\n⏳ Still catching up: {missing_lineups} matches missing lineups, "
            f"{missing_events} missing events (relative to player_match_stats)."
        )
    else:
        print("\n✅ lineups and match_events are caught up with player_match_stats.")


def get_lineups_columns(conn):
    return {row[1] for row in conn.execute("PRAGMA table_info(lineups)")}


def starter_count_check(conn):
    section("STARTER COUNTS (should be 11 per team per match)")

    cols = get_lineups_columns(conn)
    starter_col = "is_starter" if "is_starter" in cols else "started"
    if starter_col not in cols:
        print(f"⚠️  Couldn't find a starter column in `lineups`. Columns present: {sorted(cols)}")
        return

    rows = conn.execute(
        f"""
        SELECT match_id, team, SUM({starter_col}) as starters
        FROM lineups
        GROUP BY match_id, team
        """
    ).fetchall()

    bad_rows = [r for r in rows if r[2] != 11]

    print(f"Checked {len(rows)} team-match rows.")
    if not bad_rows:
        print("✅ All team-match rows have exactly 11 starters.")
    else:
        print(f"⚠️  {len(bad_rows)} team-match rows do NOT have exactly 11 starters:")
        for match_id, team, starters in bad_rows[:20]:
            print(f"   match_id={match_id}  team={team}  starters={starters}")
        if len(bad_rows) > 20:
            print(f"   ...and {len(bad_rows) - 20} more.")


def placeholder_id_check(conn):
    section("PLACEHOLDER PLAYER IDs (should be rare — bench players who never featured)")

    total = conn.execute("SELECT COUNT(*) FROM lineups").fetchone()[0]
    placeholders = conn.execute(
        "SELECT COUNT(*) FROM lineups WHERE player_id LIKE 'placeholder%'"
    ).fetchone()[0]

    pct = (placeholders / total * 100) if total else 0
    print(f"{placeholders} / {total} lineup rows are placeholder IDs ({pct:.1f}%).")

    if pct > 5:
        print("⚠️  That seems high — worth checking a few of these manually.")
        sample = conn.execute(
            """
            SELECT match_id, player_id, team, position
            FROM lineups
            WHERE player_id LIKE 'placeholder%'
            LIMIT 10
            """
        ).fetchall()
        for row in sample:
            print(f"   {row}")
    else:
        print("✅ Looks in a reasonable range for genuinely-never-featured bench players.")


def event_minute_check(conn):
    section("EVENT MINUTES (should be clean ints, not '65'' strings)")

    rows = conn.execute(
        "SELECT DISTINCT minute FROM match_events ORDER BY minute LIMIT 20"
    ).fetchall()
    minutes = [r[0] for r in rows]
    print(f"Sample of distinct minute values: {minutes}")

    bad = [m for m in minutes if isinstance(m, str) and "'" in m]
    if bad:
        print(f"⚠️  Found un-cleaned minute values: {bad}")
    else:
        print("✅ No stray apostrophes/strings spotted in this sample.")


def event_type_breakdown(conn):
    section("EVENT TYPE BREAKDOWN (sanity check — do counts look plausible?)")

    rows = conn.execute(
        """
        SELECT event_type, COUNT(*) as n
        FROM match_events
        GROUP BY event_type
        ORDER BY n DESC
        """
    ).fetchall()
    for event_type, n in rows:
        print(f"   {event_type:<25} {n}")


def schema_check(conn):
    section("SCHEMA (so column-name assumptions don't break silently again)")
    for table in ("lineups", "match_events", "fixtures"):
        cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        print(f"{table}: {cols}")


def sub_event_check(conn):
    section("SUBSTITUTION EVENTS (confirm player1/player2 meaning for subs)")

    rows = conn.execute(
        """
        SELECT player1_name, player1_id, player2_name, player2_id
        FROM match_events
        WHERE event_type = 'substitute_in'
        LIMIT 10
        """
    ).fetchall()

    if not rows:
        print("No substitute_in rows found.")
        return

    print("Sample substitute_in rows (player1 / player2):")
    for p1_name, p1_id, p2_name, p2_id in rows:
        print(f"   player1: {p1_name} ({p1_id})   player2: {p2_name} ({p2_id})")

    # Check whether there's a separate substitute_out event type at all
    event_types = {
        row[0] for row in conn.execute("SELECT DISTINCT event_type FROM match_events")
    }
    if "substitute_out" in event_types:
        print("\n✅ Separate 'substitute_out' event_type exists — subs are two rows.")
        return

    print(
        "\n⚠️  No 'substitute_out' event_type found — each sub is ONE row in "
        "'substitute_in'. Cross-checking player1 vs player2 against `lineups.is_starter` "
        "to work out which one is coming ON vs going OFF..."
    )

    # For each sample sub event, look up is_starter for both player1 and player2
    # in the same match. The player who started (is_starter=1) is the one going OFF;
    # the player who did NOT start (is_starter=0) is the one coming ON.
    sample = conn.execute(
        """
        SELECT match_id, player1_id, player1_name, player2_id, player2_name
        FROM match_events
        WHERE event_type = 'substitute_in'
        LIMIT 10
        """
    ).fetchall()

    print(f"\n{'player1 (name)':<28}{'p1 is_starter':<16}{'player2 (name)':<28}{'p2 is_starter'}")
    verdicts = []
    for match_id, p1_id, p1_name, p2_id, p2_name in sample:
        p1_starter = conn.execute(
            "SELECT is_starter FROM lineups WHERE match_id = ? AND player_id = ?",
            (match_id, p1_id),
        ).fetchone()
        p2_starter = conn.execute(
            "SELECT is_starter FROM lineups WHERE match_id = ? AND player_id = ?",
            (match_id, p2_id),
        ).fetchone()
        p1_starter = p1_starter[0] if p1_starter else None
        p2_starter = p2_starter[0] if p2_starter else None
        print(f"{p1_name:<28}{str(p1_starter):<16}{p2_name:<28}{p2_starter}")
        if p1_starter is not None and p2_starter is not None and p1_starter != p2_starter:
            verdicts.append("player1_off" if p1_starter == 1 else "player1_on")

    if not verdicts:
        print("\n⚠️  Couldn't determine a consistent pattern — check manually.")
    elif all(v == verdicts[0] for v in verdicts):
        if verdicts[0] == "player1_off":
            print(
                "\n✅ Consistent pattern: player1 = player GOING OFF (was a starter), "
                "player2 = player COMING ON (was on the bench)."
            )
        else:
            print(
                "\n✅ Consistent pattern: player1 = player COMING ON (was on the bench), "
                "player2 = player GOING OFF (was a starter)."
            )
    else:
        print(f"\n⚠️  Inconsistent pattern across sample ({verdicts}) — worth a closer look.")


def main():
    conn = get_conn()
    try:
        schema_check(conn)
        progress_check(conn)
        starter_count_check(conn)
        placeholder_id_check(conn)
        event_minute_check(conn)
        event_type_breakdown(conn)
        sub_event_check(conn)
    finally:
        conn.close()

    print(f"\n{'=' * 60}\nDone.\n{'=' * 60}")


if __name__ == "__main__":
    main()
    