"""Модуль «Магазин телефонов» — автозакупка (ручная кнопка + автоматизация).

Независимый модуль: включается/выключается тумблером phone_shop_enabled, не
трогает фарм/автоотправку/ремонт. Заходит в магазин командой phone_shop.open_command
(по умолчанию «Магазин телефонов»), выбирает редкость (phone_shop_rarity, по
умолчанию «Ширпотреб») и модель — без настройки phone_shop_model берёт первую
в списке (как показано на скринах), с настройкой ищет её по страницам. Покупает
через «Купить оптом»: по умолчанию максимум предложенного количества, либо
конкретное число (phone_shop_quantity), но не больше, чем позволяет баланс.

Автоматизация срабатывает РАЗ В СУТКИ, вместе с майнингом (то же время
mining_time по МСК) — телефоны в магазине не появляются заново каждые 10 минут,
так что более частый опрос только зря дёргает игрового бота.

Баланс («такк») проверяется ОДИН РАЗ в самом начале, ДО входа в магазин — если
слать текстовую команду ПОСРЕДИ навигации по инлайн-кнопкам магазина, игровой бот,
похоже, сбрасывает своё серверное состояние диалога, и кнопки на уже показанном
сообщении после этого перестают отвечать (флоу «зависает» точно на этом шаге).
Поэтому весь поход по магазину — только клики по кнопкам, без единого текстового
сообщения внутри, кроме самой команды открытия.
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime

from storage import CARDS_BOT
from common import clock, today_msk, MSK, parse_hhmm, seconds_until_msk

_PRICE_RE = re.compile(r"цена покупки\s*:?\s*([\d\s.,]+)", re.IGNORECASE)
_MODEL_BUTTON_RE = re.compile(r"^(.*?)\s*\(([\d\s.,]+)\s*точек\)\s*$", re.IGNORECASE)
_BUY_CONFIRM_RE = re.compile(r"вы купили\s*(\d+)", re.IGNORECASE)


def _all_buttons(message) -> list[str]:
    if message is None or not getattr(message, "reply_markup", None):
        return []
    return [getattr(b, "text", "") or "" for row in message.reply_markup.inline_keyboard for b in row]


def _find_button(message, substr: str) -> str | None:
    sub = substr.lower().strip()
    for t in _all_buttons(message):
        if sub in t.lower():
            return t
    return None


def _max_numeric_button(message) -> str | None:
    best_t, best_n = None, -1
    for t in _all_buttons(message):
        stripped = t.strip()
        digits = re.sub(r"\D", "", stripped)
        if not digits or len(stripped) - len(digits) > 2:
            continue
        n = int(digits)
        if n > best_n:
            best_n, best_t = n, t
    return best_t


def _numeric_button(message, value: int | None) -> str | None:
    if not value:
        return None
    for t in _all_buttons(message):
        stripped = t.strip()
        if stripped.isdigit() and int(stripped) == value:
            return t
    return None


class ShopModule:
    """Миксин с автозакупкой телефонов. Ожидает от связанного класса: self.account,
    self.client, self.running, self._trade_mode, self._send_and_wait, self._click_and_wait,
    self._check_balance (даёт FarmModule), self._bump, self.shop_cfg и
    last_shop/shop_next_ts (инициализируются в базовом классе — automation.py)."""

    def _loops(self):
        return super()._loops() + [self._auto_shop_loop()]

    def _shop_active(self) -> bool:
        return self.account.get("enabled", True) and self.account.get("phone_shop_enabled", False)

    async def _auto_shop_loop(self) -> None:
        """Раз в сутки, в mining_time по МСК (в отличие от самого майнинга, который
        с 2026-07-29 собирается каждые MINING_INTERVAL_HOURS часов — см.
        farm.py._mining_loop). Опрос раз в 20с, время читается на лету."""
        last_fired = None
        while self.running:
            try:
                if self._trade_mode:
                    await asyncio.sleep(2)
                    continue
                hour, minute = parse_hhmm(self.account.get("mining_time"))
                self.shop_next_ts = time.time() + seconds_until_msk(hour, minute)

                now = datetime.now(MSK)
                due = now.hour == hour and now.minute == minute and last_fired != now.date()
                if due and self._shop_active():
                    last_fired = now.date()
                    await self.buy_phones_now()
                await asyncio.sleep(20)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_shop = f"ошибка: {e}"
                await asyncio.sleep(20)

    # ---------- клик с повтором, если бот просто не спешит с ответом ----------
    async def _click_retry(self, message, button_text: str, bot: str, cfg: dict, timeout: int = 15):
        """Как _click_and_wait, но повторяет клик (не долбит наугад — тот же клик по
        той же кнопке), если бот принял клик, но не ответил за timeout. Возвращает
        (clicked, result); result is None, если так и не дождались ответа."""
        retry_delay = max(2, int(cfg.get("retry_interval", 5)))
        attempts = max(1, int(cfg.get("retry_attempts", 3)))
        clicked = False
        for i in range(attempts):
            clicked, result = await self._click_and_wait(message, button_text, bot, timeout=timeout)
            if not clicked:
                return False, None
            if result is not None:
                return True, result
            if i < attempts - 1:
                await asyncio.sleep(retry_delay)
        return clicked, None

    async def buy_phones_now(self) -> str:
        """Открыть магазин телефонов, выбрать настроенную редкость/модель и купить.
        Используется циклом и кнопкой «🏪 Купить телефоны сейчас»."""
        if not self.client or not self.running:
            self.last_shop = "аккаунт не запущен"
            return self.last_shop
        if self._trade_mode:
            self.last_shop = "идёт трейд — попробуй чуть позже"
            return self.last_shop
        cfg = self.shop_cfg
        bot = cfg.get("bot") or CARDS_BOT
        open_cmd = cfg.get("open_command") or "Магазин телефонов"
        rarity = (self.account.get("phone_shop_rarity") or "Ширпотреб").strip()
        model_pref = (self.account.get("phone_shop_model") or "").strip().lower()
        want_qty = int(self.account.get("phone_shop_quantity", 0) or 0)

        try:
            # баланс — ДО входа в магазин, одним текстовым сообщением, никак не
            # перемешиваясь с последующей навигацией по инлайн-кнопкам (см. докстринг)
            balance = await self._check_balance()

            entry = await self._send_and_wait(bot, open_cmd, timeout=20)
            if entry is None:
                self.last_shop = f"⚠️ нет ответа на «{open_cmd}» ({clock()})"
                return self.last_shop

            rarity_btn = _find_button(entry, rarity)
            if not rarity_btn:
                self.last_shop = f"⚠️ редкость «{rarity}» не найдена в магазине ({clock()})"
                return self.last_shop
            clicked, listing = await self._click_retry(entry, rarity_btn, bot, cfg)
            if not clicked or listing is None:
                self.last_shop = f"⚠️ нет ответа при выборе редкости ({clock()})"
                return self.last_shop

            model_btn, listing = await self._find_model(bot, listing, cfg, model_pref)
            if not model_btn:
                self.last_shop = f"⚠️ не нашёл модель в «{rarity}» ({clock()})"
                return self.last_shop
            clicked, detail = await self._click_retry(listing, model_btn, bot, cfg)
            if not clicked or detail is None:
                self.last_shop = f"⚠️ нет ответа на выбор модели ({clock()})"
                return self.last_shop

            model_name = model_btn.rsplit("(", 1)[0].strip()
            self.last_shop = await self._buy_selected(bot, detail, cfg, model_name, want_qty, balance)
            return self.last_shop
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.last_shop = f"ошибка: {e}"
            return self.last_shop

    async def _find_model(self, bot: str, listing, cfg: dict, model_pref: str, max_pages: int = 12):
        """Без настройки phone_shop_model — первая модель в списке (как «по умолчанию»
        на скринах). С настройкой — пролистывает страницы (кнопка next_page_button) в
        её поисках, не дальше max_pages, чтобы не зациклиться, если модели нет вовсе."""
        next_btn_label = cfg.get("next_page_button", "➡")
        for _ in range(max_pages):
            candidates = [t for t in _all_buttons(listing) if _MODEL_BUTTON_RE.match(t.strip())]
            if not model_pref:
                if candidates:
                    return candidates[0], listing
                break
            for t in candidates:
                if model_pref in t.lower():
                    return t, listing
            nxt = _find_button(listing, next_btn_label)
            if not nxt:
                break
            clicked, nxt_listing = await self._click_retry(listing, nxt, bot, cfg)
            if not clicked or nxt_listing is None:
                break
            listing = nxt_listing
        return None, listing

    async def _buy_selected(
        self, bot: str, detail, cfg: dict, model_name: str, want_qty: int, balance: int | None,
    ) -> str:
        buy_btn = cfg.get("bulk_button", "купить оптом")
        confirm_btn = cfg.get("confirm_button", "подтвердить")

        dtext = getattr(detail, "text", None) or getattr(detail, "caption", None) or ""
        price_m = _PRICE_RE.search(dtext)
        digits = re.sub(r"\D", "", price_m.group(1)) if price_m else ""
        unit_price = int(digits) if digits else None

        btn = _find_button(detail, buy_btn)
        if not btn:
            have = ", ".join(_all_buttons(detail)) or "нет кнопок"
            return f"⚠️ «{model_name}»: кнопка «{buy_btn}» не найдена (есть: {have}) ({clock()})"
        clicked, qty_msg = await self._click_retry(detail, btn, bot, cfg)
        if not clicked or qty_msg is None:
            return f"⚠️ «{model_name}»: нет ответа на «{buy_btn}» ({clock()})"

        target = want_qty if want_qty > 0 else None
        if unit_price and balance is not None:
            affordable = balance // unit_price
            if affordable <= 0:
                return f"⚠️ «{model_name}»: не хватает очков (цена {unit_price}, баланс {balance})"
            target = min(target, affordable) if target else affordable

        qty_btn = _numeric_button(qty_msg, target) or _max_numeric_button(qty_msg)
        if not qty_btn:
            have = ", ".join(_all_buttons(qty_msg)) or "нет кнопок"
            return f"⚠️ «{model_name}»: не нашёл кнопку количества (есть: {have}) ({clock()})"
        clicked, confirm_msg = await self._click_retry(qty_msg, qty_btn, bot, cfg)
        if not clicked or confirm_msg is None:
            return f"⚠️ «{model_name}»: нет ответа на выбор количества ({clock()})"

        done = confirm_msg
        if _find_button(confirm_msg, confirm_btn):
            clicked, done = await self._click_retry(confirm_msg, confirm_btn, bot, cfg)
            if not clicked:
                return f"⚠️ «{model_name}»: не подтвердилась покупка ({clock()})"
            if done is None:
                done = confirm_msg

        text = (getattr(done, "text", None) or getattr(done, "caption", None) or "") if done else ""
        m = _BUY_CONFIRM_RE.search(text)
        bought = int(m.group(1)) if m else None
        if bought:
            self._bump("shop_bought", bought)
            return f"🏪 куплено {bought} шт. «{model_name}» ({clock()} {today_msk()})"
        return f"🏪 «{model_name}»: {text[:120] or 'ответ не распознан'} ({clock()})"
