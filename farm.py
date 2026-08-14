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
    MINING_WORD, FARM_WITHDRAW_BUTTON, FARM_REMOVE_BUTTON, FARM_BROKEN_MARKER,
    BALANCE_WORD, PAY_CONFIRM_BUTTON, CONTAINER_WORD,
    DAILY_REWARD_WORD, DAILY_REWARD_BUTTON,
    UPGRADE_WORD, UPGRADE_CATEGORIES, UPGRADE_MAX_MARKER, UPGRADE_BUY_BUTTON, UPGRADE_RESERVE,
    UPGRADE_MIN_BALANCE_TO_START, UPGRADE_STEP_DELAY,
    EXCHANGE_WORD,
)
from common import MSK, parse_hhmm, seconds_until_msk, fmt_duration, clock, today_msk
from common import backoff_seconds as _backoff_seconds
from common import msg_text as _common_msg_text
from ui_engine import Design

BUFFER_SEC = 5  # буфер к кулдауну, чтобы не упереться ровно в секунду
NO_REPLY_RETRY_SEC = 20  # бот не ответил вовсе (не кулдаун!) — короткий повтор, а не
                          # полный card_interval/roulette_interval (см. _card_loop/_roulette_loop)

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

# ---------- модульная ферма (майнинг): «N. Модель», «N. Пусто», «N. ... СЛОМАНО» ----------
_FARM_SLOT_RE = re.compile(r"^(\d+)\.\s*(.+)$", re.MULTILINE)
_FARM_BALANCE_RE = re.compile(r"Баланс:\s*([\d.]+)\s*/\s*(\d+)\s*P-Coins", re.IGNORECASE)
FARM_CHECK_INTERVAL = 300  # раз в 5 минут проверяем ферму на сломанные телефоны


def parse_farm_slots(text: str | None) -> dict[int, dict]:
    """Слоты модульной фермы из ответа на «Тмайнинг»: номер -> {status, model}.
    status: "empty" | "broken" | "working"."""
    if not text:
        return {}
    slots: dict[int, dict] = {}
    for m in _FARM_SLOT_RE.finditer(text):
        num = int(m.group(1))
        rest = m.group(2).strip()
        low = rest.lower()
        if "пусто" in low:
            slots[num] = {"status": "empty"}
        elif FARM_BROKEN_MARKER in low:
            model = re.sub(rf"[^\w]*{FARM_BROKEN_MARKER}[^\w]*", "", rest, flags=re.IGNORECASE).strip()
            model = _strip_status_emoji(model) or rest
            slots[num] = {"status": "broken", "model": model}
        else:
            slots[num] = {"status": "working", "model": _strip_status_emoji(rest) or rest}
    return slots


# каждый слот теперь помечен цветным индикатором-эмодзи статуса («🟢 Model», «🔴
# Model (СЛОМАН)») — модель нужна БЕЗ этого префикса, иначе она не совпадёт с
# «Model (xN)» из списка телефонов при переустановке в слот (farm_maintenance)
def _strip_status_emoji(name: str) -> str:
    return re.sub(r"^[^\w]+", "", name).strip()


def parse_farm_balance(text: str | None) -> tuple[float, int] | None:
    if not text:
        return None
    m = _FARM_BALANCE_RE.search(text)
    return (float(m.group(1)), int(m.group(2))) if m else None


# «🎒 Ваш портфель: ... 🅿️ P-Coins: 50 ... 💰 ТОчки: 149,325,103» — это единственное
# место в ответе биржи, где «P-Coins» идёт с двоеточием (курс сверху пишет «1
# P-Coin = N ТОчек», без двоеточия) — regex не путает эти два случая
_WALLET_PCOIN_RE = re.compile(r"p-coins:\s*([\d.,\s]+)", re.IGNORECASE)


def parse_pcoin_wallet(text: str | None) -> float | None:
    if not text:
        return None
    m = _WALLET_PCOIN_RE.search(text)
    if not m:
        return None
    digits = re.sub(r"[^\d.]", "", m.group(1))
    try:
        return float(digits) if digits else None
    except ValueError:
        return None


# после обновления игры у фермы появился персистентный тумблер вкл/выкл
# («Состояние: ▶️ Включена»/«⏸ Выключена» + кнопка «Выключить»/«Включить») — по
# умолчанию НОВАЯ ферма выключена и не майнит, пока её не включат вручную.
# Между «Состояние:» и словом бот вставляет эмодзи-иконку (не просто пробел) -
# regex с одним \s* её живьём не поймал, отсюда [^\w]* (любые не-буквенные символы)
_FARM_STATE_RE = re.compile(r"состояние:[^\w]*(включена|выключена)", re.IGNORECASE)


def parse_farm_state(text: str | None) -> str | None:
    if not text:
        return None
    m = _FARM_STATE_RE.search(text)
    return m.group(1).lower() if m else None


# Случайное «Событие» (напр. «Взрыв электростанции (-20% питания)») может урезать
# доступную мощность PSU/Cooling — если текущее потребление после этого превышает
# урезанный лимит, ферма уходит в «Перегрузка» и полностью останавливается (не
# просто «выключена» вручную). «Питание (PSU ур.6): 957/1000 W» / «Охлаждение
# (Cooling ур.6): 770/900 TDP» — первое число потребление, второе лимит.
_PSU_RE = re.compile(r"питание\s*\(psu[^)]*\)\s*:\s*([\d.,\s]+)\s*/\s*([\d.,\s]+)\s*w", re.IGNORECASE)
_COOLING_RE = re.compile(r"охлаждение\s*\(cooling[^)]*\)\s*:\s*([\d.,\s]+)\s*/\s*([\d.,\s]+)\s*tdp", re.IGNORECASE)


def _parse_load_num(raw: str) -> float:
    return float(raw.strip().replace(" ", "").replace(",", "."))


def parse_power_load(text: str | None) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    """(психу, охлаждение), каждое — (потребление, лимит) либо None, если не нашли."""
    text = text or ""
    m = _PSU_RE.search(text)
    psu = (_parse_load_num(m.group(1)), _parse_load_num(m.group(2))) if m else None
    m2 = _COOLING_RE.search(text)
    cooling = (_parse_load_num(m2.group(1)), _parse_load_num(m2.group(2))) if m2 else None
    return psu, cooling


def _is_overloaded(
    psu: tuple[float, float] | None, cooling: tuple[float, float] | None,
) -> bool:
    if psu and psu[0] > psu[1]:
        return True
    if cooling and cooling[0] > cooling[1]:
        return True
    return False


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


# "Улучшить за 10,000" (кнопка «Магазина улучшений»)
_UPGRADE_COST_RE = re.compile(r"улучшить\s+за\s*([\d\s.,]+)", re.IGNORECASE)
# "Текущий уровень: 1" (детальный экран категории «Магазина улучшений»)
_UPGRADE_LEVEL_RE = re.compile(r"текущий\s+уровень:?\s*(\d+)", re.IGNORECASE)
UPGRADE_MAX_LEVEL = 6


def _fmt_points(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def parse_upgrade_cost(button_text: str | None) -> int | None:
    if not button_text:
        return None
    m = _UPGRADE_COST_RE.search(button_text)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def parse_upgrade_level(text: str | None) -> int | None:
    if not text:
        return None
    m = _UPGRADE_LEVEL_RE.search(text)
    return int(m.group(1)) if m else None


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


def _find_rarity_button(message, rarity_label: str) -> str | None:
    """Ищет кнопку категории редкости по ОСНОВЕ слова, без падежного/числового
    окончания. «Магазин телефонов» показывает категории в единственном числе
    («Платиновый»), а «Мои телефоны» — во множественном («Платиновые»); обычный
    _find_button (полная подстрока) находит только «Ширпотреб» (не склоняется) —
    для всех остальных 6 редкостей поиск ВСЕГДА молча проваливался, из-за чего
    бот не видел уже купленные телефоны этих редкостей в «Мои телефоны» (баг
    воспроизведён вживую: 13 шт. Samsung Galaxy Z TriFold лежат в «Платиновые»,
    а _has_working_phone стабильно возвращал 0)."""
    stem = rarity_label[:-2].lower() if len(rarity_label) > 4 else rarity_label.lower()
    for t in _all_buttons(message):
        if stem in t.lower():
            return t
    return None


_msg_text = _common_msg_text  # см. common.py — общий хелпер, вынесен туда из этого файла


def _exact_button(message, target: str) -> str | None:
    """Как _find_button, но ТОЧНОЕ совпадение текста кнопки (без учёта регистра) —
    нужно для «Слот N»: подстрочный поиск для N=1 мог бы случайно словить
    «Слот 10»/«Слот 11»/«Слот 12» (та же проблема, что уже чинили в _numeric_button)."""
    t_norm = target.strip().lower()
    for t in _all_buttons(message):
        if t.strip().lower() == t_norm:
            return t
    return None


# редкости телефонов, как называются кнопки в «Мои телефоны» / «Добавить телефон»
_RARITY_LABELS = [
    "Ширпотреб", "Необычный", "Редкий", "Мистический", "Хроматический", "Аркана", "Платиновый",
]


def _rarity_scan_order(preferred: str | None) -> list[str]:
    """Порядок перебора редкостей: если известна конкретная редкость модели
    (напр. account.farm_fill_rarity для TriFold — «Платиновый»), проверяем её
    ПЕРВОЙ, а не перебираем все 7 подряд начиная с «Ширпотреб» — при большом
    количестве прочих телефонов на каждой «неправильной» редкости это долгий
    постраничный перебор впустую (репорт: «заполнение фермы не работает, пока
    есть другие телефоны, зачем-то заходит в мои телефоны и Ширпотреб»)."""
    pref = (preferred or "").strip().lower()
    if not pref:
        return _RARITY_LABELS
    matched = [r for r in _RARITY_LABELS if r.lower() == pref]
    if not matched:
        return _RARITY_LABELS
    return matched + [r for r in _RARITY_LABELS if r.lower() != pref]


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
            self._mining_loop(), self._daily_reward_loop(),
            self._autopay_loop(), self._autotrade_loop(),
            self._container_loop(),
            self._farm_maintenance_loop(),
            self._pcoin_exchange_loop(),
            self._power_watchdog_loop(),
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
                if reply is None:
                    # таймаут ожидания ответа — это НЕ полный кулдаун, раньше тут молча
                    # подставлялся default (обычно час) и карточки простаивали до часа
                    # из-за одной транзиентной задержки бота
                    self.card_next_ts = time.time() + NO_REPLY_RETRY_SEC
                else:
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
                await self._publish_status(card_next_ts=self.card_next_ts, last_card=self.last_card)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                if await self._handle_dead_session(e):
                    return
                self.last_card = f"ошибка: {e}"
                self.card_next_ts = time.time() + _backoff_seconds(e)
                await self._publish_status(card_next_ts=self.card_next_ts, last_card=self.last_card)
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
                    self.roulette_next_ts = time.time() + delay + BUFFER_SEC
                elif reply is None:
                    # бот вообще не ответил на приглашение — таймаут, не кулдаун; раньше
                    # это тоже подставляло default (обычно час) вместо скорого повтора
                    self.last_roulette = f"⚠️ нет ответа ({clock()})"
                    self.roulette_next_ts = time.time() + NO_REPLY_RETRY_SEC
                else:
                    cd = parse_cooldown(text)
                    delay = cd if cd is not None else default
                    self.last_roulette = f"⏳ кулдаун {fmt_duration(delay)} ({clock()})"
                    self.roulette_next_ts = time.time() + delay + BUFFER_SEC
                await self._publish_status(roulette_next_ts=self.roulette_next_ts, last_roulette=self.last_roulette)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                if await self._handle_dead_session(e):
                    return
                self.last_roulette = f"ошибка: {e}"
                self.roulette_next_ts = time.time() + _backoff_seconds(e)
                await self._publish_status(roulette_next_ts=self.roulette_next_ts, last_roulette=self.last_roulette)
                await asyncio.sleep(5)

    # ---------- цикл майнинга ----------
    async def _mining_loop(self) -> None:
        """Раз в mining_check_interval секунд (настраивается в боте, как и все
        остальные интервалы в этом же меню — карточки/рулетка/авто-вывод/авто-трейд)
        проверяет ферму и собирает майнинг. collect_mining() сам по себе безопасен
        для частого опроса — он только ВКЛЮЧАЕТ ферму, если она выключена, и никогда
        не выключает её сам, так что не рискует сбросить часовой таймер накопления
        (этим рискует только farm_maintenance_now — у него отдельная защита
        min_power_toggle_interval). Ежедневная награда, авто-вывод и авто-трейд —
        свои независимые циклы ниже."""
        while self.running:
            try:
                if self._trade_mode or not self._farm_active() or not self.account.get("mining_enabled", True):
                    await asyncio.sleep(20)
                    continue
                now = time.time()
                if now < self.mining_next_ts:
                    await asyncio.sleep(min(20, self.mining_next_ts - now))
                    continue
                await self.collect_mining()
                interval = max(60, int(self.account.get("mining_check_interval", 14400)))
                self.mining_next_ts = time.time() + interval
                await self._publish_status(mining_next_ts=self.mining_next_ts, last_mining=self.last_mining)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                if await self._handle_dead_session(e):
                    return
                self.last_mining = f"ошибка: {e}"
                self.mining_next_ts = time.time() + _backoff_seconds(e)
                await self._publish_status(mining_next_ts=self.mining_next_ts, last_mining=self.last_mining)
                await asyncio.sleep(5)

    # ---------- ежедневная награда (свой якорь по mining_time, раз в сутки) ----------
    async def _daily_reward_loop(self) -> None:
        last_daily = None
        while self.running:
            try:
                if (
                    self._trade_mode or not self._farm_active()
                    or not self.account.get("daily_reward_enabled", True)
                ):
                    await asyncio.sleep(20)
                    continue
                hour, minute = parse_hhmm(self.account.get("mining_time"))
                now = datetime.now(MSK)
                if now.hour == hour and now.minute == minute and last_daily != now.date():
                    last_daily = now.date()
                    await self.collect_daily_reward()
                # daily-цикл не хранит next_ts (решение — сравнение часов:минут по МСК на
                # каждой итерации, см. выше) — для Dashboard считаем ту же самую следующую
                # отметку через seconds_until_msk (common.py), не меняя условие срабатывания
                await self._publish_status(
                    daily_next_ts=time.time() + seconds_until_msk(hour, minute),
                    last_daily=self.last_daily,
                )
                await asyncio.sleep(20)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                if await self._handle_dead_session(e):
                    return
                self.last_daily = f"ошибка: {e}"
                await asyncio.sleep(_backoff_seconds(e, default=20))

    # ---------- авто-вывод очков (независимый цикл, свой интервал) ----------
    async def _autopay_loop(self) -> None:
        while self.running:
            try:
                if (
                    self._trade_mode or not self._farm_active()
                    or not self.account.get("autopay_enabled", True)
                ):
                    await asyncio.sleep(20)
                    continue
                now = time.time()
                if now < self.autopay_next_ts:
                    await asyncio.sleep(min(30, self.autopay_next_ts - now))
                    continue
                await self._payout()
                interval = max(60, int(self.account.get("autopay_interval", 14400)))
                self.autopay_next_ts = time.time() + interval
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                if await self._handle_dead_session(e):
                    return
                self.last_payout = f"ошибка авто-вывода: {e}"
                self.autopay_next_ts = time.time() + _backoff_seconds(e)
                await asyncio.sleep(5)

    # ---------- авто-трейд (независимый цикл, свой интервал) ----------
    async def _autotrade_loop(self) -> None:
        while self.running:
            try:
                if (
                    self._trade_mode or not self._farm_active()
                    or not self.account.get("autotrade_enabled", False)
                    or not self.trade_runner
                ):
                    await asyncio.sleep(20)
                    continue
                now = time.time()
                if now < self.autotrade_next_ts:
                    await asyncio.sleep(min(30, self.autotrade_next_ts - now))
                    continue
                try:
                    await self.trade_runner(self.id)
                except Exception as e:  # noqa: BLE001
                    self.last_exchange = f"ошибка авто-трейда: {e}"
                interval = max(60, int(self.account.get("autotrade_interval", 14400)))
                self.autotrade_next_ts = time.time() + interval
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                if await self._handle_dead_session(e):
                    return
                self.last_exchange = f"ошибка авто-трейда: {e}"
                self.autotrade_next_ts = time.time() + _backoff_seconds(e, default=300)
                await asyncio.sleep(5)

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
                # containers_api_enabled (HTTP API, containers_api.py) — быстрее по
                # заявке самого модуля; если включены оба пути, они бы гонялись за
                # одним и тем же лимитированным стоком на рестоке. Отдаём приоритет
                # API-пути, клик-путь тогда просто простаивает, а не мешает
                if (not self._farm_active() or not self.account.get("containers_enabled", False)
                        or self.account.get("containers_api_enabled", False)):
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
                if await self._handle_dead_session(e):
                    return
                self.last_container = f"ошибка: {e}"
                self.container_next_ts = time.time() + _backoff_seconds(e)
                await asyncio.sleep(5)

    # ---------- биржа P-Coins: продажа за ТОчки (dump_pcoins_now) ИЛИ перевод
    # получателю (send_pcoins_now) — см. account.pcoin_send_enabled/pcoin_exchange_enabled ----------
    async def _pcoin_exchange_loop(self) -> None:
        while self.running:
            try:
                send_on = self.account.get("pcoin_send_enabled", False)
                sell_on = self.account.get("pcoin_exchange_enabled", False)
                if self._trade_mode or not self._farm_active() or not (send_on or sell_on):
                    await asyncio.sleep(30)
                    continue
                now = time.time()
                if now < self.pcoin_exchange_next_ts:
                    await asyncio.sleep(min(30, self.pcoin_exchange_next_ts - now))
                    continue
                # если включено и то, и другое — приоритет у перевода: P-Coins ценнее
                # оставить получателю решать, чем сразу превращать в ТОчки твинка
                if send_on:
                    await self.send_pcoins_now()
                else:
                    await self.dump_pcoins_now()
                interval = max(60, int(self.account.get("pcoin_exchange_interval", 14400)))
                self.pcoin_exchange_next_ts = time.time() + interval
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                if await self._handle_dead_session(e):
                    return
                self.last_pcoin_exchange = f"ошибка: {e}"
                self.pcoin_exchange_next_ts = time.time() + _backoff_seconds(e, default=300)
                await asyncio.sleep(5)

    def _classify_shop_reply(self, cfg: dict, reply, text: str | None) -> str:
        low = (text or "").lower()
        sold_out_marker = (cfg.get("sold_out_marker") or "раскуплены").lower()
        captcha_marker = (cfg.get("captcha_marker") or "анти-бот защита").lower()
        shop_marker = (cfg.get("shop_marker") or "магазин контейнеров").lower()
        miniapp_marker = (cfg.get("miniapp_button_marker") or "войти в магазин").lower()
        if sold_out_marker in low:
            return "sold_out"
        # магазин контейнеров переехал в мини-апп (кнопка «Войти в магазин
        # контейнеров» открывает WebView) — категорий бюджет/дорогой/донат в
        # этом сообщении больше нет, кликать по ним нечего. Проверяем ДО общего
        # shop_marker (тот всё ещё встречается в приветственном тексте мини-аппа)
        if _find_button(reply, miniapp_marker) and not any(
            _find_button(reply, kw) for kw in ("бюджет", "донат", "дорог")
        ):
            return "miniapp"
        if shop_marker in low or any(_find_button(reply, kw) for kw in ("бюджет", "донат", "дорог")):
            return "shop"
        if captcha_marker in low:
            return "captcha"
        return "unknown"

    async def _probe_shop(self, cfg: dict) -> tuple[str, object, str | None]:
        """Один запрос открытия магазина (отправляет команду заново). Возвращает
        (kind, reply, text), где kind — 'sold_out' | 'shop' | 'miniapp' (переехал
        в WebView, кликать нечего) | 'captcha' | 'none' (бот не ответил) | 'unknown'."""
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
        - магазин переехал в мини-апп (кнопка «Войти в магазин контейнеров», WebView,
          категорий бюджет/дорогой/донат в сообщении больше нет) -> кликать нечего,
          честно пишем в статус и ждём unknown_retry, без алерта владельцу;
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

                if kind == "miniapp":
                    # магазин контейнеров теперь мини-апп (WebView) — кликами по обычным
                    # inline-кнопкам туда не попасть и купить нечем, честно про это и пишем
                    # вместо вводящего в заблуждение «покупать нечего»
                    self.container_next_ts = time.time() + unknown_retry
                    self.last_container = (
                        f"🌐 магазин контейнеров теперь мини-апп — авто-покупка кликами "
                        f"недоступна, нужно вручную ({clock()} {today_msk()})"
                    )
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
            elif kind == "miniapp":
                self.container_next_ts = time.time() + unknown_retry
                self.last_container = (
                    f"🌐 магазин контейнеров теперь мини-апп — авто-покупка кликами "
                    f"недоступна, нужно вручную ({clock()} {today_msk()})"
                )
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
        attempts = max(1, int(cfg.get("retry_attempts", 3)))
        for i in range(attempts):
            clicked, result = await self._click_and_wait(msg, btn, bot, timeout=timeout)
            if not clicked:
                return None
            if result is not None:
                return result
            if i < attempts - 1:
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

    async def upgrade_account(self) -> str:
        """💰 Прокачка аккаунта: проходит по всем 7 категориям «Магазина улучшений»
        (Перезарядка, Шансы выпадения, Шансы апгрейда, Стойка/Охлаждение/Питание
        фермы, Лимит покупок) и в каждой жмёт «Улучшить за N», пока не упрётся в
        максимальный уровень (все категории 6-уровневые) или пока баланса не хватит
        с учётом неприкосновенного запаса UPGRADE_RESERVE. Полностью прокачать всё —
        ощутимые деньги: ниже фиксированного порога UPGRADE_MIN_BALANCE_TO_START (не
        настройка, не меняется через бота) даже не начинаем, только предупреждаем.

        Баланс проверяется ОДИН РАЗ в самом начале, ДО входа в магазин, и дальше
        отслеживается ЛОКАЛЬНО (вычитанием потраченного), а НЕ повторным текстовым
        запросом посреди навигации по инлайн-кнопкам: если слать текстовую команду
        между кликами, игровой бот сбрасывает состояние диалога, и кнопка «Улучшить
        за N» на уже показанном сообщении перестаёт отвечать (та же проблема уже
        чинилась в shop.py — раньше баланс запрашивался заново перед КАЖДОЙ
        покупкой, из-за чего прокачка успевала сделать ровно один апгрейд и глохла,
        репорт пользователя). Между последовательными покупками — небольшая пауза
        (UPGRADE_STEP_DELAY), чтобы не долбить игру слишком резко подряд."""
        if not self.client or not self.running:
            self.last_upgrade = "аккаунт не запущен"
            return self.last_upgrade
        if self._trade_mode:
            self.last_upgrade = "идёт трейд — попробуй чуть позже"
            return self.last_upgrade

        balance = await self._check_balance()
        if balance is None:
            self.last_upgrade = f"⚠️ не удалось проверить баланс ({clock()})"
            return self.last_upgrade
        if balance < UPGRADE_MIN_BALANCE_TO_START:
            self.last_upgrade = (
                f"⚠️ для прокачки нужно минимум {_fmt_points(UPGRADE_MIN_BALANCE_TO_START)} "
                f"ТОчек на балансе (сейчас {_fmt_points(balance)}) ({clock()})")
            return self.last_upgrade

        root = await self._send_and_wait(CARDS_BOT, UPGRADE_WORD, timeout=15)
        if root is None:
            self.last_upgrade = f"⚠️ нет ответа на «{UPGRADE_WORD}» ({clock()})"
            return self.last_upgrade

        lines: list[str] = []
        any_upgraded = False
        for label in UPGRADE_CATEGORIES:
            btn = _find_button(root, label)
            if not btn:
                # root мог протухнуть (см. комментарий ниже про переоткрытие магазина) —
                # даём ему один шанс обновиться, прежде чем сдаться по этой категории
                refreshed = await self._send_and_wait(CARDS_BOT, UPGRADE_WORD, timeout=15)
                if refreshed is not None:
                    root = refreshed
                    btn = _find_button(root, label)
            if not btn:
                lines.append(f"• {label}: кнопка не найдена")
                continue
            clicked, cur = await self._click_and_wait(root, btn, CARDS_BOT, timeout=15)
            if not clicked or cur is None:
                lines.append(f"• {label}: нет ответа")
                continue

            level_ups, spent, stop_reason = 0, 0, None
            level = parse_upgrade_level(_msg_text(cur))
            while True:
                dtext = _msg_text(cur)
                if UPGRADE_MAX_MARKER in dtext.lower() or (level is not None and level >= UPGRADE_MAX_LEVEL):
                    break
                buy_btn = _find_button(cur, UPGRADE_BUY_BUTTON)
                if not buy_btn:
                    stop_reason = "неизвестный формат ответа, стоп"
                    break
                cost = parse_upgrade_cost(buy_btn)
                if cost is None:
                    stop_reason = "не смог разобрать цену, стоп"
                    break
                if balance - cost < UPGRADE_RESERVE:
                    stop_reason = (
                        f"не хватает баланса с запасом (цена {_fmt_points(cost)}, запас "
                        f"{_fmt_points(UPGRADE_RESERVE)}, баланс {_fmt_points(balance)})")
                    break
                # игра переспрашивает подтверждение перед покупкой («✅ Подтвердить»),
                # но иногда сначала шлёт промежуточное сообщение ДО самого диалога
                # подтверждения (обнаружено вживую на /pay, см. _execute_pay) —
                # _click_and_wait_for_button не сдаётся после первого же ответа И
                # (после фикса) возвращает РЕАЛЬНЫЙ результат покупки, а не сам
                # диалог подтверждения (раньше из-за этого код думал, что купил,
                # хотя игра ещё ничего не поменяла — экран так и оставался на
                # старом уровне/цене, репорт пользователя со скриншотом)
                clicked3, result_msg = await self._click_and_wait_for_button(
                    cur, buy_btn, CARDS_BOT, "подтвердить", timeout=15)
                if not clicked3 or result_msg is None:
                    stop_reason = "подтверждение покупки не прошло, стоп"
                    break
                # сверяем реальный уровень с экрана результата — если он не вырос,
                # покупка на деле не прошла, несмотря на «успешный» клик
                new_level = parse_upgrade_level(_msg_text(result_msg))
                if new_level is not None and level is not None and new_level <= level:
                    stop_reason = f"уровень не изменился после покупки (был {level}), стоп"
                    break
                level = new_level if new_level is not None else ((level + 1) if level is not None else None)
                level_ups += 1
                spent += cost
                balance -= cost
                any_upgraded = True
                await asyncio.sleep(UPGRADE_STEP_DELAY)
                # экран успеха даёт «⬅️ Вернуться в магазин» (не «Назад») — жмём его,
                # чтобы вернуться в деталку категории с обновлённой ценой след. уровня
                return_btn = (
                    _find_button(result_msg, "вернуться в магазин")
                    or _find_button(result_msg, "назад")
                )
                if return_btn:
                    clicked4, cur2 = await self._click_and_wait(
                        result_msg, return_btn, CARDS_BOT, timeout=15)
                    cur = cur2 if clicked4 and cur2 is not None else result_msg
                else:
                    cur = result_msg

            if level_ups:
                level_note = f" [ур. {level}/{UPGRADE_MAX_LEVEL}]" if level is not None else ""
                summary = f"+{level_ups} ур.{level_note} (потрачено {_fmt_points(spent)} ТОчек)"
                if stop_reason:
                    summary += f" — {stop_reason}"
                lines.append(f"• {label}: {summary}")
            elif stop_reason:
                lines.append(f"• {label}: {stop_reason}")
            else:
                lines.append(f"• {label}: уже максимум")

            back_btn = _find_button(cur, "назад")
            new_root = None
            if back_btn:
                clicked3, back_msg = await self._click_and_wait(cur, back_btn, CARDS_BOT, timeout=15)
                if clicked3 and back_msg is not None:
                    new_root = back_msg
            if new_root is None:
                # неожиданный экран вместо магазина (например, попало уведомление о
                # достижении, всплывшее от того же бота между кликами) - "назад" не
                # нашли, а протухший root убивает ВСЕ следующие категории (клики по
                # старому меню бот молча игнорирует) - переоткрываем магазин заново
                new_root = await self._send_and_wait(CARDS_BOT, UPGRADE_WORD, timeout=15)
            if new_root is not None:
                root = new_root
            # небольшая пауза перед следующей категорией НЕЗАВИСИМО от того, была ли
            # покупка — если категория уже максимальная, покупок нет и паузы после
            # неё тоже не было; несколько категорий подряд без единой паузы ловят
            # антифлуд игры, и следующие категории перестают отвечать (репорт
            # пользователя: «проверяет первые 3 и если они прокачены — перестаёт»)
            await asyncio.sleep(UPGRADE_STEP_DELAY)

        all_maxed = all("максимум" in ln.lower() for ln in lines) if lines else True
        if any_upgraded:
            header = f"✅ прокачано ({clock()}):"
        elif all_maxed:
            header = f"✅ всё уже на максимуме ({clock()})"
        else:
            header = f"⚠️ прокачка не удалась ({clock()}):"
        self.last_upgrade = f"{header}\n" + "\n".join(lines) if lines else header
        return self.last_upgrade

    async def _buy_category(
        self, bot: str, shop_msg, category_label: str, cfg: dict, balance: int | None,
    ) -> tuple[bool, str, int]:
        """Возвращает (успех, инфо-строка, потрачено ТОчек)."""
        if not _find_button(shop_msg, category_label):
            return False, "", 0
        detail = await self._click_step(bot, shop_msg, category_label, cfg, timeout=15)
        if detail is None:
            return False, f"{category_label}: нет ответа на выбор категории", 0
        dtext = _msg_text(detail)
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

        step_text = _msg_text(step)
        total_price = parse_total_price(step_text) or (unit_price * remaining if unit_price else 0)

        # если это экран подтверждения — жмём; если покупка уже прошла сама — не мешаем
        if _find_button(step, confirm_btn):
            if not await self._click_confirm(step, confirm_btn, cfg):
                return False, f"{category_label}: не подтвердилась покупка", 0

        self._bump("containers_bought", remaining)
        if key in _CATEGORY_QTY_FIELD:  # key = "donation"/"expensive"/"budget"
            self._bump(f"containers_bought_{key}", remaining)
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

    async def _send_owner_alert(self, text: str, markup=None, error_label: str = "алерт") -> None:
        """Общая точка отправки: owner_id/alerts_enabled/alert_fn guard + try/except
        с print-фоллбэком — вынесено из 4-х похожих _notify_* функций ниже. Текст/
        markup каждая функция строит по-своему (у капчи своя многоветочная логика +
        кнопки выбора категории — не унифицируем силой, только общий "хвост" отправки)."""
        owner_id = self.account.get("owner_id")
        if not owner_id or not self.account.get("alerts_enabled", True) or not self.alert_fn:
            return
        try:
            await self.alert_fn(owner_id, text, markup)
        except Exception as e:  # noqa: BLE001
            print(f"[{self.name}] не удалось отправить {error_label}: {e}")

    async def _notify_price_mismatch(self, category_label: str, unit_price: int) -> None:
        text = Design.alert_frame(
            "⚠️", "МАГАЗИН КОНТЕЙНЕРОВ", self.name,
            (f"<b>Причина:</b> цена «{category_label}» выглядит подозрительно "
             f"({unit_price:,} ТОчек)").replace(",", " ") +
            "\n<i>Действие: категория пропущена на всякий случай — проверь вручную</i>",
        )
        await self._send_owner_alert(text, error_label="алерт о цене")

    async def _notify_restock_soon(self, cd: int) -> None:
        """Разовый алерт «ресток скоро» — за restock_alert_before секунд до расчётного
        рестока (или сразу с фактическим остатком, если он уже меньше на момент проверки)."""
        text = Design.alert_frame("⏰", "РЕСТОК СКОРО", self.name,
                                  f"<b>Магазин контейнеров:</b> ресток через <code>{fmt_duration(cd)}</code>")
        await self._send_owner_alert(text, error_label="алерт о рестоке")

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

    async def _notify_owner_captcha(self, cfg: dict, shop_text: str | None, reason: str = "captcha") -> None:
        owner_id = self.account.get("owner_id")
        if not owner_id or not self.account.get("alerts_enabled", True):
            return
        if reason == "captcha":
            if not self._captcha_alert_allowed():
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
        await self._send_owner_alert(text, markup, error_label="алерт через управляющего бота")

    # ---------- действия ----------
    async def _ensure_farm_on(self, root, cfg: dict | None = None):
        """Если ответ на «Тмайнинг» показывает «Состояние: Выключена» — жмёт
        «Включить». Новая ферма (после обновления игры) по умолчанию выключена
        и не майнит, пока её не включат явно — без этой проверки автосбор
        майнинга/обслуживание фермы могли бы молча простаивать бесконечно на
        выключенной ферме. Возвращает актуальное сообщение (то же, если ферма
        уже была включена или кнопки не нашлось)."""
        cfg = cfg if cfg is not None else self.farm_maintenance_cfg
        if parse_farm_state(_msg_text(root)) != "выключена":
            return root
        on_btn = _find_button(root, cfg.get("power_on_button", "включить"))
        if not on_btn:
            return root
        clicked, resumed = await self._click_and_wait(root, on_btn, CARDS_BOT, timeout=15)
        return resumed if clicked and resumed is not None else root

    async def collect_mining(self) -> bool:
        """«Тмайнинг» -> (включить ферму, если выключена) -> «Снять деньги с
        фермы». True при успехе."""
        if not self.client or not self.running:
            self.last_mining = "аккаунт не запущен"
            return False
        if self._trade_mode:
            self.last_mining = "идёт трейд — позже"
            return False
        try:
            reply = await self._send_and_wait(CARDS_BOT, MINING_WORD)
            reply = await self._ensure_farm_on(reply)
            clicked = await self._try_click(reply, FARM_WITHDRAW_BUTTON)
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

    async def dump_pcoins_now(self) -> str:
        """💱 Продаёт P-Coins из кошелька на бирже («/texchange») за ТОчки по
        рыночному курсу (комиссия ~5%, это условие самой игры, не баг). Флоу
        (проверено вживую): открыть биржу -> клик «Продать» -> бот просит
        ОТВЕТИТЬ ЧИСЛОМ прямо в чат (не кнопка!) -> экран «ОРДЕР НА ПРОДАЖУ
        (MARKET)» -> клик «Подтвердить ордер». Продаёт весь кошелёк (сколько
        P-Coins накопилось после «Снять P-Coins с фермы» — см. collect_mining).
        Используется циклом (тумблер pcoin_exchange_enabled) и кнопкой «💱
        Проверить биржу сейчас»."""
        if not self.client or not self.running:
            self.last_pcoin_exchange = "аккаунт не запущен"
            return self.last_pcoin_exchange
        if self._trade_mode:
            self.last_pcoin_exchange = "идёт трейд — попробуй чуть позже"
            return self.last_pcoin_exchange
        cfg = self.exchange_cfg
        bot = cfg.get("bot") or CARDS_BOT
        open_cmd = cfg.get("open_command") or EXCHANGE_WORD
        try:
            root = await self._send_and_wait(bot, open_cmd, timeout=15)
            if root is None:
                self.last_pcoin_exchange = f"⚠️ биржа ({open_cmd}) не отвечает ({clock()})"
                return self.last_pcoin_exchange

            wallet = parse_pcoin_wallet(_msg_text(root))
            if wallet is None:
                self.last_pcoin_exchange = f"⚠️ не разобрал кошелёк биржи ({clock()})"
                await self._notify_owner_exchange_unexpected(_msg_text(root))
                return self.last_pcoin_exchange
            qty = int(wallet)
            if qty <= 0:
                self.last_pcoin_exchange = f"нечего продавать (P-Coins: {wallet:g}) ({clock()})"
                return self.last_pcoin_exchange

            sell_btn = _find_button(root, cfg.get("sell_button", "продать"))
            if not sell_btn:
                self.last_pcoin_exchange = f"⚠️ кнопка «продать» не найдена ({clock()})"
                return self.last_pcoin_exchange
            clicked, ask_qty = await self._click_and_wait(root, sell_btn, bot, timeout=15)
            if not clicked or ask_qty is None:
                self.last_pcoin_exchange = f"⚠️ нет ответа на «продать» ({clock()})"
                return self.last_pcoin_exchange

            # бот просит количество ОТВЕТОМ В ЧАТ, а не кнопкой — это ожидаемый шаг
            # флоу самой игры (проверено вживую), не «текст посреди навигации»
            order = await self._send_and_wait(bot, str(qty), timeout=15)
            if order is None:
                self.last_pcoin_exchange = f"⚠️ нет ответа на количество «{qty}» ({clock()})"
                return self.last_pcoin_exchange
            confirm_btn = _find_button(order, cfg.get("confirm_button", "подтвердить ордер"))
            if not confirm_btn:
                self.last_pcoin_exchange = f"⚠️ нет кнопки подтверждения ордера ({clock()})"
                await self._notify_owner_exchange_unexpected(_msg_text(order))
                return self.last_pcoin_exchange
            clicked2, done = await self._click_and_wait(order, confirm_btn, bot, timeout=15)
            if not clicked2:
                self.last_pcoin_exchange = f"⚠️ ордер продажи не подтвердился ({clock()})"
                return self.last_pcoin_exchange

            self._bump("pcoin_sold", qty)
            self.last_pcoin_exchange = f"💱 продано {qty} P-Coin ({clock()} {today_msk()})"
            return self.last_pcoin_exchange
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.last_pcoin_exchange = f"ошибка: {e}"
            return self.last_pcoin_exchange

    async def send_pcoins_now(self) -> str:
        """💱 Переводит P-Coins из кошелька получателю (account.payout_target) —
        альтернатива dump_pcoins_now() для твинков, чей P-Coins должны копиться на
        основном аккаунте, а не превращаться в ТОчки твинка. См. _send_pcoins."""
        if not self.client or not self.running:
            self.last_pcoin_exchange = "аккаунт не запущен"
            return self.last_pcoin_exchange
        if self._trade_mode:
            self.last_pcoin_exchange = "идёт трейд — попробуй чуть позже"
            return self.last_pcoin_exchange
        target = (self.account.get("payout_target") or "").strip()
        if not target:
            self.last_pcoin_exchange = "⚠️ не задан получатель (payout_target) — задай в 🎯 Получатели"
            return self.last_pcoin_exchange
        return await self._send_pcoins(target)

    async def send_pcoins_to_now(self, target: str) -> str:
        """Как send_pcoins_now(), но с явным получателем вместо настроенного
        payout_target — для «▶️ Действия -> Финансы -> Собрать с твинков»: твинк
        шлёт P-Coins СЮДА (главному аккаунту), независимо от своего обычного
        получателя."""
        if not self.client or not self.running:
            self.last_pcoin_exchange = "аккаунт не запущен"
            return self.last_pcoin_exchange
        if self._trade_mode:
            self.last_pcoin_exchange = "идёт трейд — попробуй чуть позже"
            return self.last_pcoin_exchange
        return await self._send_pcoins(target)

    async def _send_pcoins(self, target: str) -> str:
        """Общая часть send_pcoins_now()/send_pcoins_to_now() — переводит ВЕСЬ
        кошелёк P-Coins явно заданному получателю через биржу («/texchange» ->
        «Отправить P-Coins»). Флоу (проверено вживую): открыть биржу -> «Отправить
        P-Coins» -> ответить получателем (@username/id) ТЕКСТОМ -> ответить
        количеством ТЕКСТОМ -> «Пропустить» комментарий -> «Подтвердить» перевод.
        Отдельного сообщения об успехе нет — то же сообщение просто редактируется
        обратно в «Биржа P-Coin» с уменьшенным балансом (баланс здесь не
        перепроверяем повторно текстом — см. общий баг-паттерн этого бота:
        текстовая команда посреди навигации сбрасывает диалог)."""
        cfg = self.exchange_cfg
        bot = cfg.get("bot") or CARDS_BOT
        open_cmd = cfg.get("open_command") or EXCHANGE_WORD
        try:
            root = await self._send_and_wait(bot, open_cmd, timeout=15)
            if root is None:
                self.last_pcoin_exchange = f"⚠️ биржа ({open_cmd}) не отвечает ({clock()})"
                return self.last_pcoin_exchange

            wallet = parse_pcoin_wallet(_msg_text(root))
            if wallet is None:
                self.last_pcoin_exchange = f"⚠️ не разобрал кошелёк биржи ({clock()})"
                await self._notify_owner_exchange_unexpected(_msg_text(root))
                return self.last_pcoin_exchange
            qty = int(wallet)
            if qty <= 0:
                self.last_pcoin_exchange = f"нечего переводить (P-Coins: {wallet:g}) ({clock()})"
                return self.last_pcoin_exchange

            send_btn = _find_button(root, cfg.get("send_button", "отправить p-coins"))
            if not send_btn:
                self.last_pcoin_exchange = f"⚠️ кнопка «отправить P-Coins» не найдена ({clock()})"
                return self.last_pcoin_exchange
            clicked, ask_target = await self._click_and_wait(root, send_btn, bot, timeout=15)
            if not clicked or ask_target is None:
                self.last_pcoin_exchange = f"⚠️ нет ответа на «отправить P-Coins» ({clock()})"
                return self.last_pcoin_exchange

            raw = target.lstrip("@")
            target_norm = raw if raw.isdigit() else f"@{raw}"
            not_found_marker = (cfg.get("not_found_marker") or "пользователь не найден").lower()
            ask_qty = await self._send_and_wait(bot, target_norm, timeout=15)
            if ask_qty is None:
                self.last_pcoin_exchange = f"⚠️ нет ответа на получателя «{target_norm}» ({clock()})"
                return self.last_pcoin_exchange
            if not_found_marker in _msg_text(ask_qty).lower():
                self.last_pcoin_exchange = f"⚠️ получатель «{target_norm}» не найден игрой ({clock()})"
                return self.last_pcoin_exchange

            ask_comment = await self._send_and_wait(bot, str(qty), timeout=15)
            if ask_comment is None:
                self.last_pcoin_exchange = f"⚠️ нет ответа на количество «{qty}» ({clock()})"
                return self.last_pcoin_exchange

            skip_btn = _find_button(ask_comment, cfg.get("skip_comment_button", "пропустить"))
            confirm_screen = ask_comment
            if skip_btn:
                clicked_skip, after_skip = await self._click_and_wait(ask_comment, skip_btn, bot, timeout=15)
                confirm_screen = after_skip if clicked_skip and after_skip is not None else None
            if confirm_screen is None:
                self.last_pcoin_exchange = f"⚠️ нет экрана подтверждения перевода ({clock()})"
                return self.last_pcoin_exchange

            confirm_btn = _find_button(confirm_screen, cfg.get("transfer_confirm_button", "подтвердить"))
            if not confirm_btn:
                self.last_pcoin_exchange = f"⚠️ нет кнопки подтверждения перевода ({clock()})"
                await self._notify_owner_exchange_unexpected(_msg_text(confirm_screen))
                return self.last_pcoin_exchange
            clicked2, _done = await self._click_and_wait(confirm_screen, confirm_btn, bot, timeout=15)
            if not clicked2:
                self.last_pcoin_exchange = f"⚠️ перевод не подтвердился ({clock()})"
                return self.last_pcoin_exchange

            self._bump("pcoin_sent", qty)
            self.last_pcoin_exchange = (
                f"💱 переведено {qty} P-Coin получателю «{target_norm}» ({clock()} {today_msk()})")
            return self.last_pcoin_exchange
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.last_pcoin_exchange = f"ошибка: {e}"
            return self.last_pcoin_exchange

    async def _notify_owner_exchange_unexpected(self, text: str) -> None:
        """Биржа ответила чем-то неожиданным (не разобрали кошелёк/нет кнопки
        подтверждения) — как и с контейнерами, зовём владельца разобраться,
        вместо того чтобы кликать вслепую."""
        msg = Design.alert_frame(
            "⚠️", "ОБМЕН P-COINS", self.name,
            f"<b>Причина:</b> неожиданный ответ биржи, проверь вручную\n«{text[:500]}»",
        )
        await self._send_owner_alert(msg, error_label="алерт о бирже")

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

    # ---------- обслуживание модульной фермы (снять сломанные -> автопочинка -> вернуть) ----------
    def _farm_maintenance_active(self) -> bool:
        return self._farm_active() and self.account.get("farm_maintenance_enabled", False)

    async def _farm_maintenance_loop(self) -> None:
        """Раз в farm_maintenance_interval секунд (настраивается в боте, тем же
        стилем, что и остальные интервалы) вызывает farm_maintenance_now(). Она
        сама решает, выключать ли ферму, ТОЛЬКО ПОСЛЕ проверки, что телефон для
        установки реально готов (_has_working_phone) — иначе ферма гасла бы
        впустую. А min_power_toggle_interval внутри неё не даёт выключать ферму
        чаще раза в час независимо от того, как часто срабатывает этот цикл — так
        что можно смело опрашивать чаще, ничего не теряя. Сам ремонт извлечённых
        телефонов делает НЕ этот цикл, а auto_repair_loop (repair.py)."""
        while self.running:
            try:
                if self._trade_mode or not self._farm_maintenance_active():
                    await asyncio.sleep(30)
                    continue
                now = time.time()
                if now < self.farm_maintenance_next_ts:
                    await asyncio.sleep(min(30, self.farm_maintenance_next_ts - now))
                    continue
                await self.farm_maintenance_now()
                interval = max(60, int(self.account.get("farm_maintenance_interval", 3600)))
                self.farm_maintenance_next_ts = time.time() + interval
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                if await self._handle_dead_session(e):
                    return
                self.last_farm_maintenance = f"ошибка: {e}"
                self.farm_maintenance_next_ts = time.time() + _backoff_seconds(e)
                await asyncio.sleep(5)

    # ---------- watchdog аварийной перегрузки питания/охлаждения (случайные «События») ----------
    async def _power_watchdog_loop(self) -> None:
        """Проверяет питание/охлаждение фермы часто (свой интервал, НЕ расписание
        обслуживания и НЕ кулдаун min_power_toggle_interval — это аварийная реакция
        на случайное «Событие» вроде «Взрыв электростанции», которое режет лимит
        PSU/Cooling и может увести ферму в «Перегрузка» — полная остановка, а не
        обычное выключение). Снимает РАБОЧИЕ телефоны по одному (не сломанные — см.
        _remove_working_slot), пока нагрузка не впишется в лимит; снятые запоминаются
        в farm_slot_models и вернутся сами при следующем плановом обслуживании
        (farm_maintenance_now), когда появится свободная мощность."""
        while self.running:
            try:
                interval = max(60, int(self.account.get("power_watchdog_interval", 300)))
                if (self._trade_mode or not self._farm_active()
                        or not self.account.get("power_watchdog_enabled", False)):
                    await asyncio.sleep(interval)
                    continue
                await self.relieve_overload_now()
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                if await self._handle_dead_session(e):
                    return
                self.last_power_watchdog = f"ошибка: {e}"
                await asyncio.sleep(_backoff_seconds(e, default=60))

    async def relieve_overload_now(self) -> str:
        """Обёртка над _relieve_overload_now_impl под общим с farm_maintenance_now
        локом (_farm_power_lock) — обе многошагово дёргают Выключить/Включить на
        одной и той же ферме (и через цикл, и через ручную кнопку в меню), без лока
        могли интерливиться и оставлять ферму в непредсказуемом состоянии питания."""
        async with self._farm_power_lock:
            return await self._relieve_overload_now_impl()

    async def _relieve_overload_now_impl(self) -> str:
        """Если ферма сейчас в аварийной перегрузке питания/охлаждения — снимает
        рабочие телефоны по одному (перепроверяя нагрузку после каждого), пока
        не перестанет превышать лимит, затем включает ферму обратно. Используется
        циклом (power_watchdog_enabled) и кнопкой в меню."""
        if not self.client or not self.running:
            self.last_power_watchdog = "аккаунт не запущен"
            return self.last_power_watchdog
        if self._trade_mode:
            self.last_power_watchdog = "идёт трейд — попробуй чуть позже"
            return self.last_power_watchdog
        cfg = self.farm_maintenance_cfg
        bot = cfg.get("bot") or CARDS_BOT
        mining_cmd = cfg.get("mining_command") or MINING_WORD
        powered_off = False
        try:
            root = await self._send_and_wait(bot, mining_cmd, timeout=20)
            if root is None:
                self.last_power_watchdog = f"⚠️ нет ответа на «{mining_cmd}» ({clock()})"
                return self.last_power_watchdog
            text = _msg_text(root)
            psu, cooling = parse_power_load(text)
            if not _is_overloaded(psu, cooling):
                self.last_power_watchdog = f"✅ питание/охлаждение в норме ({clock()})"
                return self.last_power_watchdog

            slots = parse_farm_slots(text)
            slot_models = self.account.setdefault("farm_slot_models", {})
            # Раньше снимали строго по номеру слота, не глядя, что за телефон — рискуя
            # снять именно настроенную "целевую" модель фермы (farm_fill_model) раньше
            # случайных прочих. Экран фермы не показывает редкость (только модель), так
            # что полной сортировки по тиру нет — но хотя бы модель из farm_fill_model
            # защищена: снимается последней, если совсем не хватает других вариантов
            fill_model = (self.account.get("farm_fill_model") or "").strip().lower()
            working_nums = sorted(
                (n for n, i in slots.items() if i["status"] == "working"),
                key=lambda n: (slots[n].get("model", "").strip().lower() == fill_model, n),
            )
            if not working_nums:
                self.last_power_watchdog = f"⚠️ перегрузка, но нет рабочих телефонов, чтобы снять ({clock()})"
                return self.last_power_watchdog

            off_result = await self._click_step(bot, root, cfg.get("power_off_button", "выключить"), cfg)
            powered_off = off_result is not None
            if powered_off:
                self.farm_last_power_off_ts = time.time()

            removed: list[str] = []
            max_removals = min(len(working_nums), max(1, int(cfg.get("power_watchdog_max_removals", 6))))
            for num in working_nums:
                if len(removed) >= max_removals:
                    break
                model = slots[num].get("model")
                fresh = await self._send_and_wait(bot, mining_cmd, timeout=20)
                if fresh is None:
                    break
                ok = await self._remove_working_slot(bot, fresh, num, cfg)
                if ok:
                    self._bump("farm_extracted")
                    removed.append(f"№{num} «{model or '?'}»")
                    if model:
                        slot_models[str(num)] = model
                # перепроверяем нагрузку после КАЖДОГО снятия — не снимаем больше, чем нужно
                check = await self._send_and_wait(bot, mining_cmd, timeout=20)
                if check is not None and not _is_overloaded(*parse_power_load(_msg_text(check))):
                    break

            if powered_off:
                fresh = await self._send_and_wait(bot, mining_cmd, timeout=20)
                resumed = None
                if fresh is not None:
                    resumed = await self._click_step(bot, fresh, cfg.get("power_on_button", "включить"), cfg)
                powered_off = resumed is None

            if self.storage:
                try:
                    self.storage.save()
                except Exception:
                    pass

            if removed:
                self.last_power_watchdog = (
                    f"⚡ перегрузка — снял: {', '.join(removed)}, вернутся сами при "
                    f"следующем обслуживании ({clock()} {today_msk()})"
                )
            else:
                self.last_power_watchdog = f"⚠️ перегрузка, снять телефон не удалось ({clock()})"
            return self.last_power_watchdog
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.last_power_watchdog = f"ошибка: {e}"
            return self.last_power_watchdog
        finally:
            if powered_off:
                try:
                    fresh = await self._send_and_wait(bot, mining_cmd, timeout=20)
                    if fresh is not None:
                        await self._click_step(bot, fresh, cfg.get("power_on_button", "включить"), cfg)
                except Exception:  # noqa: BLE001
                    pass

    async def _has_working_phone(
        self, model_name: str, cfg: dict, preferred_rarity: str | None = None,
    ) -> bool:
        """Проверяет «Мои телефоны -> Рабочие телефоны -> [редкость] -> модель
        (xN)» БЕЗ выключения фермы — чтобы решить, стоит ли вообще выключать
        ферму, ДО того как её выключать (иначе она гаснет впустую, если нужного
        телефона ещё нет — например, ремонт снятого экземпляра не завершился)."""
        return await self._count_working_phone(model_name, cfg, preferred_rarity) > 0

    async def _count_working_phone(
        self, model_name: str, cfg: dict, preferred_rarity: str | None = None,
    ) -> int:
        """Как _has_working_phone, но возвращает РЕАЛЬНОЕ количество «в запасе»
        (суффикс «(xN)» на кнопке модели в «Мои телефоны -> Рабочие телефоны»),
        а не просто «есть хотя бы один» — нужно, чтобы «Заполнить ферму» докупала
        точную нехватку, а не либо ничего, либо сразу всё целевое число (баг:
        имея ровно нужное количество уже купленным, всё равно пыталась докупить
        ещё, раз проверка была булевой и не считала, сколько реально нужно).

        preferred_rarity (напр. account.farm_fill_rarity) — если известна,
        проверяется ПЕРВОЙ, см. _rarity_scan_order."""
        if not model_name:
            return 0
        bot = cfg.get("bot") or CARDS_BOT
        phones_cmd = self.repair_cfg.get("my_phones_command", "Мои телефоны")
        entry = await self._send_and_wait(bot, phones_cmd, timeout=20)
        if entry is None:
            return 0
        working_btn = _find_button(entry, cfg.get("working_phones_button", "рабочие телефоны"))
        if not working_btn:
            return 0
        cats = await self._click_step(bot, entry, working_btn, cfg, timeout=15)
        if cats is None:
            return 0
        for rarity_label in _rarity_scan_order(preferred_rarity):
            cat_btn = _find_rarity_button(cats, rarity_label)
            if not cat_btn:
                continue
            cat_msg = await self._click_step(bot, cats, cat_btn, cfg, timeout=15)
            if cat_msg is None:
                continue
            phone_btn, _page = await self._find_phone_button_across_pages(bot, cat_msg, model_name, cfg)
            if phone_btn:
                m = re.search(r"\(x?(\d+)\)\s*$", phone_btn.strip(), re.IGNORECASE)
                return int(m.group(1)) if m else 1
        return 0

    async def farm_maintenance_now(self) -> str:
        """Обёртка над _farm_maintenance_now_impl под общим с relieve_overload_now
        локом (_farm_power_lock) — см. комментарий там."""
        async with self._farm_power_lock:
            return await self._farm_maintenance_now_impl()

    async def _farm_maintenance_now_impl(self) -> str:
        """Обслуживание фермы: для каждого слота со статусом СЛОМАН — «Слот N» ->
        «Извлечь сломанный» (телефон уходит в «Мои телефоны -> Нерабочие»),
        безусловно. Для пустых слотов — сначала (ДО выключения фермы!) решаем,
        какую модель туда просить (память в account.farm_slot_models важнее —
        так снятый на ремонт телефон возвращается именно на своё место — иначе
        account.farm_fill_model) и проверяем через _has_working_phone(), есть ли
        она реально в инвентаре; выключаем ферму ради установки, только если
        occupied < account.farm_target_phones И хотя бы для одного пустого слота
        телефон подтверждённо есть — иначе ферма и так укомплектована
        достаточно, либо ставить всё равно нечего, трогать её незачем.

        Игра требует выключенную ферму для установки/извлечения телефона из слота
        («Для установки или извлечения телефона необходимо выключить ферму») —
        КРИТИЧНО держать её выключенной СТРОГО на время этих кликов и ни секундой
        дольше (владелец теряет майнинг за каждый час простоя): сначала жмём
        «Выключить», снимаем/ставим, и СРАЗУ ЖЕ (ещё до ремонта, не дожидаясь его)
        возвращаем «Включить» — через finally, даже если по пути была ошибка, но
        только если выключали её мы сами (уже выключенную кем-то ещё не трогаем).
        Ремонт снятых телефонов (repair_now(), см. repair.py) идёт ПОСЛЕ, когда
        ферма уже снова включена — сам ремонт идёт через «Мои телефоны», ему
        включённость фермы не мешает и не должна её задерживать.

        Между шагами команда «Тмайнинг» отправляется заново (не идёт «назад» по
        уже открытым меню) — так же, как _buy_containers() между категориями:
        меньше риска зависнуть на устаревшем сообщении, если бот отредактировал
        его иначе, чем ожидалось. Используется циклом (по расписанию) и кнопкой в меню."""
        if not self.client or not self.running:
            self.last_farm_maintenance = "аккаунт не запущен"
            return self.last_farm_maintenance
        if self._trade_mode:
            self.last_farm_maintenance = "идёт трейд — попробуй чуть позже"
            return self.last_farm_maintenance
        cfg = self.farm_maintenance_cfg
        bot = cfg.get("bot") or CARDS_BOT
        mining_cmd = cfg.get("mining_command") or MINING_WORD
        powered_off = False
        try:
            root = await self._send_and_wait(bot, mining_cmd, timeout=20)
            if root is None:
                self.last_farm_maintenance = f"⚠️ нет ответа на «{mining_cmd}» ({clock()})"
                return self.last_farm_maintenance
            root = await self._ensure_farm_on(root, cfg)
            slots = parse_farm_slots(_msg_text(root))
            if not slots:
                self.last_farm_maintenance = f"⚠️ не разобрал слоты фермы ({clock()})"
                return self.last_farm_maintenance

            slot_models = self.account.setdefault("farm_slot_models", {})
            for num, info in slots.items():
                if info.get("model"):
                    slot_models[str(num)] = info["model"]

            broken_nums = sorted(n for n, i in slots.items() if i["status"] == "broken")
            empty_nums_all = sorted(n for n, i in slots.items() if i["status"] == "empty")
            occupied = len(slots) - len(empty_nums_all)
            target = max(0, int(self.account.get("farm_target_phones", 11)))
            fill_model = (self.account.get("farm_fill_model") or "").strip()
            # farm_fill_rarity не задана явно — используем phone_shop_rarity (та же
            # редкость, которой аккаунт реально ПОКУПАЕТ fill_model в магазине и,
            # значит, скорее всего, верна и для поиска уже купленных экземпляров
            # в «Мои телефоны», а не общий перебор с «Ширпотреб»)
            fill_rarity = (
                self.account.get("farm_fill_rarity") or self.account.get("phone_shop_rarity") or ""
            ).strip()

            # для каждого пустого слота модель-кандидат: своя память важнее общего
            # fill_model (снятый на ремонт телефон должен вернуться на своё же место).
            # farm_slot_models НЕ хранит редкость (её не показывает сам экран фермы) —
            # раньше «своя память»-слоты вообще не получали preferred_rarity и всегда
            # уходили в полный перебор с «Ширпотреб» (тот же паттерн, что уже когда-то
            # чинили для fill_model — см. _rarity_scan_order, репорт «зачем-то заходит
            # в Ширпотреб»). fill_rarity — не гарантированно верная редкость для КОНКРЕТНО
            # этого телефона, но разумная подсказка (проверяется первой, при промахе
            # цикл в _count_working_phone/_reinstall_phone_slot всё равно перебирает
            # остальные редкости) — лучше, чем не иметь подсказки вообще
            candidate_by_slot: dict[int, str] = {}
            rarity_by_model: dict[str, str | None] = {}
            for n in empty_nums_all:
                remembered = slot_models.get(str(n))
                model = remembered or fill_model
                if model:
                    candidate_by_slot[n] = model
                    if fill_rarity and model not in rarity_by_model:
                        rarity_by_model[model] = fill_rarity

            # у майнинга часовой таймер накопления, который сбрасывается КАЖДЫЙ раз при
            # выключении фермы (даже кратком) — если обслуживание выключало бы её чаще
            # min_power_toggle_interval (по умолчанию час), накопление никогда бы не
            # успевало дособраться. Пока действует этот «кулдаун» с прошлого выключения —
            # пропускаем ВЕСЬ проход извлечения/установки (включая сломанные), не выключая
            # ферму вообще; следующий плановый проход попробует снова
            min_toggle = max(0, int(cfg.get("min_power_toggle_interval", 3600)))
            since_last_off = time.time() - self.farm_last_power_off_ts
            toggle_cooldown = bool(broken_nums or empty_nums_all) and since_last_off < min_toggle

            # проверяем НАЛИЧИЕ телефона ДО выключения фермы — иначе она гаснет
            # впустую, если ставить пока нечего (не завершился ремонт и т.п.), а
            # заодно вообще не трогаем ферму, если она и так укомплектована
            pending_empty_nums: list[int] = []
            if not toggle_cooldown and occupied < target and candidate_by_slot:
                availability: dict[str, bool] = {}
                for n, model in candidate_by_slot.items():
                    if model not in availability:
                        availability[model] = await self._has_working_phone(
                            model, cfg, rarity_by_model.get(model))
                    if availability[model]:
                        pending_empty_nums.append(n)
                pending_empty_nums.sort()

            if toggle_cooldown:
                broken_nums = []

            if broken_nums or pending_empty_nums:
                off_result = await self._click_step(bot, root, cfg.get("power_off_button", "выключить"), cfg)
                powered_off = off_result is not None
                if powered_off:
                    self.farm_last_power_off_ts = time.time()

            extracted: list[str] = []
            just_extracted_models: list[str] = []
            for num in broken_nums:
                fresh = await self._send_and_wait(bot, mining_cmd, timeout=20)
                if fresh is None:
                    break
                ok, model = await self._extract_broken_slot(bot, fresh, num, cfg)
                if ok:
                    self._bump("farm_extracted")
                    extracted.append(f"№{num} «{model or '?'}»")
                    if model:
                        just_extracted_models.append(model)

            reinstalled: list[str] = []
            for num in pending_empty_nums:
                expected = candidate_by_slot.get(num)
                if not expected:
                    continue
                fresh = await self._send_and_wait(bot, mining_cmd, timeout=20)
                if fresh is None:
                    break
                ok = await self._reinstall_phone_slot(
                    bot, fresh, num, expected, cfg, rarity_by_model.get(expected))
                if ok:
                    self._bump("farm_reinstalled")
                    reinstalled.append(f"№{num} «{expected}»")

            # снятие/установка закончены — возвращаем ферму СРАЗУ, не дожидаясь
            # ремонта ниже (тот может занять минуты на телефон — навигация,
            # ретраи), а каждый час простоя фермы стоит владельцу майнинга.
            # ВАЖНО: помечаем «снова включена» только если клик реально удался —
            # раньше это сбрасывалось безусловно сразу после ПОПЫТКИ клика, и если
            # она молча не срабатывала (таймаут/не тот ответ), подстраховка в
            # finally ниже уже не видела, что ферма всё ещё выключена, и ферма
            # простаивала выключенной до следующего планового обслуживания
            # (репорт пользователя — «тупо потушил майнинг на время ремонта»)
            if powered_off:
                fresh = await self._send_and_wait(bot, mining_cmd, timeout=20)
                resumed = None
                if fresh is not None:
                    resumed = await self._click_step(bot, fresh, cfg.get("power_on_button", "включить"), cfg)
                powered_off = resumed is None

            if self.storage:
                try:
                    self.storage.save()
                except Exception:
                    pass

            # теперь (ферма уже снова включена) чиним снятые телефоны — не дожидаясь
            # отдельного цикла автопочинки; repair_now() сам найдёт именно эти модели
            # среди «Мои телефоны -> Нерабочие» по farm_slot_models (см. repair.py)
            repaired_now: list[str] = [
                f"«{model}»: {await self.repair_now()}" for model in just_extracted_models
            ]

            parts = []
            if extracted:
                parts.append("извлечены: " + ", ".join(extracted))
            if repaired_now:
                parts.append("отправлены в ремонт: " + "; ".join(repaired_now))
            if reinstalled:
                parts.append("возвращены в ферму: " + ", ".join(reinstalled))
            if not parts:
                if toggle_cooldown:
                    wait_left = int(min_toggle - since_last_off)
                    parts.append(f"есть что сделать, но жду кулдаун выключения фермы "
                                 f"(~{max(0, wait_left)}с) — не сбрасываю часовое накопление майнинга")
                else:
                    parts.append(
                        f"сломанных слотов нет, ферма укомплектована ({occupied}/{target}) "
                        f"либо пополнять пока нечем"
                    )
            self.last_farm_maintenance = f"🔧 {'; '.join(parts)} ({clock()} {today_msk()})"
            return self.last_farm_maintenance
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.last_farm_maintenance = f"ошибка: {e}"
            return self.last_farm_maintenance
        finally:
            # срабатывает, только если включение выше почему-то не случилось
            # (ранний return/ошибка ДО того шага) — подстраховка, а не основной путь
            if powered_off:
                try:
                    fresh = await self._send_and_wait(bot, mining_cmd, timeout=20)
                    if fresh is not None:
                        await self._click_step(bot, fresh, cfg.get("power_on_button", "включить"), cfg)
                except Exception:  # noqa: BLE001
                    pass

    async def fill_farm_now(self) -> str:
        """«Заполнить ферму»: сравнивает, сколько экземпляров account.farm_fill_model
        УЖЕ стоит на ферме, с целевым account.farm_target_phones. Если уже стоит
        достаточно (>=target) — ничего не докупает, только (при наличии пустых
        слотов без своей «памяти») зовёт farm_maintenance_now() поставить то, что
        уже есть в запасе. Если не хватает — считает РЕАЛЬНОЕ количество в запасе
        («Мои телефоны -> Рабочие телефоны», через _count_working_phone) и
        докупает в «Магазине телефонов» ТОЛЬКО точную разницу (а не весь нужный
        объём или не глядя вообще — раньше проверка была булевой «есть хотя бы
        один», из-за чего, имея ровно нужное число уже купленным, всё равно
        пыталась докупить ещё, репорт пользователя: «стоит 10, куплено 10, а он
        пытается докупить»), используя тот же механизм, что и
        ShopModule.buy_phones_now(), с временно подставленными моделью/
        количеством/редкостью. Слоты с уже своей «памятью» (farm_slot_models —
        свой телефон вернётся туда после ремонта) в докупку не считаются."""
        if not self.client or not self.running:
            self.last_farm_fill = "аккаунт не запущен"
            return self.last_farm_fill
        if self._trade_mode:
            self.last_farm_fill = "идёт трейд — попробуй чуть позже"
            return self.last_farm_fill
        fill_model = (self.account.get("farm_fill_model") or "").strip()
        if not fill_model:
            self.last_farm_fill = "⚠️ сначала настрой «Модель для пополнения пустых слотов»"
            return self.last_farm_fill

        cfg = self.farm_maintenance_cfg
        bot = cfg.get("bot") or CARDS_BOT
        mining_cmd = cfg.get("mining_command") or MINING_WORD
        root = await self._send_and_wait(bot, mining_cmd, timeout=20)
        if root is None:
            self.last_farm_fill = f"⚠️ нет ответа на «{mining_cmd}» ({clock()})"
            return self.last_farm_fill
        slots = parse_farm_slots(_msg_text(root))
        if not slots:
            self.last_farm_fill = f"⚠️ не разобрал слоты фермы ({clock()})"
            return self.last_farm_fill

        slot_models = self.account.get("farm_slot_models", {})
        empty_nums = sorted(n for n, i in slots.items() if i["status"] == "empty")
        fillable_nums = [n for n in empty_nums if not slot_models.get(str(n))]
        target = max(0, int(self.account.get("farm_target_phones", 11)))
        # регистронезависимо — как и все остальные сравнения farm_fill_model в этом файле
        # (см. _relieve_overload_now_impl); fill_model приходит свободным текстом от
        # пользователя и может не совпасть по регистру с тем, как игра рендерит модель в
        # слоте — иначе installed всегда 0, и докупка повторяется на каждый вызов
        installed = sum(1 for i in slots.values()
                        if i.get("model", "").strip().lower() == fill_model.lower())

        if installed >= target or not fillable_nums:
            self.last_farm_fill = (
                f"докупать не нужно — «{fill_model}» на ферме уже {installed}/{target} "
                f"({clock()})")
            return self.last_farm_fill

        need_to_install = min(target - installed, len(fillable_nums))
        # farm_fill_rarity не задана явно — используем phone_shop_rarity, см. комментарий
        # в farm_maintenance_now() выше
        fill_rarity = (
            self.account.get("farm_fill_rarity") or self.account.get("phone_shop_rarity") or ""
        ).strip()
        spare = await self._count_working_phone(fill_model, cfg, fill_rarity)
        shortfall = max(0, need_to_install - spare)

        bought_note = ""
        if shortfall > 0:
            rarity = (self.account.get("farm_fill_rarity") or "").strip()
            # _shop_config_lock: phone_shop_model/quantity/rarity подменяются временно
            # и восстанавливаются в finally, но buy_phones_now() делает много await
            # внутри — без общего лока с _auto_shop_loop (shop.py) ежедневный
            # автотриггер закупки мог сработать ровно в это окно и купить телефоны
            # ФЕРМЫ вместо настроенной автозакупки (реальные игровые деньги не туда)
            async with self._shop_config_lock:
                old_model = self.account.get("phone_shop_model")
                old_qty = self.account.get("phone_shop_quantity")
                old_rarity = self.account.get("phone_shop_rarity")
                self.account["phone_shop_model"] = fill_model
                self.account["phone_shop_quantity"] = shortfall
                if rarity:
                    self.account["phone_shop_rarity"] = rarity
                try:
                    # НЕ buy_phones_now() — тот сам берёт _shop_config_lock, а мы уже
                    # держим его строчкой выше (deadlock на нерентерабельном Lock)
                    shop_result = await self._buy_phones_now_impl()
                finally:
                    self.account["phone_shop_model"] = old_model
                    self.account["phone_shop_quantity"] = old_qty
                    if rarity:
                        self.account["phone_shop_rarity"] = old_rarity
            bought_note = f"докупка «{fill_model}» x{shortfall}: {shop_result}; "

        maint_result = await self.farm_maintenance_now()
        self.last_farm_fill = f"{bought_note}{maint_result}"
        return self.last_farm_fill

    async def _click_exact_step(self, bot: str, msg, exact_text: str, cfg: dict, timeout: int = 15):
        """Как _click_step, но клик СТРОГО по уже разрешённому exact_text (без
        повторного подстрочного поиска) — для «Слот N», где подстрока небезопасна."""
        retry_interval = max(3, int(cfg.get("retry_interval", 10)))
        attempts = max(1, int(cfg.get("retry_attempts", 3)))
        clicked = False
        for i in range(attempts):
            clicked, result = await self._click_and_wait(msg, exact_text, bot, timeout=timeout)
            if not clicked:
                return None
            if result is not None:
                return result
            if i < attempts - 1:
                await asyncio.sleep(retry_interval)
        return None

    async def _extract_broken_slot(self, bot: str, root, num: int, cfg: dict) -> tuple[bool, str | None]:
        """Слот N (сломан) -> карточка -> «Извлечь сломанный». Возвращает
        (успех, модель телефона, который был в слоте)."""
        prefix = cfg.get("slot_button_prefix", "Слот")
        slot_btn = _exact_button(root, f"{prefix} {num}")
        if not slot_btn:
            return False, None
        card = await self._click_exact_step(bot, root, slot_btn, cfg, timeout=15)
        if card is None:
            return False, None
        model_m = re.search(r"Телефон:\s*(.+)", _msg_text(card))
        model = model_m.group(1).strip() if model_m else None
        extract_btn = _find_button(card, cfg.get("extract_button", "извлечь сломанный"))
        if not extract_btn:
            return False, model
        after = await self._click_step(bot, card, extract_btn, cfg, timeout=15)
        return after is not None, model

    async def _remove_working_slot(self, bot: str, root, num: int, cfg: dict) -> bool:
        """Слот N (РАБОЧИЙ, не сломан) -> карточка -> «Убрать телефон» — снимает
        исправный телефон обратно в инвентарь (не путать с _extract_broken_slot).
        Используется только аварийным watchdog'ом питания (relieve_overload_now),
        чтобы снизить нагрузку на PSU/Cooling после случайного «События», а не
        рутинной автопочинкой."""
        prefix = cfg.get("slot_button_prefix", "Слот")
        slot_btn = _exact_button(root, f"{prefix} {num}")
        if not slot_btn:
            return False
        card = await self._click_exact_step(bot, root, slot_btn, cfg, timeout=15)
        if card is None:
            return False
        remove_btn = _find_button(card, cfg.get("remove_phone_button", "убрать телефон"))
        if not remove_btn:
            return False
        after = await self._click_step(bot, card, remove_btn, cfg, timeout=15)
        return after is not None

    async def _find_phone_button_across_pages(
        self, bot: str, msg, model_name: str, cfg: dict, max_pages: int = 6,
    ) -> tuple[str | None, object]:
        """В постраничном списке телефонов одной редкости ищет кнопку модели
        model_name (кнопка вида «Модель (xN)» — суффикс количества отбрасывается
        при сравнении). Возвращает (текст_кнопки, сообщение_с_этой_страницей)."""
        low_model = model_name.strip().lower()
        current = msg
        for _ in range(max_pages):
            for t in _all_buttons(current):
                name_part = re.sub(r"\s*\(x?\d+\)\s*$", "", t.strip(), flags=re.IGNORECASE).strip()
                if name_part.lower() == low_model:
                    return t, current
            next_btn = _find_button(current, cfg.get("next_page_button", "➡"))
            if not next_btn:
                break
            nxt = await self._click_step(bot, current, next_btn, cfg, timeout=15)
            if nxt is None:
                break
            current = nxt
        return None, current

    async def _reinstall_phone_slot(
        self, bot: str, root, num: int, model_name: str, cfg: dict, preferred_rarity: str | None = None,
    ) -> bool:
        """Пустой слот N -> «Добавить телефон» -> перебор редкостей -> найти
        рабочий экземпляр model_name (значит, ремонт уже завершился) -> установить.
        Если модель нигде не найдена (ремонт ещё не закончен) — просто пропускает
        слот, следующий проход цикла попробует снова.

        preferred_rarity (напр. account.farm_fill_rarity для fill_model) —
        проверяется первой, см. _rarity_scan_order."""
        prefix = cfg.get("slot_button_prefix", "Слот")
        slot_btn = _exact_button(root, f"{prefix} {num}")
        if not slot_btn:
            return False
        card = await self._click_exact_step(bot, root, slot_btn, cfg, timeout=15)
        if card is None:
            return False
        add_btn = _find_button(card, cfg.get("add_phone_button", "добавить телефон"))
        if not add_btn:
            return False
        rarity_msg = await self._click_step(bot, card, add_btn, cfg, timeout=15)
        if rarity_msg is None:
            return False

        for rarity_label in _rarity_scan_order(preferred_rarity):
            cat_btn = _find_rarity_button(rarity_msg, rarity_label)
            if not cat_btn:
                continue
            cat_msg = await self._click_step(bot, rarity_msg, cat_btn, cfg, timeout=15)
            if cat_msg is None:
                continue
            phone_btn, page_msg = await self._find_phone_button_across_pages(bot, cat_msg, model_name, cfg)
            if not phone_btn:
                continue
            installed = await self._click_step(bot, page_msg, phone_btn, cfg, timeout=15)
            if installed is None:
                return False
            # игра переспрашивает подтверждение кнопкой "Вставить в слот" (не
            # "Подтвердить") - без этого клика телефон так и остаётся не
            # установленным, а функция раньше молча считала это успехом
            confirm_btn = (
                _find_button(installed, "вставить в слот")
                or _find_button(installed, "подтвердить")
            )
            if confirm_btn:
                await self._click_step(bot, installed, confirm_btn, cfg, timeout=15)
            return True
        # рабочего экземпляра этой модели пока нет (ремонт не завершён) — либо
        # он ЕСТЬ, но не нашёлся (напр. дальше max_pages страниц одной редкости);
        # логируем, какие редкости вообще проверили, чтобы отличить одно от другого
        print(f"[{self.name}] farm: не нашёл рабочий «{model_name}» для слота №{num} "
              f"(preferred_rarity={preferred_rarity!r}, проверено: "
              f"{_rarity_scan_order(preferred_rarity)!r})")
        return False

    async def _execute_pay(self, target: str, amount: int, timeout: int = 15) -> str:
        """Отправляет «/pay target amount» и доводит перевод до конца.

        Проверено вживую на «слив твинкам»: игра НЕ всегда подтверждает перевод
        одним сообщением сразу («Вы успешно перевели...») — иногда первым
        сообщением приходит что-то промежуточное (напр. повторный «такк»/профиль),
        и только СЛЕДУЮЩИМ — настоящий диалог «Вы уверены, что хотите передать...»
        с кнопкой «Подтвердить». Старый код брал только первое сообщение после
        /pay и, не найдя на нём кнопки, сдавался — деньги списывались с исходного
        аккаунта (промежуточным сообщением), а сам перевод так и зависал
        неподтверждённым диалогом в чате, который никто не нажимал. Поэтому здесь
        ждём НЕСКОЛЬКО сообщений подряд (под одним и тем же локом бота, чтобы
        параллельный цикл не перехватил кнопку подтверждения), пока не найдём
        либо готовый успех, либо саму кнопку."""
        if not self.client or not self.running:
            return "аккаунт не запущен"
        async with self._lock_for_bot(CARDS_BOT):
            try:
                await self.client.send_message(CARDS_BOT, f"/pay {target} {amount}")
            except Exception as e:  # noqa: BLE001
                return f"ошибка отправки /pay: {e}"
            for _ in range(3):
                fut = self._register_wait(CARDS_BOT)
                try:
                    msg = await asyncio.wait_for(fut, timeout)
                except asyncio.TimeoutError:
                    self._forget_wait(CARDS_BOT, fut)
                    return "⚠️ нет ответа на /pay"
                text = (_msg_text(msg) or "").lower()
                if "успешно перевел" in text:
                    self._bump("paid", amount)
                    return f"✅ переведено {amount} -> {target} ({clock()} {today_msk()})"
                if _find_button(msg, PAY_CONFIRM_BUTTON):
                    if await self._try_click(msg, PAY_CONFIRM_BUTTON):
                        self._bump("paid", amount)
                        return f"✅ переведено {amount} -> {target} ({clock()} {today_msk()})"
                    return "⚠️ не удалось нажать «Подтвердить»"
                # что-то промежуточное (не успех и не диалог подтверждения) — ждём
                # ещё одно сообщение от бота, не сдаёмся после первого же
            return "⚠️ не дождался подтверждения /pay за несколько сообщений подряд"

    async def _payout(self) -> None:
        """«такк» -> «Точки: N» -> /pay <получатель> N*процент -> Подтвердить.
        Получатель — payout_target, доля — autopay_percent (свои же задают, по
        умолчанию 100%; остаток можно держать на балансе, например на ремонт)."""
        try:
            target = (self.account.get("payout_target") or "").strip()
            if not target:
                self.last_payout = "⚠️ получатель вывода не задан (🎯 Получатели)"
                return
            await asyncio.sleep(max(0, int(self.payout_delay)))
            if not self.running:
                return
            percent = int(self.account.get("autopay_percent", 100))
            await self._run_payout(target, percent)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.last_payout = f"ошибка вывода: {e}"

    async def payout_all_now(self) -> str:
        """«▶️ Действия -> Вывести всё» — ручной разовый вывод 100% баланса, без
        задержки и независимо от процента автовывода. Доступно в меню, когда сам
        автовывод выключен (иначе он и так регулярно выводит свою долю)."""
        target = (self.account.get("payout_target") or "").strip()
        if not target:
            return "⚠️ получатель вывода не задан (🎯 Получатели)"
        try:
            return await self._run_payout(target, 100)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.last_payout = f"ошибка вывода: {e}"
            return self.last_payout

    async def payout_to_now(self, target: str) -> str:
        """Как payout_all_now(), но с явным получателем вместо настроенного
        payout_target — для «▶️ Действия -> Финансы -> Собрать с твинков»: твинк
        платит СЮДА (главному аккаунту), независимо от своего обычного получателя."""
        try:
            return await self._run_payout(target, 100)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.last_payout = f"ошибка вывода: {e}"
            return self.last_payout

    async def _run_payout(self, target: str, percent: int) -> str:
        bal = await self._send_and_wait(CARDS_BOT, BALANCE_WORD)
        total = parse_points(getattr(bal, "text", None) or getattr(bal, "caption", None))
        if not total or total <= 0:
            self.last_payout = f"нет очков для вывода ({clock()})"
            return self.last_payout
        amount = total if percent >= 100 else int(total * percent / 100)
        if amount <= 0:
            self.last_payout = f"остаток слишком мал для вывода при {percent}% ({clock()})"
            return self.last_payout
        result = await self._execute_pay(target, amount)
        self.last_payout = (
            f"💸 выведено {amount} ({percent}%) -> {target} ({clock()} {today_msk()})"
            if result.startswith("✅") else f"{result} ({clock()})"
        )
        return self.last_payout

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
        return await self._execute_pay(target, amount)

    # ---------- инфо для меню ----------
    def card_remaining(self) -> str:
        return self._remaining(self.card_next_ts)

    def roulette_remaining(self) -> str:
        return self._remaining(self.roulette_next_ts)

    def mining_remaining(self) -> str:
        return self._remaining(self.mining_next_ts, "скоро")

    def container_remaining(self) -> str:
        return self._remaining(self.container_next_ts, "скоро")
