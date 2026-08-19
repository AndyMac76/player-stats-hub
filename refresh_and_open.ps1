# refresh_and_open.ps1
#
# Desktop-shortcut entry point: runs the same pipeline as the weekly
# scheduled task (run_weekly_scrape.ps1) on demand, then opens the
# freshly-regenerated dashboard.html. Takes a few minutes - the console
# window stays open showing progress, and waits for a keypress at the end
# so it doesn't vanish before you've seen the result.

$ProjectDir = "C:\Users\andym\OneDrive\Projects\Player Stats Hub"

& (Join-Path $ProjectDir "run_weekly_scrape.ps1")

Write-Output "`n=== Opening dashboard ==="
Start-Process (Join-Path $ProjectDir "dashboard.html")

Write-Output "`nDone - press any key to close this window."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
