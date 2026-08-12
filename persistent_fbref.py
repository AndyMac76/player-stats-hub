"""
persistent_fbref.py

Patches soccerdata's FBref driver initialization to use a persistent
Chrome profile directory, so Cloudflare's "verify you are human"
clearance can carry over between runs instead of resetting every time.

NOTE: this patches soccerdata.FBref directly rather than subclassing it.
Subclassing was tried first and broke league lookups entirely -
soccerdata's internal _all_leagues() keys off cls.__name__ to match
entries in its LEAGUE_DICT, and a subclass has a different __name__, so
every league except the hardcoded "Big 5 European Leagues Combined"
fallback silently disappeared. Patching the real FBref class in place
avoids that problem since the class name never changes.

Usage: import this module BEFORE creating any soccerdata.FBref()
instances - the patch is applied at import time, as a side effect of
importing this file. Then just use soccerdata.FBref(...) as normal.

If a match fails partway through a scrape and you suspect the driver
has died, call rebuild_driver(fbref) directly rather than relying on
soccerdata's own retry loop to recover on its own.
"""

import time
from pathlib import Path

import seleniumbase as sb
import soccerdata as sd

PROFILE_DIR = str(Path(__file__).resolve().parent / "chrome_profile")

# How many times to retry building a fresh driver if Chrome hasn't
# released the persistent profile's lock file yet, and how long to
# wait between attempts.
BUILD_ATTEMPTS = 3
BUILD_RETRY_DELAY = 3


def _build_driver(headless, path_to_browser):
    """Start a fresh Chrome driver against the persistent profile,
    retrying if the profile lock hasn't cleared yet."""
    last_error = None
    for attempt in range(1, BUILD_ATTEMPTS + 1):
        try:
            return sb.Driver(
                uc=True,
                headless=headless,
                binary_location=path_to_browser,
                user_data_dir=PROFILE_DIR,
            )
        except Exception as e:
            last_error = e
            print(
                f"    [persistent_fbref] Driver build attempt "
                f"{attempt}/{BUILD_ATTEMPTS} failed: {e}"
            )
            time.sleep(BUILD_RETRY_DELAY)

    # Every attempt failed - raise the last error so it's visible instead
    # of leaving the FBref instance with a missing/broken _driver.
    raise last_error


def _patched_init_webdriver(self):
    if hasattr(self, "_driver"):
        try:
            self._driver.quit()
        except Exception as e:
            # A dead/crashed browser can raise on quit() - that's fine,
            # we're replacing it anyway. Just don't let it block the rebuild.
            print(f"    [persistent_fbref] Ignoring error while quitting old driver: {e}")
        time.sleep(BUILD_RETRY_DELAY)

    return _build_driver(self.headless, self.path_to_browser)


sd.FBref._init_webdriver = _patched_init_webdriver


def rebuild_driver(fbref):
    """Force-rebuild the driver on an existing FBref instance.

    Use this from a scraping loop after a match fails, instead of trusting
    soccerdata's internal retry alone - it guarantees fbref._driver ends up
    valid (or raises clearly) no matter what state it started in.
    """
    fbref._driver = fbref._init_webdriver()
    return fbref._driver