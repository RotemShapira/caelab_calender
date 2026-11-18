"""
Conflict detection engine.

Two bookings conflict when they request the SAME equipment/experiment
and their date ranges overlap. Standard interval-overlap test:

    A.start <= B.end  AND  A.end >= B.start

Only bookings that are still "live" (pending or approved) are considered;
denied bookings are ignored since they no longer occupy the schedule.
"""
from datetime import date
from typing import Iterable, Sequence

from sqlalchemy.orm import Session

from . import models


def find_conflicts(
    db: Session,
    equipment: str,
    start_date: date,
    end_date: date,
    exclude_id: int | None = None,
    statuses: Sequence[str] = (models.STATUS_PENDING, models.STATUS_APPROVED),
) -> list[models.Booking]:
    """Return all bookings that overlap the given equipment/date range."""
    query = db.query(models.Booking).filter(
        models.Booking.equipment == equipment,
        models.Booking.status.in_(statuses),
        models.Booking.start_date <= end_date,
        models.Booking.end_date >= start_date,
    )
    if exclude_id is not None:
        query = query.filter(models.Booking.id != exclude_id)
    return query.order_by(models.Booking.start_date).all()


def all_conflicting_bookings(db: Session) -> dict[int, list[models.Booking]]:
    """
    Compute conflicts for every currently pending booking.

    Returns a dict mapping booking.id -> list of bookings it conflicts with.
    Only pending bookings are checked as the "subject", but they can
    conflict with other pending OR already-approved bookings.
    """
    pending: Iterable[models.Booking] = (
        db.query(models.Booking)
        .filter(models.Booking.status == models.STATUS_PENDING)
        .order_by(models.Booking.start_date)
        .all()
    )

    result: dict[int, list[models.Booking]] = {}
    for booking in pending:
        conflicts = find_conflicts(
            db,
            equipment=booking.equipment,
            start_date=booking.start_date,
            end_date=booking.end_date,
            exclude_id=booking.id,
            statuses=(models.STATUS_PENDING, models.STATUS_APPROVED),
        )
        if conflicts:
            result[booking.id] = conflicts
    return result
