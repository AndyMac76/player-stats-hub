# MLS Player Stats — Consolidated Scrape Expansion Plan

## Status as of 8 Aug 2026 session

**Confirmed complete tonight — backfill verified clean:**
- Full backfill run across all 268 existing matches finished. Ran
  `check_backfill_status.py` (new verification script, checked into repo
  going forward) and confirmed:
  - **268/268 matches** covered across `player_match_stats`, `lineups`,
    and `match_events` — fully in sync, no gaps.
  - **536/536 team-match rows** have exactly 11 starters — lineup parsing
    is solid.
  - **0% placeholder player IDs** across 10,674 lineup rows — the
    canonical-lookup fix landed cleanly, no leftover contamination from
    the pre-fix backfill.
  - **Event minutes are clean ints** — no leftover `'65''`-style strings.
  - **Event type counts look plausible** for 268 matches: 2348
    substitute_in, 1099 yellow_card, 770 goal, 70 penalty_goal, 30
    red_card, 24 own_goal, 20 yellow_red_card, 11 penalty_miss.

- **`match_events` player1/player2 mapping confirmed for subs** — there is
  no separate `substitute_out` event type; each substitution is a single
  `substitute_in` row. Cross-referenced `player1_id`/`player2_id` against
  `lineups.is_starter` for a 10-event sample and got a consistent result
  across all 10:
  - **`player1` = player coming ON** (was NOT a starter, i.e. `is_starter=0`)
  - **`player2` = player going OFF** (was a starter, i.e. `is_starter=1`)

  This mapping should be treated as confirmed and used as-is anywhere
  `match_events` substitution rows are consumed (impact-sub flags,
  red-card game-state adjustments, etc.) — no need to re-derive it, but
  worth a code comment wherever it's relied on since it's not obvious
  from the column names alone.

**Previously done (6 Aug session), still standing:**
- `scrape_player_match_stats.py` extended: writes `fixtures`, `lineups`,
  and `match_events` alongside `player_match_stats` in the same per-match
  loop.
- `resolve_player_id()` three-tier resolution (match's own
  `data-append-csv` map → `canonical_lookup` from `player_match_stats` →
  placeholder last resort), backed by `build_canonical_player_id_lookup()`.
- `backfill_lineups_events.py` — one-off catch-up pass for the 268
  matches scraped before lineups/events existed.
- `cleanup_stale_backfill.py` — used once to wipe 14 matches backfilled
  before the canonical-lookup fix landed; not needed going forward now
  the backfill is confirmed clean.

**Not yet done (next up, from the earlier 6 Aug session):**
- ~~Season aggregates (`read_player_season_stats` for `playing_time` and
  `misc`) — not pulled yet.~~ **Done — see 8 Aug update above.**
- ~~`player_season_stats` table — not created yet, depends on the above.~~
  **Done — see 8 Aug update above for final as-built schema.**
- The "last 5 starts / last 5 appearances / last 5 team fixtures" rolling
  stats logic and dashboard toggle — now unblocked, backfill and season
  aggregates are both confirmed clean via `check_backfill_status.py` and
  `pull_season_aggregates.py`.
- Traffic-light dots still need removing from `generate_dashboard.py`
  per Andy's earlier request — not done yet. Plan is to bundle this with
  the toggle wiring so it's one pass through the file instead of two.

## Status as of 8 Aug 2026 session (continued — season aggregates)

**Season aggregates pulled and confirmed working:**
- `pull_season_aggregates.py` written and debugged. Pulls
  `read_player_season_stats(stat_type='playing_time')` and
  `stat_type='misc'`, merges them, and writes `player_season_stats`.
- **Misc endpoint does NOT include aerial duels for MLS** — confirmed
  dead end, not a naming issue. Actual `misc` columns are cards
  (`CrdY`/`CrdR`/`2CrdY`), fouls (`Fls`/`Fld`), offsides, crosses,
  interceptions, tackles won, and penalties won/conceded. The original
  plan assumed aerials would come from `misc` — that assumption was
  wrong and has been dropped from the schema.
- **Season-stats endpoint has no `player_id` column** — FBref only
  exposes `player_id` on match-report pages (via `data-append-csv`), not
  on the season-aggregate pages. `player_season_stats` is keyed on
  `(player_name, team, season)` instead of `(player_id, season)`, with
  `player_id` backfilled via a name+team lookup built from
  `player_match_stats` at write time — same category of problem as the
  FPL name-reconciliation, just FBref-to-FBref this time.
- **Team-name mismatch found and fixed**: the season-aggregate endpoint
  returns FBref's short/abbreviated team names (`Atlanta Utd`, `LAFC`,
  `Houston`, `RB New York`, `NYCFC`, `Charlotte`, `Vancouver`,
  `SJ Earthquakes`, `Minnesota Utd`, `Sporting KC`, `Philadelphia`,
  `NE Revolution`) rather than the fuller canonical names already stored
  from match-level scraping (`Atlanta United`, `Los Angeles FC`, `Houston
  Dynamo`, `Red Bull New York`, `New York City FC`, etc.). Added a
  `SEASON_TEAM_ALIASES` dict in `pull_season_aggregates.py` mapping short
  → canonical form. Fixed the match rate from 52.8% to 87.4% on its own.
- **Final match rate: 803/919 (87.4%) resolved to a `player_id`.** The
  remaining 116 unmatched rows all failed on name (0 would match on name
  alone once team was normalized) — spot-checked as players present in
  the season-aggregate feed who never appear in `player_match_stats` at
  all, i.e. rostered all season but never featured in a scraped match
  (no bench-only placeholder row either, since placeholders only get
  created for players who at least appear on a match's lineup page).
  This is expected/correct behavior, not a bug — `player_id` stays NULL
  for these rather than being guessed at.
- Also fixed a `sqlite3.ProgrammingError: NAType not supported` crash —
  pandas `NaN`/`pd.NA` values (players with 0 minutes or a missing stat)
  need explicit conversion to `None` before binding to sqlite3; handled
  via a `clean_value()` helper applied to every field before insert.

**`player_season_stats` final schema (as built, not as originally planned):**
```sql
CREATE TABLE player_season_stats (
    player_id TEXT,
    player_name TEXT,
    team TEXT,
    season TEXT,
    starts INTEGER,
    subs INTEGER,
    matches_played INTEGER,
    minutes INTEGER,
    fouls_committed INTEGER,
    fouls_drawn INTEGER,
    yellow_cards INTEGER,
    red_cards INTEGER,
    PRIMARY KEY (player_name, team, season)
);
```
No aerial duel columns (not published for MLS via `misc`). No
`matches_played`/`minutes`-only "starter reliability" gap either —
`starts`/`subs`/`matches_played`/`minutes` all came through cleanly from
`playing_time`.

**Not yet done (next up):**
- The "last 5 starts / last 5 appearances / last 5 team fixtures" rolling
  stats logic and dashboard toggle — this is the next task, now that both
  the match-level backfill and season aggregates are confirmed solid.
- Traffic-light dots still need removing from `generate_dashboard.py` —
  plan is to bundle this with the toggle wiring into one pass.

**Fixtures table corrected — now includes future matches:**
- Discovered the `fixtures` table (as populated on 6 Aug) only ever
  covered the 268 already-scraped matches — it was derived as a
  side-effect of looping over `match_ids` already in
  `player_match_stats`, so it silently excluded anything not yet played.
  Confirmed via `check_future_fixtures.py`: 268/268 distinct match_ids,
  date range capped at 1 Aug 2026, zero rows beyond today.
- Fixed with `pull_full_schedule.py`, which calls `read_schedule()`
  directly instead of deriving fixtures from the match-level loop.
  `read_schedule()` returns the full season — 510 matches total, both
  played and upcoming, with `score` used to derive an `is_played` flag.
- Same short-form team-name issue as the season-aggregates endpoint
  showed up here too (`Minnesota Utd` instead of `Minnesota United`,
  etc.) — reused the same `SEASON_TEAM_ALIASES` mapping to normalize
  before writing.
- Rebuilt `fixtures` from scratch (was fully derivable from FBref, so
  safe to drop/recreate) with an added `is_played` column and a
  `(team, match_id, match_date)` primary key.
- **Result: 1020 fixture rows — 536 played (268 matches × 2 teams,
  matches prior data), 484 upcoming/unplayed (242 future matches × 2
  teams).** All team names confirmed matching `lineups` — no join gaps.

**Corrected `fixtures` schema:**
```sql
CREATE TABLE fixtures (
    team TEXT, opponent TEXT, match_id TEXT,
    match_date TEXT, venue TEXT, season TEXT,
    is_played INTEGER,
    PRIMARY KEY (team, match_id, match_date)
);
```
`match_id` may be NULL/absent for fixtures very far in the future if
FBref hasn't assigned a match-report page yet — fine for "last 5 team
fixtures" logic (date/opponent-based), just don't assume every row has
one when joining to match-level tables.

---


The existing scrape already burns hours getting through Cloudflare via the
persistent `seleniumbase` profile. Every new soccerdata method below can run
through the same `sd.FBref` instance / same `persistent_fbref.py` monkey-patch,
so the goal is to add them to ONE session rather than come back and trigger
Cloudflare defences again later.

**Efficiency note:** `read_player_match_stats`, `read_lineup`, and
`read_events` all key off `match_id`, and FBref serves them from the same
match-report page. Soccerdata caches downloaded pages under `data_dir`, so if
these three are called back-to-back for the same `match_id` in the same run,
later calls may hit the local cache instead of re-fetching. Worth pulling all
three in the same per-match loop rather than three separate full passes.

---

## What's actually available (corrected)

| Method | Level | What it gives you | Stat types supported |
|---|---|---|---|
| `read_schedule()` | Season, one call | Full team fixture list — date, opponent, venue, match_id | n/a |
| `read_lineup(match_id=...)` | Per match | Starting XI vs bench, per player | n/a |
| `read_events(match_id=...)` | Per match | Timed goals/cards/subs, players involved | n/a |
| `read_player_match_stats(match_id=...)` | Per match | Core box score (what you already scrape) | `summary`, `keepers` only |
| `read_player_season_stats()` | Season, one call | Season aggregates | `standard`, `shooting`, `playing_time`, `keeper`, `misc` |

Match-level stats are limited to `summary`/`keepers` — no match-level duels,
possession, or passing breakdown from soccerdata. Aerial duels (`misc`) and
starts/subs totals (`playing_time`) only exist as **season aggregates**.

Headed shots and shot-distance buckets ("long range shots") are not published
by FBref at any level via soccerdata — confirmed dead end, no action needed.

---

## Pull order for this session

### 1. `read_schedule()` — do this first, it's cheap
One call per league/season. Gives the full team fixture list needed to know
a team played 5 games even when a given player only appears in 3.

→ Feeds new **`fixtures`** table: `team`, `opponent`, `match_id`, `match_date`, `venue`, `season`

### 2. Season aggregates — do these next, also cheap, one call each
- `read_player_season_stats(stat_type='playing_time')` — starts, sub appearances, minutes, matches played per player for the season. This may be enough on its own for a season-long "starter reliability" view even before match-level lineups are in.
- `read_player_season_stats(stat_type='misc')` — aerial duels won/lost/won%, plus other misc counters (fouls, recoveries where published).

→ Feeds new **`player_season_stats`** table: `player_id`, `season`, plus the above columns.

### 3. Per-match loop — this is the expensive part, same loop you already run
For each `match_id` you currently hit for `read_player_match_stats`, also call:
- `read_lineup(match_id=...)` → **`lineups`** table: `player_id`, `match_id`, `started` (bool), `position` (formation slot if provided)
- `read_events(match_id=...)` → **`match_events`** table: `match_id`, `minute`, `event_type`, `player_id`, `detail` (useful later for impact-sub flags, red-card game states)

Since this reuses the match-report page your existing scrape already fetches,
slot these calls in right next to your current `read_player_match_stats` call
in the loop — don't build a separate pass over all match_ids.

---

## New tables summary

```sql
CREATE TABLE fixtures (
    team TEXT, opponent TEXT, match_id TEXT,
    match_date TEXT, venue TEXT, season TEXT
);

CREATE TABLE lineups (
    player_id TEXT, player_name TEXT, team TEXT, match_id TEXT,
    season TEXT, jersey_number INTEGER, is_starter INTEGER,
    position TEXT, minutes_played INTEGER
);

CREATE TABLE match_events (
    match_id TEXT, season TEXT, team TEXT, minute_raw TEXT, minute INTEGER,
    score TEXT, player1_name TEXT, player1_id TEXT,
    player2_name TEXT, player2_id TEXT, event_type TEXT
);

CREATE TABLE player_season_stats (
    player_id TEXT, season TEXT,
    starts INTEGER, subs INTEGER, matches_played INTEGER, minutes INTEGER,
    aerials_won INTEGER, aerials_lost INTEGER, aerials_won_pct REAL
);
```

> Note: `lineups` and `match_events` schemas above reflect the actual
> as-built columns (confirmed via `PRAGMA table_info`), not the earlier
> planning-stage sketch. In particular `match_events` uses
> `player1_id`/`player1_name`/`player2_id`/`player2_name` rather than a
> single `player_id`/`detail` pair — see the confirmed sub mapping above
> for how to interpret `player1` vs `player2` on `substitute_in` rows.
>
> `player_season_stats` as originally sketched here (with
> `aerials_won`/`aerials_lost`/`aerials_won_pct` and a `(player_id,
> season)` primary key) does NOT match what was actually built — see the
> 8 Aug update above for the real schema. Aerials aren't published for
> MLS via the `misc` endpoint, and `player_id` isn't available on the
> season-aggregate endpoint at all, so the key had to change.
>
> `fixtures` as originally sketched here (no `is_played` column, no
> primary key) also does NOT match what was actually built — the first
> attempt at populating it only captured already-played matches, since it
> was derived from the match-level loop rather than a direct
> `read_schedule()` call. See the 8 Aug fixtures-correction update above
> for the real schema and why it changed.

## What this unlocks
- **Last 5 starts** vs **last 5 appearances (started+sub)** vs **last 5 team
  fixtures regardless of featuring** — all three, via joins across
  `player_match_stats` + `lineups` + `fixtures`.
- Dashboard toggle can replace the traffic-light dots for now, per your last
  request.
- Season-long aerial duel and starts/subs reliability, even if not per-match.
- Foundation for later: impact-sub flags (now unblocked — sub mapping
  confirmed), red-card game-state adjustments, fixture-based prediction
  features feeding the broader simulator ambition.

## Not pursued
- Headed shots, long-range shot counts — not published by FBref, no path via soccerdata.
- Match-level duels/possession breakdown — not exposed by soccerdata's match-level method, season-only.

---

## Roadmap beyond this expansion

### Next up — backfill, season aggregates, and fixtures all confirmed clean
1. ~~Spot-check `lineups`/`match_events` data for a handful of matches~~
   **Done — see confirmed status above.**
2. ~~Pull season aggregates (`playing_time`, `misc`)~~ **Done — see 8 Aug
   update above. 87.4% player_id match rate, remaining gap explained as
   players who never featured in a scraped match.**
3. ~~Confirm `fixtures` covers the full season, not just played
   matches~~ **Done — was actually broken (played-matches-only), fixed
   via `pull_full_schedule.py`. Now 510 matches, 242 of them upcoming.**
4. Build the "last 5 starts" / "last 5 appearances" / "last 5 team
   fixtures regardless of played" rolling logic, joining
   `player_match_stats` + `lineups` + `fixtures`. **This is next.**
5. Wire the toggle into `generate_dashboard.py`, remove the traffic-light
   dot code (parked from an earlier session, not yet actioned) — bundle
   both changes into the same pass through the file.

### Bigger picture — raised previously, not started
**Shared scrape module.** Everything built for MLS (`resolve_player_id`,
`build_canonical_player_id_lookup`, the retry wrappers, `merge_lineup_frame`
/ `merge_events_frame`, fixtures-table logic, and now the verified
`check_backfill_status.py` checks) is league-agnostic — it'll apply
identically to the EPL (Corner Kick) and Scottish Premiership projects. Plan
is to pull this into a shared module (e.g. `fbref_scrape_common.py`) that all
three projects import from, so a bug fix or improvement only has to happen
once instead of three times. Each project keeps its own thin scrape script
wiring up `config.py` + `TEAM_ALIASES` and calling into the shared functions.

Proposed order: confirm MLS is solid end-to-end (**done**) → extract the
shared module and re-point MLS at it, confirm nothing broke → wire EPL and
Scottish projects onto it one at a time (each may surface its own
small differences — season format, stat availability — worth handling
individually rather than all at once).

**Meta-dashboard.** Once all three projects' databases are on consistent
footing, a top-level dashboard sitting above all three — a league
switcher driving which `.db` file it queries, rather than merging the
databases themselves. Keeps each league's data source-of-truth clean and
separate (different season formats, different stat availability per
competition) while unifying the view layer. Likely reuses a good chunk of
the existing `generate_dashboard.py` table/filter/sort JS, swapping data
source based on the selected league. Deliberately parked until the
per-league data is trusted — not worth debugging three data sources
through an extra layer of indirection before that.