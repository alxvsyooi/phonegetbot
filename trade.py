"""Обмен телефонами (trade) — два режима:

- TradeSession — получатель тоже подключён к боту (свой аккаунт того же владельца):
  полная двусторонняя автоматизация (обе стороны сами жмут Готов/Подтвердить).
- SoloTradeSession — получатель произвольный (@username/id, НЕ подключён к боту):
  бот автоматизирует только сторону фарма (открыть трейд, добавить телефоны,
  нажать Готов, дождаться и нажать Подтвердить); получатель принимает предложение
  и жмёт Готов/Подтвердить у себя сам, вручную.

Поток на фарм-стороне: /trade <target> -> (получатель «Принять») -> фарм добавляет
все телефоны (Добавить телефон -> Рабочий телефон -> верхняя редкость -> верхний
телефон -> Добавить несколько -> число сообщением) -> фарм «Готов» -> ждём новое
сообщение «🚨 ПОДТВЕРДИТЕ ОБМЕН 🚨» -> «Подтвердить».

Все подписи/маркеры — в settings.json -> "trade". Подробный лог с префиксом [trade].
"""
from __future__ import annotations

import asyncio
import re
import time

from storage import DEFAULT_SETTINGS, BALANCE_WORD

_COUNT_RE = re.compile(r"\(\s*x?\s*(\d+)\s*\)\s*$")
_SLOTS_RE = re.compile(r"(\d+)\s*/\s*(\d+)")


# ---------- чистые хелперы ----------
def msg_text(msg) -> str:
    return (getattr(msg, "text", None) or getattr(msg, "caption", None) or "")


def buttons(msg):
    mk = getattr(msg, "reply_markup", None)
    if not mk or not getattr(mk, "inline_keyboard", None):
        return []
    return [b for row in mk.inline_keyboard for b in row]


def btn_text(b) -> str:
    return (getattr(b, "text", "") or "")


def find_button_contains(msg, sub: str) -> str | None:
    sub = sub.lower()
    for b in buttons(msg):
        if sub in btn_text(b).lower():
            return btn_text(b)
    return None


def find_ready_button(msg) -> str | None:
    """Кнопка «Готов» (точное совпадение, чтобы не поймать «Не готов»)."""
    for b in buttons(msg):
        if btn_text(b).strip().lower() == "готов":
            return btn_text(b)
    return None


def btn_count(text: str) -> int | None:
    m = _COUNT_RE.search(text or "")
    return int(m.group(1)) if m else None


def first_rarity_button(msg, skip: list[str]) -> str | None:
    skip = [s.lower() for s in skip]
    for b in buttons(msg):
        t = btn_text(b)
        if any(s in t.lower() for s in skip):
            continue
        c = btn_count(t)
        if c and c > 0:
            return t
    return None


def first_phone_button(msg, skip: list[str]) -> tuple[str | None, int]:
    skip = [s.lower() for s in skip]
    for b in buttons(msg):
        t = btn_text(b)
        if any(s in t.lower() for s in skip):
            continue
        c = btn_count(t)
        if c is not None:
            return t, c
    return None, 0


def other_side_ready(text: str, give_marker: str, ready_label: str) -> bool:
    low = text.lower()
    idx = low.find(give_marker.lower())
    other = text[:idx] if idx > 0 else text
    return "✅" in other and ready_label.lower() in other.lower()


def number_after(text: str, marker: str) -> int | None:
    if not text:
        return None
    low = text.lower()
    i = low.find(marker.lower())
    if i < 0:
        return None
    m = re.search(r"\d+", text[i + len(marker):])
    return int(m.group()) if m else None


class _TradeCore:
    """Общая часть: настройки, клики, добавление телефонов, финиш одной стороны."""

    def __init__(self, farm, cfg: dict) -> None:
        self.farm = farm
        self.cfg = {**DEFAULT_SETTINGS.get("trade", {}), **(cfg or {})}
        self.timeout = int(self.cfg.get("step_timeout", 35))
        self.skip = self.cfg.get("skip_buttons", ["назад", "вернуться", "быстрый выбор"])
        self.action_delay = float(self.cfg.get("action_delay", 0.6))
        self.collection_left: int | None = None
        self._last_step = "старт"

    def _log(self, msg: str) -> None:
        print(f"[trade] {self.farm.name}: {msg}")

    def _progress(self, msg: str) -> None:
        self._last_step = msg
        self._log(msg)
        self.farm.last_exchange = f"🔄 {msg} ({time.strftime('%H:%M:%S')})"

    async def _safe_click(self, message, text) -> bool:
        """Нажатие, устойчивое к отсутствию callback-ответа + человеческая пауза."""
        if message is None or not text:
            return False
        await asyncio.sleep(self.action_delay)
        try:
            await message.click(text)
            return True
        except asyncio.TimeoutError:
            self._log(f"click {text!r}: без callback-ответа — считаю выполненным")
            return True
        except Exception as e:  # noqa: BLE001
            self._log(f"click {text!r}: ошибка {e!r}")
            return False

    async def _latest(self, worker, predicate, timeout):
        """Самое СВЕЖЕЕ сообщение под predicate (добираем правки, чтобы не кликать старое)."""
        first = await worker.trade_wait(predicate, timeout)
        if first is None:
            return None
        latest = first
        while True:
            nxt = await worker.trade_wait(predicate, 0.8)
            if nxt is None:
                break
            latest = nxt
        return latest

    async def _go_back_to_panel(self, worker, msg):
        c = self.cfg
        for _ in range(5):
            if msg and (find_ready_button(msg)
                        or find_button_contains(msg, c["add_phone_button"])):
                return msg
            back = (find_button_contains(msg, "вернуться")
                    or find_button_contains(msg, "назад")) if msg else None
            if not back:
                break
            await self._safe_click(msg, back)
            msg = await self._latest(worker, lambda m: True, 6)
        if msg and (find_ready_button(msg) or find_button_contains(msg, c["add_phone_button"])):
            return msg
        return None

    async def _add_all_phones(self, panel):
        """Добавляет телефоны по одному. Возвращает (добавлено, актуальная_панель)."""
        c = self.cfg
        F = self.farm
        max_slots = int(c.get("max_slots", 10))
        added = 0
        # панель = есть «Готов» ИЛИ «Добавить телефон» (при 10/10 кнопки add нет)
        is_panel = (lambda m: find_ready_button(m) is not None
                    or find_button_contains(m, c["add_phone_button"]) is not None)

        while added < max_slots:
            self._progress(f"добавляю телефоны ({added} в обмене)")
            add_btn = find_button_contains(panel, c["add_phone_button"])
            if not add_btn:
                self._log("в панели нет «Добавить телефон» (10/10?) — стоп")
                break
            await self._safe_click(panel, add_btn)

            cat = await self._latest(F, lambda m: True, self.timeout)
            wbtn = find_button_contains(cat, c["working_phone_button"]) if cat else None
            if not wbtn:
                self._log("нет «Рабочий телефон» — возвращаюсь на панель")
                panel = await self._go_back_to_panel(F, cat)
                break
            await self._safe_click(cat, wbtn)

            rar = await self._latest(F, lambda m: True, self.timeout)
            rarity = first_rarity_button(rar, self.skip) if rar else None
            if not rarity:
                self._log("редкостей с телефонами нет — телефоны кончились")
                panel = await self._go_back_to_panel(F, rar)
                break
            await self._safe_click(rar, rarity)

            plist = await self._latest(F, lambda m: True, self.timeout)
            phone, qty = first_phone_button(plist, self.skip) if plist else (None, 0)
            if not phone:
                self._log("телефонов в редкости нет — возвращаюсь на панель")
                panel = await self._go_back_to_panel(F, plist)
                break
            await self._safe_click(plist, phone)
            self._log(f"верхний телефон: {phone} x{qty}")

            detail = await self._latest(
                F, lambda m: find_button_contains(m, c["add_several_button"])
                or find_button_contains(m, c["add_one_button"]), self.timeout)
            if not detail:
                self._log("нет экрана телефона — стоп")
                break
            several = find_button_contains(detail, c["add_several_button"])
            if several:
                await self._safe_click(detail, several)
                await F.trade_wait(lambda m: "количеств" in msg_text(m).lower(),
                                   min(12, self.timeout))
                await asyncio.sleep(self.action_delay)
                await F.trade_send(str(qty))
            else:
                one = find_button_contains(detail, c["add_one_button"])
                if not one:
                    self._log("нет кнопки добавления — стоп")
                    break
                await self._safe_click(detail, one)

            added += 1
            self._log(f"добавлено {phone} x{qty} (слот {added})")

            panel = await self._latest(F, is_panel, self.timeout)
            if not panel:
                self._log("панель не вернулась после добавления — стоп")
                break

        if not (panel and (find_ready_button(panel)
                           or find_button_contains(panel, c["add_phone_button"]))):
            panel = await self._go_back_to_panel(F, panel)
        return added, panel

    async def _finish_side(self, worker, panel, timeout: float | None = None) -> bool:
        """Для одной стороны: нажать «Готов» (один раз) и поймать НОВОЕ сообщение
        «🚨 ПОДТВЕРДИТЕ ОБМЕН 🚨» -> нажать «Подтвердить».

        Обрабатываем ВСЕ входящие (lambda True), чтобы не «съесть» окно подтверждения,
        которое приходит отдельным сообщением с задержкой."""
        c = self.cfg
        confirm_btn_sub = c["confirm_button"]
        ready_pressed = False
        timeout = self.timeout if timeout is None else timeout

        if panel and find_ready_button(panel):
            if await self._safe_click(panel, find_ready_button(panel)):
                ready_pressed = True
                self._log(f"{worker.name}: нажал «Готов»")

        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = await worker.trade_wait(lambda m: True, max(2, deadline - time.time()))
            if msg is None:
                continue
            cbtn = find_button_contains(msg, confirm_btn_sub)
            if cbtn:
                self._log(f"{worker.name}: подтверждаю {cbtn!r}")
                return await self._safe_click(msg, cbtn)
            if not ready_pressed:
                rb = find_ready_button(msg)
                if rb and await self._safe_click(msg, rb):
                    ready_pressed = True
                    self._log(f"{worker.name}: нажал «Готов»")
        self._log(f"{worker.name}: окно «ПОДТВЕРДИТЕ ОБМЕН» не пришло за {timeout:.0f}с")
        return False

    async def _collection_left(self) -> int | None:
        marker = self.cfg.get("collection_marker", "телефонов в коллекции")
        await self.farm.trade_send(BALANCE_WORD)
        msg = await self.farm.trade_wait(
            lambda m: marker.lower() in msg_text(m).lower(), self.timeout)
        if not msg:
            self._log("не удалось прочитать коллекцию (такк)")
            return None
        n = number_after(msg_text(msg), marker)
        self._log(f"в коллекции осталось: {n}")
        return n


class TradeSession(_TradeCore):
    """Двусторонняя автоматизация: получатель тоже подключён к боту (свой аккаунт)."""

    def __init__(self, farm, main, cfg: dict, main_id: int) -> None:
        super().__init__(farm, cfg)
        self.main = main
        self.main_id = main_id

    async def run(self) -> str:
        if self.farm._trade_mode or self.main._trade_mode:
            return "трейд уже выполняется"
        self.farm.enter_trade_mode()
        self.main.enter_trade_mode()
        self.farm.last_exchange = "🔄 трейд начат…"
        try:
            result = await asyncio.wait_for(self._run(), timeout=self.timeout * 14)
        except asyncio.TimeoutError:
            result = f"таймаут на: {self._last_step}"
        except Exception as e:  # noqa: BLE001
            result = f"ошибка на «{self._last_step}»: {e}"
        finally:
            self.farm.exit_trade_mode()
            self.main.exit_trade_mode()
        self.farm.last_exchange = f"🔄 {result} ({time.strftime('%H:%M:%S')})"
        self._log(result)
        return result

    async def _run(self) -> str:
        c = self.cfg
        uname = (self.main.account.get("username") or "").lstrip("@")
        target = f"@{uname}" if uname else str(self.main_id)
        self._progress(f"шаг 1: отправляю {c['command'].replace('{target}', target)}")
        await self.farm.trade_send(c["command"].replace("{target}", target))
        ack = await self.farm.trade_wait(lambda m: True, 8)
        if ack is not None:
            self._log(f"ответ на /trade: {msg_text(ack)[:160]!r}")

        self._progress("шаг 2: жду предложение у главного")
        offer = await self._latest(
            self.main, lambda m: find_button_contains(m, c["accept_button"]) is not None,
            self.timeout)
        if not offer:
            return "шаг 2: получатель не получил предложение обмена"
        await self._safe_click(offer, find_button_contains(offer, c["accept_button"]))

        self._progress("шаг 3: жду панель у фарма")
        panel = await self._latest(
            self.farm, lambda m: find_button_contains(m, c["add_phone_button"]) is not None,
            self.timeout)
        if not panel:
            return "шаг 3: у фарма не открылась панель обмена"

        added, farm_panel = await self._add_all_phones(panel)
        self._log(f"добавлено слотов: {added}")
        if added == 0:
            return "шаг 4: нечего передавать (0 телефонов)"

        self._progress(f"шаг 5: добавлено {added}, Готов + подтверждение обеих сторон")
        res = await asyncio.gather(
            self._finish_side(self.farm, farm_panel),
            self._finish_side(self.main, None),
            return_exceptions=True,
        )
        rf, rm = res[0] is True, res[1] is True
        for who, r in (("фарм", res[0]), ("получатель", res[1])):
            if isinstance(r, Exception):
                self._log(f"finish {who} исключение: {r!r}")
        if rf and rm:
            self.farm._bump("exchanged", added)
            self.collection_left = await self._collection_left()
            return f"успешно, передано {added}; в коллекции осталось {self.collection_left}"
        return f"добавлено {added}, подтверждение: фарм={rf}, получатель={rm}"


class SoloTradeSession(_TradeCore):
    """Получатель НЕ подключён к боту (произвольный @username/id).

    Бот автоматизирует только сторону фарма: отправляет /trade, добавляет телефоны,
    жмёт Готов и Подтвердить. Получатель принимает предложение и жмёт Готов/Подтвердить
    у себя сам — вручную, в реальном Telegram-клиенте."""

    def __init__(self, farm, target: str, cfg: dict) -> None:
        super().__init__(farm, cfg)
        self.target = target.strip()

    async def run(self) -> str:
        if self.farm._trade_mode:
            return "трейд уже выполняется"
        self.farm.enter_trade_mode()
        self.farm.last_exchange = "🔄 трейд начат (получатель вне бота)…"
        try:
            result = await asyncio.wait_for(self._run(), timeout=self.timeout * 14)
        except asyncio.TimeoutError:
            result = f"таймаут на: {self._last_step}"
        except Exception as e:  # noqa: BLE001
            result = f"ошибка на «{self._last_step}»: {e}"
        finally:
            self.farm.exit_trade_mode()
        self.farm.last_exchange = f"🔄 {result} ({time.strftime('%H:%M:%S')})"
        self._log(result)
        return result

    async def _run(self) -> str:
        c = self.cfg
        raw = self.target.lstrip("@")
        target = raw if raw.isdigit() else f"@{raw}"  # numeric id как есть, иначе @username
        self._progress(f"шаг 1: отправляю {c['command'].replace('{target}', target)}")
        await self.farm.trade_send(c["command"].replace("{target}", target))
        ack = await self.farm.trade_wait(lambda m: True, 8)
        if ack is not None:
            self._log(f"ответ на /trade: {msg_text(ack)[:160]!r}")

        # получатель не подключён к боту — должен принять оффер вручную,
        # даём больше времени на реакцию человека
        accept_timeout = int(c.get("solo_accept_timeout", 300))
        self._progress(f"шаг 2: жду, пока «{target}» примет предложение вручную "
                       f"(до {accept_timeout}с)")
        panel = await self._latest(
            self.farm, lambda m: find_button_contains(m, c["add_phone_button"]) is not None,
            accept_timeout)
        if not panel:
            return (f"шаг 2: «{target}» не принял предложение за {accept_timeout}с "
                    f"(попроси его открыть чат с ботом и нажать «Принять»)")

        added, farm_panel = await self._add_all_phones(panel)
        self._log(f"добавлено слотов: {added}")
        if added == 0:
            return "шаг 3: нечего передавать (0 телефонов)"

        self._progress(f"шаг 4: добавлено {added}, жму Готов и жду подтверждения")
        ok = await self._finish_side(self.farm, farm_panel, timeout=accept_timeout)
        if not ok:
            return (f"добавлено {added}, но получатель не завершил обмен вручную "
                    f"(Готов/Подтвердить) за {accept_timeout}с")
        self.farm._bump("exchanged", added)
        self.collection_left = await self._collection_left()
        return f"успешно, передано {added}; в коллекции осталось {self.collection_left}"
