"""Модуль «Фарм карточек» — вся игровая автоматизация PhoneGet: карточки,
рулетка, майнинг, ежедневная награда, магазин контейнеров, вывод очков, трейд.

Независимый модуль: включается/выключается тумблером farm_enabled, не трогает
автоотправку (autosend.py). Не читает контакты, историю сообщений и номер
телефона — только то, что нужно для игровой логики (ответы игровых ботов).
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from storage import (
    CARDS_BOT, ROULETTE_BOT,
    CARD_WORD, ROULETTE_WORD, ROULETTE_BUTTON,
    MINING_WORD, MINING_BUTTON,
    BALANCE_WORD, PAY_CONFIRM_BUTTON, CONTAINER_WORD,
    DAILY_REWARD_WORD, DAILY_REWARD_BUTTON,
)
from common import MSK, parse_hhmm, seconds_until_msk, fmt_duration, clock, today_msk

BUFFER_SEC = 5  # буфер к кулдауну, чтобы не упереться ровно в секунду

# "через 1 ч 23 мин 45 сек", "через 59 мин", "через 12 сек"
_TIME_RE = re.compile(
    r"через\s*(?:(\d+)\s*ч[а-я.]*)?\s*(?:(\d+)\s*м[а-я.]*)?\s*(?:(\d+)\s*с[а-я.]*)?",
    re.IGNORECASE,
)
_POINTS_RE = re.compile(r"точки\D*?(\d[\d\s.,]*)", re.IGNORECASE)
_COLLECTION_RE = re.compile(r"телефонов\s+в\s+коллекции\D*?(\d[\d\s.,]*)", re.IGNORECASE)
# "через 3 дн. 5 ч. 10 мин." (магазин контейнеров — с днями, без секунд)
_SHOP_TIME_RE = re.compile(
    r"через\s*(?:(\d+)\s*дн[а-я.]*)?\s*(?:(\d+)\s*ч[а-я.]*)?\s*(?:(\d+)\s*мин[а-я.]*)?",
    re.IGNORECASE,
)
# страховка от «перекрёстного» парсинга: сообщение магазина контейнеров тоже содержит
# «через N ч M мин» (после «N дн.») — если парсер карточек/рулетки получит его (даже
# редкая гонка при параллельных запросах к одному боту), «дн.» в тексте выдаёт чужой
# формат, и парсить его как обычный кулдаун карточки нельзя
_DAY_HINT_RE = re.compile(r"\d+\s*дн", re.IGNORECASE)


def parse_cooldown(text: str | None) -> int | None:
    if not text or _DAY_HINT_RE.search(text):
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


def parse_collection_count(text: str | None) -> int | None:
    if not text:
        return None
    m = _COLLECTION_RE.search(text)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def parse_shop_cooldown(text: str | None) -> int | None:
    if not text:
        return None
    m = _SHOP_TIME_RE.search(text)
    if not m:
        return None
    d, h, mi = (int(x) if x else 0 for x in m.groups())
    total = d * 86400 + h * 3600 + mi * 60
    return total or None


def is_phone_won(text: str | None) -> bool:
    return bool(text) and "выпал телефон" in text.lower()


def _contains_any(text: str | None, keywords: list[str]) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(k in low for k in keywords)


# "Вы можете купить ещё 2 шт." — сколько ещё разрешает докупить эта категория
_REMAINING_RE = re.compile(r"можете купить ещ[её]\s*(\d+)", re.IGNORECASE)
# "Цена за 1 шт: 8,979,354 ТОчек"
_UNIT_PRICE_RE = re.compile(r"цена за\s*1\s*шт\.?\s*:?\s*([\d\s.,]+)", re.IGNORECASE)
# "Итоговая стоимость: 17,958,708 ТОчек"
_TOTAL_PRICE_RE = re.compile(r"итогов[а-я]*\s+стоимость\s*:?\s*([\d\s.,]+)", re.IGNORECASE)


def parse_remaining(text: str | None) -> int | None:
    if not text:
        return None
    m = _REMAINING_RE.search(text)
    return int(m.group(1)) if m else None


def parse_unit_price(text: str | None) -> int | None:
    if not text:
        return None
    m = _UNIT_PRICE_RE.search(text)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def parse_total_price(text: str | None) -> int | None:
    if not text:
        return None
    m = _TOTAL_PRICE_RE.search(text)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def _all_buttons(message) -> list[str]:
    if message is None or not getattr(message, "reply_markup", None):
        return []
    out = []
    for row in message.reply_markup.inline_keyboard:
        for b in row:
            t = getattr(b, "text", "") or ""
            if t:
                out.append(t)
    return out


def _find_button(message, substr: str) -> str | None:
    sub = substr.lower()
    for t in _all_buttons(message):
        if sub in t.lower():
            return t
    return None


_CATEGORY_KEYWORDS = {"donation": "донат", "expensive": "дорог", "budget": "бюджет"}
_CATEGORY_EMOJI = {"donation": "🎁", "expensive": "💎", "budget": "💰"}
_CATEGORY_QTY_FIELD = {
    "donation": "containers_qty_donation",
    "expensive": "containers_qty_expensive",
    "budget": "containers_qty_budget",
}
# ожидаемые границы цены за 1 шт. по факту наблюдений — используются только как
# страховка от бага парсинга (см. _buy_category), не как жёсткий лимит покупки
_DEFAULT_PRICE_RANGE = {
    "budget": (1_500_000, 3_000_000),
    "expensive": (7_000_000, 9_000_000),
    "donation": (7_000_000, 9_500_000),
}


def _category_key(label: str) -> str | None:
    low = label.lower()
    for key, kw in _CATEGORY_KEYWORDS.items():
        if kw in low:
            return key
    return None


def _price_range(cfg: dict, key: str | None) -> tuple[int, int] | None:
    if not key:
        return None
    ranges = cfg.get("price_ranges") or {}
    r = ranges.get(key) or _DEFAULT_PRICE_RANGE.get(key)
    return (int(r[0]), int(r[1])) if r else None


def _max_numeric_button(message) -> str | None:
    """Кнопка-число с наибольшим значением (селектор количества — «1», «2», ...).
    Терпит лёгкую эмодзи-обвязку цифры, но не «Купить 1 шт.» (там текста много)."""
    best_t, best_n = None, -1
    for t in _all_buttons(message):
        stripped = t.strip()
        digits = re.sub(r"\D", "", stripped)
        if not digits or len(stripped) - len(digits) > 3:
            continue
        n = int(digits)
        if n > best_n:
            best_n, best_t = n, t
    return best_t


def _numeric_button(message, value: int | None) -> str | None:
    """Кнопка-число, ТОЧНО равная value (не подстрока — «2» не должно попадать
    в «20»/«12»). Используется вместо _find_button() для выбора количества:
    подстрочный поиск там мог по ошибке ткнуть в другое число (см. shop.py,
    где та же проблема уже была исправлена этим же способом)."""
    if not value:
        return None
    for t in _all_buttons(message):
        stripped = t.strip()
        if stripped.isdigit() and int(stripped) == value:
            return t
    return None


class FarmModule:
    """Миксин с игровой автоматизацией. Ожидает от связанного класса: self.account,
    self.client, self.running, self._trade_mode, self._send_and_wait, self._click_and_wait,
    self._try_click, self._bump, self.trade_runner, self.good_keywords,
    self.container_cfg, self.payout_delay и статус-поля last_*/*_next_ts
    (инициализируются в базовом классе — automation.py)."""

    # ---------- регистрация фоновых циклов (кооперативно с autosend.py) ----------
    def _loops(self):
        return super()._loops() + [
            self._card_loop(), self._roulette_loop(),
            self._mining_loop(), self._container_loop(),
        ]

    def _farm_active(self) -> bool:
        return self.account.get("enabled", True) and self.account.get("farm_enabled", True)

    # ---------- цикл карточек ----------
    async def _card_loop(self) -> None:
        while self.running:
            try:
                if self._trade_mode:
                    await asyncio.sleep(2)
                    continue
                if not self._farm_active() or not self.account.get("card_enabled", True):
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
                    self.last_card = f"🎉 {label}{' ⭐' if good else ''} ({clock()})"
                elif cd is not None:
                    self.last_card = f"⏳ кулдаун {fmt_duration(cd)} ({clock()})"
                elif reply is None:
                    self.last_card = f"⚠️ нет ответа ({clock()})"
                else:
                    self.last_card = f"отправлено ({clock()})"
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
                if not self._farm_active() or not self.account.get("roulette_enabled", True):
                    await asyncio.sleep(20)
                    continue
                now = time.time()
                if now < self.roulette_next_ts:
                    await asyncio.sleep(min(30, self.roulette_next_ts - now))
                    continue

                reply = await self._send_and_wait(ROULETTE_BOT, ROULETTE_WORD)
                text = getattr(reply, "text", None) or getattr(reply, "caption", None)
                default = int(self.account.get("roulette_interval", 3600))

                clicked, result = await self._click_and_wait(reply, ROULETTE_BUTTON, ROULETTE_BOT, timeout=12)
                if clicked:
                    self._bump("roulette")
                    rtext = (getattr(result, "text", None)
                             or getattr(result, "caption", None) or text)
                    cd = parse_cooldown(rtext)
                    delay = cd if cd is not None else default
                    self.last_roulette = f"🎰 крутили, кулдаун {fmt_duration(delay)} ({clock()})"
                else:
                    cd = parse_cooldown(text)
                    delay = cd if cd is not None else default
                    self.last_roulette = (f"⚠️ нет ответа ({clock()})" if reply is None
                                          else f"⏳ кулдаун {fmt_duration(delay)} ({clock()})")
                self.roulette_next_ts = time.time() + delay + BUFFER_SEC
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_roulette = f"ошибка: {e}"
                self.roulette_next_ts = time.time() + 60
                await asyncio.sleep(5)

    # ---------- цикл майнинга (+ежедневная награда) ----------
    async def _mining_loop(self) -> None:
        """Раз в сутки в заданное по МСК время. Опрос раз в 20с — время меняется на лету."""
        last_fired = None
        while self.running:
            try:
                if self._trade_mode:
                    await asyncio.sleep(2)
                    continue
                hour, minute = parse_hhmm(self.account.get("mining_time"))
                self.mining_next_ts = time.time() + seconds_until_msk(hour, minute)

                now = datetime.now(MSK)
                due = now.hour == hour and now.minute == minute and last_fired != now.date()
                if due and self._farm_active():
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
        """Раз в кулдаун вызывает check_containers() — она сама разбирает ответ
        (распродано/капча/магазин открыт/бот перегружен) и решает, когда проверять
        снова. Подробности — в докстринге check_containers()."""
        while self.running:
            try:
                if self._trade_mode:
                    await asyncio.sleep(2)
                    continue
                if not self._farm_active() or not self.account.get("containers_enabled", False):
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

    def _classify_shop_reply(self, cfg: dict, reply, text: str | None) -> str:
        low = (text or "").lower()
        sold_out_marker = (cfg.get("sold_out_marker") or "раскуплены").lower()
        captcha_marker = (cfg.get("captcha_marker") or "анти-бот защита").lower()
        shop_marker = (cfg.get("shop_marker") or "магазин контейнеров").lower()
        if sold_out_marker in low:
            return "sold_out"
        if shop_marker in low or any(_find_button(reply, kw) for kw in ("бюджет", "донат", "дорог")):
            return "shop"
        if captcha_marker in low:
            return "captcha"
        return "unknown"

    async def _probe_shop(self, cfg: dict) -> tuple[str, object, str | None]:
        """Один запрос открытия магазина (отправляет команду заново). Возвращает
        (kind, reply, text), где kind — 'sold_out' | 'shop' | 'captcha' |
        'none' (бот не ответил) | 'unknown'."""
        bot = cfg.get("bot") or CARDS_BOT
        open_cmd = cfg.get("open_command") or CONTAINER_WORD
        reply = await self._send_and_wait(bot, open_cmd, timeout=20)
        text = (getattr(reply, "text", None) or getattr(reply, "caption", None)) if reply else None
        if reply is None:
            return "none", reply, text
        return self._classify_shop_reply(cfg, reply, text), reply, text

    async def _probe_shop_refresh(self, cfg: dict, prev_reply) -> tuple[str, object, str | None]:
        """Как _probe_shop, но вместо повторной отправки команды открытия магазина
        жмёт кнопку «🔄 Обновить» на предыдущем ответе (меньше сообщений в чате с
        игровым ботом на каждой минуте опроса). Если такой кнопки нет или клик не
        удался — откатывается на обычную отправку команды."""
        bot = cfg.get("bot") or CARDS_BOT
        refresh_label = cfg.get("refresh_button") or "обновить"
        btn = _find_button(prev_reply, refresh_label)
        if not btn:
            return await self._probe_shop(cfg)
        clicked, reply = await self._click_and_wait(prev_reply, btn, bot, timeout=15)
        if not clicked:
            return await self._probe_shop(cfg)
        if reply is None:
            return "none", reply, None
        text = getattr(reply, "text", None) or getattr(reply, "caption", None)
        return self._classify_shop_reply(cfg, reply, text), reply, text

    async def check_containers(self) -> str:
        """Открыть магазин контейнеров и разобрать ответ:
        - «раскуплены, след. через X», X больше early_start -> просто ждать (с запасом
          на ранний старт — см. ниже), проверить снова заранее, а не ровно в рестоке;
        - «раскуплены, след. через X», X меньше early_start (рестока скоро) -> НЕ ждать
          кулдаун целиком, а начать опрашивать магазин заранее: редко (pre_restock_interval),
          пока не подойдёт расчётное время рестока, и часто (retry_interval) после —
          ровно в момент рестока игровой бот перегружен и не отвечает, ловить его нужно
          с запасом, а не начинать долбить холодным стартом именно в этот момент;
        - меню магазина (категории) -> купить по preferred_categories/тумблерам;
        - нет ответа вообще -> игровой бот перегружен (частая ситуация на рестоке) —
          ЭТО можно долбить: повтор открытия с периодичностью retry_interval, не
          дольше retry_window (после расчётного рестока, если он известен);
        - анти-бот капча (картинка-пример) -> бот её НЕ решает и НЕ долбит магазин
          повторно (там ограниченное число попыток на капчу, спам может их сжечь) —
          шлёт владельцу интерактивный алерт с выбором категории и ждёт, пока он
          сам решит капчу и нажмёт кнопку (resume_container_check);
        - что-то совсем незнакомое -> так же алерт владельцу и стоп.

        Опрос в режиме ожидания рестока жмёт кнопку «🔄 Обновить» на уже полученном
        сообщении вместо повторной отправки команды открытия магазина (см.
        _probe_shop_refresh) — реже пишет игровому боту. Кроме того, за
        restock_alert_before секунд до расчётного рестока (по умолчанию 20 мин)
        владельцу шлётся разовый алерт через управляющего бота с оставшимся временем
        (если к моменту первой проверки времени уже меньше — в алерте уйдёт фактический
        остаток, а не ровно 20 мин).

        Используется циклом и кнопкой «Проверить сейчас». Возвращает last_container."""
        if not self.client or not self.running:
            self.last_container = "аккаунт не запущен"
            return self.last_container
        cfg = self.container_cfg
        bot = cfg.get("bot") or CARDS_BOT
        retry_interval = max(3, int(cfg.get("retry_interval", 10)))
        retry_window = max(retry_interval, int(cfg.get("retry_window", 600)))
        unknown_retry = int(cfg.get("unknown_retry", 600))
        early_start = max(0, int(cfg.get("early_start", 1800)))
        pre_restock_interval = max(5, int(cfg.get("pre_restock_interval", 60)))
        alert_before = max(0, int(cfg.get("restock_alert_before", 1200)))

        deadline = time.time() + retry_window
        attempt = 0
        prev_reply = None
        try:
            while True:
                attempt += 1
                if prev_reply is not None:
                    kind, reply, text = await self._probe_shop_refresh(cfg, prev_reply)
                else:
                    kind, reply, text = await self._probe_shop(cfg)
                if reply is not None:
                    prev_reply = reply

                if kind == "sold_out":
                    cd = parse_shop_cooldown(text)
                    if cd is not None:
                        if cd <= alert_before:
                            if not self._container_alert_sent:
                                self._container_alert_sent = True
                                await self._notify_restock_soon(cd)
                        else:
                            self._container_alert_sent = False
                    if cd is not None and cd <= early_start:
                        # рестока осталось меньше early_start — переходим в режим опроса
                        # вместо того, чтобы ждать полный кулдаун и упереться в перегруженного
                        # бота ровно в момент рестока
                        restock_at = time.time() + cd
                        deadline = max(deadline, restock_at + retry_window)
                        # реже (раз в минуту), пока не подойдёт минута до рестока — и часто
                        # (retry_interval) в последнюю минуту и после расчётного времени
                        interval = retry_interval if cd <= 60 else pre_restock_interval
                        self.last_container = f"⏳ рестока через {fmt_duration(cd)}, опрашиваю ({clock()})"
                        if time.time() >= deadline:
                            break
                        await asyncio.sleep(interval)
                        continue
                    delay = cd if cd is not None else unknown_retry
                    self.container_next_ts = time.time() + max(0, delay - early_start) + BUFFER_SEC
                    self.last_container = f"⏳ раскуплены, след. через {fmt_duration(delay)} ({clock()} {today_msk()})"
                    return self.last_container

                if kind == "shop":
                    bought = await self._buy_containers(bot, reply, cfg)
                    self.container_next_ts = time.time() + unknown_retry
                    self.last_container = (
                        f"🛒 {bought} ({clock()} {today_msk()})" if bought else
                        f"🛒 магазин открыт, покупать нечего/лимиты исчерпаны ({clock()} {today_msk()})"
                    )
                    return self.last_container

                if kind == "captcha":
                    # НЕ ретраим: капча даёт мало попыток, повторное открытие может их сжечь.
                    # Ждём, пока владелец сам решит капчу и нажмёт кнопку в алерте.
                    self.container_next_ts = time.time() + unknown_retry
                    self.last_container = f"🚨 нужна капча — жду владельца ({clock()} {today_msk()})"
                    await self._notify_owner_captcha(cfg, text, reason="captcha")
                    return self.last_container

                if kind == "none":
                    # это НЕ капча — просто бот перегружен на рестоке, тут ретраить можно
                    self.last_container = f"⏳ бот перегружен, нет ответа, попытка {attempt} ({clock()})"
                    if time.time() >= deadline:
                        break
                    await asyncio.sleep(retry_interval)
                    continue

                # неожиданный, ни на что не похожий ответ — зовём владельца разобраться
                self.container_next_ts = time.time() + unknown_retry
                self.last_container = f"⚠️ неожиданный ответ, оповестил владельца ({clock()} {today_msk()})"
                await self._notify_owner_captcha(cfg, text, reason="unknown")
                return self.last_container
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.container_next_ts = time.time() + 60
            self.last_container = f"ошибка: {e}"
            return self.last_container

        # вышли по deadline: бот так и не ответил (перегруз на рестоке)
        self.container_next_ts = time.time() + unknown_retry
        self.last_container = f"⏱ магазин не открылся за {fmt_duration(retry_window)}, отступаю ({clock()})"
        await self._notify_owner_captcha(cfg, None, reason="timeout")
        return self.last_container

    async def resume_container_check(self, prefer: str | None = None) -> str:
        """Однократная попытка открыть магазин — вызывается ВРУЧНУЮ (кнопка в алерте),
        когда владелец уже сам решил капчу. В отличие от check_containers() НЕ
        зацикливается и не долбит магазин повторно — капча даёт мало попыток.
        prefer — ключ категории ('donation'/'expensive'/'budget'), которую поставить
        первой в очереди покупки на этот заход (кнопка из алерта), либо None/'default'
        для обычного порядка preferred_categories."""
        if not self.client or not self.running:
            self.last_container = "аккаунт не запущен"
            return self.last_container
        cfg = self.container_cfg
        bot = cfg.get("bot") or CARDS_BOT
        unknown_retry = int(cfg.get("unknown_retry", 600))
        early_start = max(0, int(cfg.get("early_start", 1800)))
        try:
            kind, reply, text = await self._probe_shop(cfg)

            if kind == "sold_out":
                cd = parse_shop_cooldown(text)
                delay = cd if cd is not None else unknown_retry
                self.container_next_ts = time.time() + max(0, delay - early_start) + BUFFER_SEC
                self.last_container = f"⏳ раскуплены, след. через {fmt_duration(delay)} ({clock()} {today_msk()})"
            elif kind == "shop":
                eff_cfg = cfg
                if prefer and prefer != "default":
                    prefs = list(cfg.get("preferred_categories") or ["Донатный", "Дорогой", "Бюджетный"])
                    kw = _CATEGORY_KEYWORDS.get(prefer)
                    match = next((p for p in prefs if kw and kw in p.lower()), None)
                    if match:
                        prefs = [match] + [p for p in prefs if p != match]
                        eff_cfg = dict(cfg, preferred_categories=prefs)
                bought = await self._buy_containers(bot, reply, eff_cfg)
                self.container_next_ts = time.time() + unknown_retry
                self.last_container = (
                    f"🛒 {bought} ({clock()} {today_msk()})" if bought else
                    f"🛒 магазин открыт, покупать нечего/лимиты исчерпаны ({clock()} {today_msk()})"
                )
            elif kind == "captcha":
                self.container_next_ts = time.time() + unknown_retry
                self.last_container = f"🚨 капча всё ещё не решена ({clock()} {today_msk()})"
                await self._notify_owner_captcha(cfg, text, reason="captcha")
            elif kind == "none":
                self.container_next_ts = time.time() + unknown_retry
                self.last_container = f"⚠️ бот не отвечает ({clock()} {today_msk()})"
            else:
                self.container_next_ts = time.time() + unknown_retry
                self.last_container = f"⚠️ неожиданный ответ, оповестил владельца ({clock()} {today_msk()})"
                await self._notify_owner_captcha(cfg, text, reason="unknown")
        except Exception as e:  # noqa: BLE001
            self.container_next_ts = time.time() + 60
            self.last_container = f"ошибка: {e}"
        return self.last_container

    def _category_allowed(self, label: str) -> bool:
        low = label.lower()
        if "донат" in low:
            return self.account.get("containers_buy_donation", True)
        if "дорог" in low:
            return self.account.get("containers_buy_expensive", True)
        if "бюджет" in low:
            return self.account.get("containers_buy_budget", True)
        return True

    async def _click_step(self, bot: str, msg, label_substr: str, cfg: dict, timeout: int = 15):
        """Жмёт кнопку по подстроке и ждёт следующий ответ бота. Если бот перегружен
        и не отвечает — повторяет клик несколько раз с паузой retry_interval вместо
        того, чтобы сразу сдаться (актуально на рестоке, когда много желающих)."""
        btn = _find_button(msg, label_substr)
        if not btn:
            return None
        retry_interval = max(3, int(cfg.get("retry_interval", 10)))
        for i in range(3):
            clicked, result = await self._click_and_wait(msg, btn, bot, timeout=timeout)
            if not clicked:
                return None
            if result is not None:
                return result
            if i < 2:
                await asyncio.sleep(retry_interval)
        return None

    async def _click_confirm(self, msg, label_substr: str, cfg: dict) -> bool:
        btn = _find_button(msg, label_substr)
        if not btn:
            return False
        retry_interval = max(3, int(cfg.get("retry_interval", 10)))
        for i in range(3):
            if await self._try_click(msg, btn):
                return True
            if i < 2:
                await asyncio.sleep(retry_interval)
        return False

    async def _check_balance(self) -> int | None:
        bal = await self._send_and_wait(CARDS_BOT, BALANCE_WORD, timeout=15)
        return parse_points(getattr(bal, "text", None) or getattr(bal, "caption", None))

    async def fetch_balance_info(self) -> str:
        """«такс» -> разобранные точки/коллекция для кнопки «🧾 Такс» в карточке
        аккаунта. Если формат ответа не узнан (ни одно поле не распозналось) —
        отдаём сырой текст как есть, а не молчим о непонятном ответе."""
        if not self.client or not self.running:
            return "аккаунт не запущен"
        reply = await self._send_and_wait(CARDS_BOT, BALANCE_WORD, timeout=15)
        text = (getattr(reply, "text", None) or getattr(reply, "caption", None)) if reply else None
        if not text:
            return "⚠️ нет ответа"
        points = parse_points(text)
        collection = parse_collection_count(text)
        if points is None and collection is None:
            return text
        lines = []
        if points is not None:
            lines.append(f"💰 Точки: {points:,}".replace(",", " "))
        if collection is not None:
            lines.append(f"📱 Телефонов в коллекции: {collection}")
        return "\n".join(lines)

    async def _buy_category(
        self, bot: str, shop_msg, category_label: str, cfg: dict, balance: int | None,
    ) -> tuple[bool, str, int]:
        """Возвращает (успех, инфо-строка, потрачено ТОчек)."""
        if not _find_button(shop_msg, category_label):
            return False, "", 0
        detail = await self._click_step(bot, shop_msg, category_label, cfg, timeout=15)
        if detail is None:
            return False, f"{category_label}: нет ответа на выбор категории", 0
        dtext = getattr(detail, "text", None) or getattr(detail, "caption", None) or ""
        remaining = parse_remaining(dtext)
        if not remaining or remaining <= 0:
            return False, "", 0  # лимит категории исчерпан — не ошибка, просто нечего брать

        key = _category_key(category_label)
        unit_price = parse_unit_price(dtext)
        price_range = _price_range(cfg, key)
        if unit_price and price_range and not (price_range[0] * 0.5 <= unit_price <= price_range[1] * 1.3):
            # цена сильно не похожа на ожидаемую для категории — вероятно баг парсинга,
            # безопаснее пропустить и позвать владельца, чем спустить неизвестную сумму
            await self._notify_price_mismatch(category_label, unit_price)
            return False, f"{category_label}: подозрительная цена {unit_price}, пропускаю", 0

        if unit_price and balance is not None:
            affordable = balance // unit_price
            if affordable <= 0:
                return False, f"{category_label}: не хватает баланса (цена {unit_price}, баланс {balance})", 0
            remaining = min(remaining, affordable)

        qty_field = _CATEGORY_QTY_FIELD.get(key)
        configured_qty = int(self.account.get(qty_field, 0) or 0) if qty_field else 0
        if configured_qty > 0:
            remaining = min(remaining, configured_qty)

        single_btn = cfg.get("single_button", "купить 1")
        bulk_btn = cfg.get("bulk_button", "купить оптом")
        confirm_btn = cfg.get("confirm_button", "подтвердить")

        if remaining == 1:
            step = await self._click_step(bot, detail, single_btn, cfg, timeout=15)
        else:
            qty_msg = await self._click_step(bot, detail, bulk_btn, cfg, timeout=15)
            if qty_msg is None:
                return False, f"{category_label}: нет ответа на «купить оптом»", 0
            qty_btn = _numeric_button(qty_msg, remaining) or _max_numeric_button(qty_msg)
            if not qty_btn:
                return False, f"{category_label}: не нашёл кнопку количества", 0
            step = await self._click_step(bot, qty_msg, qty_btn, cfg, timeout=15)

        if step is None:
            return False, f"{category_label}: нет ответа после выбора количества", 0

        step_text = getattr(step, "text", None) or getattr(step, "caption", None) or ""
        total_price = parse_total_price(step_text) or (unit_price * remaining if unit_price else 0)

        # если это экран подтверждения — жмём; если покупка уже прошла сама — не мешаем
        if _find_button(step, confirm_btn):
            if not await self._click_confirm(step, confirm_btn, cfg):
                return False, f"{category_label}: не подтвердилась покупка", 0

        self._bump("containers_bought", remaining)
        return True, f"{remaining} шт. ({category_label}, ~{total_price:,} ТОчек)".replace(",", " "), total_price

    async def _buy_containers(self, bot: str, shop_msg, cfg: dict) -> str:
        """Проходит preferred_categories по порядку, покупая в каждой разрешённой
        (тумблерами containers_buy_*) категории заданное количество (containers_qty_*,
        0 = максимум доступного) — не больше, чем позволяют лимит игры и баланс.
        Баланс проверяется один раз в начале и уменьшается по ходу покупок — чтобы
        не потратить на дорогие/донатные больше, чем реально есть."""
        prefs = cfg.get("preferred_categories") or ["Донатный", "Дорогой", "Бюджетный"]
        results = []
        msg = shop_msg
        open_cmd = cfg.get("open_command") or CONTAINER_WORD
        balance = await self._check_balance()
        remaining_labels = [l for l in prefs if self._category_allowed(l)]
        for i, label in enumerate(remaining_labels):
            if balance is not None and balance <= 0:
                break
            ok, info, spent = await self._buy_category(bot, msg, label, cfg, balance)
            if ok:
                results.append(info)
                if balance is not None:
                    balance -= spent
            # Обновляем меню магазина заново перед следующей категорией НЕЗАВИСИМО
            # от успеха — если попытка дошла до клика внутри категории (даже
            # неудачно, напр. таймаут на «купить оптом»), диалог игрового бота мог
            # сдвинуться с исходного меню, и клик по СТАРОМУ сообщению для следующей
            # категории просто перестаёт отвечать (флоу «зависает» точно на этом шаге).
            if i < len(remaining_labels) - 1:
                refreshed = await self._send_and_wait(bot, open_cmd, timeout=20)
                if refreshed is not None:
                    msg = refreshed
        return "; ".join(results)

    async def _notify_price_mismatch(self, category_label: str, unit_price: int) -> None:
        owner_id = self.account.get("owner_id")
        if not owner_id or not self.account.get("alerts_enabled", True):
            return
        text = (
            f"⚠️ Магазин контейнеров — «{self.name}»: цена «{category_label}» "
            f"выглядит подозрительно ({unit_price:,} ТОчек), пропустил категорию "
            f"на всякий случай — проверь вручную.".replace(",", " ")
        )
        if self.alert_fn:
            try:
                await self.alert_fn(owner_id, text, None)
            except Exception as e:  # noqa: BLE001
                print(f"[{self.name}] не удалось отправить алерт о цене: {e}")

    async def _notify_restock_soon(self, cd: int) -> None:
        """Разовый алерт «ресток скоро» — за restock_alert_before секунд до расчётного
        рестока (или сразу с фактическим остатком, если он уже меньше на момент проверки)."""
        owner_id = self.account.get("owner_id")
        if not owner_id or not self.account.get("alerts_enabled", True):
            return
        text = f"⏰ Магазин контейнеров — «{self.name}»: ресток через {fmt_duration(cd)}."
        if not self.alert_fn:
            return
        try:
            await self.alert_fn(owner_id, text, None)
        except Exception as e:  # noqa: BLE001
            print(f"[{self.name}] не удалось отправить алерт о рестоке: {e}")

    def _captcha_markup(self, cfg: dict) -> InlineKeyboardMarkup:
        """Кнопки выбора категории прямо в алерте — жмёшь, когда сам решил капчу
        (или просто хочешь попробовать снова), и это ОДНОКРАТНО пробует магазин
        заново через resume_container_check(), уже без циклов."""
        prefs = cfg.get("preferred_categories") or ["Донатный", "Дорогой", "Бюджетный"]
        rows = []
        for label in prefs:
            key = _category_key(label)
            if not key or not self._category_allowed(label):
                continue
            emoji = _CATEGORY_EMOJI.get(key, "📦")
            rows.append([InlineKeyboardButton(f"{emoji} {label}", callback_data=f"capgo:{self.id}:{key}")])
        rows.append([InlineKeyboardButton("✅ Как настроено (по порядку)", callback_data=f"capgo:{self.id}:default")])
        rows.append([InlineKeyboardButton("⏭ Пропустить в этот раз", callback_data=f"capskip:{self.id}")])
        return InlineKeyboardMarkup(rows)

    def _captcha_alert_allowed(self) -> bool:
        """Если у владельца есть 👑 главный аккаунт — об капче контейнеров пишет
        только он (чтобы не заваливать владельца одинаковыми алертами с каждого
        из его аккаунтов). Если главный не назначен — как раньше, пишут все."""
        owner_id = self.account.get("owner_id")
        if not owner_id or not self.storage:
            return True
        siblings = [a for a in self.storage.accounts if a.get("owner_id") == owner_id]
        main = next((a for a in siblings if a.get("is_main")), None)
        return main is None or main.get("id") == self.id

    async def _notify_owner_captcha_relay(self, shop_text: str | None) -> None:
        """Альтернатива алерту с кнопками (тумблер containers_captcha_relay_enabled):
        пересылает сырой текст капчи владельцу и ждёт от него ЦИФРУ ответом прямо в
        чате с управляющим ботом — controlbot.py её подхватит и отправит через
        send_captcha_digits()."""
        owner_id = self.account.get("owner_id")
        preview = shop_text.strip() if shop_text else "(текст капчи не распознан)"
        text = (
            f"🚨 Капча — магазин контейнеров, «{self.name}»\n\n"
            f"«{preview[:500]}»\n\n"
            f"Реши капчу и пришли мне сюда ТОЛЬКО цифру ответа — я сам отправлю её игровому боту."
        )
        self._captcha_relay_pending = True
        self._captcha_relay_deadline = time.time() + 900
        if not self.alert_fn:
            print(f"[{self.name}] alert_fn не подключен — алерт капчи (релей) не отправлен")
            return
        try:
            await self.alert_fn(owner_id, text, None)
        except Exception as e:  # noqa: BLE001
            print(f"[{self.name}] не удалось отправить алерт капчи (релей): {e}")

    async def send_captcha_digits(self, digits: str) -> str:
        """Отправляет цифру-ответ капчи от лица аккаунта игровому боту (владелец
        решил её сам и прислал ответ в личку управляющему боту), затем один раз
        пробует продолжить магазин — как кнопка «✅ Как настроено» в обычном алерте."""
        self._captcha_relay_pending = False
        if not self.client or not self.running:
            return "аккаунт не запущен"
        await self.client.send_message(CARDS_BOT, digits)
        await asyncio.sleep(2)
        return await self.resume_container_check()

    async def _notify_owner_captcha(self, cfg: dict, shop_text: str | None, reason: str = "captcha") -> None:
        owner_id = self.account.get("owner_id")
        if not owner_id or not self.account.get("alerts_enabled", True):
            return
        if reason == "captcha":
            if not self._captcha_alert_allowed():
                return
            if self.account.get("containers_captcha_relay_enabled", False):
                await self._notify_owner_captcha_relay(shop_text)
                return
        alert = cfg.get("captcha_alert_text", "КАПЧА")
        preview = f"\n\n«{shop_text[:300]}»" if shop_text else ""
        if reason == "captcha":
            head = (
                f"🚨 {alert} — магазин контейнеров, «{self.name}»\n"
                f"Реши капчу САМ в чате с игровым ботом (попыток обычно мало — "
                f"бот больше не будет сам слать команду открытия магазина, пока ты не нажмёшь ниже)."
            )
        elif reason == "timeout":
            head = (
                f"⏱ Магазин контейнеров — «{self.name}»: бот игры не отвечал долго "
                f"(перегружен на рестоке). Попробуй вручную, когда будет минутка."
            )
        else:
            head = f"⚠️ Магазин контейнеров прислал неожиданный ответ — «{self.name}». Посмотри вручную."
        text = f"{head}{preview}\n\nЧто делать дальше — выбери:"
        markup = self._captcha_markup(cfg)
        # ТОЛЬКО через управляющего бота: фарм-аккаунт (phoneget) во время автоматизации
        # может быть ограничен Telegram в отправке новых личных сообщений («в муте»),
        # так что слать алерт именно тогда, когда он важнее всего, через него ненадёжно
        if not self.alert_fn:
            print(f"[{self.name}] alert_fn не подключен — алерт не отправлен: {text[:80]}")
            return
        try:
            await self.alert_fn(owner_id, text, markup)
        except Exception as e:  # noqa: BLE001
            print(f"[{self.name}] не удалось отправить алерт через управляющего бота: {e}")

    # ---------- действия ----------
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
                self.last_mining = f"💰 снято с фермы ({clock()} {today_msk()})"
            elif reply is None:
                self.last_mining = f"⚠️ нет ответа ({clock()} {today_msk()})"
            else:
                self.last_mining = f"⚠️ кнопка не найдена — рано/уже собрано ({clock()} {today_msk()})"
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
                self.last_daily = f"🎁 забрана ({clock()} {today_msk()})"
            elif reply is None:
                self.last_daily = f"⚠️ нет ответа ({clock()} {today_msk()})"
            else:
                self.last_daily = f"⚠️ кнопка не найдена — уже забрана? ({clock()} {today_msk()})"
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
                self.last_payout = f"нет очков для вывода ({clock()})"
                return
            pay = await self._send_and_wait(CARDS_BOT, f"/pay {target} {amount}")
            if await self._try_click(pay, PAY_CONFIRM_BUTTON):
                self._bump("paid", amount)
                self.last_payout = f"💸 выведено {amount} -> {target} ({clock()} {today_msk()})"
            elif pay is None:
                self.last_payout = f"⚠️ нет ответа на /pay ({clock()})"
            else:
                self.last_payout = f"⚠️ кнопка Подтвердить не найдена ({clock()})"
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.last_payout = f"ошибка вывода: {e}"

    async def manual_pay(self, target: str, amount: int) -> str:
        """Ручной разовый перевод очков произвольному получателю произвольной суммой —
        независимо от настроенного payout_target/авто-вывода. Полезно для твинков
        (перекинуть очки с одного своего аккаунта на другой вручную)."""
        if not self.client or not self.running:
            return "аккаунт не запущен"
        if self._trade_mode:
            return "идёт трейд — попробуй чуть позже"
        bal = await self._send_and_wait(CARDS_BOT, BALANCE_WORD, timeout=15)
        balance = parse_points(getattr(bal, "text", None) or getattr(bal, "caption", None))
        if balance is not None and amount > balance:
            return f"⚠️ не хватает очков (баланс {balance}, запрошено {amount})"
        pay = await self._send_and_wait(CARDS_BOT, f"/pay {target} {amount}")
        if await self._try_click(pay, PAY_CONFIRM_BUTTON):
            self._bump("paid", amount)
            return f"✅ переведено {amount} -> {target} ({clock()} {today_msk()})"
        if pay is None:
            return "⚠️ нет ответа на /pay"
        return "⚠️ кнопка «Подтвердить» не найдена — возможно перевод не прошёл"

    # ---------- инфо для меню ----------
    def card_remaining(self) -> str:
        return self._remaining(self.card_next_ts)

    def roulette_remaining(self) -> str:
        return self._remaining(self.roulette_next_ts)

    def mining_remaining(self) -> str:
        return self._remaining(self.mining_next_ts, "скоро")

    def container_remaining(self) -> str:
        return self._remaining(self.container_next_ts, "скоро")
