"""
Conflict detection engine.

Two bookings conflict ONLY when:
  1. They request the exact SAME device (`equipment`), AND
  2. Their date ranges overlap: `A.start_date <= B.end_date AND A.end_date >= B.start_date`, AND
  3. Their daily time ranges overlap: `A.start_min < B.end_min AND A.end_min > B.start_min`
     (where All-Day bookings occupy 00:00..24:00, i.e. 0..1440 minutes).

Back-to-back slots (e.g. 09:00-12:00 and 12:00-15:00) do NOT conflict.
Bookings on different devices NEVER conflict.
Only live bookings (`pending` or `approved`) are considered; `denied` bookings are ignored.
"""
from datetime import date
from typing import Iterable, Sequence

from sqlalchemy.orm import Session

from . import models


def _time_to_minutes(t: str | None, default_minutes: int) -> int:
    if not t:
        return default_minutes
    try:
        h, m = t.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return default_minutes


def times_overlap(
    start_time_a: str | None,
    end_time_a: str | None,
    start_time_b: str | None,
    end_time_b: str | None,
) -> bool:
    a_start = _time_to_minutes(start_time_a, 0)
    a_end = _time_to_minutes(end_time_a, 24 * 60)
    b_start = _time_to_minutes(start_time_b, 0)
    b_end = _time_to_minutes(end_time_b, 24 * 60)
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
    """Return all bookings on the same device whose date AND time ranges overlap."""
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
        if times_overlap(start_time, end_time, b.start_time, b.end_time)
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
