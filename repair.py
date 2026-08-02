"""Модуль «Мастерская» — автопочинка своих нерабочих телефонов и автопринятие
чужих заказов на ремонт.

Независимый модуль: включается тумблерами auto_repair_enabled/auto_accept_enabled
по отдельности, не трогает фарм/автоотправку/магазин. Принципиально:

- Автопочинка сначала всегда пробует своё оборудование в своей мастерской
  («🏠 В своей мастерской (Бесплатно)»). Если своего свободного инструмента нет,
  по умолчанию просто пропускает эту поломку до следующего прохода — чужую
  мастерскую не арендует. Тумблером repair_external_workshop_enabled (аккаунт)
  можно разрешить fallback в чужую мастерскую, когда своей не хватает; имя
  конкретной мастерской задаётся repair_external_workshop_name (пусто — берём
  первую в списке). После каждого успешно запущенного ремонта (своя или чужая
  мастерская) чиним всё оборудование своей мастерской — инструмент теряет
  прочность (-3) за использование. Конкретную чужую мастерскую можно задать
  либо владельцем (repair_external_workshop_owner, @username — надёжнее, ищем
  по общему списку), либо именем (repair_external_workshop_name — родной поиск
  игры «найти по названию», не всегда находит декорированные эмодзи имена);
  пусто оба — берём первую свободную из списка.
- Автопринятие — это про ВХОДЯЩИЕ заказы от других клиентов на ремонт ИХ
  телефонов в твоей мастерской (не путать с автопочинкой своих). Чтобы не
  конкурировать за одно и то же оборудование с автопочинкой по ночам, в тихие
  часы (repair.quiet_start..quiet_end, по умолчанию 22:00–07:00 по Тбилиси)
  автопринятие просто не подтверждает новые заказы — они истекут сами (клиенту
  через час придёт телефон обратно), а не ловятся принудительным отказом.
- Авто-отзыв (auto_review_enabled) — после завершения ремонта в ЧУЖОЙ
  мастерской игра проактивно (не в ответ на наш send/click) присылает просьбу
  оценить работу (1..5⭐ + текстовый комментарий). Ставим фиксированную оценку
  (review_stars, по умолчанию 5) без комментария (review_comment, по умолчанию
  «нет») — это прогресс к достижению «Критик» (считает КОЛИЧЕСТВО отзывов, не
  разовый факт).
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
_MODEL_COUNT_RE = re.compile(r"\(\s*x?\s*(\d+)\s*\)\s*$", re.IGNORECASE)
# «1. ⭐ Мастерская «Х» / Владелец: @username / ...» в общем списке чужих мастерских —
# нежадный захват ДО следующего пункта списка, чтобы не съесть сразу несколько карточек
_WORKSHOP_OWNER_RE = re.compile(r"(\d+)\.[\s\S]*?владелец\s*:?\s*@(\S+)", re.IGNORECASE)


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


def _nonempty_categories(message) -> list[str]:
    """Как _first_nonempty_category, но ВСЕ непустые категории по порядку —
    нужно для приоритетного поиска модели по всем категориям, не только первой."""
    out = []
    for t in _all_buttons(message):
        m = _COUNT_RE.search(t.strip())
        if m and int(m.group(1)) > 0:
            out.append(t)
    return out


def _priority_model_button(message, priority: set[str]) -> str | None:
    """Кнопка модели («Model (xN)»), чьё имя (без суффикса количества) совпадает
    с одной из priority — моделей, которые сейчас нужны ферме (см.
    _repair_priority_models)."""
    skip = ("назад", "вернуться")
    for t in _all_buttons(message):
        stripped = t.strip()
        low = stripped.lower()
        if any(s in low for s in skip):
            continue
        m = _MODEL_COUNT_RE.search(stripped)
        if not m or int(m.group(1)) <= 0:
            continue
        name = _MODEL_COUNT_RE.sub("", stripped).strip().lower()
        if name in priority:
            return t
    return None


def _first_model_button(message) -> str | None:
    """Внутри категории («Ширпотреб» и т.п.) телефоны сгруппированы по модели —
    кнопки вида «Модель (xN)» (счётчик с «x», в отличие от категорий верхнего
    уровня «Категория (N)» без «x» — другой формат, отдельный regex). Берём
    первую модель с ненулевым количеством; сама карточка ремонта откроется
    только после клика по ней (категория сама по себе — ещё не карточка)."""
    skip = ("назад", "вернуться")
    for t in _all_buttons(message):
        low = t.strip().lower()
        if any(s in low for s in skip):
            continue
        m = _MODEL_COUNT_RE.search(t.strip())
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

    # ---------- клик с повтором, если бот просто не спешит с ответом ----------
    async def _click_retry(self, message, button_text: str, bot: str, cfg: dict, timeout: int = 15):
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

    # ---------- проактивные сообщения (входящие заказы на ремонт, оценка мастерской) ----------
    def _is_proactive(self, message) -> bool:
        text = _msg_text(message).lower()
        marker = (self.repair_cfg.get("request_marker") or "запрос на ремонт телефона").lower()
        review_marker = (self.repair_cfg.get("review_marker") or "оцените работу мастерской").lower()
        return marker in text or review_marker in text

    async def _handle_proactive(self, message) -> None:
        review_marker = (self.repair_cfg.get("review_marker") or "оцените работу мастерской").lower()
        if review_marker in _msg_text(message).lower():
            await self._maybe_leave_review(message)
            return
        await self._maybe_accept_order(message)

    async def _maybe_leave_review(self, message) -> None:
        """Достижение «Критик» считает КОЛИЧЕСТВО отзывов в чужих мастерских (Критик
        I/II/III — 10/20/50 отзывов), а не разовый факт — проверено вживую. Ставим
        фиксированную оценку из настроек (по умолчанию 5⭐ без комментария): цель
        тут прогресс достижения, а не честная оценка чужой мастерской."""
        if not self.account.get("auto_review_enabled", False):
            return
        cfg = self.repair_cfg
        bot = cfg.get("bot") or CARDS_BOT
        stars = str(cfg.get("review_stars", "5"))
        comment = cfg.get("review_comment", "нет")
        star_btn = _find_button(message, stars)
        if not star_btn:
            self.last_repair = f"⚠️ отзыв: не нашёл кнопку «{stars}⭐» ({clock()})"
            return
        clicked, ask = await self._click_and_wait(message, star_btn, bot, timeout=15)
        if not clicked:
            self.last_repair = f"⚠️ отзыв: не удалось нажать «{stars}⭐» ({clock()})"
            return
        if ask is not None:
            await self._send_and_wait(bot, comment, timeout=15)
        self._bump("reviews_left")
        self.last_repair = f"⭐ оставил отзыв ({stars}) в чужой мастерской ({clock()} {today_msk()})"

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
        clicked, eq = await self._click_retry(panel, eq_btn, bot, cfg)
        if not clicked or eq is None:
            return None
        m = _CAPACITY_RE.search(_msg_text(eq))
        return (int(m.group(1)), int(m.group(2))) if m else None

    # ---------- приоритет: модели, которые сейчас нужны ферме (см. farm.py) ----------
    def _repair_priority_models(self) -> set[str]:
        """Модели телефонов, которые сейчас стоят (или должны стоять) в слотах
        модульной фермы — farm.py/farm_maintenance_now ведёт этот список в
        account.farm_slot_models при каждом проходе, включая сломанные слоты,
        ждущие ремонта. Их чиним в первую очередь: пока такой телефон не
        починен, соответствующий слот фермы простаивает пустым."""
        slot_models = self.account.get("farm_slot_models") or {}
        return {str(v).strip().lower() for v in slot_models.values() if v}

    async def _search_workshop_by_name(self, workshop_pick, ext_name: str, bot: str, cfg: dict):
        """Использует настоящий поиск игры («🔍 Найти по названию» -> ответить
        текстом запроса -> «Результаты поиска по запросу...» с кнопками
        «Мастерская №N») — раньше имя чужой мастерской искалось ПОДСТРОКОЙ только
        среди кнопок ТЕКУЩЕЙ страницы (из 500+ существующих), поэтому фактически
        никогда не находилось, если мастерская не попадала на первую страницу
        случайно (баг «не работает поиск по мастерским»). Возвращает сообщение с
        инструментами (после клика в первый результат поиска) либо None, если
        кнопки поиска нет или по запросу ничего не нашлось."""
        search_btn = _find_button(workshop_pick, cfg.get("workshop_search_button", "найти по названию"))
        if not search_btn:
            return None
        clicked, ask = await self._click_retry(workshop_pick, search_btn, bot, cfg)
        if not clicked or ask is None:
            return None
        results = await self._send_and_wait(bot, ext_name, timeout=15)
        if results is None:
            return None
        result_btn = _find_button(results, cfg.get("workshop_result_button", "мастерская №1"))
        if not result_btn:
            return None
        clicked, tools = await self._click_retry(results, result_btn, bot, cfg)
        return tools if clicked else None

    async def _find_workshop_by_owner(self, workshop_pick, owner: str, bot: str, cfg: dict):
        """Листает ОБЩИЙ (несортированный поиском) список чужих мастерских для этой
        поломки в поисках карточки с «Владелец: @owner» — надёжнее встроенного поиска
        игры по имени, который не находит мастерские с декорированным эмодзи именем
        (см. docstring workshop_owner_max_pages в storage.py). Возвращает сообщение с
        инструментами этой мастерской (после клика по её «Мастерская №N»), либо None,
        если не нашли за отведённое число страниц."""
        owner_l = owner.strip().lstrip("@").lower()
        next_btn_label = cfg.get("next_page_button", "➡")
        max_pages = max(1, int(cfg.get("workshop_owner_max_pages", 30)))
        msg = workshop_pick
        for _ in range(max_pages):
            for idx, own in _WORKSHOP_OWNER_RE.findall(_msg_text(msg)):
                if own.lower() == owner_l:
                    btn = _find_button(msg, f"мастерская №{idx}")
                    if not btn:
                        continue
                    clicked, tools = await self._click_retry(msg, btn, bot, cfg)
                    return tools if clicked else None
            nxt = _find_button(msg, next_btn_label)
            if not nxt:
                break
            clicked, nxt_msg = await self._click_retry(msg, nxt, bot, cfg)
            if not clicked or nxt_msg is None:
                break
            msg = nxt_msg
        return None

    async def _open_broken_categories(self, bot: str, cfg: dict):
        phones_cmd = cfg.get("my_phones_command") or "Мои телефоны"
        entry = await self._send_and_wait(bot, phones_cmd, timeout=20)
        if entry is None:
            return None
        broken_btn = _find_button(entry, cfg.get("broken_button", "нерабочие телефоны"))
        if not broken_btn:
            return None
        clicked, cats = await self._click_retry(entry, broken_btn, bot, cfg)
        return cats if clicked else None

    async def _find_priority_phone_card(self, bot: str, cats_msg, priority: set[str], cfg: dict):
        """Проходит по ВСЕМ непустым категориям нерабочих телефонов (не только
        первой) в поисках модели из priority и открывает её карточку. None, если
        ни в одной категории приоритетной модели не нашлось — тогда repair_now()
        падает обратно на обычный порядок «первая категория -> первая модель»."""
        for cat_label in _nonempty_categories(cats_msg):
            clicked, models_msg = await self._click_retry(cats_msg, cat_label, bot, cfg)
            if not clicked or models_msg is None:
                continue
            model_btn = _priority_model_button(models_msg, priority)
            if model_btn:
                clicked, phone_card = await self._click_retry(models_msg, model_btn, bot, cfg)
                if clicked and phone_card is not None:
                    return phone_card
            back_btn = _find_button(models_msg, "назад") or _find_button(models_msg, "вернуться")
            if back_btn:
                clicked, back_msg = await self._click_retry(models_msg, back_btn, bot, cfg)
                if clicked and back_msg is not None:
                    cats_msg = back_msg
        return None

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
        """Найти нерабочий телефон и отдать его поломку в ремонт СВОИМ
        оборудованием (только «В своей мастерской (Бесплатно)» — чужие
        мастерские не трогаем). В первую очередь ищет модели, которые сейчас
        нужны ферме (см. _repair_priority_models) — по ВСЕМ категориям, не
        только первой попавшейся — а не найдя ни одной, падает на обычный
        порядок «первая непустая категория -> первая модель». Используется
        циклом и кнопкой «🛠 Почистить нерабочие сейчас»."""
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

            cats = await self._open_broken_categories(bot, cfg)
            if cats is None:
                self.last_repair = f"⚠️ нет ответа на список категорий нерабочих ({clock()})"
                return self.last_repair

            phone_card = None
            priority_label = None
            priority = self._repair_priority_models()
            if priority:
                phone_card = await self._find_priority_phone_card(bot, cats, priority, cfg)
                if phone_card is not None:
                    priority_label = "🌾 нужен ферме"
                else:
                    # категории могли сдвинуться, пока искали приоритетную модель —
                    # берём список заново перед обычным падением на «первую попавшуюся»
                    cats = await self._open_broken_categories(bot, cfg)
                    if cats is None:
                        self.last_repair = f"⚠️ нет ответа при повторном открытии категорий ({clock()})"
                        return self.last_repair

            if phone_card is None:
                cat_btn = _first_nonempty_category(cats)
                if not cat_btn:
                    self.last_repair = f"✅ нерабочих телефонов нет ({clock()} {today_msk()})"
                    return self.last_repair
                clicked, phone_card = await self._click_retry(cats, cat_btn, bot, cfg)
                if not clicked or phone_card is None:
                    self.last_repair = f"⚠️ нет ответа при открытии категории ({clock()})"
                    return self.last_repair

                # категория открывает список МОДЕЛЕЙ («Модель (xN)»), а не сразу карточку
                # ремонта — спускаемся на уровень ниже, если поломок в ответе ещё нет
                if not _BREAKAGES_RE.search(_msg_text(phone_card)):
                    model_btn = _first_model_button(phone_card)
                    if not model_btn:
                        self.last_repair = f"⚠️ не нашёл модель в категории «{cat_btn}» ({clock()})"
                        return self.last_repair
                    clicked, phone_card = await self._click_retry(phone_card, model_btn, bot, cfg)
                    if not clicked or phone_card is None:
                        self.last_repair = f"⚠️ нет ответа при открытии модели «{model_btn}» ({clock()})"
                        return self.last_repair

            result = await self._repair_this_phone(bot, phone_card, cfg)
            self.last_repair = f"{result} {priority_label}" if priority_label else result
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
        clicked, breakages = await self._click_retry(phone_card, repair_btn, bot, cfg)
        if not clicked or breakages is None:
            return f"⚠️ «{model_name}»: нет ответа на выбор поломки ({clock()})"

        skip = ("назад", "вернуться")
        breakage_btn = next(
            (t for t in _all_buttons(breakages) if not any(s in t.lower() for s in skip)), None)
        if not breakage_btn:
            return f"⚠️ «{model_name}»: не нашёл кнопку поломки ({clock()})"
        clicked, workshop_pick = await self._click_retry(breakages, breakage_btn, bot, cfg)
        if not clicked or workshop_pick is None:
            return f"⚠️ «{model_name}»/«{breakage_btn}»: нет ответа ({clock()})"

        # своя мастерская — бесплатно, всегда пробуем её первой
        own_btn = _find_button(workshop_pick, cfg.get("own_workshop_button", "в своей мастерской"))
        if own_btn:
            clicked, tools = await self._click_retry(workshop_pick, own_btn, bot, cfg)
            if clicked and tools is not None:
                start_btn = _find_button(tools, cfg.get("start_repair_button", "начать ремонт"))
                if start_btn:
                    clicked, _started = await self._click_retry(tools, start_btn, bot, cfg)
                    if clicked:
                        self._bump("repaired")
                        await self._repair_all_equipment()
                        return f"🛠 в ремонте: «{model_name}» / {breakage_btn} ({clock()} {today_msk()})"

        # своего свободного инструмента нет — по умолчанию чужую мастерскую не
        # арендуем (см. docstring модуля); включается тумблером
        # repair_external_workshop_enabled, опционально с конкретным именем
        # мастерской в repair_external_workshop_name (иначе берём первую в списке)
        if self.account.get("repair_external_workshop_enabled", False):
            ext_owner = (self.account.get("repair_external_workshop_owner") or "").strip()
            ext_name = (self.account.get("repair_external_workshop_name") or "").strip()
            tools = None
            ext_label = ext_owner or ext_name
            # владелец — приоритетнее и надёжнее (см. _find_workshop_by_owner);
            # поиск по имени — запасной вариант, если владелец не задан/не найден
            if ext_owner:
                tools = await self._find_workshop_by_owner(workshop_pick, ext_owner, bot, cfg)
            if tools is None and ext_name:
                tools = await self._search_workshop_by_name(workshop_pick, ext_name, bot, cfg)
            if tools is None:
                nav_skip = ("назад", "вернуться", "найти по названию", "в своей мастерской")
                ext_btn = next(
                    (t for t in _all_buttons(workshop_pick)
                     if t.strip() and not any(s in t.lower() for s in nav_skip)
                     and not re.match(r"^\d+\s*/\s*\d+$", t.strip())),
                    None,
                )
                ext_label = ext_btn or "первая свободная"
                if ext_btn:
                    clicked, tools = await self._click_retry(workshop_pick, ext_btn, bot, cfg)
            if tools is not None:
                start_btn = _find_button(tools, cfg.get("start_repair_button", "начать ремонт"))
                if start_btn:
                    clicked, _started = await self._click_retry(tools, start_btn, bot, cfg)
                    if clicked:
                        self._bump("repaired")
                        self._bump("repaired_external")
                        await self._repair_all_equipment()
                        return (f"🛠 в ремонте (чужая мастерская «{ext_label}»): "
                                f"«{model_name}» / {breakage_btn} ({clock()} {today_msk()})")
            return (f"⚠️ «{model_name}»/«{breakage_btn}»: своего инструмента нет, "
                    f"чужую мастерскую найти/арендовать не удалось ({clock()})")

        return (f"⚠️ «{model_name}»/«{breakage_btn}»: своего оборудования нет — "
                f"чужую мастерскую не арендуем ({clock()})")

    async def _repair_all_equipment(self) -> None:
        """После запуска ремонта телефона используемый инструмент теряет прочность
        (-3 за ремонт) — чтобы мастерская не осталась без рабочего оборудования,
        заходим в «Моя мастерская -> Оборудование -> Починить всё оборудование»
        и подтверждаем (платно, но действующие ремонты не трогает — только
        простаивающий изношенный инструмент). Лучшая попытка: любая неудача на
        этом пути не должна ломать уже успешно запущенный ремонт телефона."""
        try:
            cfg = self.repair_cfg
            bot = cfg.get("bot") or CARDS_BOT
            workshop_cmd = cfg.get("workshop_command") or "Моя мастерская"
            panel = await self._send_and_wait(bot, workshop_cmd, timeout=15)
            if panel is None:
                return
            eq_btn = _find_button(panel, cfg.get("equipment_button", "оборудование"))
            if not eq_btn:
                return
            clicked, eq = await self._click_retry(panel, eq_btn, bot, cfg)
            if not clicked or eq is None:
                return
            fix_btn = _find_button(eq, cfg.get("repair_all_equipment_button", "починить всё оборудование"))
            if not fix_btn:
                return
            clicked, confirm_msg = await self._click_retry(eq, fix_btn, bot, cfg)
            if not clicked or confirm_msg is None:
                return
            confirm_btn = _find_button(confirm_msg, cfg.get("repair_all_confirm_button", "подтвердить"))
            if confirm_btn:
                await self._click_retry(confirm_msg, confirm_btn, bot, cfg)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass
