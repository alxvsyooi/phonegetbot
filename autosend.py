"""Модуль «Автоотправка» — произвольные задачи по расписанию (любой бот, любой
текст) + быстрые команды «.trade»/«.pay 505050», напечатанные самому собеседнику
в личке, автоматически превращаются в команду боту карточек.

Независимый модуль: включается/выключается тумблером autosend_enabled, не трогает
фарм (farm.py). Работает только с сообщениями, которые ЭТОТ аккаунт сам отправляет
или получает как ответ на свою же отправку — не читает историю чужой переписки,
контакты и т.п.
"""
from __future__ import annotations

import asyncio
import time

from storage import CARDS_BOT, ROULETTE_BOT, PAY_CONFIRM_BUTTON
from common import parse_hhmm, seconds_until_msk, fmt_duration, clock


class AutosendModule:
    """Миксин с автоотправкой. Ожидает от связанного класса: self.account,
    self.client, self.running, self._trade_mode, self._send_and_wait, self._try_click,
    self.self_commands_cfg, self.storage и last_self_cmd (инициализируется в
    базовом классе — automation.py)."""

    def _loops(self):
        return super()._loops() + [self._tasks_loop()]

    def _autosend_active(self) -> bool:
        return self.account.get("enabled", True) and self.account.get("autosend_enabled", True)

    # ---------- быстрые команды «.trade» / «.pay 505050» ----------
    async def _on_self_command(self, _client, message) -> None:
        """Своё сообщение вида «.trade» / «.pay 505050» в личке с кем-то -> шлём
        соответствующую команду боту карточек (получатель = собеседник этой личке)
        и, если бот попросит подтверждение кнопкой «Подтвердить», жмём её сами."""
        if not self._autosend_active() or not self.account.get("self_commands_enabled", True):
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
            self.last_self_cmd = f"✅ .{verb} ({who}) -> {cmd}{confirmed} ({clock()})"
        except Exception as e:  # noqa: BLE001
            self.last_self_cmd = f"ошибка .{verb}: {e}"

    # ---------- произвольные задачи по расписанию ----------
    def _task_init_next(self, task: dict) -> float:
        if task.get("mode") == "daily":
            h, m = parse_hhmm(task.get("time"))
            return time.time() + seconds_until_msk(h, m)
        return time.time()  # интервальная: запустить сразу

    def _task_after_next(self, task: dict) -> float:
        if task.get("mode") == "daily":
            h, m = parse_hhmm(task.get("time"))
            return time.time() + seconds_until_msk(h, m)
        return time.time() + max(10, int(task.get("interval", 3600)))

    async def _tasks_loop(self) -> None:
        while self.running:
            try:
                if self._trade_mode or not self._autosend_active():
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
            task["last"] = f"✅ '{text}' → @{bot}{clicked} ({clock()})"
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
        return fmt_duration(int(rem)) if rem > 0 else "сейчас"
