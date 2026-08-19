# run_weekly_scrape.ps1
#
# Weekly refresh for the Player Stats Hub project - covers every active
# league in config.LEAGUES (currently MLS and EPL) in a single run, since
# each pipeline script loops over active leagues by default:
#   1. Pulls each league's full season schedule into fixtures
#      (cheap, catches newly-assigned match-report IDs for fixtures that
#      were previously only "pending-...")
#   2. Scrapes any newly completed matches into player_match_stats,
#      lineups, and match_events
#   3. Backfills lineups/events for any match that has player_match_stats
#      but is still missing lineups/events - catches matches where step 2
#      saved the stats but a transient error skipped lineups/events for
#      that match (scrape_player_match_stats.py won't retry it on its own,
#      since its resumability check is keyed off player_match_stats already
#      being present)
#   4. Pulls season-level aggregates into player_season_stats
#   5. Recalculates the rolling 5-match averages into player_rolling_stats
#   6. Regenerates dashboard.html from the refreshed data - without this
#      step the database stays current but the actual page everyone looks
#      at (including the desktop shortcuts and the live GitHub Pages copy)
#      would silently go stale
#   7. Commits and pushes dashboard.html so the live GitHub Pages copy
#      (andymac76.github.io/player-stats-hub) stays in sync automatically -
#      non-fatal if it fails (e.g. no internet), just logged and skipped
#
# Logs output to a timestamped file so a failure overnight is easy to
# check the next day.

# Player/team names routinely contain non-ASCII characters (accents,
# Balkan/Scandinavian names, etc.) - without this, Python's stdout defaults
# to the Windows console's codepage (cp1252) on this machine, which can't
# encode them. Any print() of such a name then crashes mid-match, aborting
# that match's processing before anything gets saved.
$env:PYTHONIOENCODING = "utf-8"

$ProjectDir = "C:\Users\andym\OneDrive\Projects\Player Stats Hub"
$PythonExe  = "C:\Users\andym\AppData\Local\Programs\Python\Python314\python.exe"
$LogDir     = Join-Path $ProjectDir "logs"

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

$Timestamp = Get-Date -Format "yyyy-MM-dd_HH-mm"
$LogFile   = Join-Path $LogDir "scrape_$Timestamp.log"

Set-Location $ProjectDir

Write-Output "=== Starting weekly run: $Timestamp ===" | Tee-Object -FilePath $LogFile -Append

Write-Output "=== Pulling full schedules ===" | Tee-Object -FilePath $LogFile -Append
& $PythonExe "pull_full_schedule.py" 2>&1 | Tee-Object -FilePath $LogFile -Append

Write-Output "=== Scraping newly completed matches ===" | Tee-Object -FilePath $LogFile -Append
& $PythonExe "scrape_player_match_stats.py" 2>&1 | Tee-Object -FilePath $LogFile -Append

Write-Output "=== Backfilling any missed lineups/events ===" | Tee-Object -FilePath $LogFile -Append
& $PythonExe "backfill_lineups_events.py" 2>&1 | Tee-Object -FilePath $LogFile -Append

Write-Output "=== Pulling season aggregates ===" | Tee-Object -FilePath $LogFile -Append
& $PythonExe "pull_season_aggregates.py" 2>&1 | Tee-Object -FilePath $LogFile -Append

Write-Output "=== Recalculating rolling stats ===" | Tee-Object -FilePath $LogFile -Append
& $PythonExe "rolling_stats.py" 2>&1 | Tee-Object -FilePath $LogFile -Append

Write-Output "=== Regenerating dashboard.html ===" | Tee-Object -FilePath $LogFile -Append
& $PythonExe "generate_dashboard.py" 2>&1 | Tee-Object -FilePath $LogFile -Append

Write-Output "=== Publishing dashboard.html to GitHub Pages ===" | Tee-Object -FilePath $LogFile -Append
try {
    git add dashboard.html 2>&1 | Tee-Object -FilePath $LogFile -Append
    $changes = git diff --cached --quiet; $hasChanges = ($LASTEXITCODE -ne 0)
    if ($hasChanges) {
        git commit -m "Auto-update dashboard.html ($Timestamp)" 2>&1 | Tee-Object -FilePath $LogFile -Append
        git push 2>&1 | Tee-Object -FilePath $LogFile -Append
    } else {
        Write-Output "No changes to dashboard.html - nothing to publish." | Tee-Object -FilePath $LogFile -Append
    }
} catch {
    Write-Output "WARNING: publishing to GitHub failed: $_" | Tee-Object -FilePath $LogFile -Append
}

Write-Output "=== Done: $(Get-Date -Format 'yyyy-MM-dd_HH-mm') ===" | Tee-Object -FilePath $LogFile -Append
