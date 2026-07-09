"""Управляющий бот (@BotFather) с инлайн-кнопками. Мультипользовательский:
каждый видит и настраивает ТОЛЬКО свои аккаунты (изоляция по owner_id).
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Any

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
    ReplyKeyboardMarkup, KeyboardButton,
)
from pyrogram.errors import (
    SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired, FloodWait,
)

from manager import Manager
from storage import Storage

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


# постоянная кнопка снизу экрана — жмёшь и сразу шлётся «/start», не нужно набирать
_START_KEYBOARD = ReplyKeyboardMarkup([[KeyboardButton("/start")]], resize_keyboard=True)

_WELCOME_TEXT = (
    "👋 Привет! Я бот-помощник для игры <b>PhoneGet</b> в Telegram.\n\n"
    "Что я умею — два независимых модуля, каждый включается отдельно:\n\n"
    "🌾 <b>Фарм карточек</b> — сам забирает карточки, крутит рулетку, собирает "
    "урожай с фермы и ежедневную награду, следит за магазином контейнеров.\n\n"
    "📨 <b>Автоотправка</b> — шлёт любые сообщения любым ботам по расписанию, "
    "или по короткой команде вроде «.trade»/«.pay», которую ты печатаешь в чате "
    "с кем-то — я сам подставлю получателя и отправлю нужную команду.\n\n"
    "🔐 Чтобы начать, я попрошу тебя войти в аккаунт (номер + код — как обычный "
    "вход в Telegram). Это даёт полный доступ к аккаунту, поэтому добавляй сюда "
    "только тот аккаунт, которым управляешь ты сам.\n\n"
    "👇 Жми «📋 Мои аккаунты», чтобы добавить первый аккаунт."
)


class ControlBot:
    def __init__(self, settings: dict[str, Any], storage: Storage, manager: Manager) -> None:
        self.settings = settings
        self.storage = storage
        self.manager = manager
        self.multi_user = bool(settings.get("multi_user", False))
        self.admin_ids: set[int] = set(settings.get("admin_ids", []))
        self.states: dict[int, dict[str, Any]] = {}  # FSM: user_id -> состояние

        self.app = Client(
            name="control_bot",
            api_id=int(settings["api_id"]),
            api_hash=settings["api_hash"],
            bot_token=settings["bot_token"],
            workdir=BASE_DIR,
        )
        self._register()

    # ============ регистрация ============
    def _register(self) -> None:
        if self.multi_user:
            auth = filters.all                      # открыт всем
        else:
            auth = filters.user(list(self.admin_ids)) if self.admin_ids else filters.all

        @self.app.on_message(filters.command("start") & auth)
        async def _start(_c, m: Message):
            self.states.pop(m.from_user.id, None)
            # сообщение может нести только один тип клавиатуры — шлём два:
            # 1) закрепляем снизу постоянную кнопку «/start» (один раз хватит, дальше видна всегда)
            await m.reply(_WELCOME_TEXT, reply_markup=_START_KEYBOARD)
            # 2) сама панель — с инлайн-кнопками
            await m.reply("🤖 <b>Панель управления</b>", reply_markup=self._main_menu())

        @self.app.on_message(filters.private & auth & ~filters.command("start"))
        async def _text(_c, m: Message):
            await self._on_text(m)

        @self.app.on_callback_query(auth)
        async def _cb(_c, q: CallbackQuery):
            await self._on_callback(q)

    # ============ доступ/владение ============
    def _my_accounts(self, uid: int) -> list[dict]:
        return [a for a in self.storage.accounts if a.get("owner_id") == uid]

    def _owned(self, uid: int, aid: int) -> dict | None:
        acc = self.storage.get(aid)
        return acc if acc and acc.get("owner_id") == uid else None

    # ============ меню ============
    def _main_menu(self) -> InlineKeyboardMarkup:
        rows = [[_btn("📋 Мои аккаунты", "list")]]
        repo_url = self.settings.get("repo_url")
        if repo_url:
            rows.append([InlineKeyboardButton("📖 Как развернуть свою копию", url=repo_url)])
        return InlineKeyboardMarkup(rows)

    def _accounts_menu(self, uid: int) -> InlineKeyboardMarkup:
        rows = []
        for acc in self._my_accounts(uid):
            w = self.manager.workers.get(acc["id"])
            mark = "🟢" if (w and w.running and acc.get("enabled", True)) else "🔴"
            rows.append([_btn(f"{mark} {acc.get('name')}", f"acc:{acc['id']}")])
        rows.append([_btn("➕ По номеру", "add_phone"), _btn("➕ Session", "add_session")])
        if self._my_accounts(uid):
            rows.append([_btn("☎️ Показать все номера", "allphones")])
        rows.append([_btn("⬅️ Назад", "home")])
        return InlineKeyboardMarkup(rows)

    def _account_menu(self, acc: dict) -> InlineKeyboardMarkup:
        aid = acc["id"]
        en = acc.get("enabled", True)
        return InlineKeyboardMarkup([
            [_btn("⏸ Выключить" if en else "▶️ Включить", f"toggle:{aid}")],
            [_btn("🌾 Фарм карточек", f"farm:{aid}"), _btn("📨 Автоотправка", f"autosend:{aid}")],
            [_btn("📨 Сообщения", f"msgs:{aid}"), _btn("☎️ Телефон", f"phone:{aid}")],
            [_btn("✏️ Имя", f"rename:{aid}"), _btn("🗑 Удалить", f"del:{aid}")],
            [_btn("🔄 Обновить", f"acc:{aid}"), _btn("⬅️ Назад", "list")],
        ])

    # ---------- 🌾 Фарм карточек (независимый модуль) ----------
    def _farm_menu(self, acc: dict) -> InlineKeyboardMarkup:
        aid = acc["id"]
        fe = acc.get("farm_enabled", True)
        return InlineKeyboardMarkup([
            [_btn("⏸ Выключить фарм" if fe else "▶️ Включить фарм", f"tfarm:{aid}")],
            [_btn("⚙️ Автоматизация", f"autom:{aid}"), _btn("⏱ Интервалы", f"intervals:{aid}")],
            [_btn("🎯 Получатели", f"targets:{aid}")],
            [_btn("▶️ Действия", f"actions:{aid}")],
            [_btn("⬅️ Назад", f"acc:{aid}")],
        ])

    def _farm_text(self, acc: dict) -> str:
        fe = "вкл ✅" if acc.get("farm_enabled", True) else "выкл ❌"
        return f"🌾 <b>Фарм карточек</b> — {acc.get('name')}\nМодуль: {fe}"

    def _targets_menu(self, acc: dict) -> InlineKeyboardMarkup:
        aid = acc["id"]
        return InlineKeyboardMarkup([
            [_btn(f"💸 Вывод (/pay) -> {acc.get('payout_target') or 'не задано'}",
                  f"setpaytarget:{aid}")],
            [_btn(f"🤝 Трейд -> {acc.get('trade_target') or 'не задано'}",
                  f"settradetarget:{aid}")],
            [_btn("👑 Сделать получателем для всех моих аккаунтов", f"copytargets:{aid}")],
            [_btn("⬅️ Назад", f"farm:{aid}")],
        ])

    # ---------- 📨 Автоотправка (независимый модуль) ----------
    def _autosend_menu(self, acc: dict) -> InlineKeyboardMarkup:
        aid = acc["id"]
        ae = acc.get("autosend_enabled", True)
        sc = "вкл ✅" if acc.get("self_commands_enabled", True) else "выкл ❌"
        return InlineKeyboardMarkup([
            [_btn("⏸ Выключить автоотправку" if ae else "▶️ Включить автоотправку", f"tautosend:{aid}")],
            [_btn("🧩 Мои задачи", f"tasks:{aid}")],
            [_btn(f"💬 Команды .trade/.pay в личках: {sc}", f"tselfcmd:{aid}")],
            [_btn("⬅️ Назад", f"acc:{aid}")],
        ])

    def _autosend_text(self, acc: dict) -> str:
        ae = "вкл ✅" if acc.get("autosend_enabled", True) else "выкл ❌"
        n = len(acc.get("tasks", []))
        return (f"📨 <b>Автоотправка</b> — {acc.get('name')}\nМодуль: {ae}\n"
                f"Задач настроено: {n}")

    def _messages_menu(self, acc: dict) -> InlineKeyboardMarkup:
        aid = acc["id"]
        return InlineKeyboardMarkup([
            [_btn("🔄 Обновить", f"msgs:{aid}")],
            [_btn("⬅️ Назад", f"acc:{aid}")],
        ])

    def _messages_text(self, acc: dict) -> str:
        w = self.manager.workers.get(acc["id"])
        header = f"📨 <b>Последние сообщения</b> — {acc.get('name')}\n"
        if not w or not w.running:
            return header + "\nАккаунт не запущен."
        return header + "\n" + "\n".join(w.recent_lines(15))

    def _automation_menu(self, acc: dict) -> InlineKeyboardMarkup:
        aid = acc["id"]
        def s(f, d=True):
            return "вкл ✅" if acc.get(f, d) else "выкл ❌"
        return InlineKeyboardMarkup([
            [_btn(f"🃏 Карточки: {s('card_enabled')}", f"tcard:{aid}")],
            [_btn(f"🎰 Рулетка: {s('roulette_enabled')}", f"troul:{aid}")],
            [_btn(f"⛏ Майнинг: {s('mining_enabled')}", f"tmine:{aid}")],
            [_btn(f"🎁 Ежедневная награда: {s('daily_reward_enabled')}", f"tdaily:{aid}")],
            [_btn(f"💸 Авто-вывод: {s('autopay_enabled')}", f"tpay:{aid}")],
            [_btn(f"🤝 Авто-трейд: {s('autotrade_enabled', False)}", f"ttrade:{aid}")],
            [_btn(f"📦 Магазин контейнеров: {s('containers_enabled', False)}", f"tcont:{aid}")],
            [_btn("⬅️ Назад", f"farm:{aid}")],
        ])

    def _intervals_menu(self, acc: dict) -> InlineKeyboardMarkup:
        aid = acc["id"]
        return InlineKeyboardMarkup([
            [_btn(f"⏱ Карточки: {acc.get('card_interval', 3600)}с", f"setcard:{aid}")],
            [_btn(f"⏱ Рулетка: {acc.get('roulette_interval', 3600)}с", f"setroul:{aid}")],
            [_btn(f"⏰ Майнинг (МСК): {acc.get('mining_time', '01:00')}", f"setmtime:{aid}")],
            [_btn("⬅️ Назад", f"farm:{aid}")],
        ])

    def _actions_menu(self, acc: dict) -> InlineKeyboardMarkup:
        aid = acc["id"]
        return InlineKeyboardMarkup([
            [_btn("⛏ Собрать майнинг сейчас", f"mine:{aid}")],
            [_btn("🔄 Слить телефоны (по 🎯 получателю трейда)", f"exch:{aid}")],
            [_btn("📦 Проверить магазин контейнеров", f"checkcont:{aid}")],
            [_btn("⬅️ Назад", f"farm:{aid}")],
        ])

    @staticmethod
    def _task_sched(t: dict) -> str:
        return (f"{t.get('interval', 3600)}с" if t.get("mode") == "interval"
                else f"{t.get('time', '00:00')} МСК")

    def _tasks_menu(self, acc: dict) -> InlineKeyboardMarkup:
        aid = acc["id"]
        rows = []
        for t in acc.get("tasks", []):
            mk = "🟢" if t.get("enabled", True) else "🔴"
            title = t.get("name") or t.get("text") or "задача"
            rows.append([_btn(f"{mk} {title[:24]} ({self._task_sched(t)})", f"task:{aid}:{t['id']}")])
        rows.append([_btn("➕ Добавить задачу", f"addtask:{aid}")])
        rows.append([_btn("⬅️ Назад", f"autosend:{aid}")])
        return InlineKeyboardMarkup(rows)

    def _task_menu(self, acc: dict, t: dict) -> InlineKeyboardMarkup:
        aid, tid = acc["id"], t["id"]
        return InlineKeyboardMarkup([
            [_btn(f"{'⏸ Выключить' if t.get('enabled', True) else '▶️ Включить'}", f"ttask:{aid}:{tid}")],
            [_btn("▶️ Запустить сейчас", f"runtask:{aid}:{tid}")],
            [_btn("🗑 Удалить задачу", f"deltask:{aid}:{tid}")],
            [_btn("⬅️ Назад", f"tasks:{aid}")],
        ])

    def _task_text(self, acc: dict, t: dict) -> str:
        w = self.manager.workers.get(acc["id"])
        rem = w.task_remaining(t) if w else "—"
        click = f"\nКнопка после: <code>{t['click']}</code>" if t.get("click") else ""
        return (
            f"🧩 <b>Задача</b> (id {t['id']}) — {acc.get('name')}\n"
            f"Бот: <code>@{(t.get('bot') or '').lstrip('@')}</code>\n"
            f"Текст: <code>{t.get('text')}</code>\n"
            f"Расписание: {self._task_sched(t)}{click}\n"
            f"Включена: {'да' if t.get('enabled', True) else 'нет'}\n"
            f"До запуска: <b>{rem}</b>\n"
            f"Последний: {t.get('last', '—')}"
        )

    def _get_task(self, acc: dict, tid: int) -> dict | None:
        return next((t for t in acc.get("tasks", []) if t.get("id") == tid), None)

    # ============ текст карточки ============
    def _account_text(self, acc: dict) -> str:
        w = self.manager.workers.get(acc["id"])
        def fl(f, d=True):
            return "вкл" if acc.get(f, d) else "выкл"
        lines = [
            f"<b>{acc.get('name')}</b>  (id <code>{acc['id']}</code>)",
            f"Статус: {w.status if w else 'остановлен'}",
            f"Общий: {fl('enabled')}  |  🌾 Фарм: {fl('farm_enabled')}  |  "
            f"📨 Автоотправка: {fl('autosend_enabled')}",
            "",
        ]
        if w:
            lines += [
                f"🃏 Карточки: до след. <b>{w.card_remaining()}</b>\n   {w.last_card}",
                f"🎰 Рулетка: до след. <b>{w.roulette_remaining()}</b>\n   {w.last_roulette}",
                f"⛏ Майнинг ({acc.get('mining_time', '01:00')} МСК): через <b>{w.mining_remaining()}</b>"
                f"\n   {w.last_mining}",
                f"🎁 Ежедневная награда: {w.last_daily}",
                f"💸 Вывод: {w.last_payout}",
                f"🔄 Обмен: {w.last_exchange}",
                f"📦 Магазин: до след. <b>{w.container_remaining()}</b>\n   {w.last_container}",
                f"💬 Последняя .команда: {w.last_self_cmd}",
                "",
                f"📊 Всего: {w.stats_line()}",
            ]
        else:
            st = acc.get("stats", {})
            lines.append(
                f"📊 Всего: 📱 {st.get('phones', 0)} (⭐{st.get('good_phones', 0)}) | "
                f"🎰 {st.get('roulette', 0)} | ⛏ {st.get('mining', 0)} | 🎁 {st.get('daily', 0)} | "
                f"💸 {st.get('paid', 0)} | 🔄 {st.get('exchanged', 0)}"
            )
        return "\n".join(lines)

    # ============ callback ============
    async def _on_callback(self, q: CallbackQuery) -> None:
        data = q.data
        uid = q.from_user.id
        try:
            if data == "home":
                self.states.pop(uid, None)
                await q.message.edit_text("🤖 <b>Панель управления</b>", reply_markup=self._main_menu())
                return await q.answer()
            if data == "list":
                await q.message.edit_text("📋 <b>Мои аккаунты</b>", reply_markup=self._accounts_menu(uid))
                return await q.answer()
            if data == "add_phone":
                self.states[uid] = {"flow": "add", "method": "phone", "step": "api_id", "data": {}}
                await q.message.edit_text("➕ Добавление по номеру.\nОтправь <b>api_id</b> (my.telegram.org):")
                return await q.answer()
            if data == "add_session":
                self.states[uid] = {"flow": "add", "method": "session", "step": "api_id", "data": {}}
                await q.message.edit_text("➕ Добавление по session string.\nОтправь <b>api_id</b>:")
                return await q.answer()
            if data == "allphones":
                await self._start_all_phones(q, uid)
                return

            # дальше всё про конкретный аккаунт — проверяем владение
            aid = int(data.split(":")[1])
            acc = self._owned(uid, aid)
            if not acc:
                return await q.answer("Это не ваш аккаунт", show_alert=True)

            if data.startswith("acc:"):
                await self._show(q, acc)
            elif data.startswith("farm:"):
                await q.message.edit_text(self._farm_text(acc), reply_markup=self._farm_menu(acc))
            elif data.startswith("autosend:"):
                await q.message.edit_text(self._autosend_text(acc), reply_markup=self._autosend_menu(acc))
            elif data.startswith("autom:"):
                await q.message.edit_text(f"⚙️ Автоматизация — <b>{acc.get('name')}</b>",
                                          reply_markup=self._automation_menu(acc))
            elif data.startswith("intervals:"):
                await q.message.edit_text(f"⏱ Интервалы — <b>{acc.get('name')}</b>",
                                          reply_markup=self._intervals_menu(acc))
            elif data.startswith("actions:"):
                await q.message.edit_text(f"▶️ Действия — <b>{acc.get('name')}</b>",
                                          reply_markup=self._actions_menu(acc))
            elif data.startswith("targets:"):
                await q.message.edit_text(f"🎯 Получатели — <b>{acc.get('name')}</b>\n"
                                          f"Введи @username или числовой id получателя.",
                                          reply_markup=self._targets_menu(acc))
            elif data.startswith("msgs:"):
                await q.message.edit_text(self._messages_text(acc),
                                          reply_markup=self._messages_menu(acc))
            elif data.startswith("setpaytarget:"):
                self._ask(uid, aid, "payout_target")
                await q.message.edit_text(
                    "Получатель вывода (/pay). Пришли <b>@username</b> или <b>числовой id</b>, "
                    "либо <code>-</code> чтобы отключить:")
            elif data.startswith("settradetarget:"):
                self._ask(uid, aid, "trade_target")
                await q.message.edit_text(
                    "Получатель трейда. Пришли <b>@username</b> или <b>числовой id</b> "
                    "(если это твой другой аккаунт в этом боте — обмен пройдёт полностью "
                    "автоматически с обеих сторон; иначе получателю нужно будет самому "
                    "принять/подтвердить обмен), либо <code>-</code> чтобы отключить:")
            elif data.startswith("copytargets:"):
                n = self.manager.copy_targets(uid, aid)
                for a in self._my_accounts(uid):
                    a["is_main"] = (a["id"] == aid)
                self.storage.save()
                await q.answer(
                    f"«{acc.get('name')}» назначен получателем для {n} аккаунт(ов)" if n
                    else "Больше нет других аккаунтов", show_alert=True)
                await q.message.edit_text(f"🎯 Получатели — <b>{acc.get('name')}</b>",
                                          reply_markup=self._targets_menu(acc))
            elif data.startswith("toggle:"):
                await self._toggle(q, acc, "enabled", redisplay="account")
            elif data.startswith("tfarm:"):
                await self._toggle(q, acc, "farm_enabled", redisplay="farm")
            elif data.startswith("tautosend:"):
                await self._toggle(q, acc, "autosend_enabled", redisplay="autosend")
            elif data.startswith("tcard:"):
                await self._toggle(q, acc, "card_enabled")
            elif data.startswith("troul:"):
                await self._toggle(q, acc, "roulette_enabled")
            elif data.startswith("tmine:"):
                await self._toggle(q, acc, "mining_enabled")
            elif data.startswith("tdaily:"):
                await self._toggle(q, acc, "daily_reward_enabled")
            elif data.startswith("tpay:"):
                await self._toggle(q, acc, "autopay_enabled")
            elif data.startswith("ttrade:"):
                await self._toggle(q, acc, "autotrade_enabled")
            elif data.startswith("tcont:"):
                await self._toggle(q, acc, "containers_enabled")
            elif data.startswith("tselfcmd:"):
                await self._toggle(q, acc, "self_commands_enabled", redisplay="autosend")
            elif data.startswith("setcard:"):
                self._ask(uid, aid, "card_interval")
                await q.message.edit_text("Отправь интервал карточек в секундах (>=10):")
            elif data.startswith("setroul:"):
                self._ask(uid, aid, "roulette_interval")
                await q.message.edit_text("Отправь интервал рулетки в секундах (>=10):")
            elif data.startswith("setmtime:"):
                self._ask(uid, aid, "mining_time")
                await q.message.edit_text("Отправь время майнинга по МСК ЧЧ:ММ (напр. 01:00):")
            elif data.startswith("rename:"):
                self._ask(uid, aid, "name")
                await q.message.edit_text("Отправь новое имя аккаунта:")
            elif data.startswith("mine:"):
                await self._collect_mining(q, aid)
            elif data.startswith("exch:"):
                await self._start_trade(q, aid)
            elif data.startswith("phone:"):
                await self._start_show_phone(q, aid)
            elif data.startswith("checkcont:"):
                await self._check_containers(q, aid)
            elif data.startswith("del:"):
                await q.message.edit_text(
                    "Точно удалить аккаунт?",
                    reply_markup=InlineKeyboardMarkup([[
                        _btn("✅ Да", f"delyes:{aid}"), _btn("❌ Нет", f"acc:{aid}")]]))
            elif data.startswith("delyes:"):
                await self.manager.stop_account(aid)
                self.storage.remove(aid)
                await q.message.edit_text("🗑 Удалён.", reply_markup=self._accounts_menu(uid))

            elif data.startswith("tasks:"):
                await q.message.edit_text(f"🧩 Задачи — <b>{acc.get('name')}</b>",
                                          reply_markup=self._tasks_menu(acc))
            elif data.startswith("addtask:"):
                self.states[uid] = {"flow": "addtask", "acc_id": aid, "step": "bot", "data": {}}
                await q.message.edit_text("➕ Новая задача.\nКому слать — <b>@username</b> бота:")
            elif data.startswith("task:"):
                t = self._get_task(acc, int(data.split(":")[2]))
                if t:
                    await q.message.edit_text(self._task_text(acc, t),
                                              reply_markup=self._task_menu(acc, t))
            elif data.startswith("ttask:"):
                t = self._get_task(acc, int(data.split(":")[2]))
                if t:
                    t["enabled"] = not t.get("enabled", True)
                    self.storage.save()
                    await q.message.edit_text(self._task_text(acc, t),
                                              reply_markup=self._task_menu(acc, t))
            elif data.startswith("runtask:"):
                tid = int(data.split(":")[2])
                w = self.manager.workers.get(aid)
                if not w or not w.running:
                    return await q.answer("Аккаунт не запущен", show_alert=True)
                await q.answer("⏳ Запускаю задачу...")
                asyncio.create_task(self._run_task_now(q, aid, tid))
                return
            elif data.startswith("deltask:"):
                tid = int(data.split(":")[2])
                acc["tasks"] = [t for t in acc.get("tasks", []) if t.get("id") != tid]
                self.storage.save()
                await q.message.edit_text(f"🧩 Задачи — <b>{acc.get('name')}</b>",
                                          reply_markup=self._tasks_menu(acc))
            await q.answer()
        except Exception as e:  # noqa: BLE001
            try:
                await q.answer(f"Ошибка: {e}", show_alert=True)
            except Exception:
                pass

    async def _show(self, q: CallbackQuery, acc: dict) -> None:
        await q.message.edit_text(self._account_text(acc), reply_markup=self._account_menu(acc))

    def _ask(self, uid: int, aid: int, field: str) -> None:
        self.states[uid] = {"flow": "set", "field": field, "acc_id": aid}

    async def _toggle(self, q: CallbackQuery, acc: dict, field: str, redisplay: str = "automation") -> None:
        default = False if field in ("autotrade_enabled", "containers_enabled") else True
        acc[field] = not acc.get(field, default)
        self.storage.save()
        if field == "enabled":
            if acc[field]:
                await self.manager.start_account(acc["id"])
            else:
                await self.manager.stop_account(acc["id"])
        if redisplay == "account":
            await self._show(q, acc)
        elif redisplay == "farm":
            await q.message.edit_text(self._farm_text(acc), reply_markup=self._farm_menu(acc))
        elif redisplay == "autosend":
            await q.message.edit_text(self._autosend_text(acc), reply_markup=self._autosend_menu(acc))
        else:  # "automation" — фарм-подтумблеры (карточки/рулетка/майнинг/…)
            await q.message.edit_text(f"⚙️ Автоматизация — <b>{acc.get('name')}</b>",
                                      reply_markup=self._automation_menu(acc))

    async def _collect_mining(self, q: CallbackQuery, aid: int) -> None:
        w = self.manager.workers.get(aid)
        if not w or not w.running:
            return await q.answer("Аккаунт не запущен", show_alert=True)
        await q.answer("⏳ Собираю майнинг...")
        asyncio.create_task(self._run_collect(q, aid, w))

    async def _run_collect(self, q: CallbackQuery, aid: int, w) -> None:
        await w.collect_mining()
        acc = self.storage.get(aid)
        await self._safe_edit(q, self._account_text(acc) + f"\n\n⛏ {w.last_mining}",
                              self._account_menu(acc))

    async def _check_containers(self, q: CallbackQuery, aid: int) -> None:
        w = self.manager.workers.get(aid)
        if not w or not w.running:
            return await q.answer("Аккаунт не запущен", show_alert=True)
        await q.answer("⏳ Проверяю магазин...")
        asyncio.create_task(self._run_check_containers(q, aid, w))

    async def _run_check_containers(self, q: CallbackQuery, aid: int, w) -> None:
        await w.check_containers()
        acc = self.storage.get(aid)
        await self._safe_edit(q, self._account_text(acc) + f"\n\n📦 {w.last_container}",
                              self._account_menu(acc))

    async def _start_show_phone(self, q: CallbackQuery, aid: int) -> None:
        w = self.manager.workers.get(aid)
        if not w or not w.running:
            return await q.answer("Аккаунт не запущен", show_alert=True)
        await q.answer("⏳ Запрашиваю у Telegram...")
        asyncio.create_task(self._run_show_phone(q, aid, w))

    async def _run_show_phone(self, q: CallbackQuery, aid: int, w) -> None:
        info = await w.get_account_info()
        acc = self.storage.get(aid)
        text = f"☎️ <b>{acc.get('name')}</b>\n\n{self._format_phone_info(info)}"
        await self._safe_edit(q, text, self._account_menu(acc))

    @staticmethod
    def _format_phone_info(info: dict | None) -> str:
        if not info:
            return "⚠️ не удалось получить (аккаунт не запущен)"
        if info.get("error"):
            return f"⚠️ ошибка: {info['error']}"
        name = " ".join(filter(None, [info.get("first_name"), info.get("last_name")])) or "—"
        uname = f"@{info['username']}" if info.get("username") else "—"
        return (
            f"Телефон: <code>+{info.get('phone') or '—'}</code>\n"
            f"Имя: {name}\n"
            f"Username: {uname}\n"
            f"id: <code>{info.get('id')}</code>"
            f"{' | Premium ⭐' if info.get('is_premium') else ''}"
        )

    async def _start_all_phones(self, q: CallbackQuery, uid: int) -> None:
        accs = [a for a in self._my_accounts(uid) if self.manager.workers.get(a["id"])
                and self.manager.workers[a["id"]].running]
        if not accs:
            return await q.answer("Нет запущенных аккаунтов", show_alert=True)
        await q.answer("⏳ Запрашиваю номера у Telegram...")
        asyncio.create_task(self._run_all_phones(q, uid, accs))

    async def _run_all_phones(self, q: CallbackQuery, uid: int, accs: list[dict]) -> None:
        lines = ["☎️ <b>Номера телефонов моих аккаунтов</b>", ""]
        for acc in accs:
            w = self.manager.workers.get(acc["id"])
            info = await w.get_account_info() if w else None
            if info and not info.get("error") and info.get("phone"):
                lines.append(f"• {acc.get('name')}: <code>+{info['phone']}</code>")
            else:
                lines.append(f"• {acc.get('name')}: ⚠️ не удалось получить")
        await self._safe_edit(q, "\n".join(lines), self._accounts_menu(uid))

    async def _start_trade(self, q: CallbackQuery, aid: int) -> None:
        w = self.manager.workers.get(aid)
        if not w or not w.running:
            return await q.answer("Аккаунт не запущен", show_alert=True)
        if not (w.account.get("trade_target") or "").strip():
            return await q.answer("Не задан получатель трейда (🎯 Получатели)", show_alert=True)
        await q.answer("⏳ Запускаю трейд (это надолго)...")
        asyncio.create_task(self._run_trade(q, aid))

    async def _run_trade(self, q: CallbackQuery, aid: int) -> None:
        result = await self.manager.run_trade(aid)
        acc = self.storage.get(aid)
        await self._safe_edit(q, self._account_text(acc) + f"\n\n🔄 Результат: {result}",
                              self._account_menu(acc))

    async def _run_task_now(self, q: CallbackQuery, aid: int, tid: int) -> None:
        w = self.manager.workers.get(aid)
        res = await w.run_task_now(tid) if w else "аккаунт не запущен"
        acc = self.storage.get(aid)
        t = self._get_task(acc, tid)
        if t:
            await self._safe_edit(q, self._task_text(acc, t), self._task_menu(acc, t))
        else:
            await self._safe_edit(q, f"🧩 {res}", self._tasks_menu(acc))

    async def _safe_edit(self, q: CallbackQuery, text: str, markup) -> None:
        try:
            await q.message.edit_text(text, reply_markup=markup)
        except Exception:
            try:
                await q.message.reply(text)
            except Exception:
                pass

    # ============ текстовый ввод (FSM) ============
    async def _on_text(self, m: Message) -> None:
        state = self.states.get(m.from_user.id)
        if not state:
            return
        if state.get("flow") == "set":
            await self._handle_set(m, state)
        elif state.get("flow") == "add":
            await self._handle_add(m, state)
        elif state.get("flow") == "addtask":
            await self._handle_addtask(m, state)

    async def _handle_set(self, m: Message, state: dict) -> None:
        uid = m.from_user.id
        acc = self._owned(uid, state["acc_id"])
        if not acc:
            self.states.pop(uid, None)
            return
        field = state["field"]
        val = m.text.strip()
        if field in ("card_interval", "roulette_interval"):
            if not val.isdigit() or int(val) < 10:
                await m.reply("Нужно число секунд (>=10). Ещё раз:")
                return
            acc[field] = int(val)
        elif field == "mining_time":
            mt = re.match(r"^\s*(\d{1,2})[:.\s](\d{1,2})\s*$", val)
            if not mt or not (0 <= int(mt.group(1)) < 24 and 0 <= int(mt.group(2)) < 60):
                await m.reply("Формат ЧЧ:ММ, напр. 01:00. Ещё раз:")
                return
            acc[field] = f"{int(mt.group(1)):02d}:{int(mt.group(2)):02d}"
        elif field in ("payout_target", "trade_target"):
            if val in ("-", "—", "нет"):
                acc[field] = ""
            elif " " in val or len(val) > 64:
                await m.reply("Один токен без пробелов — @username или числовой id. Ещё раз:")
                return
            else:
                acc[field] = val
            self.storage.save()
            self.states.pop(uid, None)
            await m.reply(f"🎯 Получатели — <b>{acc.get('name')}</b>",
                          reply_markup=self._targets_menu(acc))
            return
        else:  # name
            acc[field] = val
        self.storage.save()
        self.states.pop(uid, None)
        await m.reply(self._account_text(acc), reply_markup=self._account_menu(acc))

    async def _handle_addtask(self, m: Message, state: dict) -> None:
        uid = m.from_user.id
        acc = self._owned(uid, state["acc_id"])
        if not acc:
            self.states.pop(uid, None)
            return
        step = state["step"]
        data = state["data"]
        val = m.text.strip()
        if step == "bot":
            data["bot"] = val.lstrip("@")
            state["step"] = "text"
            await m.reply("Что отправлять — <b>текст сообщения</b> (напр. Ткарточка):")
        elif step == "text":
            data["text"] = val
            state["step"] = "sched"
            await m.reply("Расписание: <b>число секунд</b> (интервал, напр. 3600) "
                          "ИЛИ <b>ЧЧ:ММ</b> (ежедневно по МСК):")
        elif step == "sched":
            mt = re.match(r"^\s*(\d{1,2})[:.](\d{1,2})\s*$", val)
            if mt and 0 <= int(mt.group(1)) < 24 and 0 <= int(mt.group(2)) < 60:
                data["mode"] = "daily"
                data["time"] = f"{int(mt.group(1)):02d}:{int(mt.group(2)):02d}"
            elif val.isdigit() and int(val) >= 10:
                data["mode"] = "interval"
                data["interval"] = int(val)
            else:
                await m.reply("Не понял. Число секунд (>=10) или ЧЧ:ММ. Ещё раз:")
                return
            state["step"] = "click"
            await m.reply("Нажимать кнопку после отправки? Пришли <b>текст кнопки</b> "
                          "или <code>-</code> если не нужно:")
        elif step == "click":
            data["click"] = "" if val in ("-", "—", "нет") else val
            tid = max((t.get("id", 0) for t in acc.get("tasks", [])), default=0) + 1
            task = {
                "id": tid, "enabled": True,
                "bot": data["bot"], "text": data["text"],
                "mode": data["mode"],
                "interval": data.get("interval", 3600),
                "time": data.get("time", "00:00"),
                "click": data["click"], "last": "—",
            }
            acc.setdefault("tasks", []).append(task)
            self.storage.save()
            self.states.pop(uid, None)
            await m.reply("✅ Задача добавлена.", reply_markup=self._task_menu(acc, task))

    async def _handle_add(self, m: Message, state: dict) -> None:
        uid = m.from_user.id
        step = state["step"]
        data = state["data"]
        val = m.text.strip()
        try:
            if step == "api_id":
                if not val.isdigit():
                    await m.reply("api_id — число. Ещё раз:")
                    return
                data["api_id"] = int(val)
                state["step"] = "api_hash"
                await m.reply("Теперь <b>api_hash</b>:")
            elif step == "api_hash":
                data["api_hash"] = val
                if state["method"] == "session":
                    state["step"] = "session"
                    await m.reply("Отправь <b>session string</b> (Pyrogram):")
                else:
                    state["step"] = "phone"
                    await m.reply("Отправь <b>номер</b> в формате +79991234567:")
            elif step == "session":
                data["session_string"] = val
                await self._finalize_add(m, data)
            elif step == "phone":
                data["phone"] = val
                await m.reply("📲 Запрашиваю код...")
                client = Client("login_tmp", api_id=data["api_id"], api_hash=data["api_hash"],
                                in_memory=True)
                await client.connect()
                sent = await client.send_code(val)
                state["client"] = client
                data["phone_code_hash"] = sent.phone_code_hash
                state["step"] = "code"
                await m.reply("Введи код. ⚠️ С ПРОБЕЛАМИ (1 2 3 4 5), иначе Telegram сбросит.")
            elif step == "code":
                client: Client = state["client"]
                code = val.replace(" ", "").replace("-", "")
                try:
                    await client.sign_in(data["phone"], data["phone_code_hash"], code)
                except SessionPasswordNeeded:
                    state["step"] = "password"
                    await m.reply("2FA. Отправь облачный пароль:")
                    return
                except (PhoneCodeInvalid, PhoneCodeExpired) as e:
                    await m.reply(f"Код неверный/истёк ({e}). Начни заново: /start")
                    await self._cleanup(state)
                    self.states.pop(uid, None)
                    return
                data["session_string"] = await client.export_session_string()
                await self._cleanup(state)
                await self._finalize_add(m, data, name=data["phone"])
            elif step == "password":
                client: Client = state["client"]
                await client.check_password(val)
                data["session_string"] = await client.export_session_string()
                await self._cleanup(state)
                await self._finalize_add(m, data, name=data["phone"])
        except FloodWait as e:
            await m.reply(f"FloodWait: подожди {e.value} сек.")
            await self._cleanup(state)
            self.states.pop(uid, None)
        except Exception as e:  # noqa: BLE001
            await m.reply(f"Ошибка: {e}\nНачни заново: /start")
            await self._cleanup(state)
            self.states.pop(uid, None)

    async def _cleanup(self, state: dict) -> None:
        client = state.get("client")
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
            state["client"] = None

    async def _finalize_add(self, m: Message, data: dict, name: str | None = None) -> None:
        uid = m.from_user.id
        acc = self.storage.add({
            "name": name or f"acc_{self.storage.next_id()}",
            "owner_id": uid,
            "api_id": int(data["api_id"]),
            "api_hash": data["api_hash"],
            "session_string": data["session_string"],
            "card_interval": int(self.settings.get("default_card_interval", 3600)),
            "roulette_interval": int(self.settings.get("default_roulette_interval", 3600)),
        })
        self.states.pop(uid, None)
        await self.manager.start_account(acc["id"])
        await m.reply(
            f"✅ Аккаунт «{acc['name']}» добавлен и запущен.\n"
            f"Чтобы работали авто-вывод и авто-трейд, задай получателей: "
            f"🌾 Фарм карточек → 🎯 Получатели.",
            reply_markup=self._account_menu(acc))
