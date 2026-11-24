"""
Notifications (Admin/Manager collision & limit alerts + User status emails).

Reads recipient email addresses and SMTP settings from the `app_settings`
SQLite table (managed in `/admin`), falling back to environment variables
if unset. Includes SSRF protections blocking private/loopback/link-local
SMTP targets and restricting SMTP ports to standard mail submission ports.
"""
import ipaddress
import logging
import os
import smtplib
import socket
from email.mime.text import MIMEText
from sqlalchemy.orm import Session

import requests

from .models import AppSetting, User

logger = logging.getLogger("lab_scheduler.notifications")

WEBHOOK_URL = os.getenv("WEBHOOK_URL")

SETTING_NOTIFY_EMAIL_TO = "notify_email_to"
SETTING_SMTP_HOST = "smtp_host"
SETTING_SMTP_PORT = "smtp_port"
SETTING_SMTP_USER = "smtp_user"
SETTING_SMTP_PASSWORD = "smtp_password"
SETTING_SMTP_FROM = "smtp_from"

_ALLOWED_SMTP_PORTS = (25, 465, 587, 2525)


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


def _validate_smtp_target(host: str, port: int) -> tuple[bool, str]:
    """
    SSRF guard: ensures SMTP port is a standard mail port and the resolved
    hostname points exclusively to public internet IP addresses (blocking
    localhost, LAN RFC1918 subnets, and link-local addresses).
    """
    if port not in _ALLOWED_SMTP_PORTS:
        return False, f"SMTP port {port} is not allowed. Use one of: {', '.join(map(str, _ALLOWED_SMTP_PORTS))}."

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False, f"Could not resolve SMTP host '{host}'."

    if not infos:
        return False, f"No addresses resolved for SMTP host '{host}'."

    for _, _, _, _, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            return False, "Invalid IP address resolved for SMTP host."
        if (
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_multicast
            or ip_obj.is_reserved
            or ip_obj.is_unspecified
        ):
            return False, "Blocked: SMTP host resolves to a private or local network address."

    return True, ""


def _send_webhook(message: str) -> None:
    if not WEBHOOK_URL:
        return
    try:
        requests.post(WEBHOOK_URL, json={"text": message}, timeout=5)
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("Webhook notification failed: %s", exc)


def _send_email_raw(
    subject: str,
    message: str,
    db: Session | None = None,
    recipients_override: list[str] | None = None,
) -> tuple[bool, str]:
    cfg = get_notification_config(db)
    smtp_host = cfg["smtp_host"]

    if recipients_override is not None:
        recipients = [addr.strip() for addr in recipients_override if addr and addr.strip()]
    else:
        notify_to = cfg["notify_email_to"]
        recipients = [addr.strip() for addr in notify_to.split(",") if addr.strip()]

    if not recipients:
        return False, "Recipient email address is empty."
    if not smtp_host:
        return (
            False,
            "Outgoing SMTP Server (SMTP Host) is not configured yet. "
            "Expand 'Outgoing SMTP Server Settings' below and enter an SMTP server "
            "(for Gmail: host smtp.gmail.com, port 587, your Gmail address, and a 16-char Google App Password).",
        )

    try:
        smtp_port = int(cfg["smtp_port"] or "587")
    except ValueError:
        return False, "SMTP port must be a valid number (e.g. 587)."

    target_ok, target_err = _validate_smtp_target(smtp_host, smtp_port)
    if not target_ok:
        return False, target_err

    try:
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
    except smtplib.SMTPAuthenticationError:
        return False, "SMTP authentication failed. Check your username and App Password."
    except Exception as exc:
        logger.warning("Email notification failed: %s", exc)
        return False, "Could not connect to SMTP server. Verify host, port, and credentials."


def send_test_email(db: Session) -> tuple[bool, str]:
    return _send_email_raw(
        subject="[Lab Scheduler] Test Collision Notification",
        message=(
            "This is a test email from your Lab Experiment Scheduler.\n"
            "Email notifications are configured and working properly!"
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
        f"Please resolve in the admin/manager panel."
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


def notify_potentiostat_limit(new_booking, user_overlapping_bookings, db: Session | None = None) -> None:
    """Called when a single user requests more than 3 concurrent potentiostat channels."""
    existing_summary = "; ".join(
        f"#{b.id} {b.equipment} ({b.start_date} to {b.end_date}, {b.time_label}, status={b.status})"
        for b in user_overlapping_bookings
    )
    message = (
        f"[Lab Scheduler] Potentiostat 3-channel limit exceeded by {new_booking.user_name}.\n"
        f"New request: #{new_booking.id} on '{new_booking.equipment}' "
        f"({new_booking.start_date} to {new_booking.end_date}, {new_booking.time_label}).\n"
        f"Existing overlapping potentiostat bookings for {new_booking.user_name}: {existing_summary}\n"
        f"The request was saved as pending for Lab Manager / Admin approval."
    )
    logger.info(message)
    _send_webhook(message)
    ok, err = _send_email_raw(
        subject=f"Lab Scheduler >3 Potentiostat channels: {new_booking.user_name}",
        message=message,
        db=db,
    )
    if not ok:
        logger.warning("Potentiostat limit email skipped/failed: %s", err)


def notify_user_booking_status(
    booking,
    action: str,
    actor_name: str,
    db: Session,
) -> None:
    """
    Sends an email to the user who made `booking` whenever a Manager/Admin
    approves, denies, revokes, or cancels their booking.
    `action` is one of: "approved", "denied", "revoked", "cancelled".
    """
    user = db.query(User).filter(User.username == booking.user_name).first()
    user_email = (user.email or "").strip() if user else ""
    if not user_email:
        logger.info(
            "User '%s' has no email configured; skipping '%s' notification for booking #%s.",
            booking.user_name,
            action,
            booking.id,
        )
        return

    action_titles = {
        "approved": ("Booking Approved", "has been APPROVED"),
        "denied": ("Booking Request Denied", "has been DENIED"),
        "revoked": ("Booking Revoked / Cancelled", "has been REVOKED / CANCELLED"),
        "cancelled": ("Booking Cancelled", "has been CANCELLED"),
    }
    subject_prefix, verb = action_titles.get(action, ("Booking Status Updated", f"status changed to {action}"))

    subject = f"[Lab Scheduler] {subject_prefix}: {booking.equipment}"
    message = (
        f"Hello {booking.user_name},\n\n"
        f"Your booking #{booking.id} {verb} by {actor_name}.\n\n"
        f"Details:\n"
        f"  Device / Channel: {booking.equipment}\n"
        f"  Dates:            {booking.start_date} to {booking.end_date}\n"
        f"  Hours:            {booking.time_label}\n"
        f"  Notes:            {booking.experiment_details or '—'}\n\n"
        f"You can view the latest approved schedule in the Lab Scheduler web app."
    )
    ok, err = _send_email_raw(
        subject=subject,
        message=message,
        db=db,
        recipients_override=[user_email],
    )
    if not ok:
        logger.warning("User status email to '%s' (%s) failed: %s", booking.user_name, user_email, err)
