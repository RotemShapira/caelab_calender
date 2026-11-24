"""
Portable JSON export & import for migrating Lab Scheduler data between
instances or machines (devices, potentiostats & channels, lab users with their
hashed passwords & emails, bookings, holidays, and email/category settings).

Security & safety guarantees:
  - Strictly excludes `session_secret`, `app_base_url`, and `admin_credentials`
    so each server keeps its own cryptographic secret, public URL, and active
    admin login.
  - Validates the entire JSON payload before modifying the database.
  - Executes the import inside a single atomic SQLite transaction (rolls back
    cleanly on any error).
"""
from __future__ import annotations

import json
import secrets
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from . import models, notifications

BACKUP_FORMAT = "lab-scheduler-backup"
BACKUP_SCHEMA_VERSION = 1
MAX_IMPORT_BYTES = 25 * 1024 * 1024  # 25 MB

EXPORTABLE_SETTING_KEYS = (
    "potentiostat_cat1_name",
    "potentiostat_cat2_name",
    notifications.SETTING_NOTIFY_EMAIL_TO,
    notifications.SETTING_SMTP_HOST,
    notifications.SETTING_SMTP_PORT,
    notifications.SETTING_SMTP_USER,
    notifications.SETTING_SMTP_PASSWORD,
    notifications.SETTING_SMTP_FROM,
)

_VALID_BOOKING_STATUSES = {
    models.STATUS_APPROVED,
    models.STATUS_PENDING,
    models.STATUS_DENIED,
}
_VALID_BOOKING_TYPES = {
    models.BOOKING_TYPE_DEVICE,
    models.BOOKING_TYPE_POTENTIOSTAT,
}


def _parse_optional_dt(val: Any) -> datetime | None:
    if val is None or val == "":
        return None
    if not isinstance(val, str):
        raise ValueError("Invalid datetime value in backup file.")
    cleaned = val.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1]
    return datetime.fromisoformat(cleaned)


def _clean_time_str(val: Any) -> str | None:
    if val is None or val == "":
        return None
    if not isinstance(val, str):
        raise ValueError("Invalid time string in backup file.")
    t = val.strip()
    if len(t) != 5 or t[2] != ":":
        raise ValueError(f"Invalid time format '{t}' in backup file.")
    return t


def export_backup_dict(db: Session) -> dict[str, Any]:
    """Serializes all portable lab data into a versioned JSON-ready dictionary."""
    devices = db.query(models.Device).order_by(models.Device.name).all()
    potentiostats = db.query(models.Potentiostat).order_by(models.Potentiostat.name).all()
    users = db.query(models.User).order_by(models.User.username).all()
    bookings = (
        db.query(models.Booking)
        .order_by(models.Booking.start_date, models.Booking.id)
        .all()
    )
    holidays = db.query(models.Holiday).order_by(models.Holiday.date).all()
    settings_rows = (
        db.query(models.AppSetting)
        .filter(models.AppSetting.key.in_(EXPORTABLE_SETTING_KEYS))
        .all()
    )

    pots_out: list[dict[str, Any]] = []
    for p in potentiostats:
        channels = (
            db.query(models.PotentiostatChannel)
            .filter(models.PotentiostatChannel.potentiostat_id == p.id)
            .order_by(models.PotentiostatChannel.category, models.PotentiostatChannel.name)
            .all()
        )
        pots_out.append(
            {
                "name": p.name,
                "status": p.status or models.EQUIP_STATUS_AVAILABLE,
                "channels": [
                    {
                        "name": ch.name,
                        "category": ch.category or "1",
                        "status": ch.status or models.EQUIP_STATUS_AVAILABLE,
                    }
                    for ch in channels
                ],
            }
        )

    return {
        "format": BACKUP_FORMAT,
        "schema_version": BACKUP_SCHEMA_VERSION,
        "exported_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "devices": [
            {
                "name": d.name,
                "status": d.status or models.EQUIP_STATUS_AVAILABLE,
            }
            for d in devices
        ],
        "potentiostats": pots_out,
        "users": [
            {
                "username": u.username,
                "email": u.email or "",
                "password_hash": u.password_hash,
                "is_manager": bool(u.is_manager),
                "must_change_password": bool(u.must_change_password),
            }
            for u in users
        ],
        "bookings": [
            {
                "user_name": b.user_name,
                "equipment": b.equipment,
                "experiment_details": b.experiment_details or "",
                "start_date": b.start_date.isoformat(),
                "end_date": b.end_date.isoformat(),
                "start_time": b.start_time,
                "end_time": b.end_time,
                "booking_type": b.booking_type or models.BOOKING_TYPE_DEVICE,
                "potentiostat_name": b.potentiostat_name,
                "channel_category": b.channel_category,
                "status": b.status,
                "confirmed_at": b.confirmed_at.isoformat() if b.confirmed_at else None,
                "confirm_token": b.confirm_token,
                "reminder_sent_at": b.reminder_sent_at.isoformat() if b.reminder_sent_at else None,
            }
            for b in bookings
        ],
        "holidays": [
            {
                "date": h.date.isoformat(),
                "name": h.name,
                "source": h.source or "manual",
                "is_deleted": bool(h.is_deleted),
            }
            for h in holidays
        ],
        "app_settings": {
            row.key: (row.value or "")
            for row in settings_rows
            if row.key in EXPORTABLE_SETTING_KEYS
        },
    }


def validate_and_parse_backup(raw_bytes: bytes) -> dict[str, Any]:
    """Parses and strictly validates an uploaded backup JSON payload."""
    if not raw_bytes:
        raise ValueError("Uploaded backup file is empty.")
    if len(raw_bytes) > MAX_IMPORT_BYTES:
        raise ValueError("Backup file exceeds the 25 MB size limit.")

    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid JSON backup file: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("Backup file root must be a JSON object.")
    if data.get("format") != BACKUP_FORMAT:
        raise ValueError("Unrecognized backup format. Please upload a file exported from Lab Scheduler.")
    if data.get("schema_version") != BACKUP_SCHEMA_VERSION:
        raise ValueError(f"Unsupported backup schema version: {data.get('schema_version')}.")

    # Validate devices
    raw_devices = data.get("devices", [])
    if not isinstance(raw_devices, list):
        raise ValueError("Invalid 'devices' section in backup.")
    devices: list[dict[str, str]] = []
    seen_devs: set[str] = set()
    for item in raw_devices:
        if not isinstance(item, dict):
            raise ValueError("Invalid device entry in backup.")
        name = str(item.get("name", "")).strip()
        if not name or len(name) > 120:
            raise ValueError("Device name in backup must be 1 to 120 characters.")
        if name in seen_devs:
            continue
        seen_devs.add(name)
        st = str(item.get("status", models.EQUIP_STATUS_AVAILABLE)).strip().lower()
        if st not in models.ALL_EQUIP_STATUSES:
            st = models.EQUIP_STATUS_AVAILABLE
        devices.append({"name": name, "status": st})

    # Validate potentiostats & channels
    raw_pots = data.get("potentiostats", [])
    if not isinstance(raw_pots, list):
        raise ValueError("Invalid 'potentiostats' section in backup.")
    potentiostats: list[dict[str, Any]] = []
    seen_pots: set[str] = set()
    for item in raw_pots:
        if not isinstance(item, dict):
            raise ValueError("Invalid potentiostat entry in backup.")
        pname = str(item.get("name", "")).strip()
        if not pname or len(pname) > 120:
            raise ValueError("Potentiostat name in backup must be 1 to 120 characters.")
        if pname in seen_pots:
            continue
        seen_pots.add(pname)
        pst = str(item.get("status", models.EQUIP_STATUS_AVAILABLE)).strip().lower()
        if pst not in models.ALL_EQUIP_STATUSES:
            pst = models.EQUIP_STATUS_AVAILABLE

        raw_chs = item.get("channels", [])
        if not isinstance(raw_chs, list):
            raise ValueError(f"Invalid channels list for potentiostat '{pname}'.")
        chs: list[dict[str, str]] = []
        seen_chs: set[str] = set()
        for ch in raw_chs:
            if not isinstance(ch, dict):
                raise ValueError(f"Invalid channel entry on potentiostat '{pname}'.")
            cname = str(ch.get("name", "")).strip()
            if not cname or len(cname) > 80:
                raise ValueError(f"Channel name on '{pname}' must be 1 to 80 characters.")
            if cname in seen_chs:
                continue
            seen_chs.add(cname)
            cat = "2" if str(ch.get("category", "1")).strip() == "2" else "1"
            cst = str(ch.get("status", models.EQUIP_STATUS_AVAILABLE)).strip().lower()
            if cst not in models.ALL_EQUIP_STATUSES:
                cst = models.EQUIP_STATUS_AVAILABLE
            chs.append({"name": cname, "category": cat, "status": cst})
        potentiostats.append({"name": pname, "status": pst, "channels": chs})

    # Validate users
    raw_users = data.get("users", [])
    if not isinstance(raw_users, list):
        raise ValueError("Invalid 'users' section in backup.")
    users: list[dict[str, Any]] = []
    seen_users: set[str] = set()
    for item in raw_users:
        if not isinstance(item, dict):
            raise ValueError("Invalid user entry in backup.")
        uname = str(item.get("username", "")).strip()
        pwhash = str(item.get("password_hash", "")).strip()
        if not uname or len(uname) > 120:
            raise ValueError("User username in backup must be 1 to 120 characters.")
        if not pwhash or len(pwhash) > 255:
            raise ValueError(f"Missing or invalid password_hash for user '{uname}'.")
        if uname in seen_users:
            continue
        seen_users.add(uname)
        email = str(item.get("email", "") or "").strip()[:255]
        users.append(
            {
                "username": uname,
                "email": email,
                "password_hash": pwhash,
                "is_manager": bool(item.get("is_manager", False)),
                "must_change_password": bool(item.get("must_change_password", False)),
            }
        )

    # Validate bookings
    raw_bookings = data.get("bookings", [])
    if not isinstance(raw_bookings, list):
        raise ValueError("Invalid 'bookings' section in backup.")
    bookings: list[dict[str, Any]] = []
    for item in raw_bookings:
        if not isinstance(item, dict):
            raise ValueError("Invalid booking entry in backup.")
        uname = str(item.get("user_name", "")).strip()
        eq = str(item.get("equipment", "")).strip()
        if not uname or len(uname) > 120 or not eq or len(eq) > 120:
            raise ValueError("Booking user_name and equipment must be 1 to 120 characters.")
        try:
            sd = date.fromisoformat(str(item.get("start_date", "")).strip())
            ed = date.fromisoformat(str(item.get("end_date", "")).strip())
        except ValueError as exc:
            raise ValueError(f"Invalid booking date in backup: {exc}") from exc
        if ed < sd:
            raise ValueError(f"Booking end_date ({ed}) is before start_date ({sd}).")
        st_time = _clean_time_str(item.get("start_time"))
        en_time = _clean_time_str(item.get("end_time"))
        btype = str(item.get("booking_type", models.BOOKING_TYPE_DEVICE)).strip().lower()
        if btype not in _VALID_BOOKING_TYPES:
            btype = models.BOOKING_TYPE_DEVICE
        bstatus = str(item.get("status", models.STATUS_APPROVED)).strip().lower()
        if bstatus not in _VALID_BOOKING_STATUSES:
            raise ValueError(f"Invalid booking status '{bstatus}' in backup.")
        pot_name_raw = item.get("potentiostat_name")
        pot_name = str(pot_name_raw).strip()[:120] if pot_name_raw else None
        cat_raw = item.get("channel_category")
        cat = ("2" if str(cat_raw).strip() == "2" else "1") if cat_raw else None
        tok_raw = item.get("confirm_token")
        tok = str(tok_raw).strip()[:64] if tok_raw else secrets.token_urlsafe(32)
        bookings.append(
            {
                "user_name": uname,
                "equipment": eq,
                "experiment_details": str(item.get("experiment_details", "") or "")[:2000],
                "start_date": sd,
                "end_date": ed,
                "start_time": st_time,
                "end_time": en_time,
                "booking_type": btype,
                "potentiostat_name": pot_name,
                "channel_category": cat,
                "status": bstatus,
                "confirmed_at": _parse_optional_dt(item.get("confirmed_at")),
                "confirm_token": tok,
                "reminder_sent_at": _parse_optional_dt(item.get("reminder_sent_at")),
            }
        )

    # Validate holidays
    raw_holidays = data.get("holidays", [])
    if not isinstance(raw_holidays, list):
        raise ValueError("Invalid 'holidays' section in backup.")
    holidays: list[dict[str, Any]] = []
    seen_hdates: set[date] = set()
    for item in raw_holidays:
        if not isinstance(item, dict):
            raise ValueError("Invalid holiday entry in backup.")
        try:
            hd = date.fromisoformat(str(item.get("date", "")).strip())
        except ValueError as exc:
            raise ValueError(f"Invalid holiday date in backup: {exc}") from exc
        if hd in seen_hdates:
            continue
        seen_hdates.add(hd)
        hname = str(item.get("name", "")).strip()[:160]
        if not hname:
            raise ValueError(f"Blank holiday name for date {hd.isoformat()}.")
        hsrc = str(item.get("source", "manual") or "manual").strip()[:40]
        holidays.append(
            {
                "date": hd,
                "name": hname,
                "source": hsrc,
                "is_deleted": bool(item.get("is_deleted", False)),
            }
        )

    # Validate allow-listed settings only
    raw_settings = data.get("app_settings", {})
    if not isinstance(raw_settings, dict):
        raise ValueError("Invalid 'app_settings' section in backup.")
    app_settings: dict[str, str] = {}
    for k, v in raw_settings.items():
        if k in EXPORTABLE_SETTING_KEYS:
            app_settings[k] = str(v if v is not None else "")[:500]

    return {
        "devices": devices,
        "potentiostats": potentiostats,
        "users": users,
        "bookings": bookings,
        "holidays": holidays,
        "app_settings": app_settings,
    }


def import_backup_dict(
    db: Session,
    parsed: dict[str, Any],
    mode: str = "replace",
) -> dict[str, int]:
    """
    Imports validated backup data into SQLite inside one transaction.
      - mode='replace': replaces all lab data (preserving admin_credentials,
        session_secret, and app_base_url).
      - mode='merge': keeps existing rows and inserts only missing items.
    """
    is_replace = mode != "merge"
    counts = {
        "devices": 0,
        "potentiostats": 0,
        "channels": 0,
        "users": 0,
        "bookings": 0,
        "holidays": 0,
        "settings": 0,
    }

    try:
        if is_replace:
            db.query(models.PotentiostatChannel).delete(synchronize_session=False)
            db.query(models.Potentiostat).delete(synchronize_session=False)
            db.query(models.Device).delete(synchronize_session=False)
            db.query(models.User).delete(synchronize_session=False)
            db.query(models.Booking).delete(synchronize_session=False)
            db.query(models.Holiday).delete(synchronize_session=False)
            db.query(models.AppSetting).filter(
                models.AppSetting.key.in_(EXPORTABLE_SETTING_KEYS)
            ).delete(synchronize_session=False)
            db.flush()

        # 1. Devices
        existing_devs = {d.name for d in db.query(models.Device).all()}
        for d in parsed["devices"]:
            if d["name"] in existing_devs:
                continue
            db.add(models.Device(name=d["name"], status=d["status"]))
            existing_devs.add(d["name"])
            counts["devices"] += 1

        # 2. Potentiostats & channels
        existing_pots = {p.name: p for p in db.query(models.Potentiostat).all()}
        for p in parsed["potentiostats"]:
            pot_obj = existing_pots.get(p["name"])
            if pot_obj is None:
                pot_obj = models.Potentiostat(name=p["name"], status=p["status"])
                db.add(pot_obj)
                db.flush()
                existing_pots[p["name"]] = pot_obj
                counts["potentiostats"] += 1
            existing_chs = {
                ch.name
                for ch in db.query(models.PotentiostatChannel)
                .filter(models.PotentiostatChannel.potentiostat_id == pot_obj.id)
                .all()
            }
            for ch in p["channels"]:
                if ch["name"] in existing_chs:
                    continue
                db.add(
                    models.PotentiostatChannel(
                        potentiostat_id=pot_obj.id,
                        name=ch["name"],
                        category=ch["category"],
                        status=ch["status"],
                    )
                )
                existing_chs.add(ch["name"])
                counts["channels"] += 1

        # 3. Users (skip any username that collides with an AdminCredential on this machine)
        admin_usernames = {a.username for a in db.query(models.AdminCredential).all()}
        existing_users = {u.username for u in db.query(models.User).all()}
        for u in parsed["users"]:
            if u["username"] in existing_users or u["username"] in admin_usernames:
                continue
            db.add(
                models.User(
                    username=u["username"],
                    email=u["email"],
                    password_hash=u["password_hash"],
                    is_manager=u["is_manager"],
                    must_change_password=u["must_change_password"],
                )
            )
            existing_users.add(u["username"])
            counts["users"] += 1

        # 4. Bookings
        existing_booking_sigs = {
            (
                b.user_name,
                b.equipment,
                b.start_date,
                b.end_date,
                b.start_time,
                b.end_time,
                b.status,
            )
            for b in db.query(models.Booking).all()
        }
        for b in parsed["bookings"]:
            sig = (
                b["user_name"],
                b["equipment"],
                b["start_date"],
                b["end_date"],
                b["start_time"],
                b["end_time"],
                b["status"],
            )
            if not is_replace and sig in existing_booking_sigs:
                continue
            db.add(
                models.Booking(
                    user_name=b["user_name"],
                    equipment=b["equipment"],
                    experiment_details=b["experiment_details"],
                    start_date=b["start_date"],
                    end_date=b["end_date"],
                    start_time=b["start_time"],
                    end_time=b["end_time"],
                    booking_type=b["booking_type"],
                    potentiostat_name=b["potentiostat_name"],
                    channel_category=b["channel_category"],
                    status=b["status"],
                    confirmed_at=b["confirmed_at"],
                    confirm_token=b["confirm_token"],
                    reminder_sent_at=b["reminder_sent_at"],
                )
            )
            existing_booking_sigs.add(sig)
            counts["bookings"] += 1

        # 5. Holidays
        existing_holidays = {h.date for h in db.query(models.Holiday).all()}
        for h in parsed["holidays"]:
            if h["date"] in existing_holidays:
                continue
            db.add(
                models.Holiday(
                    date=h["date"],
                    name=h["name"],
                    source=h["source"],
                    is_deleted=h["is_deleted"],
                )
            )
            existing_holidays.add(h["date"])
            counts["holidays"] += 1

        # 6. Exportable App Settings (SMTP + potentiostat category labels)
        for k, v in parsed["app_settings"].items():
            row = db.query(models.AppSetting).filter(models.AppSetting.key == k).first()
            if row is None:
                db.add(models.AppSetting(key=k, value=v))
                counts["settings"] += 1
            elif is_replace:
                row.value = v
                counts["settings"] += 1

        db.commit()
    except Exception:
        db.rollback()
        raise

    return counts
