"""Базовый воркер аккаунта: подключение к Telegram, общие примитивы обмена
сообщениями, статистика, лог последних сообщений (виден только владельцу).

Игровая автоматизация живёт в farm.py (FarmModule) — карточки/рулетка/майнинг/
контейнеры/вывод/трейд. Автоотправка живёт в autosend.py (AutosendModule) —
произвольные задачи по расписанию + «.trade»/«.pay» из личек. Модули НЕЗАВИСИМЫ:
каждый включается/выключается своим тумблером (farm_enabled / autosend_enabled)
и живёт в своих файлах/хендлерах, не пересекаясь друг с другом.

Этот файл сознательно не читает контакты и историю чужой переписки — только то,
что нужно для игровой логики, автоотправки и владельца собственного аккаунта
(ответы ботов, свои же сообщения, свой номер телефона через get_account_info()).
"""
from __future__ import annotations

import asyncio
import html
import time
from typing import Any

from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler, EditedMessageHandler

from storage import CARDS_BOT, ACTION_DELAY
from common import MSK, parse_hhmm, seconds_until_msk, fmt_duration, clock
from farm import FarmModule
from autosend import AutosendModule
from shop import ShopModule
from repair import RepairModule
from containers_api import ContainersApiModule

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
        shop_cfg: dict | None = None,
        repair_cfg: dict | None = None,
        farm_maintenance_cfg: dict | None = None,
        exchange_cfg: dict | None = None,
        avito_cfg: dict | None = None,
        containers_api_cfg: dict | None = None,
    ) -> None:
        self.account = account                 # ссылка на словарь из Storage (читаем «вживую»)
        self.storage = storage
        self.good_keywords = [k.lower() for k in (good_keywords or [])]
        self.payout_delay = payout_delay
        self.container_cfg = container_cfg or {}
        self.self_commands_cfg = self_commands_cfg or {}
        self.shop_cfg = shop_cfg or {}
        self.repair_cfg = repair_cfg or {}
        self.exchange_cfg = exchange_cfg or {}
        self.farm_maintenance_cfg = farm_maintenance_cfg or {}
        self.containers_api_cfg = containers_api_cfg or {}
        self.avito_cfg = avito_cfg or {}

        self.client: Client | None = None
        self.running = False
        self.trade_runner = None               # callable(farm_id) -> coroutine (ставит Manager)
        self.alert_fn = None                    # callable(owner_id, text, markup) -> coroutine (ставит Manager)

        self._tasks: list[asyncio.Task] = []
        self._pending: dict[str, list[asyncio.Future]] = {}  # username -> очередь ожидающих (FIFO)
        self._bot_locks: dict[str, asyncio.Lock] = {}        # username -> лок на «отправил/кликнул + жду ответ»
        self._trade_mode = False
        self._trade_queue: asyncio.Queue | None = None
        self._task_next: dict[int, float] = {}   # tid -> время следующего запуска
        self._container_alert_sent = False       # чтобы не слать «ресток скоро» повторно на один и тот же ресток

        now = 0.0
        self.card_next_ts = now
        self.roulette_next_ts = now
        self.mining_next_ts = now
        self.autopay_next_ts = now
        self.autotrade_next_ts = now
        self.container_next_ts = now
        self.shop_next_ts = now
        self.repair_next_ts = now
        self.pcoin_exchange_next_ts = now
        self.farm_maintenance_next_ts = now
        self.farm_last_power_off_ts = now  # см. farm_maintenance_now — не выключать ферму чаще
                                            # min_power_toggle_interval (сбрасывает часовой майнинг-таймер)
        self.status = "остановлен"
        self.last_card = "—"
        self.last_roulette = "—"
        self.last_mining = "—"
        self.last_payout = "—"
        self.last_exchange = "—"
        self.last_container = "—"
        self.last_daily = "—"
        self.last_self_cmd = "—"
        self.last_shop = "—"
        self.last_repair = "—"
        self.last_upgrade = "—"
        self.last_accept = "—"
        self.last_farm_maintenance = "—"
        self.last_pcoin_exchange = "—"
        self.last_power_watchdog = "—"
        self.last_containers_api = "—"

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
            f"💸 {st.get('paid', 0)} | 🔄 {st.get('exchanged', 0)} | 📦 {st.get('containers_bought', 0)}"
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
        # «Акк»: пересылка новых личных сообщений от людей владельцу аккаунта через
        # управляющего бота — включается тумблером «📩 Акк» (только для аккаунтов
        # создателя), пригодится, когда нет прямого доступа к самому Telegram-аккаунту.
        self.client.add_handler(
            MessageHandler(self._on_incoming_private,
                            filters.incoming & filters.private & ~filters.bot), group=2
        )
        await self.client.start()

        try:  # запомним tg_id/username (нужно для трейда и определения владельца)
            me = await self.client.get_me()
            live_username = me.username or ""
            # раньше username обновлялся, только если раньше было ПУСТО — если человек
            # сменил @username в Telegram, сохранённое значение никогда не подтягивалось
            # заново, и _match_own_worker (см. manager.py) переставал узнавать этот
            # аккаунт как «свой» у получателей трейда (обмен зависал до таймаута, никто
            # не нажимал «Принять» — репорт пользователя)
            if self.account.get("tg_id") != me.id or self.account.get("username") != live_username:
                self.account["tg_id"] = me.id
                self.account["username"] = live_username
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
        for futs in self._pending.values():
            for fut in futs:
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

    # ---------- проактивные сообщения бота (не ответ на наш send/click) ----------
    def _is_proactive(self, message) -> bool:
        """Хук: True, если это сообщение бот прислал САМ, не в ответ на что-то наше
        (например, входящий запрос на ремонт от клиента) — такие НЕ должны попадать
        в FIFO-очередь _pending, иначе могут перехватить ответ, который реально кто-то
        ждёт. Переопределяется миксинами (см. RepairModule)."""
        return False

    async def _handle_proactive(self, message) -> None:
        pass

    # ---------- приём сообщений ----------
    async def _on_message(self, _client, message) -> None:
        uname = (getattr(message.chat, "username", "") or "").lower()
        if self._trade_mode and self._trade_queue is not None:
            # во время трейда копим только сообщения игрового бота карточек
            if uname == CARDS_BOT.lower():
                await self._trade_queue.put(message)
            return
        if self._is_proactive(message):
            await self._handle_proactive(message)
            return
        # НЕСКОЛЬКО циклов (карточки/майнинг/контейнеры/.trade-.pay) могут одновременно
        # ждать ответ от ОДНОГО и того же бота (чаще всего phonegetcardsbot). Раньше тут
        # был один слот на username — второй ожидающий просто затирал future первого, и
        # тот уходил в таймаут, как будто бот не ответил (внешне выглядело так, будто
        # именно «Ткарточка» не работает, хотя дело было в конкуренции с параллельным
        # циклом). Поэтому ожидающие — очередь FIFO: раздаём ответы по порядку регистрации.
        queue = self._pending.get(uname)
        if queue:
            fut = queue.pop(0)
            if not queue:
                self._pending.pop(uname, None)
            if not fut.done():
                fut.set_result(message)

    async def _on_incoming_private(self, _client, message) -> None:
        """«📩 Акк»: новое личное сообщение от человека (не бота) — сохраняем в
        историю (видна в разделе «📩 Акк» карточки аккаунта) и дублируем владельцу
        алертом через управляющего бота; сам аккаунт не отвечает."""
        if not self.account.get("msg_alert_enabled", False):
            return
        sender = message.from_user
        who = f"@{sender.username}" if sender and sender.username else (
            (sender.first_name if sender else None) or "—")
        body = message.text or message.caption or "[медиа]"

        log = self.account.setdefault("msg_log", [])
        log.append({"from": who, "text": body[:500], "ts": time.strftime("%d.%m %H:%M")})
        del log[:-20]  # держим только последние 20
        if self.storage:
            try:
                self.storage.save()
            except Exception:
                pass

        owner_id = self.account.get("owner_id")
        if not owner_id or not self.alert_fn:
            return
        text = (f"📩 <b>{self.name}</b> — сообщение от {html.escape(who)}:\n"
                f"{html.escape(body)}")
        try:
            await self.alert_fn(owner_id, text, None)
        except Exception as e:  # noqa: BLE001
            print(f"[{self.name}] не удалось отправить алерт о сообщении: {e}")

    async def get_account_info(self) -> dict[str, Any] | None:
        """Читает у Telegram актуальные данные аккаунта (в т.ч. номер телефона)
        через уже авторизованную сессию — SMS/повторный вход не нужны. Виден
        только владельцу аккаунта (проверка владения — в controlbot.py)."""
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

    def _register_wait(self, username: str) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending.setdefault(username.lower(), []).append(fut)
        return fut

    def _forget_wait(self, username: str, fut: asyncio.Future) -> None:
        queue = self._pending.get(username.lower())
        if not queue:
            return
        try:
            queue.remove(fut)
        except ValueError:
            pass
        if not queue:
            self._pending.pop(username.lower(), None)

    def _lock_for_bot(self, username: str) -> asyncio.Lock:
        key = username.lower()
        lock = self._bot_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._bot_locks[key] = lock
        return lock

    async def _send_and_wait(self, username: str, text: str, timeout: int = 25, throttle: bool = True):
        # Лок на бота держим ВЕСЬ круг «отправил -> получил ответ»: несколько циклов
        # (карточки/майнинг/контейнеры/такс/ручной перевод) ходят в ОДНОГО бота
        # (phonegetcardsbot) — без лока параллельная отправка могла зарегистрировать
        # своё ожидание раньше нашего и перехватить наш же ответ (внешне выглядело
        # как «Ткарточка не работает» или как будто карточка распарсила ответ магазина).
        async with self._lock_for_bot(username):
            if throttle:
                await asyncio.sleep(ACTION_DELAY)
            fut = self._register_wait(username)
            await self.client.send_message(username, text)
            try:
                return await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                self._forget_wait(username, fut)
                return None

    async def _click_and_wait(
        self, message, button_text: str, bot: str, timeout: int = 12,
    ) -> tuple[bool, Any]:
        """Клик по кнопке + ожидание следующего сообщения ОТ ЭТОГО БОТА — атомарно
        под локом бота на всём участке (клик и регистрация ожидания без зазора между
        ними), чтобы параллельный запрос к тому же боту не перехватил ответ на наш
        клик. Возвращает (clicked, result); result is None — клик прошёл, но бот не
        ответил за timeout."""
        async with self._lock_for_bot(bot):
            clicked = await self._try_click(message, button_text)
            if not clicked:
                return False, None
            fut = self._register_wait(bot)
            try:
                return True, await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                self._forget_wait(bot, fut)
                return True, None

    async def _click_and_wait_for_button(
        self, message, button_text: str, bot: str, target_button: str,
        timeout: int = 12, extra_waits: int = 2,
    ) -> tuple[bool, Any]:
        """Как _click_and_wait, но если ответ не содержит target_button (напр.
        «Подтвердить») — не сдаётся сразу, а ждёт ещё до extra_waits следующих
        сообщений от бота (под тем же локом). Обнаружено вживую на /pay (см.
        farm.py._execute_pay): игра иногда шлёт промежуточное сообщение ПЕРЕД
        настоящим диалогом подтверждения, и старый код, беря только первый ответ,
        решал, что подтверждение не нужно, — а реальный диалог оставался висеть
        ненажатым. Если у промежуточного сообщения ЕСТЬ какие-то СВОИ кнопки (не
        target_button) — останавливаемся сразу: это, похоже, другой экран, а не
        промежуточная заглушка. Возвращает (clicked_target, result) — result это
        последнее увиденное сообщение, clicked_target — была ли реально нажата
        target_button."""
        async with self._lock_for_bot(bot):
            clicked = await self._try_click(message, button_text)
            if not clicked:
                return False, None
            msg = None
            for _ in range(1 + max(0, extra_waits)):
                fut = self._register_wait(bot)
                try:
                    msg = await asyncio.wait_for(fut, timeout)
                except asyncio.TimeoutError:
                    self._forget_wait(bot, fut)
                    return False, msg
                mk = getattr(msg, "reply_markup", None)
                rows = mk.inline_keyboard if mk and getattr(mk, "inline_keyboard", None) else []
                btn = next(
                    (b.text for row in rows for b in row
                     if target_button.lower() in (getattr(b, "text", "") or "").lower()),
                    None,
                )
                if btn:
                    # раньше здесь просто возвращали msg (диалог ДО клика по target_button),
                    # из-за чего вызывающий код (farm.py.upgrade_account) принимал диалог
                    # «Вы уверены?» за результат покупки, не находил в нём кнопок следующего
                    # шага и решал, что всё сломалось, — а реальный ответ на подтверждение
                    # никто не забирал (баг: прокачка на деле не покупала уровни)
                    ok = await self._try_click(msg, btn)
                    if not ok:
                        return False, msg
                    fut2 = self._register_wait(bot)
                    try:
                        result = await asyncio.wait_for(fut2, timeout)
                    except asyncio.TimeoutError:
                        self._forget_wait(bot, fut2)
                        return True, None
                    return True, result
                if rows:
                    return False, msg  # другой экран с другими кнопками — не промежуточное сообщение
            return False, msg

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
        """Жмёт кнопку по подстроке (учёт эмодзи). Терпит отсутствие callback-ответа.

        Точное совпадение текста кнопки проверяется ПЕРВЫМ и имеет приоритет над
        подстрокой: короткая метка вроде «Слот 1» иначе может подстрокой случайно
        поймать «Слот 10»/«Слот 11»/«Слот 12» (та же проблема, что уже чинили
        числовыми кнопками в farm.py/shop.py — см. _numeric_button).

        Общая пауза ACTION_DELAY перед каждым кликом — единственный общий путь
        всех кликов по кнопкам во всём боте (магазин контейнеров через HTTP API
        сюда не заходит вообще, см. containers_api.py — там своя скорость)."""
        if message is None or not getattr(message, "reply_markup", None):
            return False
        await asyncio.sleep(ACTION_DELAY)
        target = None
        for row in message.reply_markup.inline_keyboard:
            for b in row:
                if (getattr(b, "text", "") or "") == button_text:
                    target = b.text
                    break
            if target:
                break
        if not target:
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


class AccountWorker(FarmModule, AutosendModule, ShopModule, RepairModule, ContainersApiModule, _WorkerBase):
    """Полный воркер аккаунта = 🌾 Фарм карточек (farm.py) + 📨 Автоотправка
    (autosend.py) + 🏪 Магазин телефонов (shop.py) + 🛠 Мастерская (repair.py) +
    базовое подключение (этот файл). Модули независимы: каждый включается/
    выключается своим тумблером (farm_enabled / autosend_enabled / phone_shop_enabled /
    auto_repair_enabled / auto_accept_enabled)."""
    pass
