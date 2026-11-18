"""
Notification stubs.

These fire whenever a new booking is created that conflicts with an
existing one. Out of the box they just log to stdout (visible via
`docker logs`), but both a webhook (e.g. Slack/Discord/n8n/Home Assistant)
and an SMTP email path are wired up and only need environment variables
set to activate. Nothing is sent unless the relevant env vars are present.
"""
import logging
import os
import smtplib
from email.mime.text import MIMEText

import requests

logger = logging.getLogger("lab_scheduler.notifications")

WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g. Slack incoming webhook URL

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER or "lab-scheduler@localhost")
NOTIFY_EMAIL_TO = os.getenv("NOTIFY_EMAIL_TO")  # comma-separated list of admin emails


def _send_webhook(message: str) -> None:
    if not WEBHOOK_URL:
        return
    try:
        requests.post(WEBHOOK_URL, json={"text": message}, timeout=5)
    except Exception as exc:  # pragma: no cover - best effort, never block the request
        logger.warning("Webhook notification failed: %s", exc)


def _send_email(subject: str, message: str) -> None:
    if not (SMTP_HOST and NOTIFY_EMAIL_TO):
        return
    try:
        recipients = [addr.strip() for addr in NOTIFY_EMAIL_TO.split(",") if addr.strip()]
        msg = MIMEText(message)
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = ", ".join(recipients)

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            if SMTP_USER and SMTP_PASSWORD:
                server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, recipients, msg.as_string())
    except Exception as exc:  # pragma: no cover - best effort, never block the request
        logger.warning("Email notification failed: %s", exc)


def notify_conflict(new_booking, conflicting_bookings) -> None:
    """Called whenever a freshly submitted booking overlaps existing ones."""
    conflict_summary = "; ".join(
        f"#{b.id} {b.user_name} ({b.start_date} to {b.end_date}, status={b.status})"
        for b in conflicting_bookings
    )
    message = (
        f"[Lab Scheduler] Conflict detected on '{new_booking.equipment}'.\n"
        f"New request: #{new_booking.id} by {new_booking.user_name} "
        f"({new_booking.start_date} to {new_booking.end_date}).\n"
        f"Conflicts with: {conflict_summary}\n"
        f"Please resolve in the admin panel."
    )
    logger.info(message)
    _send_webhook(message)
    _send_email(subject=f"Lab Scheduler conflict: {new_booking.equipment}", message=message)
