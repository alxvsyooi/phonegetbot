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
# "через 3 дн. 5 ч. 10 мин." (магазин контейнеров — с днями, без секунд)
_SHOP_TIME_RE = re.compile(
    r"через\s*(?:(\d+)\s*дн[а-я.]*)?\s*(?:(\d+)\s*ч[а-я.]*)?\s*(?:(\d+)\s*мин[а-я.]*)?",
    re.IGNORECASE,
)


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


class FarmModule:
    """Миксин с игровой автоматизацией. Ожидает от связанного класса: self.account,
    self.client, self.running, self._trade_mode, self._send_and_wait, self._wait_next,
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

                clicked = await self._try_click(reply, ROULETTE_BUTTON)
                if clicked:
                    self._bump("roulette")
                    result = await self._wait_next(ROULETTE_BOT, timeout=12)
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
        """Раз в кулдаун шлёт «Магазин контейнеров». Если ответ — стандартное
        «раскуплены, след. через X» — просто ждёт это время. Если ответ ДРУГОЙ
        (контейнеры есть в наличии / нужна капча) — оповещает владельца и ждёт
        unknown_retry перед следующей попыткой."""
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
                self.last_container = f"⏳ раскуплены, след. через {fmt_duration(delay)} ({clock()} {today_msk()})"
            elif reply is None:
                self.container_next_ts = time.time() + unknown_retry
                self.last_container = f"⚠️ нет ответа ({clock()})"
            else:
                # неожиданный ответ: возможно есть в наличии / нужна капча — зовём владельца
                self.container_next_ts = time.time() + unknown_retry
                self.last_container = f"🚨 неожиданный ответ, оповестил владельца ({clock()} {today_msk()})"
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

    # ---------- инфо для меню ----------
    def card_remaining(self) -> str:
        return self._remaining(self.card_next_ts)

    def roulette_remaining(self) -> str:
        return self._remaining(self.roulette_next_ts)

    def mining_remaining(self) -> str:
        return self._remaining(self.mining_next_ts, "скоро")

    def container_remaining(self) -> str:
        return self._remaining(self.container_next_ts, "скоро")
