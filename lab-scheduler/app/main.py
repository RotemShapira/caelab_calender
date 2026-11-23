import asyncio
import logging
import os
from datetime import date
from urllib.parse import quote_plus

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, status
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
    require_manager_or_admin,
    require_user,
    verify_same_origin,
)
from .database import Base, SessionLocal, engine, get_db
from .models import (
    AdminCredential,
    AppSetting,
    BOOKING_TYPE_DEVICE,
    BOOKING_TYPE_POTENTIOSTAT,
    Booking,
    Device,
    Holiday,
    Potentiostat,
    PotentiostatChannel,
    User,
)
from .security import (
    SESSION_TTL_SECONDS,
    check_and_record_booking_rate_limit,
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
        if "booking_type" not in cols:
            conn.execute(text("ALTER TABLE bookings ADD COLUMN booking_type VARCHAR(20) NOT NULL DEFAULT 'device'"))
            logger.info("Added booking_type column to bookings table.")
        if "potentiostat_name" not in cols:
            conn.execute(text("ALTER TABLE bookings ADD COLUMN potentiostat_name VARCHAR(120)"))
            logger.info("Added potentiostat_name column to bookings table.")
        if "channel_category" not in cols:
            conn.execute(text("ALTER TABLE bookings ADD COLUMN channel_category VARCHAR(20)"))
            logger.info("Added channel_category column to bookings table.")

        user_cols = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(users)")).fetchall()
        }
        if "is_manager" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN is_manager BOOLEAN NOT NULL DEFAULT 0"))
            logger.info("Added is_manager column to users table.")

        holiday_cols = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(holidays)")).fetchall()
        }
        if "is_deleted" not in holiday_cols:
            conn.execute(text("ALTER TABLE holidays ADD COLUMN is_deleted BOOLEAN NOT NULL DEFAULT 0"))
            logger.info("Added is_deleted column to holidays table.")


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


SETTING_POT_CAT1 = "potentiostat_cat1_name"
SETTING_POT_CAT2 = "potentiostat_cat2_name"


def _get_potentiostat_category_labels(db: Session) -> dict[str, str]:
    c1 = db.query(AppSetting).filter(AppSetting.key == SETTING_POT_CAT1).first()
    c2 = db.query(AppSetting).filter(AppSetting.key == SETTING_POT_CAT2).first()
    return {
        "1": (c1.value.strip() if c1 and c1.value.strip() else "Category 1 (Standard)"),
        "2": (c2.value.strip() if c2 and c2.value.strip() else "Category 2 (High Current / EIS)"),
    }


def _get_potentiostats_payload(db: Session) -> list[dict]:
    pots = db.query(Potentiostat).order_by(Potentiostat.name).all()
    payload = []
    for p in pots:
        channels = (
            db.query(PotentiostatChannel)
            .filter(PotentiostatChannel.potentiostat_id == p.id)
            .order_by(PotentiostatChannel.category, PotentiostatChannel.name)
            .all()
        )
        payload.append(
            {
                "id": p.id,
                "name": p.name,
                "channels": [
                    {"id": ch.id, "name": ch.name, "category": ch.category}
                    for ch in channels
                ],
            }
        )
    return payload


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
    ip_key = f"ip:{client_ip}"
    user_key = f"user:{clean_user.lower()}"

    if is_rate_limited(ip_key) or (clean_user and is_rate_limited(user_key)):
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
        record_failed_attempt(ip_key)
        if clean_user:
            record_failed_attempt(user_key)
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "Invalid username or password.",
                "username": clean_user,
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    clear_failed_attempts(ip_key)
    clear_failed_attempts(user_key)
    if admin_ok:
        role = "admin"
        matched_username = admin_rec.username
        matched_hash = admin_rec.password_hash
    else:
        role = "manager" if user_rec.is_manager else "user"
        matched_username = user_rec.username
        matched_hash = user_rec.password_hash
    token = create_session_token(matched_username, role, matched_hash)

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
    resp = templates.TemplateResponse(
        "account.html",
        {
            "request": request,
            "username": current_user["username"],
            "errors": None,
            "success": True,
        },
    )
    is_https = request.headers.get("x-forwarded-proto", "").lower() == "https"
    resp.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=create_session_token(record.username, current_user["role"], record.password_hash),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=is_https,
    )
    return resp


# ---------------------------------------------------------------------------
# Authenticated Lab Member: booking submission form & dashboard
# ---------------------------------------------------------------------------
@app.get("/")
def booking_form(
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_user),
):
    devices = db.query(Device).order_by(Device.name).all()
    potentiostats = _get_potentiostats_payload(db)
    pot_cat_labels = _get_potentiostat_category_labels(db)
    if current_user["role"] in ("admin", "manager"):
        usernames = _get_allowed_usernames(db)
    else:
        usernames = [current_user["username"]]
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "errors": None,
            "form_data": {},
            "devices": devices,
            "potentiostats": potentiostats,
            "pot_cat_labels": pot_cat_labels,
            "usernames": usernames,
        },
    )


@app.post("/bookings")
def create_booking(
    request: Request,
    user_name: str = Form(...),
    equipment_selection: str = Form(...),
    channel_ids: list[int] = Form(default=[]),
    experiment_details: str = Form(""),
    start_date: date = Form(...),
    end_date: date = Form(...),
    use_hours: str = Form(""),
    start_time: str = Form(""),
    end_time: str = Form(""),
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_user),
):
    devices = db.query(Device).order_by(Device.name).all()
    device_names = {d.name for d in devices}
    potentiostats = _get_potentiostats_payload(db)
    pot_cat_labels = _get_potentiostat_category_labels(db)
    if current_user["role"] in ("admin", "manager"):
        usernames = _get_allowed_usernames(db)
        effective_user_name = user_name.strip()
    else:
        usernames = [current_user["username"]]
        effective_user_name = current_user["username"]

    effective_start_time = start_time.strip() if use_hours else ""
    effective_end_time = end_time.strip() if use_hours else ""

    form_data = {
        "user_name": effective_user_name,
        "equipment_selection": equipment_selection,
        "channel_ids": channel_ids,
        "experiment_details": experiment_details,
        "start_date": start_date,
        "end_date": end_date,
        "use_hours": bool(use_hours),
        "start_time": effective_start_time,
        "end_time": effective_end_time,
    }

    errors: list[str] = []
    if not check_and_record_booking_rate_limit(current_user["username"]):
        errors.append("Too many booking requests submitted in a short period. Please wait a few minutes and try again.")
    if effective_user_name not in usernames:
        errors.append("Please select a valid user from the dropdown list.")

    # Resolve whether the user picked a General Device or a Potentiostat + Channel(s)
    targets: list[dict] = []  # each: {"equipment": str, "booking_type": str, "pot_name": str|None, "cat": str|None}

    if equipment_selection.startswith("device::"):
        chosen_dev = equipment_selection[len("device::"):].strip()
        if chosen_dev not in device_names:
            errors.append("Please select a valid device from the dropdown list.")
        else:
            targets.append(
                {
                    "equipment": chosen_dev,
                    "booking_type": BOOKING_TYPE_DEVICE,
                    "pot_name": None,
                    "cat": None,
                }
            )
    elif equipment_selection.startswith("potentiostat::"):
        try:
            pot_id = int(equipment_selection[len("potentiostat::"):])
        except ValueError:
            pot_id = -1
        pot = db.query(Potentiostat).get(pot_id)
        if not pot:
            errors.append("Please select a valid potentiostat from the dropdown list.")
        else:
            valid_channels = (
                db.query(PotentiostatChannel)
                .filter(
                    PotentiostatChannel.potentiostat_id == pot.id,
                    PotentiostatChannel.id.in_(channel_ids),
                )
                .order_by(PotentiostatChannel.category, PotentiostatChannel.name)
                .all()
            ) if channel_ids else []
            if not valid_channels:
                errors.append(f"Please select at least one channel on {pot.name}.")
            else:
                for ch in valid_channels:
                    targets.append(
                        {
                            "equipment": f"{pot.name} — {ch.name}",
                            "booking_type": BOOKING_TYPE_POTENTIOSTAT,
                            "pot_name": pot.name,
                            "cat": ch.category,
                        }
                    )
    else:
        errors.append("Please select a device or potentiostat from the dropdown list.")

    validated_list: list[tuple[schemas.BookingCreate, dict]] = []
    if not errors:
        for t in targets:
            try:
                validated = schemas.BookingCreate(
                    user_name=effective_user_name,
                    equipment=t["equipment"],
                    experiment_details=experiment_details,
                    start_date=start_date,
                    end_date=end_date,
                    start_time=effective_start_time or None,
                    end_time=effective_end_time or None,
                )
                validated_list.append((validated, t))
            except ValidationError as exc:
                errors = [err["msg"] for err in exc.errors()]
                break

    if errors or not validated_list:
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "errors": errors,
                "form_data": form_data,
                "devices": devices,
                "potentiostats": potentiostats,
                "pot_cat_labels": pot_cat_labels,
                "usernames": usernames,
            },
            status_code=422,
        )

    created_results: list[dict] = []
    any_conflict = False
    is_potentiostat_booking = False

    for validated, meta in validated_list:
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
        if has_conflict:
            any_conflict = True
        if meta["booking_type"] == BOOKING_TYPE_POTENTIOSTAT:
            is_potentiostat_booking = True

        booking = models.Booking(
            user_name=validated.user_name,
            equipment=validated.equipment,
            experiment_details=validated.experiment_details,
            start_date=validated.start_date,
            end_date=validated.end_date,
            start_time=validated.start_time,
            end_time=validated.end_time,
            booking_type=meta["booking_type"],
            potentiostat_name=meta["pot_name"],
            channel_category=meta["cat"],
            status=models.STATUS_PENDING if has_conflict else models.STATUS_APPROVED,
        )
        db.add(booking)
        db.commit()
        db.refresh(booking)

        if has_conflict:
            notifications.notify_conflict(booking, conflicts, db=db)

        created_results.append(
            {
                "booking": booking,
                "has_conflict": has_conflict,
                "conflicts": conflicts,
            }
        )

    return templates.TemplateResponse(
        "confirmation.html",
        {
            "request": request,
            "booking": created_results[0]["booking"],
            "has_conflict": any_conflict,
            "conflicts": created_results[0]["conflicts"],
            "results": created_results,
            "is_potentiostat_booking": is_potentiostat_booking,
            "pot_cat_labels": pot_cat_labels,
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
        .filter(
            models.Booking.status == models.STATUS_APPROVED,
            models.Booking.booking_type == BOOKING_TYPE_DEVICE,
        )
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
    registered_devices = [d.name for d in db.query(Device).order_by(Device.name).all()]
    booked_devices = [b.equipment for b in approved]
    filter_devices = sorted(set(registered_devices + booked_devices), key=str.lower)

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "bookings": approved,
            "bookings_json": bookings_json,
            "filter_devices": filter_devices,
            "today": date.today(),
        },
    )


@app.get("/potentiostats")
def potentiostats_dashboard(
    request: Request,
    db: Session = Depends(get_db),
    _user: dict = Depends(require_user),
):
    approved = (
        db.query(models.Booking)
        .filter(
            models.Booking.status == models.STATUS_APPROVED,
            models.Booking.booking_type == BOOKING_TYPE_POTENTIOSTAT,
        )
        .order_by(models.Booking.start_date, models.Booking.start_time)
        .all()
    )
    potentiostats = _get_potentiostats_payload(db)
    pot_cat_labels = _get_potentiostat_category_labels(db)

    bookings_json = [
        {
            "id": b.id,
            "equipment": b.equipment,
            "potentiostat_name": b.potentiostat_name or b.equipment,
            "channel_name": b.channel_name,
            "channel_category": b.channel_category or "1",
            "category_label": pot_cat_labels.get(b.channel_category or "1", "Category 1"),
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

    filter_potentiostats = sorted(
        set([p["name"] for p in potentiostats] + [b.potentiostat_name for b in approved if b.potentiostat_name]),
        key=str.lower,
    )

    return templates.TemplateResponse(
        "potentiostats_dashboard.html",
        {
            "request": request,
            "bookings": approved,
            "bookings_json": bookings_json,
            "potentiostats": potentiostats,
            "filter_potentiostats": filter_potentiostats,
            "pot_cat_labels": pot_cat_labels,
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
    _actor: dict = Depends(require_manager_or_admin),
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
def _serialize_booking_for_modal(b: models.Booking) -> dict:
    return {
        "id": b.id,
        "equipment": b.equipment,
        "user_name": b.user_name,
        "start_date": b.start_date.isoformat(),
        "end_date": b.end_date.isoformat(),
        "time_label": b.time_label,
        "experiment_details": b.experiment_details or "",
        "status": b.status,
    }


@app.get("/admin")
def admin_panel(
    request: Request,
    msg: str | None = None,
    err: str | None = None,
    db: Session = Depends(get_db),
    actor: dict = Depends(require_manager_or_admin),
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

    # Also compute which denied bookings would collide with live bookings if re-approved
    denied_conflicts_by_id: dict[int, list[models.Booking]] = {}
    for b in denied:
        c_list = conflict.find_conflicts(
            db,
            equipment=b.equipment,
            start_date=b.start_date,
            end_date=b.end_date,
            start_time=b.start_time,
            end_time=b.end_time,
            exclude_id=b.id,
            statuses=(models.STATUS_PENDING, models.STATUS_APPROVED),
        )
        if c_list:
            denied_conflicts_by_id[b.id] = c_list

    # Build JSON options map for the interactive Conflict Resolution Popup Modal
    conflict_modal_options: dict[str, list[dict]] = {}
    for b in pending:
        if b.id in conflicts_by_id:
            group = [b] + conflicts_by_id[b.id]
            seen = set()
            unique_group = []
            for item in group:
                if item.id not in seen:
                    seen.add(item.id)
                    unique_group.append(_serialize_booking_for_modal(item))
            conflict_modal_options[str(b.id)] = unique_group

    for b in denied:
        if b.id in denied_conflicts_by_id:
            group = [b] + denied_conflicts_by_id[b.id]
            seen = set()
            unique_group = []
            for item in group:
                if item.id not in seen:
                    seen.add(item.id)
                    unique_group.append(_serialize_booking_for_modal(item))
            conflict_modal_options[str(b.id)] = unique_group

    users = db.query(User).order_by(User.username).all()
    devices = db.query(Device).order_by(Device.name).all()
    potentiostats = _get_potentiostats_payload(db)
    pot_cat_labels = _get_potentiostat_category_labels(db)
    notif_cfg = notifications.get_notification_config(db)

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "pending": pending,
            "approved": approved,
            "denied": denied,
            "conflicts_by_id": conflicts_by_id,
            "denied_conflicts_by_id": denied_conflicts_by_id,
            "conflict_modal_options": conflict_modal_options,
            "users": users,
            "devices": devices,
            "potentiostats": potentiostats,
            "pot_cat_labels": pot_cat_labels,
            "notif_cfg": notif_cfg,
            "is_full_admin": actor["role"] == "admin",
            "actor_username": actor["username"],
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


@app.post("/admin/potentiostats/categories")
def admin_update_pot_categories(
    cat1_name: str = Form(...),
    cat2_name: str = Form(...),
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    c1 = cat1_name.strip()
    c2 = cat2_name.strip()
    if not c1 or not c2 or len(c1) > 80 or len(c2) > 80:
        return RedirectResponse(
            url=f"/admin?err={quote_plus('Both category names must be 1 to 80 characters.')}",
            status_code=303,
        )
    notifications.set_setting(db, SETTING_POT_CAT1, c1)
    notifications.set_setting(db, SETTING_POT_CAT2, c2)
    db.commit()
    return RedirectResponse(
        url=f"/admin?msg={quote_plus(f'Updated potentiostat channel categories to \"{c1}\" and \"{c2}\".')}",
        status_code=303,
    )


@app.post("/admin/potentiostats/create")
def admin_create_potentiostat(
    name: str = Form(...),
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    clean_name = name.strip()
    if not clean_name or len(clean_name) > 120:
        return RedirectResponse(
            url=f"/admin?err={quote_plus('Potentiostat name must be 1 to 120 characters.')}",
            status_code=303,
        )
    if db.query(Potentiostat).filter(Potentiostat.name == clean_name).first():
        return RedirectResponse(
            url=f"/admin?err={quote_plus(f'Potentiostat \"{clean_name}\" already exists.')}",
            status_code=303,
        )
    db.add(Potentiostat(name=clean_name))
    db.commit()
    return RedirectResponse(
        url=f"/admin?msg={quote_plus(f'Added potentiostat \"{clean_name}\". You can now add its channels below.')}",
        status_code=303,
    )


@app.post("/admin/potentiostats/{pot_id}/delete")
def admin_delete_potentiostat(
    pot_id: int,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    pot = db.query(Potentiostat).get(pot_id)
    if not pot:
        raise HTTPException(status_code=404, detail="Potentiostat not found")
    pname = pot.name
    db.delete(pot)
    db.commit()
    return RedirectResponse(
        url=f"/admin?msg={quote_plus(f'Removed potentiostat \"{pname}\" and its channels.')}",
        status_code=303,
    )


@app.post("/admin/potentiostats/{pot_id}/channels/create")
def admin_create_potentiostat_channel(
    pot_id: int,
    channel_name: str = Form(...),
    category: str = Form("1"),
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    pot = db.query(Potentiostat).get(pot_id)
    if not pot:
        raise HTTPException(status_code=404, detail="Potentiostat not found")
    clean_ch = channel_name.strip()
    cat = "2" if category.strip() == "2" else "1"
    if not clean_ch or len(clean_ch) > 80:
        return RedirectResponse(
            url=f"/admin?err={quote_plus('Channel name must be 1 to 80 characters.')}",
            status_code=303,
        )
    existing = (
        db.query(PotentiostatChannel)
        .filter(
            PotentiostatChannel.potentiostat_id == pot.id,
            PotentiostatChannel.name == clean_ch,
        )
        .first()
    )
    if existing:
        return RedirectResponse(
            url=f"/admin?err={quote_plus(f'Channel \"{clean_ch}\" already exists on {pot.name}.')}",
            status_code=303,
        )
    db.add(PotentiostatChannel(potentiostat_id=pot.id, name=clean_ch, category=cat))
    db.commit()
    return RedirectResponse(
        url=f"/admin?msg={quote_plus(f'Added channel \"{clean_ch}\" to {pot.name}.')}",
        status_code=303,
    )


@app.post("/admin/potentiostats/channels/{channel_id}/delete")
def admin_delete_potentiostat_channel(
    channel_id: int,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    ch = db.query(PotentiostatChannel).get(channel_id)
    if not ch:
        raise HTTPException(status_code=404, detail="Channel not found")
    cname = ch.name
    db.delete(ch)
    db.commit()
    return RedirectResponse(
        url=f"/admin?msg={quote_plus(f'Removed channel \"{cname}\".')}",
        status_code=303,
    )


@app.post("/admin/users/create")
def admin_create_user(
    username: str = Form(...),
    default_password: str = Form(...),
    db: Session = Depends(get_db),
    _actor: dict = Depends(require_manager_or_admin),
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

    db.add(User(username=clean_username, password_hash=hash_password(default_password), is_manager=False))
    db.commit()
    return RedirectResponse(
        url=f"/admin?msg={quote_plus(f'Added lab user \"{clean_username}\".')}",
        status_code=303,
    )


@app.post("/admin/users/{user_id}/toggle-manager")
def admin_toggle_manager(
    user_id: int,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    user = db.query(User).get(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_manager = not bool(user.is_manager)
    db.commit()
    status_label = "promoted to Lab Manager" if user.is_manager else "removed from Lab Manager role"
    return RedirectResponse(
        url=f"/admin?msg={quote_plus(f'User \"{user.username}\" was {status_label}.')}",
        status_code=303,
    )


@app.post("/admin/users/{user_id}/reset-password")
def admin_reset_user_password(
    user_id: int,
    new_password: str = Form(...),
    db: Session = Depends(get_db),
    actor: dict = Depends(require_manager_or_admin),
):
    user = db.query(User).get(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if actor["role"] == "manager" and user.is_manager:
        return RedirectResponse(
            url=f"/admin?err={quote_plus('Only the main Lab Admin can reset another Manager password.')}",
            status_code=303,
        )
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
    actor: dict = Depends(require_manager_or_admin),
):
    user = db.query(User).get(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if actor["role"] == "manager" and (user.is_manager or user.username == actor["username"]):
        return RedirectResponse(
            url=f"/admin?err={quote_plus('Only the main Lab Admin can delete a Lab Manager account.')}",
            status_code=303,
        )
    uname = user.username
    db.delete(user)
    db.commit()
    return RedirectResponse(
        url=f"/admin?msg={quote_plus(f'Deleted user \"{uname}\".')}",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# Admin & Manager Holidays Tab (/admin/holidays) — supports future years!
# ---------------------------------------------------------------------------
@app.get("/admin/holidays")
def admin_holidays_page(
    request: Request,
    year: int | None = None,
    msg: str | None = None,
    err: str | None = None,
    db: Session = Depends(get_db),
    _actor: dict = Depends(require_manager_or_admin),
):
    current_year = date.today().year
    selected_year = year if (year and 2020 <= year <= 2100) else current_year
    start_d = date(selected_year, 1, 1)
    end_d = date(selected_year, 12, 31)
    year_holidays = (
        db.query(Holiday)
        .filter(
            Holiday.date >= start_d,
            Holiday.date <= end_d,
            Holiday.is_deleted == False,  # noqa: E712
        )
        .order_by(Holiday.date)
        .all()
    )
    deleted_holidays = (
        db.query(Holiday)
        .filter(
            Holiday.date >= start_d,
            Holiday.date <= end_d,
            Holiday.is_deleted == True,  # noqa: E712
        )
        .order_by(Holiday.date)
        .all()
    )

    # Summarize all years that currently have active saved holidays
    all_dates = [
        row[0]
        for row in db.query(Holiday.date).filter(Holiday.is_deleted == False).all()  # noqa: E712
    ]
    year_counts: dict[int, int] = {}
    for d in all_dates:
        year_counts[d.year] = year_counts.get(d.year, 0) + 1
    loaded_years = sorted(year_counts.items())

    return templates.TemplateResponse(
        "admin_holidays.html",
        {
            "request": request,
            "current_year": current_year,
            "selected_year": selected_year,
            "holidays": year_holidays,
            "deleted_holidays": deleted_holidays,
            "loaded_years": loaded_years,
            "msg": msg,
            "err": err,
        },
    )


@app.post("/admin/holidays/sync")
def admin_holidays_sync(
    year: int = Form(...),
    end_year: int | None = Form(None),
    db: Session = Depends(get_db),
    _actor: dict = Depends(require_manager_or_admin),
):
    target_end = end_year if end_year is not None else year
    if not (2020 <= year <= 2100) or not (2020 <= target_end <= 2100):
        return RedirectResponse(
            url=f"/admin/holidays?err={quote_plus('Years must be between 2020 and 2100.')}",
            status_code=303,
        )
    if target_end < year:
        target_end = year
    if target_end - year >= 10:
        return RedirectResponse(
            url=f"/admin/holidays?year={year}&err={quote_plus('You can import at most 10 years at once.')}",
            status_code=303,
        )

    total_added = 0
    total_skipped = 0
    last_source = "unavailable"
    for y in range(year, target_end + 1):
        added, skipped, src = holidays.sync_israeli_holidays_to_db(db, y)
        total_added += added
        total_skipped += skipped
        if src != "unavailable":
            last_source = src

    if last_source == "unavailable":
        return RedirectResponse(
            url=f"/admin/holidays?year={year}&err={quote_plus('Could not reach the open-access Israeli holiday calendars. Check internet access or add holidays manually.')}",
            status_code=303,
        )

    year_label = f"{year}" if year == target_end else f"{year}–{target_end}"
    summary = (
        f"Imported {total_added} holiday(s) for {year_label} from {last_source}"
        + (f" ({total_skipped} existing date(s) kept unchanged)." if total_skipped else ".")
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
    _actor: dict = Depends(require_manager_or_admin),
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
        existing.is_deleted = False
        action_msg = f"Updated holiday on {holiday_date.isoformat()} to '{clean_name}'."
    else:
        db.add(Holiday(date=holiday_date, name=clean_name, source="manual", is_deleted=False))
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
    _actor: dict = Depends(require_manager_or_admin),
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
    _actor: dict = Depends(require_manager_or_admin),
):
    row = db.query(Holiday).get(holiday_id)
    if not row:
        raise HTTPException(status_code=404, detail="Holiday not found")
    hdate = row.date.isoformat()
    hname = row.name
    # Soft-delete so future open-calendar imports never re-add this date
    row.is_deleted = True
    row.source = "manual"
    db.commit()
    return RedirectResponse(
        url=f"/admin/holidays?year={year}&msg={quote_plus(f'Removed holiday \"{hname}\" ({hdate}). Future imports will keep this date excluded.')}",
        status_code=303,
    )


@app.post("/admin/holidays/{holiday_id}/restore")
def admin_holidays_restore(
    holiday_id: int,
    year: int = Form(...),
    db: Session = Depends(get_db),
    _actor: dict = Depends(require_manager_or_admin),
):
    row = db.query(Holiday).get(holiday_id)
    if not row:
        raise HTTPException(status_code=404, detail="Holiday not found")
    row.is_deleted = False
    db.commit()
    return RedirectResponse(
        url=f"/admin/holidays?year={year}&msg={quote_plus(f'Restored holiday \"{row.name}\" ({row.date.isoformat()}).')}",
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
            value=create_session_token(record.username, "admin", record.password_hash),
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=is_https,
        )
    return resp


def _approve_and_deny_conflicts(db: Session, winner: models.Booking) -> list[models.Booking]:
    """
    Approves `winner` and automatically denies every overlapping live booking
    (pending or approved) on the same device & overlapping date/time, guaranteeing
    no double-booking can ever exist.
    """
    overlapping = conflict.find_conflicts(
        db,
        equipment=winner.equipment,
        start_date=winner.start_date,
        end_date=winner.end_date,
        start_time=winner.start_time,
        end_time=winner.end_time,
        exclude_id=winner.id,
        statuses=(models.STATUS_PENDING, models.STATUS_APPROVED),
    )
    winner.status = models.STATUS_APPROVED
    for other in overlapping:
        other.status = models.STATUS_DENIED
    db.commit()
    return overlapping


@app.post("/admin/bookings/resolve-conflict")
def resolve_conflict_choice(
    winner_id: int = Form(...),
    db: Session = Depends(get_db),
    _actor: dict = Depends(require_manager_or_admin),
):
    winner = db.query(models.Booking).get(winner_id)
    if not winner:
        raise HTTPException(status_code=404, detail="Chosen booking not found")
    denied_others = _approve_and_deny_conflicts(db, winner)
    if denied_others:
        denied_summary = ", ".join(f"#{b.id} ({b.user_name})" for b in denied_others)
        msg = (
            f"Conflict resolved: Approved #{winner.id} ({winner.user_name} on {winner.equipment}, "
            f"{winner.start_date}–{winner.end_date} {winner.time_label}) and automatically denied "
            f"conflicting booking(s): {denied_summary}."
        )
    else:
        msg = f"Approved #{winner.id} ({winner.user_name} on {winner.equipment})."
    return RedirectResponse(url=f"/admin?msg={quote_plus(msg)}", status_code=303)


@app.post("/admin/bookings/{booking_id}/approve")
def approve_booking(
    booking_id: int,
    db: Session = Depends(get_db),
    _actor: dict = Depends(require_manager_or_admin),
):
    booking = db.query(models.Booking).get(booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    denied_others = _approve_and_deny_conflicts(db, booking)
    if denied_others:
        denied_summary = ", ".join(f"#{b.id} ({b.user_name})" for b in denied_others)
        msg = (
            f"Approved #{booking.id} ({booking.user_name}) and automatically denied "
            f"conflicting booking(s): {denied_summary}."
        )
        return RedirectResponse(url=f"/admin?msg={quote_plus(msg)}", status_code=303)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/bookings/{booking_id}/deny")
def deny_booking(
    booking_id: int,
    db: Session = Depends(get_db),
    _actor: dict = Depends(require_manager_or_admin),
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
    _actor: dict = Depends(require_manager_or_admin),
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


@app.get("/api/equipment-bookings")
def get_equipment_bookings(
    equipment: list[str] = Query(default=[]),
    db: Session = Depends(get_db),
    _user: dict = Depends(require_user),
):
    """
    Returns live (approved/pending) bookings for one or more selected devices or
    potentiostat channels so the interactive booking calendar can display booked
    hours in Red (other users) or Green (current user) and available hours blank.
    """
    clean_eq = [e.strip() for e in equipment if e and e.strip()]
    if not clean_eq:
        return []
    rows = (
        db.query(models.Booking)
        .filter(
            models.Booking.equipment.in_(clean_eq),
            models.Booking.status.in_((models.STATUS_APPROVED, models.STATUS_PENDING)),
        )
        .order_by(models.Booking.start_date, models.Booking.start_time)
        .all()
    )
    return [
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
            "status": b.status,
        }
        for b in rows
    ]


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
