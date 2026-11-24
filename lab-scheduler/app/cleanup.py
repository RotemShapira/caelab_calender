"""
Background housekeeping tasks:
  1. Night-before booking confirmation reminders (sent starting at REMINDER_HOUR,
     default 18:00 local lab time, the evening before `start_date`).
  2. Automatic deletion of approved bookings that were NOT confirmed before
     their starting date & time (freeing the slot for other lab members).
  3. Daily purge of denied bookings older than DENIED_RETENTION_DAYS (default 30).
"""
import asyncio
import logging
import os
import secrets
import time
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from . import models, notifications

logger = logging.getLogger("lab_scheduler.cleanup")

DENIED_RETENTION_DAYS = int(os.getenv("DENIED_RETENTION_DAYS", "30"))
REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "18"))  # 18:00 (6:00 PM) the night before
_TICK_INTERVAL_SECONDS = 5 * 60  # check reminders & expired unconfirmed bookings every 5 minutes
_PURGE_INTERVAL_SECONDS = 24 * 60 * 60


def booking_start_datetime(booking: models.Booking) -> datetime:
    """Returns the booking's local starting datetime (HH:MM or 00:00 for All Day)."""
    hh, mm = 0, 0
    if booking.start_time and ":" in booking.start_time:
        try:
            parts = booking.start_time.split(":")
            hh, mm = int(parts[0]), int(parts[1])
        except ValueError:
            hh, mm = 0, 0
    return datetime(booking.start_date.year, booking.start_date.month, booking.start_date.day, hh, mm)


def send_due_booking_reminders(db: Session) -> int:
    """
    Sends night-before confirmation reminders for upcoming unconfirmed approved bookings:
      - Bookings starting tomorrow once local time reaches REMINDER_HOUR (default 18:00)
      - Bookings starting today that haven't started yet and didn't get a reminder yet
    """
    now = datetime.now()
    today = now.date()
    tomorrow = today + timedelta(days=1)

    target_dates = [today]
    if now.hour >= REMINDER_HOUR:
        target_dates.append(tomorrow)

    due = (
        db.query(models.Booking)
        .filter(
            models.Booking.status == models.STATUS_APPROVED,
            models.Booking.confirmed_at.is_(None),
            models.Booking.reminder_sent_at.is_(None),
            models.Booking.start_date.in_(target_dates),
        )
        .all()
    )

    if not due:
        return 0

    base_url = notifications.get_app_base_url(db)
    sent_count = 0
    for b in due:
        start_dt = booking_start_datetime(b)
        if now >= start_dt:
            # Already past start time; auto-delete handler will clean it up.
            continue
        if not b.confirm_token:
            b.confirm_token = secrets.token_urlsafe(32)
        b.reminder_sent_at = now
        db.commit()

        confirm_url = f"{base_url}/bookings/confirm/{b.confirm_token}" if base_url else ""
        notifications.notify_booking_reminder(b, confirm_url, db)
        sent_count += 1

    if sent_count:
        logger.info("Processed %d booking confirmation reminder(s).", sent_count)
    return sent_count


def delete_unconfirmed_started_bookings(db: Session) -> int:
    """
    Permanently deletes approved bookings that were NOT confirmed before
    their starting date & time, and emails the user.
    """
    now = datetime.now()
    today = now.date()

    candidates = (
        db.query(models.Booking)
        .filter(
            models.Booking.status == models.STATUS_APPROVED,
            models.Booking.confirmed_at.is_(None),
            models.Booking.start_date <= today,
        )
        .all()
    )

    deleted_count = 0
    for b in candidates:
        start_dt = booking_start_datetime(b)
        if now >= start_dt:
            logger.info(
                "Auto-deleting unconfirmed booking #%d (%s on %s, start %s).",
                b.id,
                b.user_name,
                b.equipment,
                start_dt.isoformat(),
            )
            notifications.notify_booking_auto_deleted(b, db)
            db.delete(b)
            deleted_count += 1

    if deleted_count:
        db.commit()
    return deleted_count


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
    """Runs for the lifetime of the app: reminders & expiry every 5m, denied purge daily."""
    last_purge = 0.0
    while True:
        db = session_factory()
        try:
            send_due_booking_reminders(db)
            delete_unconfirmed_started_bookings(db)
            if time.time() - last_purge >= _PURGE_INTERVAL_SECONDS:
                purge_old_denied_bookings(db)
                last_purge = time.time()
        except Exception:  # pragma: no cover - never let background loop crash the app
            logger.exception("Error in background housekeeping loop.")
        finally:
            db.close()
        await asyncio.sleep(_TICK_INTERVAL_SECONDS)
