"""
Conflict notifications (Email + Webhook).

Reads the collision notification recipient email address (and SMTP settings)
from the `app_settings` SQLite table (managed directly in the Admin Panel
at `/admin`), falling back to environment variables if unset.
"""
import logging
import os
import smtplib
from email.mime.text import MIMEText
from sqlalchemy.orm import Session

import requests

from .models import AppSetting

logger = logging.getLogger("lab_scheduler.notifications")

WEBHOOK_URL = os.getenv("WEBHOOK_URL")

SETTING_NOTIFY_EMAIL_TO = "notify_email_to"
SETTING_SMTP_HOST = "smtp_host"
SETTING_SMTP_PORT = "smtp_port"
SETTING_SMTP_USER = "smtp_user"
SETTING_SMTP_PASSWORD = "smtp_password"
SETTING_SMTP_FROM = "smtp_from"


def _get_setting(db: Session | None, key: str, env_var: str, default: str = "") -> str:
    if db is not None:
        row = db.query(AppSetting).filter(AppSetting.key == key).first()
        if row is not None and row.value.strip():
            return row.value.strip()
    return (os.getenv(env_var) or default).strip()


def get_notification_config(db: Session | None = None) -> dict[str, str]:
    smtp_user = _get_setting(db, SETTING_SMTP_USER, "SMTP_USER", "")
    return {
        "notify_email_to": _get_setting(db, SETTING_NOTIFY_EMAIL_TO, "NOTIFY_EMAIL_TO", ""),
        "smtp_host": _get_setting(db, SETTING_SMTP_HOST, "SMTP_HOST", ""),
        "smtp_port": _get_setting(db, SETTING_SMTP_PORT, "SMTP_PORT", "587"),
        "smtp_user": smtp_user,
        "smtp_password": _get_setting(db, SETTING_SMTP_PASSWORD, "SMTP_PASSWORD", ""),
        "smtp_from": _get_setting(
            db, SETTING_SMTP_FROM, "SMTP_FROM", smtp_user or "lab-scheduler@localhost"
        ),
    }


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if row is None:
        db.add(AppSetting(key=key, value=value.strip()))
    else:
        row.value = value.strip()


def _send_webhook(message: str) -> None:
    if not WEBHOOK_URL:
        return
    try:
        requests.post(WEBHOOK_URL, json={"text": message}, timeout=5)
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("Webhook notification failed: %s", exc)


def _send_email_raw(subject: str, message: str, db: Session | None = None) -> tuple[bool, str]:
    cfg = get_notification_config(db)
    notify_to = cfg["notify_email_to"]
    smtp_host = cfg["smtp_host"]

    if not notify_to:
        return False, "Recipient email address is empty."
    if not smtp_host:
        return (
            False,
            "Outgoing SMTP Server (SMTP Host) is not configured yet. "
            "Expand 'Outgoing SMTP Server Settings' below and enter an SMTP server "
            "(for Gmail: host smtp.gmail.com, port 587, your Gmail address, and a 16-char Google App Password).",
        )

    recipients = [addr.strip() for addr in notify_to.split(",") if addr.strip()]
    if not recipients:
        return False, "No valid recipient email address found."

    try:
        smtp_port = int(cfg["smtp_port"] or "587")
        msg = MIMEText(message)
        msg["Subject"] = subject
        msg["From"] = cfg["smtp_from"]
        msg["To"] = ", ".join(recipients)

        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=12) as server:
                if cfg["smtp_user"] and cfg["smtp_password"]:
                    server.login(cfg["smtp_user"], cfg["smtp_password"])
                server.sendmail(cfg["smtp_from"], recipients, msg.as_string())
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=12) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                if cfg["smtp_user"] and cfg["smtp_password"]:
                    server.login(cfg["smtp_user"], cfg["smtp_password"])
                server.sendmail(cfg["smtp_from"], recipients, msg.as_string())

        logger.info("Sent email '%s' to %s", subject, ", ".join(recipients))
        return True, f"Email sent successfully to {', '.join(recipients)}."
    except Exception as exc:
        logger.warning("Email notification failed: %s", exc)
        return False, f"SMTP error: {exc}"


def send_test_email(db: Session) -> tuple[bool, str]:
    return _send_email_raw(
        subject="[Lab Scheduler] Test Collision Notification",
        message=(
            "This is a test email from your Lab Experiment Scheduler.\n"
            "Collision email notifications are configured and working properly!"
        ),
        db=db,
    )


def notify_conflict(new_booking, conflicting_bookings, db: Session | None = None) -> None:
    """Called whenever a freshly submitted booking overlaps existing ones."""
    conflict_summary = "; ".join(
        f"#{b.id} {b.user_name} ({b.start_date} to {b.end_date}, {b.time_label}, status={b.status})"
        for b in conflicting_bookings
    )
    message = (
        f"[Lab Scheduler] Conflict detected on '{new_booking.equipment}'.\n"
        f"New request: #{new_booking.id} by {new_booking.user_name} "
        f"({new_booking.start_date} to {new_booking.end_date}, {new_booking.time_label}).\n"
        f"Conflicts with: {conflict_summary}\n"
        f"Please resolve in the admin panel."
    )
    logger.info(message)
    _send_webhook(message)
    ok, err = _send_email_raw(
        subject=f"Lab Scheduler conflict: {new_booking.equipment}",
        message=message,
        db=db,
    )
    if not ok:
        logger.warning("Conflict email skipped/failed: %s", err)
