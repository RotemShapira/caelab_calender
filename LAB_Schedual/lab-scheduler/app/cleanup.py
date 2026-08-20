"""
Housekeeping for denied bookings.

Denied requests stick around briefly so the admin can review recent
decisions, but there's no reason to keep them forever. This module:

  - Deletes denied bookings whose `updated_at` (the time they were denied)
    is older than DENIED_RETENTION_DAYS (default 30, configurable via env).
  - Runs automatically once a day in the background (wired up in
    main.py's startup event via cleanup_loop).
  - Also backs the "Delete" button in the admin panel for removing a
    single denied booking immediately, without waiting 30 days.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from . import models

logger = logging.getLogger("lab_scheduler.cleanup")

DENIED_RETENTION_DAYS = int(os.getenv("DENIED_RETENTION_DAYS", "30"))
_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60  # check once a day


def purge_old_denied_bookings(db: Session, days: int = DENIED_RETENTION_DAYS) -> int:
    """Delete denied bookings older than `days`. Returns the number deleted."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    old = (
        db.query(models.Booking)
        .filter(
            models.Booking.status == models.STATUS_DENIED,
            models.Booking.updated_at < cutoff,
        )
        .all()
    )
    count = len(old)
    for booking in old:
        db.delete(booking)
    if count:
        db.commit()
        logger.info("Purged %d denied booking(s) older than %d days.", count, days)
    return count


async def cleanup_loop(session_factory) -> None:
    """Runs for the lifetime of the app: purge once at startup, then daily."""
    while True:
        db = session_factory()
        try:
            purge_old_denied_bookings(db)
        except Exception:  # pragma: no cover - never let this crash the app
            logger.exception("Error while purging old denied bookings.")
        finally:
            db.close()
        await asyncio.sleep(_CLEANUP_INTERVAL_SECONDS)
