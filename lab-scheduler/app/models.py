from sqlalchemy import Column, Integer, String, Date, DateTime, Text
from sqlalchemy.sql import func

from .database import Base

# Booking status values
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"

ALL_STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_DENIED)


class Booking(Base):
    __tablename__ = "bookings"

    id = Column(Integer, primary_key=True, index=True)
    user_name = Column(String(120), nullable=False)
    equipment = Column(String(120), nullable=False, index=True)
    experiment_details = Column(Text, nullable=True)
    start_date = Column(Date, nullable=False, index=True)
    end_date = Column(Date, nullable=False, index=True)

    # Optional hourly bounds ("HH:MM" 24h format). Both NULL = All Day booking.
    start_time = Column(String(5), nullable=True)
    end_time = Column(String(5), nullable=True)

    status = Column(String(20), default=STATUS_PENDING, nullable=False, index=True)

    # Free-text note the admin can leave when resolving a conflict
    admin_note = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    @property
    def time_label(self) -> str:
        if self.start_time and self.end_time:
            return f"{self.start_time}–{self.end_time}"
        return "All Day"

    def __repr__(self):
        return (
            f"<Booking {self.id} {self.equipment} "
            f"{self.start_date}..{self.end_date} ({self.time_label}) [{self.status}]>"
        )


class AdminCredential(Base):
    """
    Admin login, stored in the DB so it can be changed from the GUI
    instead of only via the ADMIN_USERNAME/ADMIN_PASSWORD env vars.
    """
    __tablename__ = "admin_credentials"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(120), nullable=False, unique=True)
    password_hash = Column(String(255), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class User(Base):
    """
    Lab member user accounts created and managed by the Admin from /admin.
    """
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(120), nullable=False, unique=True, index=True)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Device(Base):
    """
    Shared lab devices/equipment managed by the Admin from /admin.
    Populates the Equipment dropdown on the booking form.
    """
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), nullable=False, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Holiday(Base):
    """
    Holidays shown on the schedule calendar.
    Base entries can be imported from an open-access Israeli holiday calendar
    and manually added, edited, or deleted by the Admin at /admin/holidays.
    """
    __tablename__ = "holidays"

    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, nullable=False, unique=True, index=True)
    name = Column(String(160), nullable=False)
    source = Column(String(30), nullable=False, default="manual")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AppSetting(Base):
    """
    Key-value settings persisted in SQLite (such as conflict notification
    recipient email addresses, SMTP settings, and session signing key).
    """
    __tablename__ = "app_settings"

    key = Column(String(120), primary_key=True)
    value = Column(Text, nullable=False, default="")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
