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
TBILISI = timezone(timedelta(hours=4))


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


def seconds_until_periodic_msk(hour: int, minute: int, period_hours: int) -> float:
    """Как seconds_until_msk, но якорь hour:minute повторяется каждые period_hours
    часов (а не раз в сутки) — следующее срабатывание вместо следующего дня."""
    now = datetime.now(MSK)
    anchor = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    period = timedelta(hours=period_hours)
    if anchor <= now:
        steps = (now - anchor) // period + 1
        anchor += steps * period
    return (anchor - now).total_seconds()


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


def in_time_window(tz: timezone, start: str, end: str) -> bool:
    """True, если сейчас (в tz) время попадает в окно [start, end) ЧЧ:ММ. Понимает
    окна через полночь (start > end), напр. «22:00»..«07:00» — тихие часы автопринятия."""
    sh, sm = parse_hhmm(start)
    eh, em = parse_hhmm(end)
    start_min, end_min = sh * 60 + sm, eh * 60 + em
    if start_min == end_min:
        return False
    now_min = datetime.now(tz).hour * 60 + datetime.now(tz).minute
    if start_min < end_min:
        return start_min <= now_min < end_min
    return now_min >= start_min or now_min < end_min
