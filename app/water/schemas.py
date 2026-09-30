from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ShiftStatus = Literal["draft", "confirmed", "anomaly", "anomaly_reviewed", "corrected"]


class FacilityCreate(BaseModel):
    code: str = Field(min_length=2, max_length=40, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=120)
    unit: str = Field(default="m3", min_length=1, max_length=16)
    capacity_per_shift: float = Field(gt=0, le=1_000_000_000)
    nominal_shift_hours: float = Field(default=8.0, ge=2, le=12)
    continuity_tolerance_minutes: int = Field(default=60, ge=0, le=720)

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("unit")
    @classmethod
    def normalize_unit(cls, value: str) -> str:
        return value.strip().lower()


class Readings(BaseModel):
    influent: float | None = Field(default=None, ge=-1_000_000_000, le=1_000_000_000)
    effluent: float | None = Field(default=None, ge=-1_000_000_000, le=1_000_000_000)
    return_flow: float | None = Field(default=None, ge=-1_000_000_000, le=1_000_000_000)

    @model_validator(mode="after")
    def require_at_least_one_metric(self) -> "Readings":
        if self.influent is None and self.effluent is None and self.return_flow is None:
            raise ValueError("至少登记一项仪表读数（允许部分缺测，但不能三项全空）")
        return self


class ShiftRegister(BaseModel):
    facility_code: str = Field(min_length=2, max_length=40)
    batch_code: str = Field(min_length=2, max_length=80)
    starts_at: str = Field(min_length=16, max_length=40)
    ends_at: str = Field(min_length=16, max_length=40)
    readings: Readings
    unit: str = Field(default="m3", min_length=1, max_length=16)
    note: str = Field(default="", max_length=500)
    idempotency_key: str = Field(min_length=6, max_length=160)

    @field_validator("facility_code")
    @classmethod
    def normalize_facility(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("batch_code", "unit")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()


class ShiftAmend(BaseModel):
    reason: str = Field(min_length=2, max_length=500)
    readings: Readings
    unit: str = Field(default="m3", min_length=1, max_length=16)
    note: str = Field(default="", max_length=500)
    idempotency_key: str = Field(min_length=6, max_length=160)

    @field_validator("unit")
    @classmethod
    def normalize_unit(cls, value: str) -> str:
        return value.strip()


class ShiftReview(BaseModel):
    decision: Literal["confirm", "correct"]
    note: str = Field(default="", max_length=500)
    corrected_readings: Readings | None = None
    unit: str = Field(default="m3", min_length=1, max_length=16)
    idempotency_key: str = Field(min_length=6, max_length=160)

    @field_validator("unit")
    @classmethod
    def normalize_unit(cls, value: str) -> str:
        return value.strip()


class ShiftRollback(BaseModel):
    target_version: int = Field(ge=1)
    reason: str = Field(min_length=2, max_length=500)
    idempotency_key: str = Field(min_length=6, max_length=160)


class BatchCreate(BaseModel):
    batch_code: str = Field(min_length=2, max_length=80)
    note: str = Field(default="", max_length=500)


class BatchClose(BaseModel):
    note: str = Field(default="", max_length=500)


class ImportEntry(BaseModel):
    facility_code: str = Field(min_length=2, max_length=40)
    starts_at: str = Field(min_length=16, max_length=40)
    ends_at: str = Field(min_length=16, max_length=40)
    readings: Readings
    unit: str = Field(default="m3", min_length=1, max_length=16)
    note: str = Field(default="", max_length=500)

    @field_validator("facility_code")
    @classmethod
    def normalize_facility(cls, value: str) -> str:
        return value.strip().upper()


class ImportRequest(BaseModel):
    batch_code: str = Field(min_length=2, max_length=80)
    import_key: str = Field(min_length=6, max_length=160)
    entries: list[ImportEntry] = Field(min_length=1, max_length=500)
