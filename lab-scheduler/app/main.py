import asyncio
import logging
import os
from datetime import date

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session

from . import cleanup, conflict, google_calendar, models, notifications, schemas
from .auth import require_admin
from .database import Base, SessionLocal, engine, get_db
from .models import AdminCredential
from .security import hash_password, verify_password

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("lab_scheduler")

# Create tables on startup if they don't already exist.
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Lab Experiment Scheduler")

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


def _seed_admin_credentials() -> None:
    """
    Ensures the `admin_credentials` table has an initial admin login seeded
    from ADMIN_USERNAME / ADMIN_PASSWORD environment variables.
    If the table already has a default ('admin' / 'changeme') record and the
    environment variables specify new credentials, updates the record to match.
    """
    env_username = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
    env_password = os.getenv("ADMIN_PASSWORD", "changeme")

    db = SessionLocal()
    try:
        records = db.query(AdminCredential).all()
        if not records:
            db.add(
                AdminCredential(
                    username=env_username,
                    password_hash=hash_password(env_password),
                )
            )
            db.commit()
            logger.info("Seeded initial admin credentials for user '%s'.", env_username)
        elif len(records) == 1:
            rec = records[0]
            # If the DB still holds the untouched factory default ('admin'/'changeme')
            # and the user set custom ADMIN_USERNAME/ADMIN_PASSWORD in docker-compose/.env,
            # apply the environment values automatically.
            if (
                rec.username == "admin"
                and verify_password("changeme", rec.password_hash)
                and (env_username != "admin" or env_password != "changeme")
            ):
                rec.username = env_username
                rec.password_hash = hash_password(env_password)
                db.commit()
                logger.info("Updated default admin credentials from environment for user '%s'.", env_username)
    finally:
        db.close()


@app.on_event("startup")
async def _on_startup() -> None:
    """
    Seeds admin credentials if needed and kicks off the daily background loop
    that purges denied bookings older than DENIED_RETENTION_DAYS (default 30).
    """
    _seed_admin_credentials()
    asyncio.create_task(cleanup.cleanup_loop(SessionLocal))


# ---------------------------------------------------------------------------
# Public: booking submission form
# ---------------------------------------------------------------------------
@app.get("/")
def booking_form(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "errors": None, "form_data": {}},
    )


@app.post("/bookings")
def create_booking(
    request: Request,
    user_name: str = Form(...),
    equipment: str = Form(...),
    experiment_details: str = Form(""),
    start_date: date = Form(...),
    end_date: date = Form(...),
    db: Session = Depends(get_db),
):
    form_data = {
        "user_name": user_name,
        "equipment": equipment,
        "experiment_details": experiment_details,
        "start_date": start_date,
        "end_date": end_date,
    }

    # Validate
    try:
        validated = schemas.BookingCreate(**form_data)
    except ValidationError as exc:
        errors = [err["msg"] for err in exc.errors()]
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "errors": errors, "form_data": form_data},
            status_code=422,
        )

    # Check for conflicts BEFORE inserting, so we know which status to assign.
    # (exclude_id=None since this booking doesn't exist in the DB yet.)
    conflicts = conflict.find_conflicts(
        db,
        equipment=validated.equipment,
        start_date=validated.start_date,
        end_date=validated.end_date,
        exclude_id=None,
    )
    has_conflict = len(conflicts) > 0

    # No conflict -> auto-approve. Conflict -> stays pending for admin review.
    booking = models.Booking(
        user_name=validated.user_name,
        equipment=validated.equipment,
        experiment_details=validated.experiment_details,
        start_date=validated.start_date,
        end_date=validated.end_date,
        status=models.STATUS_PENDING if has_conflict else models.STATUS_APPROVED,
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)

    if has_conflict:
        notifications.notify_conflict(booking, conflicts)

    return templates.TemplateResponse(
        "confirmation.html",
        {
            "request": request,
            "booking": booking,
            "has_conflict": has_conflict,
            "conflicts": conflicts,
        },
    )


# ---------------------------------------------------------------------------
# Public: finalized schedule dashboard
# ---------------------------------------------------------------------------
@app.get("/dashboard")
def dashboard(request: Request, db: Session = Depends(get_db)):
    approved = (
        db.query(models.Booking)
        .filter(models.Booking.status == models.STATUS_APPROVED)
        .order_by(models.Booking.start_date)
        .all()
    )
    # Plain, JSON-serializable version for the calendar JS (dates as ISO strings).
    bookings_json = [
        {
            "id": b.id,
            "equipment": b.equipment,
            "user_name": b.user_name,
            "experiment_details": b.experiment_details or "",
            "start_date": b.start_date.isoformat(),
            "end_date": b.end_date.isoformat(),
        }
        for b in approved
    ]
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "bookings": approved,
            "bookings_json": bookings_json,
            "today": date.today(),
        },
    )


# ---------------------------------------------------------------------------
# Admin: history of completed (approved + already finished) bookings
# ---------------------------------------------------------------------------
@app.get("/history")
def history(
    request: Request,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    bookings = (
        db.query(models.Booking)
        .filter(
            models.Booking.status == models.STATUS_APPROVED,
            models.Booking.end_date < date.today(),
        )
        .order_by(models.Booking.end_date.desc())
        .all()
    )

    return templates.TemplateResponse(
        "history.html",
        {
            "request": request,
            "bookings": bookings,
        },
    )


# ---------------------------------------------------------------------------
# Admin: conflict resolution panel (password protected)
# ---------------------------------------------------------------------------
@app.get("/admin")
def admin_panel(
    request: Request,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    conflicts_by_id = conflict.all_conflicting_bookings(db)

    pending = (
        db.query(models.Booking)
        .filter(models.Booking.status == models.STATUS_PENDING)
        .order_by(models.Booking.start_date)
        .all()
    )
    approved = (
        db.query(models.Booking)
        .filter(models.Booking.status == models.STATUS_APPROVED)
        .order_by(models.Booking.start_date)
        .all()
    )
    denied = (
        db.query(models.Booking)
        .filter(models.Booking.status == models.STATUS_DENIED)
        .order_by(models.Booking.updated_at.desc())
        .limit(25)
        .all()
    )

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "pending": pending,
            "approved": approved,
            "denied": denied,
            "conflicts_by_id": conflicts_by_id,
        },
    )


@app.get("/admin/settings")
def admin_settings_form(
    request: Request,
    admin: str = Depends(require_admin),
):
    return templates.TemplateResponse(
        "admin_settings.html",
        {"request": request, "admin_username": admin, "errors": None, "success": False},
    )


@app.post("/admin/settings")
def admin_settings_update(
    request: Request,
    current_password: str = Form(...),
    new_username: str = Form(...),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    db: Session = Depends(get_db),
    admin: str = Depends(require_admin),
):
    record = db.query(AdminCredential).filter(AdminCredential.username == admin).first()

    errors = []
    if record is None or not verify_password(current_password, record.password_hash):
        errors.append("Current password is incorrect.")
    if not new_username.strip():
        errors.append("New username cannot be blank.")
    if new_password and new_password != confirm_password:
        errors.append("New password and confirmation do not match.")

    if errors:
        return templates.TemplateResponse(
            "admin_settings.html",
            {"request": request, "admin_username": admin, "errors": errors, "success": False},
            status_code=422,
        )

    record.username = new_username.strip()
    if new_password:
        record.password_hash = hash_password(new_password)
    db.commit()

    return templates.TemplateResponse(
        "admin_settings.html",
        {"request": request, "admin_username": record.username, "errors": None, "success": True},
    )


@app.post("/admin/bookings/{booking_id}/approve")
def approve_booking(
    booking_id: int,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    booking = db.query(models.Booking).get(booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    booking.status = models.STATUS_APPROVED
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/bookings/{booking_id}/deny")
def deny_booking(
    booking_id: int,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    booking = db.query(models.Booking).get(booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    booking.status = models.STATUS_DENIED
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/bookings/{booking_id}/delete")
def delete_booking(
    booking_id: int,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    """
    Permanently deletes a booking. Restricted to already-denied bookings
    as a safety guard, so this can't be used to accidentally wipe a live
    pending/approved request — deny it first, then delete. Denied bookings
    are also purged automatically after DENIED_RETENTION_DAYS regardless
    (see app/cleanup.py).
    """
    booking = db.query(models.Booking).get(booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.status != models.STATUS_DENIED:
        raise HTTPException(
            status_code=400,
            detail="Only denied bookings can be deleted. Deny it first, then delete.",
        )
    db.delete(booking)
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/api/holidays")
def get_holidays(year: int | None = None):
    target_year = year if year is not None else date.today().year
    return google_calendar.fetch_holidays(target_year)


@app.get("/health")
def health_check():
    return {"status": "ok"}
