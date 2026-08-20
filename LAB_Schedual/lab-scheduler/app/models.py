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

    status = Column(String(20), default=STATUS_PENDING, nullable=False, index=True)

    # Free-text note the admin can leave when resolving a conflict
    admin_note = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<Booking {self.id} {self.equipment} {self.start_date}..{self.end_date} [{self.status}]>"


class AdminCredential(Base):
    """
    Admin login, stored in the DB so it can be changed from the GUI
    instead of only via the ADMIN_USERNAME/ADMIN_PASSWORD env vars.
    password_hash is a salted PBKDF2 hash (see app/security.py) — never
    stores the plaintext password.
    """
    __tablename__ = "admin_credentials"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(120), nullable=False, unique=True)
    password_hash = Column(String(255), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
