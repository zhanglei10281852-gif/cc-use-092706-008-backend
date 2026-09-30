"""再生水处理系统班次读数登记的 API 模型。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Metric = Literal["influent", "effluent", "return_flow"]
Unit = Literal["m3", "L"]
ShiftSeq = Literal[1, 2, 3]


class FacilityCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=120)


class MeterPointCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    metric: Metric
    unit: Unit


class MeterPointsUpsert(BaseModel):
    points: list[MeterPointCreate] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_codes(self) -> "MeterPointsUpsert":
        codes = [point.code for point in self.points]
        if len(set(codes)) != len(codes):
            raise ValueError("同一设施下表计编码不能重复")
        return self


class ReadingIn(BaseModel):
    point_code: str = Field(min_length=1, max_length=64)
    # 计数器接班/交班读数；缺测传 null（部分缺测），两者不能同时为 null。
    start_value: float | None = None
    end_value: float | None = None
    unit: Unit
    start_observed_at: datetime | None = None
    end_observed_at: datetime | None = None

    @model_validator(mode="after")
    def at_least_one_value(self) -> "ReadingIn":
        if self.start_value is None and self.end_value is None:
            raise ValueError("接班与交班读数至少提供一个，整表缺测请省略该读数")
        return self


class ShiftEntry(BaseModel):
    facility_code: str = Field(min_length=2, max_length=64)
    shift_date: date
    shift_seq: ShiftSeq
    starts_at: datetime
    ends_at: datetime
    readings: list[ReadingIn] = Field(default_factory=list, max_length=100)
    reason: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def validate_window(self) -> "ShiftEntry":
        if self.ends_at <= self.starts_at:
            raise ValueError("班次结束时间必须晚于开始时间")
        if self.shift_date != self.starts_at.date():
            raise ValueError("shift_date 必须与班次开始时间处于同一自然日")
        codes = [item.point_code for item in self.readings]
        if len(set(codes)) != len(codes):
            raise ValueError("同一班次内表计读数不能重复")
        return self


class ShiftRegistration(ShiftEntry):
    idempotency_key: str | None = Field(default=None, min_length=6, max_length=160)


class BatchImport(BaseModel):
    batch_key: str = Field(min_length=6, max_length=160)
    reason: str = Field(default="", max_length=1000)
    entries: list[ShiftEntry] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique_shift_targets(self) -> "BatchImport":
        targets = [(e.facility_code, e.shift_date.isoformat(), e.shift_seq) for e in self.entries]
        if len(set(targets)) != len(targets):
            raise ValueError("同一批次不能包含两条相同设施与班次的记录")
        return self


class RevisionRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=1000)
    readings: list[ReadingIn] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_codes(self) -> "RevisionRequest":
        codes = [item.point_code for item in self.readings]
        if len(set(codes)) != len(codes):
            raise ValueError("同一班次内表计读数不能重复")
        return self


class RollbackRequest(BaseModel):
    version: int = Field(ge=1)
    reason: str = Field(min_length=2, max_length=1000)


class ReviewResolution(BaseModel):
    action: Literal["confirm", "reject"]
    resolution: str = Field(min_length=2, max_length=1000)
