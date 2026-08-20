from datetime import date
from pydantic import BaseModel, field_validator


class BookingCreate(BaseModel):
    user_name: str
    equipment: str
    experiment_details: str | None = None
    start_date: date
    end_date: date

    @field_validator("user_name", "equipment")
    @classmethod
    def not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("This field cannot be blank.")
        return v

    @field_validator("end_date")
    @classmethod
    def end_after_start(cls, v: date, info):
        start = info.data.get("start_date")
        if start and v < start:
            raise ValueError("End date cannot be before start date.")
        return v
