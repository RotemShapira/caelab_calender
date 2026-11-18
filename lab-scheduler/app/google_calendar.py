"""
Google Calendar integration — fetches Israeli public holiday dates so the
dashboard calendar can mark them, using Google's public "Holidays in
Israel" calendar via a simple API key (no OAuth / user login needed,
since it's a publicly readable calendar).

Requires an environment variable:
    GOOGLE_API_KEY      - a Google Cloud API key with the "Google Calendar
                           API" enabled (Calendar API must be turned on
                           in the same GCP project the key belongs to).

Optional:
    GOOGLE_CALENDAR_ID  - defaults to Google's public "Holidays in Israel"
                           calendar (official holidays only). Set this to
                           "en.jewish#holiday@group.v.calendar.google.com"
                           (no ".official") if you also want minor/optional
                           observances included, not just statutory holidays.

If GOOGLE_API_KEY isn't set, or a request fails for any reason, this
degrades gracefully: it logs a warning and returns no holidays, so the
rest of the app (including the calendar UI) keeps working normally —
it just won't show holiday shading until a key is configured.
"""
import logging
import os
import time

import requests

logger = logging.getLogger("lab_scheduler.google_calendar")

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GOOGLE_CALENDAR_ID = os.getenv(
    "GOOGLE_CALENDAR_ID", "en.jewish.official#holiday@group.v.calendar.google.com"
)

_CACHE_TTL_SECONDS = 24 * 60 * 60  # holiday lists don't change intra-day
_cache: dict[int, tuple[float, dict[str, str]]] = {}


def fetch_holidays(year: int) -> dict[str, str]:
    """
    Return {"YYYY-MM-DD": "Holiday Name"} for the given calendar year.
    Results are cached in-memory per year for _CACHE_TTL_SECONDS so the
    dashboard doesn't hit the Google API on every page view.
    """
    cached = _cache.get(year)
    if cached and (time.time() - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    if not GOOGLE_API_KEY:
        logger.info("GOOGLE_API_KEY not set — holiday overlay disabled.")
        return {}

    url = f"https://www.googleapis.com/calendar/v3/calendars/{GOOGLE_CALENDAR_ID}/events"
    params = {
        "key": GOOGLE_API_KEY,
        "timeMin": f"{year}-01-01T00:00:00Z",
        "timeMax": f"{year + 1}-01-01T00:00:00Z",
        "singleEvents": "true",
        "maxResults": "250",
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # pragma: no cover - network/best effort
        logger.warning("Failed to fetch holidays from Google Calendar: %s", exc)
        # Fall back to a stale cached copy if we have one, rather than
        # flashing the calendar back to "no holidays" on a transient error.
        stale = _cache.get(year)
        return stale[1] if stale else {}

    holidays: dict[str, str] = {}
    for event in data.get("items", []):
        start = event.get("start", {})
        date_str = start.get("date") or (start.get("dateTime") or "")[:10]
        name = event.get("summary")
        if date_str and name:
            holidays[date_str] = name

    _cache[year] = (time.time(), holidays)
    return holidays
