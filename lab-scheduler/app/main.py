import asyncio
import logging
import os
from datetime import date
from urllib.parse import quote_plus

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import cleanup, conflict, holidays, models, notifications, schemas
from .auth import (
    LoginRequiredException,
    SESSION_COOKIE_NAME,
    get_client_ip,
    get_current_session,
    require_admin,
    require_user,
    verify_same_origin,
)
from .database import Base, SessionLocal, engine, get_db
from .models import AdminCredential, AppSetting, Booking, Device, Holiday, User
from .security import (
    SESSION_TTL_SECONDS,
    clear_failed_attempts,
    create_session_token,
    hash_password,
    is_rate_limited,
    record_failed_attempt,
    set_session_secret,
    verify_password,
    verify_password_constant_time,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("lab_scheduler")


def _auto_migrate_schema() -> None:
    """
    Safely creates new tables and adds new columns to existing SQLite tables
    in-place so future updates never delete or reset existing lab data.
    """
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        cols = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(bookings)")).fetchall()
        }
        if "start_time" not in cols:
            conn.execute(text("ALTER TABLE bookings ADD COLUMN start_time VARCHAR(5)"))
            logger.info("Added start_time column to bookings table.")
        if "end_time" not in cols:
            conn.execute(text("ALTER TABLE bookings ADD COLUMN end_time VARCHAR(5)"))
            logger.info("Added end_time column to bookings table.")


_auto_migrate_schema()

# Disable public Swagger/OpenAPI endpoints (/docs, /redoc, /openapi.json).
app = FastAPI(
    title="Lab Experiment Scheduler",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.middleware("http")
async def security_and_user_context_middleware(request: Request, call_next):
    request.state.user = get_current_session(request)
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "script-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "frame-ancestors 'none'"
    )
    return response


@app.exception_handler(LoginRequiredException)
async def _login_redirect_handler(request: Request, exc: LoginRequiredException):
    return RedirectResponse(url="/login", status_code=303)


def _get_allowed_usernames(db: Session) -> list[str]:
    user_names = [u.username for u in db.query(User).order_by(User.username).all()]
    admin_names = [a.username for a in db.query(AdminCredential).order_by(AdminCredential.username).all()]
    return sorted(set(user_names + admin_names), key=str.lower)


def _seed_startup_state() -> None:
    """
    1. Ensures `admin_credentials` has an initial admin login seeded from env vars.
    2. Initializes persistent `session_secret` in `app_settings`.
    3. Seeds `devices` from existing bookings if `devices` is empty.
    4. Auto-imports open-access Israeli holidays for the current year if `holidays` is empty.
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
            if (
                rec.username == "admin"
                and verify_password("changeme", rec.password_hash)
                and (env_username != "admin" or env_password != "changeme")
            ):
                rec.username = env_username
                rec.password_hash = hash_password(env_password)
                db.commit()
                logger.info("Updated default admin credentials from environment for user '%s'.", env_username)

        secret_row = db.query(AppSetting).filter(AppSetting.key == "session_secret").first()
        if secret_row is None or not secret_row.value:
            secret_val = os.urandom(32).hex()
            if secret_row is None:
                db.add(AppSetting(key="session_secret", value=secret_val))
            else:
                secret_row.value = secret_val
            db.commit()
        else:
            secret_val = secret_row.value
        set_session_secret(secret_val)

        # If devices table is empty, migrate any distinct equipment names from existing bookings
        if db.query(Device).count() == 0:
            existing_eq = [
                row[0]
                for row in db.query(Booking.equipment).distinct().all()
                if row[0] and row[0].strip()
            ]
            for eq in sorted(set(existing_eq)):
                db.add(Device(name=eq.strip()))
            if existing_eq:
                db.commit()
                logger.info("Seeded %d device(s) from existing bookings.", len(existing_eq))

        # If holidays table is empty, pre-populate current year's open-access Israeli holidays
        if db.query(Holiday).count() == 0:
            added, _, src = holidays.sync_israeli_holidays_to_db(db, date.today().year)
            if added:
                logger.info("Auto-imported %d Israeli holidays for %d from %s.", added, date.today().year, src)
    finally:
        db.close()


@app.on_event("startup")
async def _on_startup() -> None:
    _seed_startup_state()
    asyncio.create_task(cleanup.cleanup_loop(SessionLocal))


# ---------------------------------------------------------------------------
# User & Admin Login / Logout / Self-Service Password Change
# ---------------------------------------------------------------------------
@app.get("/login")
def login_page(request: Request):
    if getattr(request.state, "user", None):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": None, "username": ""},
    )


@app.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    verify_same_origin(request)
    client_ip = get_client_ip(request)
    clean_user = username.strip()

    if is_rate_limited(client_ip):
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "Too many failed login attempts. Please wait 5 minutes and try again.",
                "username": clean_user,
            },
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    admin_rec = db.query(AdminCredential).filter(AdminCredential.username == clean_user).first()
    user_rec = db.query(User).filter(User.username == clean_user).first()

    admin_ok = verify_password_constant_time(
        password, admin_rec.password_hash if admin_rec else None
    )
    user_ok = verify_password_constant_time(
        password, user_rec.password_hash if user_rec else None
    )

    if not (admin_ok or user_ok):
        record_failed_attempt(client_ip)
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "Invalid username or password.",
                "username": clean_user,
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    clear_failed_attempts(client_ip)
    role = "admin" if admin_ok else "user"
    matched_username = admin_rec.username if admin_ok else user_rec.username
    token = create_session_token(matched_username, role)

    is_https = request.headers.get("x-forwarded-proto", "").lower() == "https"
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=is_https,
    )
    return resp


@app.post("/logout")
def logout(request: Request):
    verify_same_origin(request)
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE_NAME)
    return resp


@app.get("/account")
def account_page(
    request: Request,
    current_user: dict = Depends(require_user),
):
    if current_user["role"] == "admin":
        return RedirectResponse(url="/admin/settings", status_code=303)
    return templates.TemplateResponse(
        "account.html",
        {
            "request": request,
            "username": current_user["username"],
            "errors": None,
            "success": False,
        },
    )


@app.post("/account")
def account_update_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_user),
):
    if current_user["role"] == "admin":
        return RedirectResponse(url="/admin/settings", status_code=303)

    record = db.query(User).filter(User.username == current_user["username"]).first()
    errors = []
    if record is None or not verify_password_constant_time(current_password, record.password_hash):
        errors.append("Current password is incorrect.")
    if len(new_password) < 4:
        errors.append("New password must be at least 4 characters.")
    if new_password != confirm_password:
        errors.append("New password and confirmation do not match.")

    if errors:
        return templates.TemplateResponse(
            "account.html",
            {
                "request": request,
                "username": current_user["username"],
                "errors": errors,
                "success": False,
            },
            status_code=422,
        )

    record.password_hash = hash_password(new_password)
    db.commit()
    return templates.TemplateResponse(
        "account.html",
        {
            "request": request,
            "username": current_user["username"],
            "errors": None,
            "success": True,
        },
    )


# ---------------------------------------------------------------------------
# Authenticated Lab Member: booking submission form & dashboard
# ---------------------------------------------------------------------------
@app.get("/")
def booking_form(
    request: Request,
    db: Session = Depends(get_db),
    _user: dict = Depends(require_user),
):
    devices = db.query(Device).order_by(Device.name).all()
    usernames = _get_allowed_usernames(db)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "errors": None,
            "form_data": {},
            "devices": devices,
            "usernames": usernames,
        },
    )


@app.post("/bookings")
def create_booking(
    request: Request,
    user_name: str = Form(...),
    equipment: str = Form(...),
    experiment_details: str = Form(""),
    start_date: date = Form(...),
    end_date: date = Form(...),
    use_hours: str = Form(""),
    start_time: str = Form(""),
    end_time: str = Form(""),
    db: Session = Depends(get_db),
    _user: dict = Depends(require_user),
):
    devices = db.query(Device).order_by(Device.name).all()
    device_names = {d.name for d in devices}
    usernames = _get_allowed_usernames(db)

    effective_start_time = start_time.strip() if use_hours else ""
    effective_end_time = end_time.strip() if use_hours else ""

    form_data = {
        "user_name": user_name,
        "equipment": equipment,
        "experiment_details": experiment_details,
        "start_date": start_date,
        "end_date": end_date,
        "use_hours": bool(use_hours),
        "start_time": effective_start_time,
        "end_time": effective_end_time,
    }

    errors: list[str] = []
    if user_name.strip() not in usernames:
        errors.append("Please select a valid user from the dropdown list.")
    if not devices:
        errors.append("No devices are configured yet. Ask the lab admin to add devices in the Admin panel.")
    elif equipment.strip() not in device_names:
        errors.append("Please select a valid device from the dropdown list.")

    validated = None
    if not errors:
        try:
            validated = schemas.BookingCreate(
                user_name=user_name,
                equipment=equipment,
                experiment_details=experiment_details,
                start_date=start_date,
                end_date=end_date,
                start_time=effective_start_time or None,
                end_time=effective_end_time or None,
            )
        except ValidationError as exc:
            errors = [err["msg"] for err in exc.errors()]

    if errors or validated is None:
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "errors": errors,
                "form_data": form_data,
                "devices": devices,
                "usernames": usernames,
            },
            status_code=422,
        )

    conflicts = conflict.find_conflicts(
        db,
        equipment=validated.equipment,
        start_date=validated.start_date,
        end_date=validated.end_date,
        start_time=validated.start_time,
        end_time=validated.end_time,
        exclude_id=None,
    )
    has_conflict = len(conflicts) > 0

    booking = models.Booking(
        user_name=validated.user_name,
        equipment=validated.equipment,
        experiment_details=validated.experiment_details,
        start_date=validated.start_date,
        end_date=validated.end_date,
        start_time=validated.start_time,
        end_time=validated.end_time,
        status=models.STATUS_PENDING if has_conflict else models.STATUS_APPROVED,
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)

    if has_conflict:
        notifications.notify_conflict(booking, conflicts, db=db)

    return templates.TemplateResponse(
        "confirmation.html",
        {
            "request": request,
            "booking": booking,
            "has_conflict": has_conflict,
            "conflicts": conflicts,
        },
    )


@app.get("/dashboard")
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    _user: dict = Depends(require_user),
):
    approved = (
        db.query(models.Booking)
        .filter(models.Booking.status == models.STATUS_APPROVED)
        .order_by(models.Booking.start_date, models.Booking.start_time)
        .all()
    )
    bookings_json = [
        {
            "id": b.id,
            "equipment": b.equipment,
            "user_name": b.user_name,
            "experiment_details": b.experiment_details or "",
            "start_date": b.start_date.isoformat(),
            "end_date": b.end_date.isoformat(),
            "start_time": b.start_time,
            "end_time": b.end_time,
            "time_label": b.time_label,
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
# Admin: history of completed bookings
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
# Admin: conflict resolution, devices, users, and notification settings
# ---------------------------------------------------------------------------
@app.get("/admin")
def admin_panel(
    request: Request,
    msg: str | None = None,
    err: str | None = None,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    conflicts_by_id = conflict.all_conflicting_bookings(db)

    pending = (
        db.query(models.Booking)
        .filter(models.Booking.status == models.STATUS_PENDING)
        .order_by(models.Booking.start_date, models.Booking.start_time)
        .all()
    )
    approved = (
        db.query(models.Booking)
        .filter(models.Booking.status == models.STATUS_APPROVED)
        .order_by(models.Booking.start_date, models.Booking.start_time)
        .all()
    )
    denied = (
        db.query(models.Booking)
        .filter(models.Booking.status == models.STATUS_DENIED)
        .order_by(models.Booking.updated_at.desc())
        .limit(25)
        .all()
    )
    users = db.query(User).order_by(User.username).all()
    devices = db.query(Device).order_by(Device.name).all()
    notif_cfg = notifications.get_notification_config(db)

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "pending": pending,
            "approved": approved,
            "denied": denied,
            "conflicts_by_id": conflicts_by_id,
            "users": users,
            "devices": devices,
            "notif_cfg": notif_cfg,
            "admin_msg": msg,
            "admin_err": err,
        },
    )


@app.post("/admin/notifications")
def admin_save_notifications(
    notify_email_to: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: str = Form("587"),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    smtp_from: str = Form(""),
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    notifications.set_setting(db, notifications.SETTING_NOTIFY_EMAIL_TO, notify_email_to)
    notifications.set_setting(db, notifications.SETTING_SMTP_HOST, smtp_host)
    notifications.set_setting(db, notifications.SETTING_SMTP_PORT, smtp_port or "587")
    notifications.set_setting(db, notifications.SETTING_SMTP_USER, smtp_user)
    if smtp_password.strip():
        notifications.set_setting(db, notifications.SETTING_SMTP_PASSWORD, smtp_password)
    notifications.set_setting(db, notifications.SETTING_SMTP_FROM, smtp_from)
    db.commit()
    return RedirectResponse(
        url=f"/admin?msg={quote_plus('Collision email notification settings saved.')}",
        status_code=303,
    )


@app.post("/admin/notifications/test")
def admin_test_notification(
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    ok, detail = notifications.send_test_email(db)
    param = "msg" if ok else "err"
    return RedirectResponse(url=f"/admin?{param}={quote_plus(detail)}", status_code=303)


@app.post("/admin/devices/create")
def admin_create_device(
    name: str = Form(...),
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    clean_name = name.strip()
    if not clean_name or len(clean_name) > 120:
        return RedirectResponse(
            url=f"/admin?err={quote_plus('Device name must be 1 to 120 characters.')}",
            status_code=303,
        )
    if db.query(Device).filter(Device.name == clean_name).first():
        return RedirectResponse(
            url=f"/admin?err={quote_plus(f'Device \"{clean_name}\" already exists.')}",
            status_code=303,
        )
    db.add(Device(name=clean_name))
    db.commit()
    return RedirectResponse(
        url=f"/admin?msg={quote_plus(f'Added device \"{clean_name}\".')}",
        status_code=303,
    )


@app.post("/admin/devices/{device_id}/delete")
def admin_delete_device(
    device_id: int,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    device = db.query(Device).get(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    dname = device.name
    db.delete(device)
    db.commit()
    return RedirectResponse(
        url=f"/admin?msg={quote_plus(f'Removed device \"{dname}\" from the list.')}",
        status_code=303,
    )


@app.post("/admin/users/create")
def admin_create_user(
    username: str = Form(...),
    default_password: str = Form(...),
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    clean_username = username.strip()
    if not clean_username or len(clean_username) > 120:
        return RedirectResponse(
            url=f"/admin?err={quote_plus('Username must be 1 to 120 characters.')}",
            status_code=303,
        )
    if len(default_password) < 4:
        return RedirectResponse(
            url=f"/admin?err={quote_plus('Default password must be at least 4 characters.')}",
            status_code=303,
        )
    existing_user = db.query(User).filter(User.username == clean_username).first()
    existing_admin = db.query(AdminCredential).filter(AdminCredential.username == clean_username).first()
    if existing_user or existing_admin:
        return RedirectResponse(
            url=f"/admin?err={quote_plus(f'Username \"{clean_username}\" already exists.')}",
            status_code=303,
        )

    db.add(User(username=clean_username, password_hash=hash_password(default_password)))
    db.commit()
    return RedirectResponse(
        url=f"/admin?msg={quote_plus(f'Added lab user \"{clean_username}\".')}",
        status_code=303,
    )


@app.post("/admin/users/{user_id}/reset-password")
def admin_reset_user_password(
    user_id: int,
    new_password: str = Form(...),
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    user = db.query(User).get(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if len(new_password) < 4:
        return RedirectResponse(
            url=f"/admin?err={quote_plus('Password must be at least 4 characters.')}",
            status_code=303,
        )
    user.password_hash = hash_password(new_password)
    db.commit()
    return RedirectResponse(
        url=f"/admin?msg={quote_plus(f'Reset password for \"{user.username}\".')}",
        status_code=303,
    )


@app.post("/admin/users/{user_id}/delete")
def admin_delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    user = db.query(User).get(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    uname = user.username
    db.delete(user)
    db.commit()
    return RedirectResponse(
        url=f"/admin?msg={quote_plus(f'Deleted user \"{uname}\".')}",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# Admin-only Holidays Tab (/admin/holidays)
# ---------------------------------------------------------------------------
@app.get("/admin/holidays")
def admin_holidays_page(
    request: Request,
    year: int | None = None,
    msg: str | None = None,
    err: str | None = None,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    selected_year = year if (year and 2020 <= year <= 2100) else date.today().year
    start_d = date(selected_year, 1, 1)
    end_d = date(selected_year, 12, 31)
    year_holidays = (
        db.query(Holiday)
        .filter(Holiday.date >= start_d, Holiday.date <= end_d)
        .order_by(Holiday.date)
        .all()
    )
    return templates.TemplateResponse(
        "admin_holidays.html",
        {
            "request": request,
            "selected_year": selected_year,
            "holidays": year_holidays,
            "msg": msg,
            "err": err,
        },
    )


@app.post("/admin/holidays/sync")
def admin_holidays_sync(
    year: int = Form(...),
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    if not (2020 <= year <= 2100):
        return RedirectResponse(
            url=f"/admin/holidays?err={quote_plus('Year must be between 2020 and 2100.')}",
            status_code=303,
        )
    added, skipped, source_name = holidays.sync_israeli_holidays_to_db(db, year)
    if source_name == "unavailable":
        return RedirectResponse(
            url=f"/admin/holidays?year={year}&err={quote_plus('Could not reach the open-access Israeli holiday calendars. Check internet access or add holidays manually.')}",
            status_code=303,
        )
    summary = (
        f"Imported {added} holiday(s) for {year} from {source_name}"
        + (f" ({skipped} existing date(s) kept unchanged)." if skipped else ".")
    )
    return RedirectResponse(
        url=f"/admin/holidays?year={year}&msg={quote_plus(summary)}",
        status_code=303,
    )


@app.post("/admin/holidays/add")
def admin_holidays_add(
    holiday_date: date = Form(...),
    name: str = Form(...),
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    clean_name = name.strip()
    year = holiday_date.year
    if not clean_name or len(clean_name) > 160:
        return RedirectResponse(
            url=f"/admin/holidays?year={year}&err={quote_plus('Holiday name must be 1 to 160 characters.')}",
            status_code=303,
        )
    existing = db.query(Holiday).filter(Holiday.date == holiday_date).first()
    if existing:
        existing.name = clean_name
        existing.source = "manual"
        action_msg = f"Updated holiday on {holiday_date.isoformat()} to '{clean_name}'."
    else:
        db.add(Holiday(date=holiday_date, name=clean_name, source="manual"))
        action_msg = f"Added holiday '{clean_name}' on {holiday_date.isoformat()}."
    db.commit()
    return RedirectResponse(
        url=f"/admin/holidays?year={year}&msg={quote_plus(action_msg)}",
        status_code=303,
    )


@app.post("/admin/holidays/{holiday_id}/edit")
def admin_holidays_edit(
    holiday_id: int,
    name: str = Form(...),
    year: int = Form(...),
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    row = db.query(Holiday).get(holiday_id)
    if not row:
        raise HTTPException(status_code=404, detail="Holiday not found")
    clean_name = name.strip()
    if not clean_name or len(clean_name) > 160:
        return RedirectResponse(
            url=f"/admin/holidays?year={year}&err={quote_plus('Holiday name must be 1 to 160 characters.')}",
            status_code=303,
        )
    row.name = clean_name
    row.source = "manual"
    db.commit()
    return RedirectResponse(
        url=f"/admin/holidays?year={year}&msg={quote_plus(f'Renamed holiday on {row.date.isoformat()} to \"{clean_name}\".')}",
        status_code=303,
    )


@app.post("/admin/holidays/{holiday_id}/delete")
def admin_holidays_delete(
    holiday_id: int,
    year: int = Form(...),
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    row = db.query(Holiday).get(holiday_id)
    if not row:
        raise HTTPException(status_code=404, detail="Holiday not found")
    hdate = row.date.isoformat()
    hname = row.name
    db.delete(row)
    db.commit()
    return RedirectResponse(
        url=f"/admin/holidays?year={year}&msg={quote_plus(f'Deleted holiday \"{hname}\" ({hdate}).')}",
        status_code=303,
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
    if record is None or not verify_password_constant_time(
        current_password, record.password_hash if record else None
    ):
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

    resp = templates.TemplateResponse(
        "admin_settings.html",
        {"request": request, "admin_username": record.username, "errors": None, "success": True},
    )
    if request.cookies.get(SESSION_COOKIE_NAME):
        is_https = request.headers.get("x-forwarded-proto", "").lower() == "https"
        resp.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=create_session_token(record.username, "admin"),
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=is_https,
        )
    return resp


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
def get_holidays(
    year: int | None = None,
    db: Session = Depends(get_db),
    _user: dict = Depends(require_user),
):
    target_year = year if year is not None else date.today().year
    if not (2020 <= target_year <= 2100):
        return {}
    return holidays.get_holidays_for_year(db, target_year)


@app.get("/health")
def health_check():
    return {"status": "ok"}
