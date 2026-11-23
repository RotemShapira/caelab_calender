"""
Conflict detection engine using continuous (date, time) intervals.

Two bookings conflict ONLY when:
  1. They request the exact SAME device / potentiostat channel (`equipment`), AND
  2. Their continuous intervals overlap:
       A_start < B_end  AND  A_end > B_start
     where each endpoint is `(date, minutes_from_midnight)`
     (All-Day bookings use `start_min = 0` and `end_min = 1440`).

Back-to-back bookings (e.g. 09:00-12:00 and 12:00-15:00, or Mon 14:00 -> Tue 10:00
and Tue 10:00 -> Tue 14:00) do NOT conflict.
Bookings on different devices or different potentiostat channels NEVER conflict.
Only live bookings (`pending` or `approved`) are considered; `denied` bookings are ignored.
"""
from datetime import date
from typing import Iterable, Sequence

from sqlalchemy.orm import Session

from . import models


def _time_to_minutes(t: str | None, default_minutes: int) -> int:
    if not t:
        return default_minutes
    if t == "24:00":
        return 24 * 60
    try:
        h, m = t.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return default_minutes


def intervals_overlap(
    start_date_a: date,
    end_date_a: date,
    start_time_a: str | None,
    end_time_a: str | None,
    start_date_b: date,
    end_date_b: date,
    start_time_b: str | None,
    end_time_b: str | None,
) -> bool:
    a_start = (start_date_a, _time_to_minutes(start_time_a, 0))
    a_end = (end_date_a, _time_to_minutes(end_time_a, 24 * 60))
    b_start = (start_date_b, _time_to_minutes(start_time_b, 0))
    b_end = (end_date_b, _time_to_minutes(end_time_b, 24 * 60))
    return a_start < b_end and a_end > b_start


def find_conflicts(
    db: Session,
    equipment: str,
    start_date: date,
    end_date: date,
    start_time: str | None = None,
    end_time: str | None = None,
    exclude_id: int | None = None,
    statuses: Sequence[str] = (models.STATUS_PENDING, models.STATUS_APPROVED),
) -> list[models.Booking]:
    """Return all live bookings on the same device whose continuous (date, time) intervals overlap."""
    query = db.query(models.Booking).filter(
        models.Booking.equipment == equipment,
        models.Booking.status.in_(statuses),
        models.Booking.start_date <= end_date,
        models.Booking.end_date >= start_date,
    )
    if exclude_id is not None:
        query = query.filter(models.Booking.id != exclude_id)

    candidates = query.order_by(models.Booking.start_date, models.Booking.start_time).all()
    return [
        b
        for b in candidates
        if intervals_overlap(
            start_date,
            end_date,
            start_time,
            end_time,
            b.start_date,
            b.end_date,
            b.start_time,
            b.end_time,
        )
    ]


def all_conflicting_bookings(db: Session) -> dict[int, list[models.Booking]]:
    """
    Compute conflicts for every currently pending booking.
    Returns a dict mapping booking.id -> list of bookings it conflicts with.
    """
    pending: Iterable[models.Booking] = (
        db.query(models.Booking)
        .filter(models.Booking.status == models.STATUS_PENDING)
        .order_by(models.Booking.start_date, models.Booking.start_time)
        .all()
    )

    result: dict[int, list[models.Booking]] = {}
    for booking in pending:
        conflicts = find_conflicts(
            db,
            equipment=booking.equipment,
            start_date=booking.start_date,
            end_date=booking.end_date,
            start_time=booking.start_time,
            end_time=booking.end_time,
            exclude_id=booking.id,
            statuses=(models.STATUS_PENDING, models.STATUS_APPROVED),
        )
        if conflicts:
            result[booking.id] = conflicts
    return result
