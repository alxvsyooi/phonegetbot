"""Общие мелкие утилиты времени/сообщений — использует и farm.py, и autosend.py.

Отдельный файл специально, чтобы не было циклического импорта между
automation.py (базовый воркер) и farm.py/autosend.py (независимые модули).
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

from storage import MINING_HOUR, MINING_MINUTE

MSK = timezone(timedelta(hours=3))


def parse_hhmm(value: str | None) -> tuple[int, int]:
    if value:
        m = re.match(r"^\s*(\d{1,2})[:.\s](\d{1,2})\s*$", value)
        if m:
            h, mi = int(m.group(1)), int(m.group(2))
            if 0 <= h < 24 and 0 <= mi < 60:
                return h, mi
    return MINING_HOUR, MINING_MINUTE


def seconds_until_msk(hour: int, minute: int) -> float:
    now = datetime.now(MSK)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}ч")
    if m:
        parts.append(f"{m}м")
    parts.append(f"{s}с")
    return " ".join(parts)


def clock() -> str:
    return time.strftime("%H:%M:%S")


def today_msk() -> str:
    return datetime.now(MSK).strftime("%d.%m")
