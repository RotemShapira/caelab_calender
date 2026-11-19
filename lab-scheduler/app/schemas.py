import re
from datetime import date
from pydantic import BaseModel, field_validator, model_validator

MAX_NAME_LENGTH = 120
MAX_DETAILS_LENGTH = 2000
MAX_BOOKING_SPAN_DAYS = 365
MIN_YEAR = 2020
MAX_YEAR = 2100

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class BookingCreate(BaseModel):
    user_name: str
    equipment: str
    experiment_details: str | None = None
    start_date: date
    end_date: date
    start_time: str | None = None
    end_time: str | None = None

    @field_validator("user_name", "equipment")
    @classmethod
    def validate_short_text(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("This field cannot be blank.")
        if len(v) > MAX_NAME_LENGTH:
            raise ValueError(f"Must be at most {MAX_NAME_LENGTH} characters.")
        return v

    @field_validator("experiment_details")
    @classmethod
    def validate_details(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if len(v) > MAX_DETAILS_LENGTH:
            raise ValueError(f"Experiment details must be at most {MAX_DETAILS_LENGTH} characters.")
        return v

    @field_validator("start_time", "end_time")
    @classmethod
    def validate_time_format(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if not _TIME_RE.match(v):
            raise ValueError("Time must be in 24-hour HH:MM format (e.g. 09:00).")
        return v

    @field_validator("start_date")
    @classmethod
    def validate_start_year(cls, v: date) -> date:
        if not (MIN_YEAR <= v.year <= MAX_YEAR):
            raise ValueError(f"Start date year must be between {MIN_YEAR} and {MAX_YEAR}.")
        return v

    @field_validator("end_date")
    @classmethod
    def end_after_start(cls, v: date, info) -> date:
        if not (MIN_YEAR <= v.year <= MAX_YEAR):
            raise ValueError(f"End date year must be between {MIN_YEAR} and {MAX_YEAR}.")
        start = info.data.get("start_date")
        if start:
            if v < start:
                raise ValueError("End date cannot be before start date.")
            if (v - start).days > MAX_BOOKING_SPAN_DAYS:
                raise ValueError(f"A single booking cannot exceed {MAX_BOOKING_SPAN_DAYS} days.")
        return v

    @model_validator(mode="after")
    def validate_time_pair(self):
        if bool(self.start_time) != bool(self.end_time):
            raise ValueError("Please provide both Start Time and End Time, or leave both blank for All Day.")
        if self.start_time and self.end_time:
            if self.end_time <= self.start_time:
                raise ValueError("End time must be after start time.")
        return self
