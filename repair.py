"""Модуль «Мастерская» — автопочинка своих нерабочих телефонов и автопринятие
чужих заказов на ремонт.

Независимый модуль: включается тумблерами auto_repair_enabled/auto_accept_enabled
по отдельности, не трогает фарм/автоотправку/магазин. Принципиально:

- Автопочинка чинит ТОЛЬКО своим оборудованием в своей мастерской («🏠 В своей
  мастерской (Бесплатно)») — никогда не арендует и не шлёт телефон в чужую
  мастерскую, даже если своего инструмента сейчас не хватает (тогда просто
  пропускает эту поломку до следующего прохода).
- Автопринятие — это про ВХОДЯЩИЕ заказы от других клиентов на ремонт ИХ
  телефонов в твоей мастерской (не путать с автопочинкой своих). Чтобы не
  конкурировать за одно и то же оборудование с автопочинкой по ночам, в тихие
  часы (repair.quiet_start..quiet_end, по умолчанию 22:00–07:00 по Тбилиси)
  автопринятие просто не подтверждает новые заказы — они истекут сами (клиенту
  через час придёт телефон обратно), а не ловятся принудительным отказом.
"""
from __future__ import annotations

import asyncio
import re
import time

from storage import CARDS_BOT
from common import clock, today_msk, in_time_window, TBILISI

_CAPACITY_RE = re.compile(r"занято ремонтом\D*?(\d+)\s*/\s*(\d+)", re.IGNORECASE)
_MODEL_RE = re.compile(r"модель\s*:?\s*(.+)", re.IGNORECASE)
_BREAKAGES_RE = re.compile(r"поломки\s*:?\s*(.+)", re.IGNORECASE)
_COUNT_RE = re.compile(r"\((\d+)\)\s*$")


def _all_buttons(message) -> list[str]:
    if message is None or not getattr(message, "reply_markup", None):
        return []
    return [getattr(b, "text", "") or "" for row in message.reply_markup.inline_keyboard for b in row]


def _find_button(message, substr: str) -> str | None:
    sub = substr.lower()
    for t in _all_buttons(message):
        if sub in t.lower():
            return t
    return None


def _first_nonempty_category(message) -> str | None:
    for t in _all_buttons(message):
        m = _COUNT_RE.search(t.strip())
        if m and int(m.group(1)) > 0:
            return t
    return None


def _msg_text(message) -> str:
    if message is None:
        return ""
    return getattr(message, "text", None) or getattr(message, "caption", None) or ""


class RepairModule:
    """Миксин с мастерской. Ожидает от связанного класса: self.account, self.client,
    self.running, self._trade_mode, self._send_and_wait, self._click_and_wait,
    self._try_click, self._bump, self.repair_cfg и last_repair/last_accept/
    repair_next_ts (инициализируются в базовом классе — automation.py)."""

    def _loops(self):
        return super()._loops() + [self._auto_repair_loop()]

    # ---------- проактивные сообщения (входящие заказы на ремонт) ----------
    def _is_proactive(self, message) -> bool:
        marker = (self.repair_cfg.get("request_marker") or "запрос на ремонт телефона").lower()
        return marker in _msg_text(message).lower()

    async def _handle_proactive(self, message) -> None:
        await self._maybe_accept_order(message)

    async def _maybe_accept_order(self, message) -> None:
        cfg = self.repair_cfg
        if not self.account.get("auto_accept_enabled", False):
            return
        quiet_start = cfg.get("quiet_start", "22:00")
        quiet_end = cfg.get("quiet_end", "07:00")
        if in_time_window(TBILISI, quiet_start, quiet_end):
            self.last_accept = f"⏸ тихие часы ({quiet_start}–{quiet_end} Тбилиси) — пропустил заказ ({clock()})"
            return
        cap = await self._workshop_capacity()
        if cap and cap[0] >= cap[1]:
            self.last_accept = f"⏸ мастерская занята ({cap[0]}/{cap[1]}) — пропустил заказ ({clock()})"
            return
        accept_btn = cfg.get("accept_button", "принять заказ")
        ok = await self._try_click(message, accept_btn)
        if ok:
            self._bump("accepted_orders")
            self.last_accept = f"✅ принял заказ ({clock()} {today_msk()}): {_msg_text(message)[:150]}"
        else:
            self.last_accept = f"⚠️ не удалось нажать «{accept_btn}» ({clock()})"

    # ---------- вместимость мастерской ----------
    async def _workshop_capacity(self) -> tuple[int, int] | None:
        cfg = self.repair_cfg
        bot = cfg.get("bot") or CARDS_BOT
        workshop_cmd = cfg.get("workshop_command") or "Моя мастерская"
        panel = await self._send_and_wait(bot, workshop_cmd, timeout=15)
        if panel is None:
            return None
        eq_btn = _find_button(panel, cfg.get("equipment_button", "оборудование"))
        if not eq_btn:
            return None
        clicked, eq = await self._click_and_wait(panel, eq_btn, bot, timeout=15)
        if not clicked or eq is None:
            return None
        m = _CAPACITY_RE.search(_msg_text(eq))
        return (int(m.group(1)), int(m.group(2))) if m else None

    # ---------- автопочинка своих нерабочих телефонов ----------
    def _repair_active(self) -> bool:
        return self.account.get("enabled", True) and self.account.get("auto_repair_enabled", False)

    async def _auto_repair_loop(self) -> None:
        while self.running:
            try:
                if self._trade_mode or not self._repair_active():
                    await asyncio.sleep(30)
                    continue
                now = time.time()
                if now < self.repair_next_ts:
                    await asyncio.sleep(min(60, self.repair_next_ts - now))
                    continue
                await self.repair_now()
                interval = max(60, int(self.repair_cfg.get("check_interval", 300)))
                self.repair_next_ts = time.time() + interval
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_repair = f"ошибка: {e}"
                self.repair_next_ts = time.time() + 300
                await asyncio.sleep(5)

    async def repair_now(self) -> str:
        """Найти первый нерабочий телефон с ещё не отданной в ремонт поломкой и
        отдать её в ремонт СВОИМ оборудованием (только «В своей мастерской
        (Бесплатно)» — чужие мастерские не трогаем). Используется циклом и
        кнопкой «🛠 Почистить нерабочие сейчас»."""
        if not self.client or not self.running:
            self.last_repair = "аккаунт не запущен"
            return self.last_repair
        if self._trade_mode:
            self.last_repair = "идёт трейд — попробуй чуть позже"
            return self.last_repair
        cfg = self.repair_cfg
        bot = cfg.get("bot") or CARDS_BOT
        try:
            cap = await self._workshop_capacity()
            if cap and cap[0] >= cap[1]:
                self.last_repair = f"⏸ мастерская занята ({cap[0]}/{cap[1]}), пропускаю ({clock()})"
                return self.last_repair

            phones_cmd = cfg.get("my_phones_command") or "Мои телефоны"
            entry = await self._send_and_wait(bot, phones_cmd, timeout=20)
            if entry is None:
                self.last_repair = f"⚠️ нет ответа на «{phones_cmd}» ({clock()})"
                return self.last_repair
            broken_btn = _find_button(entry, cfg.get("broken_button", "нерабочие телефоны"))
            if not broken_btn:
                self.last_repair = f"⚠️ кнопка нерабочих телефонов не найдена ({clock()})"
                return self.last_repair
            clicked, cats = await self._click_and_wait(entry, broken_btn, bot, timeout=15)
            if not clicked or cats is None:
                self.last_repair = f"⚠️ нет ответа на список категорий ({clock()})"
                return self.last_repair

            cat_btn = _first_nonempty_category(cats)
            if not cat_btn:
                self.last_repair = f"✅ нерабочих телефонов нет ({clock()} {today_msk()})"
                return self.last_repair
            clicked, phone_card = await self._click_and_wait(cats, cat_btn, bot, timeout=15)
            if not clicked or phone_card is None:
                self.last_repair = f"⚠️ нет ответа при открытии категории ({clock()})"
                return self.last_repair

            self.last_repair = await self._repair_this_phone(bot, phone_card, cfg)
            return self.last_repair
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.last_repair = f"ошибка: {e}"
            return self.last_repair

    async def _repair_this_phone(self, bot: str, phone_card, cfg: dict) -> str:
        text = _msg_text(phone_card)
        model_m = _MODEL_RE.search(text)
        model_name = model_m.group(1).strip() if model_m else "телефон"
        if not _BREAKAGES_RE.search(text):
            return f"⚠️ не нашёл список поломок у «{model_name}» ({clock()})"

        repair_btn = _find_button(phone_card, cfg.get("repair_button", "отдать в ремонт"))
        if not repair_btn:
            return f"⚠️ «{model_name}»: кнопка «в ремонт» не найдена ({clock()})"
        clicked, breakages = await self._click_and_wait(phone_card, repair_btn, bot, timeout=15)
        if not clicked or breakages is None:
            return f"⚠️ «{model_name}»: нет ответа на выбор поломки ({clock()})"

        skip = ("назад", "вернуться")
        breakage_btn = next(
            (t for t in _all_buttons(breakages) if not any(s in t.lower() for s in skip)), None)
        if not breakage_btn:
            return f"⚠️ «{model_name}»: не нашёл кнопку поломки ({clock()})"
        clicked, workshop_pick = await self._click_and_wait(breakages, breakage_btn, bot, timeout=15)
        if not clicked or workshop_pick is None:
            return f"⚠️ «{model_name}»/«{breakage_btn}»: нет ответа ({clock()})"

        own_btn = _find_button(workshop_pick, cfg.get("own_workshop_button", "в своей мастерской"))
        if not own_btn:
            return (f"⚠️ «{model_name}»/«{breakage_btn}»: своего оборудования нет — "
                    f"чужую мастерскую не арендуем ({clock()})")
        clicked, tools = await self._click_and_wait(workshop_pick, own_btn, bot, timeout=15)
        if not clicked or tools is None:
            return f"⚠️ «{model_name}»/«{breakage_btn}»: нет ответа своей мастерской ({clock()})"

        start_btn = _find_button(tools, cfg.get("start_repair_button", "начать ремонт"))
        if not start_btn:
            return f"⚠️ «{model_name}»/«{breakage_btn}»: нет свободного своего инструмента ({clock()})"
        clicked, _started = await self._click_and_wait(tools, start_btn, bot, timeout=15)
        if not clicked:
            return f"⚠️ «{model_name}»/«{breakage_btn}»: клик не прошёл ({clock()})"

        self._bump("repaired")
        return f"🛠 в ремонте: «{model_name}» / {breakage_btn} ({clock()} {today_msk()})"
