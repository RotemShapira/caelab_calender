from sqlalchemy import Boolean, Column, Integer, String, Date, DateTime, Text, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .database import Base

# Booking status values
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"

ALL_STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_DENIED)

BOOKING_TYPE_DEVICE = "device"
BOOKING_TYPE_POTENTIOSTAT = "potentiostat"


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

    # "device" (general lab equipment) or "potentiostat" (potentiostat channel)
    booking_type = Column(String(20), default=BOOKING_TYPE_DEVICE, nullable=False, index=True)
    potentiostat_name = Column(String(120), nullable=True, index=True)
    channel_category = Column(String(20), nullable=True)  # "1" or "2"

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

    @property
    def channel_name(self) -> str:
        if self.potentiostat_name and self.equipment.startswith(f"{self.potentiostat_name} — "):
            return self.equipment[len(f"{self.potentiostat_name} — "):]
        return self.equipment

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
    Lab member user accounts created and managed by the Admin/Manager from /admin.
    If `is_manager` is True (set only by the Admin), the user can resolve double
    bookings, approve/deny requests, manage holidays, and add/remove lab members.
    """
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(120), nullable=False, unique=True, index=True)
    password_hash = Column(String(255), nullable=False)
    is_manager = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Device(Base):
    """
    General lab devices/equipment (apart from potentiostats), managed in /admin.
    """
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), nullable=False, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Potentiostat(Base):
    """
    Potentiostat units managed in /admin. Each potentiostat has its own list
    of channels separated into 2 configurable categories.
    """
    __tablename__ = "potentiostats"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), nullable=False, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    channels = relationship(
        "PotentiostatChannel",
        back_populates="potentiostat",
        cascade="all, delete-orphan",
        order_by="PotentiostatChannel.category, PotentiostatChannel.name",
    )


class PotentiostatChannel(Base):
    """
    Individual channel belonging to a Potentiostat, assigned to Category "1" or "2".
    """
    __tablename__ = "potentiostat_channels"
    __table_args__ = (
        UniqueConstraint("potentiostat_id", "name", name="uq_potentiostat_channel_name"),
    )

    id = Column(Integer, primary_key=True, index=True)
    potentiostat_id = Column(Integer, ForeignKey("potentiostats.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(80), nullable=False)
    category = Column(String(20), nullable=False, default="1")  # "1" or "2"
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    potentiostat = relationship("Potentiostat", back_populates="channels")


class Holiday(Base):
    """
    Holidays shown on the schedule calendars.
    """
    __tablename__ = "holidays"

    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, nullable=False, unique=True, index=True)
    name = Column(String(160), nullable=False)
    source = Column(String(30), nullable=False, default="manual")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AppSetting(Base):
    """
    Key-value settings persisted in SQLite.
    """
    __tablename__ = "app_settings"

    key = Column(String(120), primary_key=True)
    value = Column(Text, nullable=False, default="")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
