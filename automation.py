"""Базовый воркер аккаунта: подключение к Telegram, общие примитивы обмена
сообщениями, статистика, лог последних сообщений (виден только владельцу).

Игровая автоматизация живёт в farm.py (FarmModule) — карточки/рулетка/майнинг/
контейнеры/вывод/трейд. Автоотправка живёт в autosend.py (AutosendModule) —
произвольные задачи по расписанию + «.trade»/«.pay» из личек. Модули НЕЗАВИСИМЫ:
каждый включается/выключается своим тумблером (farm_enabled / autosend_enabled)
и живёт в своих файлах/хендлерах, не пересекаясь друг с другом.

Этот файл сознательно не читает контакты, историю чужой переписки и номер
телефона аккаунта — только то, что нужно для игровой логики и автоотправки
(ответы ботов, свои же сообщения).
"""
from __future__ import annotations

import asyncio
import html
import time
from collections import deque
from typing import Any

from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler, EditedMessageHandler

from storage import CARDS_BOT
from common import MSK, parse_hhmm, seconds_until_msk, fmt_duration, clock, chat_label, msg_preview
from farm import FarmModule
from autosend import AutosendModule

MSG_LOG_SIZE = 30  # сколько последних сообщений держим в памяти на аккаунт

# main.py использует эти имена как `from automation import MSK, _parse_hhmm`
_parse_hhmm = parse_hhmm
_seconds_until_msk = seconds_until_msk


class _WorkerBase:
    """Подключение к Telegram + общие примитивы. Сам по себе не содержит ни
    игровой логики, ни автоотправки — это в FarmModule/AutosendModule ниже."""

    def __init__(
        self,
        account: dict[str, Any],
        storage=None,
        good_keywords: list[str] | None = None,
        payout_delay: int = 120,
        container_cfg: dict | None = None,
        self_commands_cfg: dict | None = None,
    ) -> None:
        self.account = account                 # ссылка на словарь из Storage (читаем «вживую»)
        self.storage = storage
        self.good_keywords = [k.lower() for k in (good_keywords or [])]
        self.payout_delay = payout_delay
        self.container_cfg = container_cfg or {}
        self.self_commands_cfg = self_commands_cfg or {}

        self.client: Client | None = None
        self.running = False
        self.trade_runner = None               # callable(farm_id) -> coroutine (ставит Manager)

        self._tasks: list[asyncio.Task] = []
        self._pending: dict[str, asyncio.Future] = {}
        self._trade_mode = False
        self._trade_queue: asyncio.Queue | None = None
        self._task_next: dict[int, float] = {}   # tid -> время следующего запуска
        self.recent_messages: deque = deque(maxlen=MSG_LOG_SIZE)  # лог сообщений (виден владельцу)

        now = 0.0
        self.card_next_ts = now
        self.roulette_next_ts = now
        self.mining_next_ts = now
        self.container_next_ts = now
        self.status = "остановлен"
        self.last_card = "—"
        self.last_roulette = "—"
        self.last_mining = "—"
        self.last_payout = "—"
        self.last_exchange = "—"
        self.last_container = "—"
        self.last_daily = "—"
        self.last_self_cmd = "—"

    # ---------- свойства ----------
    @property
    def id(self) -> int:
        return self.account["id"]

    @property
    def name(self) -> str:
        return self.account.get("name", f"acc{self.id}")

    # ---------- статистика ----------
    def _bump(self, key: str, n: int = 1) -> None:
        for d in ("stats", "stats_day"):
            st = self.account.setdefault(d, {})
            st[key] = st.get(key, 0) + n
        if self.storage:
            try:
                self.storage.save()
            except Exception:
                pass

    def stats_line(self) -> str:
        st = self.account.get("stats", {})
        return (
            f"📱 {st.get('phones', 0)} (⭐{st.get('good_phones', 0)}) | "
            f"🎰 {st.get('roulette', 0)} | ⛏ {st.get('mining', 0)} | 🎁 {st.get('daily', 0)} | "
            f"💸 {st.get('paid', 0)} | 🔄 {st.get('exchanged', 0)}"
        )

    # ---------- жизненный цикл ----------
    async def start(self) -> None:
        if self.running:
            return
        self.client = Client(
            name=f"acc_{self.id}",
            api_id=int(self.account["api_id"]),
            api_hash=self.account["api_hash"],
            session_string=self.account["session_string"],
            in_memory=True,
            sleep_threshold=0,  # не «спать» молча на FloodWait — пусть бросает
        )
        # ловим ответы от ЛЮБЫХ ботов (игровые + кастомные задачи) — для кнопок/кулдаунов
        bot_filter = filters.incoming & filters.bot
        self.client.add_handler(MessageHandler(self._on_message, bot_filter))
        self.client.add_handler(EditedMessageHandler(self._on_message, bot_filter))
        # автоотправка: свои же сообщения в личках («.trade», «.pay 505050») — модуль autosend.py.
        # Отдельная группа, чтобы не пересекаться с обработкой ответов ботов.
        self.client.add_handler(
            MessageHandler(self._on_self_command, filters.me & filters.private), group=1
        )
        # лог последних сообщений (для кнопки «📨 Сообщения», виден только владельцу),
        # отдельная группа — просто наблюдатель, ничего не решает и не блокирует
        self.client.add_handler(MessageHandler(self._on_any_message, filters.all), group=2)
        await self.client.start()

        try:  # запомним tg_id/username (нужно для трейда и определения владельца)
            me = await self.client.get_me()
            if self.account.get("tg_id") != me.id or not self.account.get("username"):
                self.account["tg_id"] = me.id
                self.account["username"] = me.username or ""
                if self.storage:
                    self.storage.save()
        except Exception:
            pass

        self.running = True
        self.status = "работает"
        self._tasks = [asyncio.create_task(coro) for coro in self._loops()]
        print(f"[{self.name}] запущен")

    def _loops(self):
        """Список корутин фоновых циклов. FarmModule/AutosendModule добавляют
        свои через кооперативный super()._loops() + [...]."""
        return []

    async def stop(self) -> None:
        self.running = False
        for t in self._tasks:
            t.cancel()
        self._tasks = []
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        if self.client:
            try:
                await self.client.stop()
            except Exception:
                pass
        self.client = None
        self.status = "остановлен"
        print(f"[{self.name}] остановлен")

    # ---------- приём сообщений ----------
    async def _on_message(self, _client, message) -> None:
        uname = (getattr(message.chat, "username", "") or "").lower()
        if self._trade_mode and self._trade_queue is not None:
            # во время трейда копим только сообщения игрового бота карточек
            if uname == CARDS_BOT.lower():
                await self._trade_queue.put(message)
            return
        fut = self._pending.pop(uname, None)
        if fut and not fut.done():
            fut.set_result(message)

    async def _on_any_message(self, _client, message) -> None:
        """Наблюдатель: складывает КАЖДОЕ сообщение (входящее и своё) в кольцевой
        буфер для кнопки «📨 Сообщения». Видно только владельцу аккаунта в боте.
        Ничего не решает, ничему не мешает."""
        try:
            self.recent_messages.append({
                "time": clock(),
                "dir": "out" if getattr(message, "outgoing", False) else "in",
                "chat": html.escape(chat_label(message.chat)),
                "text": html.escape(msg_preview(message))[:120],
            })
        except Exception:
            pass

    def recent_lines(self, limit: int = 15) -> list[str]:
        items = list(self.recent_messages)[-limit:]
        items.reverse()  # новые сверху
        if not items:
            return ["(пока пусто)"]
        return [
            f"[{e['time']}] {'🔼' if e['dir'] == 'out' else '🔽'} <b>{e['chat']}</b>: {e['text']}"
            for e in items
        ]

    async def _send_and_wait(self, username: str, text: str, timeout: int = 25):
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[username.lower()] = fut
        await self.client.send_message(username, text)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(username.lower(), None)
            return None

    async def _wait_next(self, username: str, timeout: int = 12):
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[username.lower()] = fut
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(username.lower(), None)
            return None

    # ---------- примитивы режима трейда (использует farm.py / trade.py) ----------
    def enter_trade_mode(self) -> None:
        self._trade_queue = asyncio.Queue()
        self._trade_mode = True

    def exit_trade_mode(self) -> None:
        self._trade_mode = False
        self._trade_queue = None

    async def trade_send(self, text: str) -> None:
        await self.client.send_message(CARDS_BOT, text)

    async def trade_wait(self, predicate, timeout: float):
        end = time.time() + timeout
        while True:
            remaining = end - time.time()
            if remaining <= 0 or self._trade_queue is None:
                return None
            try:
                msg = await asyncio.wait_for(self._trade_queue.get(), remaining)
            except asyncio.TimeoutError:
                return None
            try:
                if predicate(msg):
                    return msg
            except Exception:
                continue

    # ---------- клик по кнопке ----------
    async def _try_click(self, message, button_text: str) -> bool:
        """Жмёт кнопку по подстроке (учёт эмодзи). Терпит отсутствие callback-ответа."""
        if message is None or not getattr(message, "reply_markup", None):
            return False
        target = None
        sub = button_text.lower()
        for row in message.reply_markup.inline_keyboard:
            for b in row:
                if sub in (getattr(b, "text", "") or "").lower():
                    target = b.text
                    break
            if target:
                break
        if not target:
            return False
        try:
            await message.click(target)
            return True
        except asyncio.TimeoutError:
            return True  # нет callback-ответа, но нажатие прошло
        except Exception:
            return False

    # ---------- инфо для меню ----------
    def _remaining(self, ts: float, ready: str = "готово") -> str:
        if not self.running:
            return "—"
        rem = ts - time.time()
        return fmt_duration(int(rem)) if rem > 0 else ready


class AccountWorker(FarmModule, AutosendModule, _WorkerBase):
    """Полный воркер аккаунта = 🌾 Фарм карточек (farm.py) + 📨 Автоотправка
    (autosend.py) + базовое подключение (этот файл). Модули независимы: каждый
    включается/выключается своим тумблером (farm_enabled / autosend_enabled)."""
    pass
