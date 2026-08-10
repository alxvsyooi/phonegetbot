"""UI Engine: дизайн-система статусов + движок "не редактировать, если не изменилось".

Чистый модуль без зависимости от Pyrogram — только форматирование и кэш
последнего отрендеренного состояния экрана. Фактический вызов
edit_text/обработку MessageNotModified по-прежнему делает
ControlBot._safe_edit (controlbot.py) — этот модуль лишь решает, НУЖНО ли
его вызывать, чтобы не дёргать Telegram API, когда содержимое не изменилось.
"""
from __future__ import annotations

import time
from typing import Any

from common import fmt_duration

_BAR_FULL = "▓"
_BAR_EMPTY = "░"


class Design:
    """Чистые форматтеры статусов/прогресса — общий визуальный словарь для всех экранов."""

    @staticmethod
    def status_badge(on: bool) -> str:
        """HTML-разметка — только для ТЕКСТА сообщения (parse_mode=HTML)."""
        return "🟢 <code>[ON]</code>" if on else "🔴 <code>[OFF]</code>"

    @staticmethod
    def status_badge_plain(on: bool) -> str:
        """Без HTML-тегов — Telegram не парсит разметку в подписях inline-кнопок."""
        return "🟢 ON" if on else "🔴 OFF"

    @staticmethod
    def wait_badge() -> str:
        return "⏳ <code>[WAIT]</code>"

    @staticmethod
    def alert_badge() -> str:
        return "⚠️ <code>[ALERT]</code>"

    @staticmethod
    def progress_bar(fraction: float, width: int = 10) -> str:
        fraction = max(0.0, min(1.0, fraction))
        filled = round(fraction * width)
        return _BAR_FULL * filled + _BAR_EMPTY * (width - filled)

    @staticmethod
    def duration(seconds: float) -> str:
        return fmt_duration(seconds)

    @staticmethod
    def countdown(next_ts: float | None) -> str:
        """"⏳ Next: 14m 20s" / "⏳ сейчас" для готового к запуску модуля."""
        if not next_ts:
            return "—"
        remaining = next_ts - time.time()
        if remaining <= 0:
            return "готово ✅"
        return fmt_duration(remaining)

    @staticmethod
    def alert_frame(emoji: str, title: str, account_name: str, body: str) -> str:
        """Единая визуальная рамка для push-уведомлений (заголовок/разделитель/аккаунт
        кодом — формат "Drop Alert"/"Session Warning" из ТЗ), вокруг УЖЕ существующего
        текста алерта — сам текст/логика вызова не меняются, меняется только оформление."""
        return (
            f"{emoji} <b>{title}</b>\n"
            "───\n"
            f"<b>Аккаунт:</b> <code>{account_name}</code>\n"
            f"{body}"
        )


def _markup_signature(markup: Any) -> tuple:
    """Извлекает (text, callback_data/url) из InlineKeyboardMarkup для сравнения —
    сами объекты pyrogram.types.InlineKeyboardButton не поддерживают ==/hash по значению."""
    if markup is None:
        return ()
    rows = getattr(markup, "inline_keyboard", None)
    if rows is None:
        return ()
    return tuple(
        tuple((btn.text, getattr(btn, "callback_data", None), getattr(btn, "url", None)) for btn in row)
        for row in rows
    )


class RenderEngine:
    """Кэш последнего отрендеренного (text, markup) на экран, чтобы не звать
    edit_text повторно, если ничего не изменилось (вместо надежды на то, что
    Telegram сам вернёт MessageNotModified и мы его молча проглотим)."""

    def __init__(self) -> None:
        self._last: dict[tuple[int, str], tuple[str, tuple]] = {}

    def should_render(self, chat_id: int, screen_key: str, text: str, markup: Any) -> bool:
        cache_key = (chat_id, screen_key)
        signature = (text, _markup_signature(markup))
        if self._last.get(cache_key) == signature:
            return False
        self._last[cache_key] = signature
        return True

    def invalidate(self, chat_id: int, screen_key: str) -> None:
        self._last.pop((chat_id, screen_key), None)


class DashboardRegistry:
    """user_id -> (chat_id, message_id) для экрана Dashboard, чтобы redis_bridge.py
    знал, какие именно открытые сообщения перерисовывать по Pub/Sub-событию,
    не рассылая уведомления всем подряд."""

    def __init__(self) -> None:
        self._open: dict[int, tuple[int, int]] = {}

    def register(self, user_id: int, chat_id: int, message_id: int) -> None:
        self._open[user_id] = (chat_id, message_id)

    def get(self, user_id: int) -> tuple[int, int] | None:
        return self._open.get(user_id)

    def items(self) -> list[tuple[int, tuple[int, int]]]:
        return list(self._open.items())

    def forget(self, user_id: int) -> None:
        self._open.pop(user_id, None)
