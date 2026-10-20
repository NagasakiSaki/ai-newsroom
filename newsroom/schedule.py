"""北京时间采编计划，不调用操作系统或公众号发表功能。"""
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

BEIJING = timezone(timedelta(hours=8))


class SchedulePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    mode: Literal["daily", "interval"] = "daily"
    times: list[str] = Field(default_factory=lambda: ["09:00", "18:00"], min_length=1, max_length=12)
    interval_minutes: int = Field(default=30, ge=5, le=1440)

    @field_validator("times")
    @classmethod
    def valid_times(cls, values):
        import re
        if any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) for value in values):
            raise ValueError("时间须为 HH:MM，例如 09:00、18:00")
        return sorted(set(values))


def next_occurrence(plan, current=None):
    current = current or datetime.now(timezone.utc)
    if not plan["enabled"]:
        return None
    if plan["mode"] == "interval":
        return (current + timedelta(minutes=plan["interval_minutes"])).astimezone(timezone.utc).isoformat()
    local = current.astimezone(BEIJING)
    for day in range(2):
        date = local + timedelta(days=day)
        for value in sorted(plan["times"]):
            hour, minute = map(int, value.split(":"))
            candidate = date.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate > local:
                return candidate.astimezone(timezone.utc).isoformat()
    raise ValueError("定时计划没有有效时间")
