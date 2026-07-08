"""Воркер одного аккаунта: карточки, рулетка, майнинг, вывод очков, примитивы трейда."""
from __future__ import annotations

import asyncio
import html
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler, EditedMessageHandler

from storage import (
    CARDS_BOT, ROULETTE_BOT,
    CARD_WORD, ROULETTE_WORD, ROULETTE_BUTTON,
    MINING_WORD, MINING_BUTTON, MINING_HOUR, MINING_MINUTE,
    BALANCE_WORD, PAY_CONFIRM_BUTTON, CONTAINER_WORD,
    DAILY_REWARD_WORD, DAILY_REWARD_BUTTON,
)

MSK = timezone(timedelta(hours=3))
BUFFER_SEC = 5  # буфер к кулдауну, чтобы не упереться ровно в секунду
MSG_LOG_SIZE = 30  # сколько последних сообщений держим в памяти на аккаунт

# "через 1 ч 23 мин 45 сек", "через 59 мин", "через 12 сек"
_TIME_RE = re.compile(
    r"через\s*(?:(\d+)\s*ч[а-я.]*)?\s*(?:(\d+)\s*м[а-я.]*)?\s*(?:(\d+)\s*с[а-я.]*)?",
    re.IGNORECASE,
)
_POINTS_RE = re.compile(r"точки\D*?(\d[\d\s.,]*)", re.IGNORECASE)

# "через 3 дн. 5 ч. 10 мин." (магазин контейнеров — с днями, без секунд)
_SHOP_TIME_RE = re.compile(
    r"через\s*(?:(\d+)\s*дн[а-я.]*)?\s*(?:(\d+)\s*ч[а-я.]*)?\s*(?:(\d+)\s*мин[а-я.]*)?",
    re.IGNORECASE,
)


def parse_shop_cooldown(text: str | None) -> int | None:
    if not text:
        return None
    m = _SHOP_TIME_RE.search(text)
    if not m:
        return None
    d, h, mi = (int(x) if x else 0 for x in m.groups())
    total = d * 86400 + h * 3600 + mi * 60
    return total or None


# ---------- парсеры ----------
def parse_cooldown(text: str | None) -> int | None:
    if not text:
        return None
    m = _TIME_RE.search(text)
    if not m:
        return None
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    total = h * 3600 + mi * 60 + s
    return total or None


def parse_points(text: str | None) -> int | None:
    if not text:
        return None
    m = _POINTS_RE.search(text)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def is_phone_won(text: str | None) -> bool:
    return bool(text) and "выпал телефон" in text.lower()


def _contains_any(text: str | None, keywords: list[str]) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(k in low for k in keywords)


def _parse_hhmm(value: str | None) -> tuple[int, int]:
    if value:
        m = re.match(r"^\s*(\d{1,2})[:.\s](\d{1,2})\s*$", value)
        if m:
            h, mi = int(m.group(1)), int(m.group(2))
            if 0 <= h < 24 and 0 <= mi < 60:
                return h, mi
    return MINING_HOUR, MINING_MINUTE


def _seconds_until_msk(hour: int, minute: int) -> float:
    now = datetime.now(MSK)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _fmt(seconds: int) -> str:
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


def _clock() -> str:
    return time.strftime("%H:%M:%S")


def _today() -> str:
    return datetime.now(MSK).strftime("%d.%m")


def _chat_label(chat) -> str:
    uname = getattr(chat, "username", None)
    if uname:
        return f"@{uname}"
    title = getattr(chat, "title", None)
    if title:
        return title
    first = getattr(chat, "first_name", None)
    if first:
        return first
    return str(getattr(chat, "id", "чат"))


def _msg_preview(message) -> str:
    text = getattr(message, "text", None) or getattr(message, "caption", None)
    if text:
        return text
    media = getattr(message, "media", None)
    if media:
        return f"[{str(media).rsplit('.', 1)[-1].lower()}]"
    return "[сообщение]"


class AccountWorker:
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
        self.recent_messages: deque = deque(maxlen=MSG_LOG_SIZE)  # лог всех сообщений аккаунта

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
        # свои же сообщения в личках («.trade», «.pay 505050») -> команда боту карточек.
        # Отдельная группа, чтобы не пересекаться с обработкой ответов ботов.
        self.client.add_handler(
            MessageHandler(self._on_self_command, filters.me & filters.private), group=1
        )
        # постоянный лог последних сообщений на аккаунте (для кнопки «📨 Сообщения»),
        # отдельная группа — просто наблюдатель, ничего не решает и не блокирует
        self.client.add_handler(MessageHandler(self._on_any_message, filters.all), group=2)
        await self.client.start()

        try:  # запомним tg_id/username (нужно для трейда и пометки главного)
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
        self._tasks = [
            asyncio.create_task(self._card_loop()),
            asyncio.create_task(self._roulette_loop()),
            asyncio.create_task(self._mining_loop()),
            asyncio.create_task(self._container_loop()),
            asyncio.create_task(self._tasks_loop()),
        ]
        print(f"[{self.name}] запущен")

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

    async def _on_self_command(self, _client, message) -> None:
        """Своё сообщение вида «.trade» / «.pay 505050» в личке с кем-то -> шлём
        соответствующую команду боту карточек (получатель = собеседник этой личке)
        и, если бот попросит подтверждение кнопкой «Подтвердить», жмём её сами."""
        if not self.account.get("enabled", True) or not self.account.get(
            "self_commands_enabled", True
        ):
            return
        if self._trade_mode:
            self.last_self_cmd = "идёт трейд — попробуй чуть позже"
            return
        text = (message.text or "").strip()
        if not text.startswith("."):
            return
        chat_uname = (getattr(message.chat, "username", "") or "")
        if chat_uname.lower() in (CARDS_BOT.lower(), ROULETTE_BOT.lower()):
            return  # не трогаем переписку с самими игровыми ботами
        parts = text[1:].split(maxsplit=1)
        if not parts:
            return
        verb = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        tmpl = self.self_commands_cfg.get(verb)
        if not tmpl:
            return
        target = f"@{chat_uname}" if chat_uname else str(message.chat.id)
        cmd = tmpl.replace("{target}", target).replace("{arg}", arg)
        who = chat_uname or message.chat.id
        try:
            reply = await self._send_and_wait(CARDS_BOT, cmd, timeout=15)
            confirmed = " + подтверждено ✅" if await self._try_click(
                reply, PAY_CONFIRM_BUTTON
            ) else ""
            self.last_self_cmd = f"✅ .{verb} ({who}) -> {cmd}{confirmed} ({_clock()})"
        except Exception as e:  # noqa: BLE001
            self.last_self_cmd = f"ошибка .{verb}: {e}"

    async def _on_any_message(self, _client, message) -> None:
        """Наблюдатель: складывает КАЖДОЕ сообщение (входящее и своё) в кольцевой
        буфер для кнопки «📨 Сообщения». Ничего не решает, ничему не мешает."""
        try:
            self.recent_messages.append({
                "time": _clock(),
                "dir": "out" if getattr(message, "outgoing", False) else "in",
                "chat": html.escape(_chat_label(message.chat)),
                "text": html.escape(_msg_preview(message))[:120],
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

    # ---------- примитивы режима трейда ----------
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

    # ---------- цикл карточек ----------
    async def _card_loop(self) -> None:
        while self.running:
            try:
                if self._trade_mode:
                    await asyncio.sleep(2)
                    continue
                if not self.account.get("enabled", True) or not self.account.get("card_enabled", True):
                    await asyncio.sleep(20)
                    continue
                now = time.time()
                if now < self.card_next_ts:
                    await asyncio.sleep(min(30, self.card_next_ts - now))
                    continue

                reply = await self._send_and_wait(CARDS_BOT, CARD_WORD)
                text = getattr(reply, "text", None) or getattr(reply, "caption", None)
                cd = parse_cooldown(text)
                default = int(self.account.get("card_interval", 3600))
                self.card_next_ts = time.time() + (cd if cd is not None else default) + BUFFER_SEC

                if is_phone_won(text):
                    self._bump("phones")
                    good = _contains_any(text, self.good_keywords)
                    if good:
                        self._bump("good_phones")
                    label = (text or "телефон").split("\n")[0][:60]
                    self.last_card = f"🎉 {label}{' ⭐' if good else ''} ({_clock()})"
                elif cd is not None:
                    self.last_card = f"⏳ кулдаун {_fmt(cd)} ({_clock()})"
                elif reply is None:
                    self.last_card = f"⚠️ нет ответа ({_clock()})"
                else:
                    self.last_card = f"отправлено ({_clock()})"
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_card = f"ошибка: {e}"
                self.card_next_ts = time.time() + 60
                await asyncio.sleep(5)

    # ---------- цикл рулетки ----------
    async def _roulette_loop(self) -> None:
        while self.running:
            try:
                if self._trade_mode:
                    await asyncio.sleep(2)
                    continue
                if not self.account.get("enabled", True) or not self.account.get("roulette_enabled", True):
                    await asyncio.sleep(20)
                    continue
                now = time.time()
                if now < self.roulette_next_ts:
                    await asyncio.sleep(min(30, self.roulette_next_ts - now))
                    continue

                reply = await self._send_and_wait(ROULETTE_BOT, ROULETTE_WORD)
                text = getattr(reply, "text", None) or getattr(reply, "caption", None)
                default = int(self.account.get("roulette_interval", 3600))

                clicked = await self._try_click(reply, ROULETTE_BUTTON)
                if clicked:
                    self._bump("roulette")
                    result = await self._wait_next(ROULETTE_BOT, timeout=12)
                    rtext = (getattr(result, "text", None)
                             or getattr(result, "caption", None) or text)
                    cd = parse_cooldown(rtext)
                    delay = cd if cd is not None else default
                    self.last_roulette = f"🎰 крутили, кулдаун {_fmt(delay)} ({_clock()})"
                else:
                    cd = parse_cooldown(text)
                    delay = cd if cd is not None else default
                    self.last_roulette = (f"⚠️ нет ответа ({_clock()})" if reply is None
                                          else f"⏳ кулдаун {_fmt(delay)} ({_clock()})")
                self.roulette_next_ts = time.time() + delay + BUFFER_SEC
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_roulette = f"ошибка: {e}"
                self.roulette_next_ts = time.time() + 60
                await asyncio.sleep(5)

    # ---------- цикл майнинга ----------
    async def _mining_loop(self) -> None:
        """Раз в сутки в заданное по МСК время. Опрос раз в 20с — время меняется на лету."""
        last_fired = None
        while self.running:
            try:
                if self._trade_mode:
                    await asyncio.sleep(2)
                    continue
                hour, minute = _parse_hhmm(self.account.get("mining_time"))
                self.mining_next_ts = time.time() + _seconds_until_msk(hour, minute)

                now = datetime.now(MSK)
                due = now.hour == hour and now.minute == minute and last_fired != now.date()
                if due and self.account.get("enabled", True):
                    last_fired = now.date()
                    if self.account.get("mining_enabled", True):
                        clicked = await self.collect_mining()
                        if clicked and self.account.get("autopay_enabled", True):
                            await self._payout()
                        if clicked and self.account.get("autotrade_enabled", False) and self.trade_runner:
                            try:
                                await self.trade_runner(self.id)
                            except Exception as e:  # noqa: BLE001
                                self.last_exchange = f"ошибка авто-трейда: {e}"
                    if self.account.get("daily_reward_enabled", True):
                        await self.collect_daily_reward()
                await asyncio.sleep(20)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_mining = f"ошибка: {e}"
                await asyncio.sleep(20)

    # ---------- магазин контейнеров ----------
    async def _container_loop(self) -> None:
        """Раз в кулдаун шлёт «Магазин контейнеров». Если ответ — стандартное
        «раскуплены, след. через X» — просто ждёт это время. Если ответ ДРУГОЙ
        (контейнеры есть в наличии / нужна капча) — оповещает владельца и ждёт
        unknown_retry перед следующей попыткой."""
        while self.running:
            try:
                if self._trade_mode:
                    await asyncio.sleep(2)
                    continue
                if not self.account.get("enabled", True) or not self.account.get(
                    "containers_enabled", False
                ):
                    await asyncio.sleep(20)
                    continue
                now = time.time()
                if now < self.container_next_ts:
                    await asyncio.sleep(min(30, self.container_next_ts - now))
                    continue
                await self.check_containers()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_container = f"ошибка: {e}"
                self.container_next_ts = time.time() + 60
                await asyncio.sleep(5)

    async def check_containers(self) -> str:
        """«Магазин контейнеров» -> распознать «раскуплены, след. через X» и ждать,
        либо (неожиданный ответ) оповестить владельца. Используется циклом и
        кнопкой «Проверить сейчас». Возвращает last_container."""
        if not self.client or not self.running:
            self.last_container = "аккаунт не запущен"
            return self.last_container
        cfg = self.container_cfg
        bot = cfg.get("bot") or CARDS_BOT
        marker = cfg.get("sold_out_marker", "раскуплены")
        unknown_retry = int(cfg.get("unknown_retry", 600))
        try:
            reply = await self._send_and_wait(bot, CONTAINER_WORD)
            text = getattr(reply, "text", None) or getattr(reply, "caption", None)
            if text and marker.lower() in text.lower():
                cd = parse_shop_cooldown(text)
                delay = cd if cd is not None else unknown_retry
                self.container_next_ts = time.time() + delay + BUFFER_SEC
                self.last_container = f"⏳ раскуплены, след. через {_fmt(delay)} ({_clock()} {_today()})"
            elif reply is None:
                self.container_next_ts = time.time() + unknown_retry
                self.last_container = f"⚠️ нет ответа ({_clock()})"
            else:
                # неожиданный ответ: возможно есть в наличии / нужна капча — зовём владельца
                self.container_next_ts = time.time() + unknown_retry
                self.last_container = f"🚨 неожиданный ответ, оповестил владельца ({_clock()} {_today()})"
                await self._notify_owner_captcha(cfg, text)
        except Exception as e:  # noqa: BLE001
            self.container_next_ts = time.time() + 60
            self.last_container = f"ошибка: {e}"
        return self.last_container

    async def _notify_owner_captcha(self, cfg: dict, shop_text: str | None) -> None:
        owner_id = self.account.get("owner_id")
        if not owner_id:
            return
        alert = cfg.get("captcha_alert_text", "КАПЧА")
        preview = f"\n\n«{shop_text[:200]}»" if shop_text else ""
        try:
            await self.client.send_message(owner_id, f"{alert} — «{self.name}»{preview}")
        except Exception as e:  # noqa: BLE001
            print(f"[{self.name}] не удалось оповестить владельца: {e}")

    # ---------- действия ----------
    async def get_account_info(self) -> dict[str, Any] | None:
        """Читает у Telegram актуальные данные аккаунта (в т.ч. номер телефона)
        через уже авторизованную сессию — SMS/повторный вход не нужны."""
        if not self.client or not self.running:
            return None
        try:
            me = await self.client.get_me()
            return {
                "phone": me.phone_number,
                "id": me.id,
                "username": me.username,
                "first_name": me.first_name,
                "last_name": me.last_name,
                "is_premium": bool(getattr(me, "is_premium", False)),
            }
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}

    async def collect_mining(self) -> bool:
        """«Тмайнинг» -> «Снять деньги с фермы». True при успехе."""
        if not self.client or not self.running:
            self.last_mining = "аккаунт не запущен"
            return False
        if self._trade_mode:
            self.last_mining = "идёт трейд — позже"
            return False
        try:
            reply = await self._send_and_wait(CARDS_BOT, MINING_WORD)
            clicked = await self._try_click(reply, MINING_BUTTON)
            if clicked:
                self._bump("mining")
                self.last_mining = f"💰 снято с фермы ({_clock()} {_today()})"
            elif reply is None:
                self.last_mining = f"⚠️ нет ответа ({_clock()} {_today()})"
            else:
                self.last_mining = f"⚠️ кнопка не найдена — рано/уже собрано ({_clock()} {_today()})"
            return clicked
        except Exception as e:  # noqa: BLE001
            self.last_mining = f"ошибка: {e}"
            return False

    async def collect_daily_reward(self) -> bool:
        """«Ежедневная награда» -> «Забрать». True при успехе. Собирается вместе
        с майнингом (тот же триггер по времени), но включается отдельным тогглом."""
        if not self.client or not self.running:
            self.last_daily = "аккаунт не запущен"
            return False
        if self._trade_mode:
            self.last_daily = "идёт трейд — позже"
            return False
        try:
            reply = await self._send_and_wait(CARDS_BOT, DAILY_REWARD_WORD)
            clicked = await self._try_click(reply, DAILY_REWARD_BUTTON)
            if clicked:
                self._bump("daily")
                self.last_daily = f"🎁 забрана ({_clock()} {_today()})"
            elif reply is None:
                self.last_daily = f"⚠️ нет ответа ({_clock()} {_today()})"
            else:
                self.last_daily = f"⚠️ кнопка не найдена — уже забрана? ({_clock()} {_today()})"
            return clicked
        except Exception as e:  # noqa: BLE001
            self.last_daily = f"ошибка: {e}"
            return False

    async def _payout(self) -> None:
        """«такк» -> «Точки: N» -> /pay <получатель> N -> Подтвердить.
        Получатель — payout_target, который пользователь задал сам (@username или id)."""
        try:
            target = (self.account.get("payout_target") or "").strip()
            if not target:
                self.last_payout = "⚠️ получатель вывода не задан (🎯 Получатели)"
                return
            await asyncio.sleep(max(0, int(self.payout_delay)))
            if not self.running:
                return
            bal = await self._send_and_wait(CARDS_BOT, BALANCE_WORD)
            amount = parse_points(getattr(bal, "text", None) or getattr(bal, "caption", None))
            if not amount or amount <= 0:
                self.last_payout = f"нет очков для вывода ({_clock()})"
                return
            pay = await self._send_and_wait(CARDS_BOT, f"/pay {target} {amount}")
            if await self._try_click(pay, PAY_CONFIRM_BUTTON):
                self._bump("paid", amount)
                self.last_payout = f"💸 выведено {amount} -> {target} ({_clock()} {_today()})"
            elif pay is None:
                self.last_payout = f"⚠️ нет ответа на /pay ({_clock()})"
            else:
                self.last_payout = f"⚠️ кнопка Подтвердить не найдена ({_clock()})"
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.last_payout = f"ошибка вывода: {e}"

    # ---------- произвольные задачи пользователя ----------
    def _task_init_next(self, task: dict) -> float:
        if task.get("mode") == "daily":
            h, m = _parse_hhmm(task.get("time"))
            return time.time() + _seconds_until_msk(h, m)
        return time.time()  # интервальная: запустить сразу

    def _task_after_next(self, task: dict) -> float:
        if task.get("mode") == "daily":
            h, m = _parse_hhmm(task.get("time"))
            return time.time() + _seconds_until_msk(h, m)
        return time.time() + max(10, int(task.get("interval", 3600)))

    async def _tasks_loop(self) -> None:
        while self.running:
            try:
                if self._trade_mode or not self.account.get("enabled", True):
                    await asyncio.sleep(15)
                    continue
                now = time.time()
                for task in list(self.account.get("tasks", [])):
                    if not task.get("enabled", True):
                        continue
                    tid = task.get("id")
                    nxt = self._task_next.get(tid)
                    if nxt is None:
                        nxt = self._task_init_next(task)
                        self._task_next[tid] = nxt
                    if now >= nxt:
                        await self._run_task(task)
                        self._task_next[tid] = self._task_after_next(task)
                await asyncio.sleep(15)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                await asyncio.sleep(15)

    async def _run_task(self, task: dict) -> bool:
        bot = (task.get("bot") or "").lstrip("@").strip()
        text = task.get("text") or ""
        if not bot or not text:
            task["last"] = "⚠️ не задан бот/текст"
            return False
        if not self.client or not self.running:
            return False
        try:
            reply = await self._send_and_wait(bot, text)
            clicked = ""
            if task.get("click"):
                ok = await self._try_click(reply, task["click"])
                clicked = " + кнопка ✅" if ok else " + кнопка ⚠️"
            task["last"] = f"✅ '{text}' → @{bot}{clicked} ({_clock()})"
            if self.storage:
                self.storage.save()
            return True
        except Exception as e:  # noqa: BLE001
            task["last"] = f"ошибка: {e}"
            return False

    async def run_task_now(self, tid: int) -> str:
        task = next((t for t in self.account.get("tasks", []) if t.get("id") == tid), None)
        if not task:
            return "задача не найдена"
        await self._run_task(task)
        self._task_next[tid] = self._task_after_next(task)
        return task.get("last", "—")

    def task_remaining(self, task: dict) -> str:
        if not self.running:
            return "—"
        nxt = self._task_next.get(task.get("id"))
        if nxt is None:
            return "скоро"
        rem = nxt - time.time()
        return _fmt(int(rem)) if rem > 0 else "сейчас"

    # ---------- инфо для меню ----------
    def _remaining(self, ts: float, ready: str = "готово") -> str:
        if not self.running:
            return "—"
        rem = ts - time.time()
        return _fmt(int(rem)) if rem > 0 else ready

    def card_remaining(self) -> str:
        return self._remaining(self.card_next_ts)

    def roulette_remaining(self) -> str:
        return self._remaining(self.roulette_next_ts)

    def mining_remaining(self) -> str:
        return self._remaining(self.mining_next_ts, "скоро")

    def container_remaining(self) -> str:
        return self._remaining(self.container_next_ts, "скоро")
