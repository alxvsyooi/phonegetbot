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
from common import msg_text as _msg_text
from common import backoff_seconds as _backoff_seconds
from common import all_buttons as _all_buttons
from common import find_button as _find_button
from ui_engine import Design

_PRICE_RE = re.compile(r"цена покупки\s*:?\s*([\d\s.,]+)", re.IGNORECASE)
_MODEL_BUTTON_RE = re.compile(r"^(.*?)\s*\(([\d\s.,]+)\s*точек\)\s*$", re.IGNORECASE)
_BUY_CONFIRM_RE = re.compile(r"вы купили\s*(\d+)", re.IGNORECASE)


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
        собирается по своему отдельному расписанию времён — см. farm.py._mining_loop /
        account.mining_check_times). Опрос раз в 20с, время читается на лету."""
        last_fired = None
        while self.running:
            try:
                if self._trade_mode:
                    await asyncio.sleep(2)
                    continue
                hour, minute = parse_hhmm(self.account.get("mining_time"))
                self.shop_next_ts = time.time() + seconds_until_msk(hour, minute)
                await self._publish_status(shop_next_ts=self.shop_next_ts, last_shop=self.last_shop)

                now = datetime.now(MSK)
                due = now.hour == hour and now.minute == minute and last_fired != now.date()
                if due and self._shop_active():
                    last_fired = now.date()
                    result = await self.buy_phones_now()
                    # В отличие от farm.py, магазин телефонов раньше вообще никогда
                    # не звал alert_fn — капча/непонятный ответ тут просто оседали
                    # в last_shop незамеченными до следующего ручного захода в бота.
                    # Не различаем причину (капча/бот перегружен/etc.) — просто любой
                    # неуспех автопокупки (⚠️-статус) стоит один раз показать владельцу
                    if result.startswith("⚠️"):
                        await self._send_owner_alert(
                            Design.alert_frame("⚠️", "МАГАЗИН ТЕЛЕФОНОВ", self.name,
                                               f"<b>Автозакупка не прошла:</b> {result}"),
                            error_label="алерт о магазине телефонов",
                        )
                await asyncio.sleep(20)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                if await self._handle_dead_session(e):
                    return
                self.last_shop = f"ошибка: {e}"
                await self._publish_status(last_shop=self.last_shop)
                await asyncio.sleep(_backoff_seconds(e, default=20))

    # _click_retry — теперь общий метод в automation.py._WorkerBase (было
    # продублировано дословно здесь и в repair.py)

    async def buy_phones_now(self) -> str:
        """Обёртка под _shop_config_lock — farm.py.fill_farm_now() тоже держит этот
        лок, пока временно подменяет phone_shop_model/quantity/rarity для докупки
        модели фермы; без общей блокировки ежедневный авто-цикл (или ручная кнопка)
        мог сработать в это окно и купить телефоны фермы вместо настроенной
        автозакупки. fill_farm_now зовёт _buy_phones_now_impl() напрямую — он уже
        держит этот же лок сам, повторный async with здесь дал бы deadlock."""
        async with self._shop_config_lock:
            return await self._buy_phones_now_impl()

    async def _buy_phones_now_impl(self) -> str:
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
                # диагностика живого репорта: редкость визуально ЕСТЬ при ручном заходе,
                # но автозакупка её не находит — печатаем, что реально пришло в ответ на
                # open_cmd (подозрение: словили чужое/непрошенное сообщение бота вместо
                # экрана магазина — см. FIFO-корреляцию в automation.py._pending)
                print(f"[{self.name}] shop: редкость «{rarity}» не найдена; текст ответа: "
                      f"{_msg_text(entry)[:200]!r}; кнопки: {_all_buttons(entry)!r}")
                return self.last_shop
            clicked, listing = await self._click_retry(entry, rarity_btn, bot, cfg)
            if not clicked or listing is None:
                self.last_shop = f"⚠️ нет ответа при выборе редкости ({clock()})"
                return self.last_shop

            model_btn, listing = await self._find_model(bot, listing, cfg, model_pref)
            if not model_btn:
                self.last_shop = f"⚠️ не нашёл модель в «{rarity}» ({clock()})"
                print(f"[{self.name}] shop: модель не найдена (pref={model_pref!r}); "
                      f"кнопки последней страницы: {_all_buttons(listing)!r}")
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
            if self._trade_mode:
                # трейд стартовал посреди пролистывания страниц — не долбим дальше
                # в того же бота, см. automation.py._on_message
                return None, listing
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

        dtext = _msg_text(detail)
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
        price_unknown = False
        if unit_price and balance is not None:
            affordable = balance // unit_price
            if affordable <= 0:
                return f"⚠️ «{model_name}»: не хватает очков (цена {unit_price}, баланс {balance})"
            target = min(target, affordable) if target else affordable
        elif target is None:
            # Не разобрали цену (формат ответа игры мог чуть измениться) И
            # phone_shop_quantity=0 ("без ограничений") — раньше в этом случае
            # шли прямо на _max_numeric_button ниже и брали МАКСИМАЛЬНОЕ
            # предложенное количество без всякой проверки баланса. Раз цену не
            # знаем — безопаснее взять 1 шт., чем купить неизвестно сколько
            # неизвестно за сколько.
            target = 1
            price_unknown = True

        if price_unknown:
            # НЕ падаем на _max_numeric_button здесь — если точной кнопки "1" нет,
            # это значит "безопасное количество недоступно", а не "бери максимум"
            qty_btn = _numeric_button(qty_msg, target)
            if not qty_btn:
                return (f"⚠️ «{model_name}»: не разобрал цену покупки, кнопки «1 шт.» тоже нет — "
                        f"пропускаю на всякий случай, проверь вручную ({clock()})")
        else:
            qty_btn = _numeric_button(qty_msg, target) or _max_numeric_button(qty_msg)
        if not qty_btn:
            have = ", ".join(_all_buttons(qty_msg)) or "нет кнопок"
            return f"⚠️ «{model_name}»: не нашёл кнопку количества (есть: {have}) ({clock()})"
        # игра иногда шлёт промежуточное сообщение ДО настоящего диалога
        # подтверждения покупки (обнаружено вживую на /pay, см. farm.py._execute_pay) —
        # _click_and_wait_for_button не сдаётся, если кнопки нет в первом же ответе
        clicked_confirm, done = await self._click_and_wait_for_button(
            qty_msg, qty_btn, bot, confirm_btn, timeout=15)
        if done is None:
            return f"⚠️ «{model_name}»: нет ответа на выбор количества ({clock()})"
        if not clicked_confirm and _find_button(done, confirm_btn):
            return f"⚠️ «{model_name}»: не подтвердилась покупка ({clock()})"

        text = _msg_text(done)
        m = _BUY_CONFIRM_RE.search(text)
        bought = int(m.group(1)) if m else None
        if bought:
            self._bump("shop_bought", bought)
            return f"🏪 куплено {bought} шт. «{model_name}» ({clock()} {today_msk()})"
        return f"🏪 «{model_name}»: {text[:120] or 'ответ не распознан'} ({clock()})"

    # ---------- «Выполнить достижения»: разовая ручная команда (НЕ автоматизация) ----------
    async def complete_achievements_now(self) -> str:
        """Разовая ручная команда для достижений, которые дешевле/безопаснее
        сделать по кнопке, чем гонять фоновым циклом. Сейчас внутри только флип
        на Авито (Рыночек зарешал/Барыга) — сам флип бесплатен (объявление ничего
        не стоит выставить), но проверяем баланс ПЕРЕД запуском на будущее: если
        сюда добавятся действия, которые реально тратят ТОчки (напр. апгрейды
        уровня 6 — Лепрекон/Ждун/«Сливы!»/«Босс качалки», их точная стоимость
        достижения не проверена вживую), минимум achievements_min_balance (по
        умолчанию 150 000) — грубая подстраховка, чтобы не начинать, если на
        счету заведомо мало."""
        if not self.client or not self.running:
            return "аккаунт не запущен"
        if self._trade_mode:
            return "идёт трейд — попробуй чуть позже"
        min_balance = int(self.account.get("achievements_min_balance", 150000) or 0)
        balance = await self._check_balance()
        if balance is None:
            return f"⚠️ не удалось проверить баланс «Такк» ({clock()})"
        if balance < min_balance:
            return (f"⚠️ недостаточно ТОчек: {balance:,} из нужных минимум {min_balance:,} "
                    f"({clock()})").replace(",", " ")
        return await self._post_avito_flip()

    async def _post_avito_flip(self) -> str:
        """Выставляет первый попавшийся телефон настроенной редкости (по умолчанию
        Ширпотреб) на Авито по цене avito_flip_price (по умолчанию 5000 ТОчек —
        на порядок дороже официальной/закупочной цены Ширпотреба) — достаточно,
        чтобы засчитать «Рыночек зарешал»/«Барыга» после покупки. По просьбе не
        автоматизировано целиком: купить это же объявление с другого твинка/чужого
        игрока — отдельное ручное действие владельца в игре, эта команда только
        избавляет от утомительной части (навигация + подача объявления). Разовое
        достижение — повторный флип на том же аккаунте бесполезен, но команда не
        проверяет это сама (решать владельцу). Флоу проверен вживую: /avito ->
        «Подать объявление» -> «Телефоны» -> редкость -> первый телефон -> цена
        (текст) -> «Пропустить» описание -> «Подтвердить» (текст)."""
        cfg = self.avito_cfg
        bot = cfg.get("bot") or CARDS_BOT
        open_cmd = cfg.get("open_command") or "/avito"
        rarity = (cfg.get("rarity") or "Ширпотреб").strip()
        price = int(self.account.get("avito_flip_price", 5000) or 5000)
        skip_nav = ("назад", "вернуться")

        try:
            entry = await self._send_and_wait(bot, open_cmd, timeout=15)
            if entry is None:
                return f"⚠️ нет ответа на «{open_cmd}» ({clock()})"
            post_btn = _find_button(entry, cfg.get("post_button", "подать объявление"))
            if not post_btn:
                return f"⚠️ кнопка «подать объявление» не найдена ({clock()})"
            clicked, type_msg = await self._click_and_wait(entry, post_btn, bot, timeout=15)
            if not clicked or type_msg is None:
                return f"⚠️ нет ответа на «подать объявление» ({clock()})"

            phones_btn = _find_button(type_msg, cfg.get("type_phones_button", "телефоны"))
            if not phones_btn:
                return f"⚠️ кнопка «телефоны» не найдена ({clock()})"
            clicked, rarity_msg = await self._click_and_wait(type_msg, phones_btn, bot, timeout=15)
            if not clicked or rarity_msg is None:
                return f"⚠️ нет ответа на «телефоны» ({clock()})"

            rarity_btn = _find_button(rarity_msg, rarity)
            if not rarity_btn:
                return f"⚠️ редкость «{rarity}» не найдена на Авито ({clock()})"
            clicked, listing = await self._click_and_wait(rarity_msg, rarity_btn, bot, timeout=15)
            if not clicked or listing is None:
                return f"⚠️ нет ответа при выборе редкости ({clock()})"

            model_btn = next(
                (t for t in _all_buttons(listing) if t.strip() and not any(s in t.lower() for s in skip_nav)),
                None,
            )
            if not model_btn:
                return f"⚠️ нет доступных телефонов редкости «{rarity}» для продажи ({clock()})"
            model_name = model_btn.rsplit("(", 1)[0].strip()
            clicked, price_ask = await self._click_and_wait(listing, model_btn, bot, timeout=15)
            if not clicked or price_ask is None:
                return f"⚠️ «{model_name}»: нет ответа на выбор телефона ({clock()})"

            # цена и подтверждение — обычные текстовые ответы в чат (не кнопки),
            # это ожидаемый шаг флоу самой игры (проверено вживую)
            desc_ask = await self._send_and_wait(bot, str(price), timeout=15)
            if desc_ask is None:
                return f"⚠️ «{model_name}»: нет ответа на цену ({clock()})"
            skip_btn = _find_button(desc_ask, cfg.get("description_skip_button", "пропустить"))
            if not skip_btn:
                return f"⚠️ «{model_name}»: кнопка «пропустить» описание не найдена ({clock()})"
            clicked, confirm_ask = await self._click_and_wait(desc_ask, skip_btn, bot, timeout=15)
            if not clicked or confirm_ask is None:
                return f"⚠️ «{model_name}»: нет ответа после пропуска описания ({clock()})"

            done = await self._send_and_wait(bot, cfg.get("confirm_text", "Подтвердить"), timeout=15)
            if done is None:
                return f"⚠️ «{model_name}»: нет ответа на подтверждение ({clock()})"

            self._bump("avito_listed")
            price_fmt = f"{price:,}".replace(",", " ")
            return (f"📢 «{model_name}» выставлен на Авито за {price_fmt} ТОчек — купи вручную с "
                    f"другого аккаунта, чтобы засчитать достижение ({clock()} {today_msk()})")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            return f"ошибка: {e}"
