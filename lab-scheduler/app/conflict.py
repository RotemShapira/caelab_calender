"""
Conflict detection engine using continuous (date, time) intervals.

Two bookings conflict ONLY when:
  1. They request the exact SAME device / potentiostat channel (`equipment`), AND
  2. Their continuous intervals overlap:
       A_start < B_end  AND  A_end > B_start
     where each endpoint is `(date, minutes_from_midnight)`
     (All-Day bookings use `start_min = 0` and `end_min = 1440`).

Also enforces the per-user Potentiostat concurrency limit:
  - A single user may hold at most 3 concurrent potentiostat channels at any
    given moment. Attempting to book a 4th+ overlapping channel flags the
    request as a conflict (saved as `pending` for Lab Manager / Admin review).
"""
from datetime import date
from typing import Iterable, Sequence

from sqlalchemy.orm import Session

from . import models

MAX_CONCURRENT_POTENTIOSTAT_CHANNELS = 3


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


def _to_interval(start_date: date, end_date: date, start_time: str | None, end_time: str | None):
    return (
        (start_date, _time_to_minutes(start_time, 0)),
        (end_date, _time_to_minutes(end_time, 24 * 60)),
    )


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
    a_start, a_end = _to_interval(start_date_a, end_date_a, start_time_a, end_time_a)
    b_start, b_end = _to_interval(start_date_b, end_date_b, start_time_b, end_time_b)
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


def check_user_potentiostat_channel_limit(
    db: Session,
    user_name: str,
    start_date: date,
    end_date: date,
    start_time: str | None = None,
    end_time: str | None = None,
    exclude_id: int | None = None,
    limit: int = MAX_CONCURRENT_POTENTIOSTAT_CHANNELS,
) -> tuple[bool, list[models.Booking], int]:
    """
    Checks whether adding a potentiostat channel booking for `user_name` over
    [start_date/start_time, end_date/end_time) would exceed `limit` (3)
    concurrent potentiostat channels at any point in time.

    Returns (exceeds_limit, overlapping_user_pot_bookings, peak_channel_count_with_new).
    """
    req_start, req_end = _to_interval(start_date, end_date, start_time, end_time)
    query = db.query(models.Booking).filter(
        models.Booking.booking_type == models.BOOKING_TYPE_POTENTIOSTAT,
        models.Booking.user_name == user_name,
        models.Booking.status.in_((models.STATUS_PENDING, models.STATUS_APPROVED)),
        models.Booking.start_date <= end_date,
        models.Booking.end_date >= start_date,
    )
    if exclude_id is not None:
        query = query.filter(models.Booking.id != exclude_id)

    candidates = query.order_by(models.Booking.start_date, models.Booking.start_time).all()
    overlapping = [
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
    if not overlapping:
        return False, [], 1

    # Sweep-line over clipped endpoints; -1 sorts before +1 at identical points (half-open intervals)
    events: list[tuple[tuple[date, int], int]] = []
    for b in overlapping:
        b_start, b_end = _to_interval(b.start_date, b.end_date, b.start_time, b.end_time)
        clipped_start = max(req_start, b_start)
        clipped_end = min(req_end, b_end)
        if clipped_start < clipped_end:
            events.append((clipped_start, 1))
            events.append((clipped_end, -1))

    events.sort(key=lambda e: (e[0], e[1]))
    cur = 0
    max_existing = 0
    for _, delta in events:
        cur += delta
        if cur > max_existing:
            max_existing = cur

    peak_with_new = max_existing + 1
    return peak_with_new > limit, overlapping, peak_with_new


def all_conflicting_bookings(db: Session) -> dict[int, list[models.Booking]]:
    """
    Compute same-device conflicts for every currently pending booking.
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


def all_potentiostat_limit_violations(db: Session) -> dict[int, list[models.Booking]]:
    """
    For every pending potentiostat booking, returns mapping booking.id -> list
    of that user's overlapping potentiostat bookings when the 3-channel limit is exceeded.
    """
    pending: Iterable[models.Booking] = (
        db.query(models.Booking)
        .filter(
            models.Booking.status == models.STATUS_PENDING,
            models.Booking.booking_type == models.BOOKING_TYPE_POTENTIOSTAT,
        )
        .order_by(models.Booking.start_date, models.Booking.start_time)
        .all()
    )
    result: dict[int, list[models.Booking]] = {}
    for booking in pending:
        exceeds, overlapping, _ = check_user_potentiostat_channel_limit(
            db,
            user_name=booking.user_name,
            start_date=booking.start_date,
            end_date=booking.end_date,
            start_time=booking.start_time,
            end_time=booking.end_time,
            exclude_id=booking.id,
        )
        if exceeds:
            result[booking.id] = overlapping
    return result
