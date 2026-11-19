"""
Open-access Israeli holidays importer + database-backed holiday store.

Fetches base Israeli public holidays without needing any API key using:
  1. Hebcal Open JSON API (Israel schedule: i=on, major + modern holidays)
  2. Fallback: Nager.Date Open Public Holidays API (country IL)
  3. Fallback: Google Calendar public iCalendar (.ics) feed for Israel

Imported holidays are saved into the `holidays` SQLite table so the Admin
can view, add, rename, or delete any holiday manually at `/admin/holidays`.
"""
import logging
import re
from datetime import date
from sqlalchemy.orm import Session

import requests

from .models import Holiday

logger = logging.getLogger("lab_scheduler.holidays")


def _fetch_from_hebcal(year: int) -> dict[str, str]:
    url = "https://www.hebcal.com/hebcal"
    params = {
        "v": "1",
        "cfg": "json",
        "maj": "on",
        "min": "off",
        "mod": "on",
        "nx": "off",
        "year": str(year),
        "month": "x",
        "ss": "off",
        "mf": "off",
        "c": "off",
        "geo": "none",
        "i": "on",  # Israel holiday schedule
    }
    resp = requests.get(url, params=params, timeout=12)
    resp.raise_for_status()
    data = resp.json()
    results: dict[str, str] = {}
    for item in data.get("items", []):
        if item.get("category") != "holiday":
            continue
        date_str = (item.get("date") or "")[:10]
        title = (item.get("title") or "").strip()
        if date_str and title:
            if date_str in results:
                if title not in results[date_str]:
                    results[date_str] = f"{results[date_str]} / {title}"
            else:
                results[date_str] = title
    return results


def _fetch_from_nager(year: int) -> dict[str, str]:
    url = f"https://date.nager.at/api/v3/PublicHolidays/{year}/IL"
    resp = requests.get(url, timeout=12)
    resp.raise_for_status()
    items = resp.json()
    results: dict[str, str] = {}
    for item in items:
        date_str = (item.get("date") or "")[:10]
        name = (item.get("name") or item.get("localName") or "").strip()
        if date_str and name:
            results[date_str] = name
    return results


def _fetch_from_public_ics(year: int) -> dict[str, str]:
    url = (
        "https://calendar.google.com/calendar/ical/"
        "en.jewish.official%23holiday%40group.v.calendar.google.com/public/basic.ics"
    )
    resp = requests.get(url, timeout=12)
    resp.raise_for_status()
    text = resp.text.replace("\r\n ", "").replace("\n ", "")
    results: dict[str, str] = {}
    year_prefix = f"{year}"
    for block in text.split("BEGIN:VEVENT")[1:]:
        dt_match = re.search(r"DTSTART[^:]*:(\d{8})", block)
        sum_match = re.search(r"SUMMARY[^:]*:([^\r\n]+)", block)
        if dt_match and sum_match:
            raw_d = dt_match.group(1)
            if raw_d.startswith(year_prefix):
                iso = f"{raw_d[0:4]}-{raw_d[4:6]}-{raw_d[6:8]}"
                name = sum_match.group(1).replace("\\,", ",").strip()
                results[iso] = name
    return results


def fetch_open_israeli_holidays(year: int) -> tuple[dict[str, str], str]:
    """
    Returns ({'YYYY-MM-DD': 'Holiday Name'}, source_name) from open-access calendars.
    """
    if not (2020 <= year <= 2100):
        return {}, "none"

    for source_name, fn in (
        ("Hebcal Open Israel Calendar", _fetch_from_hebcal),
        ("Nager.Date Public Holidays (IL)", _fetch_from_nager),
        ("Google Public Israel Holidays ICS", _fetch_from_public_ics),
    ):
        try:
            data = fn(year)
            if data:
                return data, source_name
        except Exception as exc:
            logger.warning("Holiday import from %s failed: %s", source_name, exc)

    return {}, "unavailable"


def sync_israeli_holidays_to_db(db: Session, year: int) -> tuple[int, int, str]:
    """
    Imports open-access Israeli holidays for `year` into the `holidays` table.
    NEVER deletes existing holidays from any year, NEVER overwrites manual
    edits/renames, and NEVER re-adds dates previously deleted (`is_deleted=True`).
    Returns (added_count, skipped_count, source_name).
    """
    fetched, source_name = fetch_open_israeli_holidays(year)
    if not fetched:
        return 0, 0, source_name

    added = 0
    skipped = 0
    for iso_date, name in sorted(fetched.items()):
        try:
            d = date.fromisoformat(iso_date)
        except ValueError:
            continue
        # Check ALL rows (including manually edited and manually deleted rows)
        # so an import never overwrites or resurrects manual changes.
        existing = db.query(Holiday).filter(Holiday.date == d).first()
        if existing is not None:
            skipped += 1
            continue
        db.add(Holiday(date=d, name=name[:160], source="auto", is_deleted=False))
        added += 1

    if added:
        db.commit()
    return added, skipped, source_name


def get_holidays_for_year(db: Session, year: int) -> dict[str, str]:
    """Return {'YYYY-MM-DD': 'Holiday Name'} of active (non-deleted) holidays for `year`."""
    if not (2020 <= year <= 2100):
        return {}
    start_d = date(year, 1, 1)
    end_d = date(year, 12, 31)
    rows = (
        db.query(Holiday)
        .filter(
            Holiday.date >= start_d,
            Holiday.date <= end_d,
            Holiday.is_deleted == False,  # noqa: E712
        )
        .order_by(Holiday.date)
        .all()
    )
    return {row.date.isoformat(): row.name for row in rows}
