"""Управляющий бот (@BotFather) с инлайн-кнопками. Мультипользовательский:
каждый видит и настраивает ТОЛЬКО свои аккаунты (изоляция по owner_id).
"""
from __future__ import annotations

import asyncio
import html
import os
import re
import shutil
import tempfile
import time
from typing import Any

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
    ReplyKeyboardMarkup, KeyboardButton,
)
from pyrogram.errors import (
    SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired, FloodWait, MessageNotModified,
)

from common import clock, fmt_duration
from manager import Manager
from redis_client import RedisClient
from storage import Storage
from ui_engine import Design, DashboardRegistry, RenderEngine

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


# постоянная кнопка снизу экрана — жмёшь и сразу шлётся «/start», не нужно набирать
_START_KEYBOARD = ReplyKeyboardMarkup([[KeyboardButton("/start")]], resize_keyboard=True)

_LOGO_PATH = os.path.join(BASE_DIR, "assets", "nexuscards_logo.jpg")

_WELCOME_TEXT = (
    "👋 Добро пожаловать в NEXUSCARDS!\n\n"
    "Запустите автоматический фарм ресурсов и карточек на всех ваших "
    "Telegram-аккаунтах без ручной рутины.\n\n"
    "<blockquote>🛠 Как начать за 3 шага:\n"
    "1. Нажмите кнопку «🚀 Начать работу»\n"
    "2. Добавьте аккаунт (по номеру или сессии)\n"
    "3. Включите нужные модули автосбора</blockquote>\n\n"
    "Готовы настроить свою ферму?"
)

_WELCOME_KEYBOARD = InlineKeyboardMarkup([
    [_btn("🚀 Начать работу", "welcome_start")],
    [_btn("⚙️ Возможности", "welcome_features"), _btn("❓ Помощь и FAQ", "welcome_faq")],
])

_FEATURES_STUB_TEXT = (
    "⚙️ <b>ВОЗМОЖНОСТИ</b>\n\n"
    "NEXUSCARDS ведёт весь игровой цикл ваших аккаунтов автоматически, "
    "пока вы спите.\n\n"
    "<blockquote>🎮 Игровой цикл: карточки, рулетка, майнинг, ежедневная награда\n"
    "💰 Финансы: авто-вывод очков, авто-трейд, сбор/слив между твинками\n"
    "🔧 Ферма: обслуживание, заполнение слотов, P-Coins, защита от перегрузки\n"
    "🛠 Мастерская: автопочинка телефонов и оборудования, приём заказов, отзывы\n"
    "📦 Магазин контейнеров: мгновенная покупка через API на ресток\n"
    "🏪 Магазин телефонов: автозакупка по редкости и модели\n"
    "📨 Автоотправка: свои задачи по расписанию + быстрые команды в личке</blockquote>\n\n"
    "Каждый модуль включается отдельно — под каждый аккаунт своя настройка."
)
_WELCOME_BACK_KEYBOARD = InlineKeyboardMarkup([[_btn("⬅️ Назад", "welcome_back")]])

# ---------- ❓ База знаний и поддержка ----------
_FAQ_TEXT = (
    "❓ <b>БАЗА ЗНАНИЙ И ПОДДЕРЖКА</b>\n\n"
    "Выберите нужную тему ниже, чтобы получить мгновенный ответ, или свяжитесь "
    "с владельцем бота напрямую.\n\n"
    "<blockquote>💡 Перед обращением в саппорт рекомендуем проверить статус "
    "вашей сессии и прочитать ответы на частые вопросы.</blockquote>"
)

_FAQ_TOPICS = [
    ("session", "🔐 Как правильно добавить .session?",
     "🔐 <b>ПОДКЛЮЧЕНИЕ .SESSION</b>\n\n"
     "1. Перейдите в «Мои аккаунты» ➔ «+ Session».\n"
     "2. Отправьте боту файл <code>.session</code> и файл <code>.json</code> (если используется прокси).\n"
     "3. Бот проверит валидность и присвоит аккаунту статус 🟢 ONLINE.\n\n"
     "⚠️ Не используйте сессии, которые одновременно активны в других сторонних софтах."),
    ("offline", "🔴 Что делать, если статус «OFFLINE»?",
     "🔴 <b>СТАТУС «OFFLINE»</b>\n\n"
     "Основные причины:\n"
     "• Telegram завершил активную сессию (слетел авторизационный ключ).\n"
     "• Временный сбой прокси или IP-адреса.\n"
     "• Telegram затребовал повторную верификацию.\n\n"
     "Решение: перейдите в настройки аккаунта и нажмите «Переподключить» или "
     "добавьте <code>.session</code> заново."),
    ("security", "🛡 Безопасность: банят ли за автосбор?",
     "🛡 <b>БЕЗОПАСНОСТЬ И ЛИМИТЫ</b>\n\n"
     "NEXUSCARDS использует рандомизированные задержки между действиями "
     "(таймеры) и эмулирует поведение реального пользователя.\n\n"
     "Для 100% безопасности:\n"
     "• Не подключайте более 3–5 аккаунтов с одного IP (используйте прокси).\n"
     "• Не ставьте слишком частый интервал фарминга на «молодых» аккаунтах."),
]


def _faq_menu() -> InlineKeyboardMarkup:
    rows = [[_btn(label, f"faqtopic:{key}")] for key, label, _ in _FAQ_TOPICS]
    rows.append([_btn("👨‍💻 Написать админу (Прямой контакт)", "faqcontact")])
    rows.append([_btn("⬅️ Назад в меню", "welcome_back")])
    return InlineKeyboardMarkup(rows)


def _faq_topic_text(key: str) -> str:
    return next((t for k, _, t in _FAQ_TOPICS if k == key), _FAQ_TEXT)


_FAQ_TOPIC_BACK_KEYBOARD = InlineKeyboardMarkup([[_btn("⬅️ Назад", "welcome_faq")]])
_ADMIN_CONTACT_USERNAME = "phntmk"  # для кнопки «👨‍💻 Написать админу» в FAQ


class ControlBot:
    def __init__(
        self, settings: dict[str, Any], storage: Storage, manager: Manager,
        redis_client: RedisClient | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.manager = manager
        self.redis_client = redis_client
        self.multi_user = bool(settings.get("multi_user", False))
        self.admin_ids: set[int] = set(settings.get("admin_ids", []))
        self.states: dict[int, dict[str, Any]] = {}  # FSM: user_id -> состояние
        self.render_engine = RenderEngine()   # хэш-кэш "не редактировать, если не изменилось"
        self.dashboards = DashboardRegistry()  # user_id -> открытое Dashboard-сообщение (для redis_bridge)

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
            uid = m.from_user.id
            self.states.pop(uid, None)
            if not self._my_accounts(uid):
                # первый заход — закрепляем снизу постоянную кнопку «/start» отдельным
                # сообщением (сообщение может нести только ОДИН тип клавиатуры — reply
                # или inline, не оба сразу); дальше держится и без повтора
                await m.reply("👋", reply_markup=_START_KEYBOARD)
            # приветственный экран NEXUSCARDS — на каждый /start (и для новых, и для
            # уже настроенных аккаунтов); переход к Dashboard — через «🚀 Начать работу»
            await m.reply_photo(_LOGO_PATH, caption=_WELCOME_TEXT, reply_markup=_WELCOME_KEYBOARD)

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
    def _is_creator(self, uid: int) -> bool:
        return uid in self.admin_ids

    def _main_menu(self, uid: int = 0) -> InlineKeyboardMarkup:
        rows = [[_btn("📋 Мои аккаунты", "list")]]
        repo_url = self.settings.get("repo_url")
        if repo_url:
            rows.append([InlineKeyboardButton("📖 Как развернуть свою копию", url=repo_url)])
        if self._is_creator(uid):
            rows.append([_btn("📣 Рассылка всем пользователям", "broadcast")])
        return InlineKeyboardMarkup(rows)

    # ============ 🏠 Dashboard (корневой экран, пилотный редизайн) ============
    # Модули — на уровне аккаунта, а не бота; для пользователя с несколькими
    # аккаунтами дэшборд детально показывает 👑 главный (тот же приоритет, что
    # уже используется в _accounts_menu), остальные — через кнопку «📱 Аккаунты».
    _DASHBOARD_MODULES = (
        ("🎴", "Карточки", "card_enabled", "card_next_ts", "last_card"),
        ("⛏️", "Майнинг", "mining_enabled", "mining_next_ts", "last_mining"),
        ("🎰", "Рулетка", "roulette_enabled", "roulette_next_ts", "last_roulette"),
        ("🎁", "Daily Bonus", "daily_reward_enabled", "daily_next_ts", "last_daily"),
    )

    def _dashboard_headline_account(self, uid: int) -> dict | None:
        accs = self._my_accounts(uid)
        if not accs:
            return None
        return next((a for a in accs if a.get("is_main")), accs[0])

    async def _live_field(self, acc_id: int, field: str, default: Any = None) -> Any:
        """Читает live-значение (next_ts/last_*) сначала из Redis (переживает рестарт
        витрины), при пустом/выключенном Redis — из атрибутов уже запущенного
        in-process воркера (manager.workers), как это и раньше читал контрол-бот."""
        value = None
        if self.redis_client:
            value = await self.redis_client.get_status(acc_id, field)
        if value is None:
            w = self.manager.workers.get(acc_id)
            value = getattr(w, field, None) if w else None
        return value if value is not None else default

    async def _module_status_line(self, acc: dict, emoji: str, label: str,
                                   enabled_field: str, next_field: str) -> str:
        aid = acc["id"]
        w = self.manager.workers.get(aid)
        running = bool(w and w.running and acc.get("enabled", True))
        if not acc.get(enabled_field, True):
            tail = "⏸️ <code>PAUSED</code>"
            badge = Design.status_badge(False)
        elif not running:
            tail = "аккаунт остановлен"
            badge = Design.alert_badge()
        else:
            next_ts = await self._live_field(aid, next_field)
            tail = f"⏳ Next: <code>{Design.countdown(next_ts)}</code>"
            badge = Design.status_badge(True)
        return f"├ {emoji} {label}: {badge} | {tail}"

    async def _dashboard_text(self, uid: int) -> str:
        accs = self._my_accounts(uid)
        running = sum(
            1 for a in accs
            if (w := self.manager.workers.get(a["id"])) and w.running and a.get("enabled", True)
        )
        lines = [
            "🖥 <b>NEXUSCARDS</b>",
            "───",
            f"<b>Статус системы:</b> {'🟢 <code>ONLINE</code>' if running else '🔴 <code>OFFLINE</code>'}",
            f"<b>Активных аккаунтов:</b> <code>{running} / {len(accs)}</code>",
            "",
        ]
        main = self._dashboard_headline_account(uid)
        if main is None:
            lines.append("Аккаунтов пока нет — добавь первый через «📱 Аккаунты» ниже.")
        else:
            crown = "👑 " if main.get("is_main") else ""
            lines.append(f"<b>Модули автосбора</b> ({crown}{html.escape(str(main.get('name', '')))}):")
            for i, (emoji, label, enabled_field, next_field, _last_field) in enumerate(self._DASHBOARD_MODULES):
                lines.append(await self._module_status_line(main, emoji, label, enabled_field, next_field))
            lines[-1] = lines[-1].replace("├", "└", 1)  # последняя строка списка — другой символ ветки
            if len(accs) > 1:
                lines.append(f"<i>+ ещё {len(accs) - 1} аккаунт(ов) — см. «📱 Аккаунты»</i>")
            last_drop = await self._live_field(main["id"], "last_card", "—")
            lines += ["", f"<b>Последний дроп:</b> <code>{html.escape(str(last_drop))}</code>"]
        lines += ["───", f"<i>Обновлено: {clock()}</i>"]
        return "\n".join(lines)

    def _dashboard_menu(self, uid: int) -> InlineKeyboardMarkup:
        accs = self._my_accounts(uid)
        main = self._dashboard_headline_account(uid)
        if main is None:
            return InlineKeyboardMarkup([[_btn("📱 Аккаунты", "list")]])
        return InlineKeyboardMarkup([
            [_btn("⚙️ Автоматизация", f"autom:{main['id']}"), _btn(f"📱 Аккаунты ({len(accs)})", "list")],
            [_btn("📊 Статистика", f"dashstats:{main['id']}"), _btn("🔄 Обновить", "home")],
        ])

    # ============ 📊 Статистика за период ============
    # История копится отдельным фоновым циклом (main.py:stats_snapshot_loop) в виде
    # периодических снимков account["stats"] в Redis — "за период" = текущая сумма
    # минус ближайший снимок ДО начала периода. Без Redis (или пока история не
    # накопилась на весь запрошенный период) — честно показываем, что доступно.
    _STATS_PERIODS = [
        ("1h", "Час", 3600), ("7h", "7 часов", 7 * 3600),
        ("1d", "День", 86400), ("1w", "Неделя", 7 * 86400),
        ("1mo", "Месяц", 30 * 86400), ("all", "Всё время", None),
    ]

    async def _stats_period_values(self, acc: dict, period_seconds: int | None) -> tuple[dict, str]:
        current = acc.get("stats", {})
        if period_seconds is None:
            return current, ""
        if not self.redis_client or not self.redis_client.enabled:
            return current, " <i>(без Redis история не копится — показано «Всё время»)</i>"
        now = time.time()
        snap = await self.redis_client.stats_snapshot_before(acc["id"], now - period_seconds)
        if snap is None:
            earliest = await self.redis_client.earliest_stats_snapshot(acc["id"])
            if earliest is None:
                return current, " <i>(история ещё не накопилась — показано «Всё время»)</i>"
            snap_ts, snap = earliest
            covered = fmt_duration(int(now - snap_ts))
            return (
                {k: max(0, int(current.get(k, 0)) - int(snap.get(k, 0))) for k in current},
                f" <i>(истории меньше периода — показано за {covered}, с начала учёта)</i>",
            )
        delta = {k: max(0, int(current.get(k, 0)) - int(snap.get(k, 0))) for k in current}
        return delta, ""

    async def _stats_text(self, acc: dict, period_key: str) -> str:
        seconds = next((s for k, _, s in self._STATS_PERIODS if k == period_key), None)
        label = next((lbl for k, lbl, _ in self._STATS_PERIODS if k == period_key), "Всё время")
        values, note = await self._stats_period_values(acc, seconds)
        crown = "👑 " if acc.get("is_main") else ""
        return (
            "📊 <b>СТАТИСТИКА</b>\n"
            f"Профиль: {crown}{html.escape(str(acc.get('name', '')))}\n"
            f"Период: <b>{label}</b>{note}\n"
            "───\n"
            f"📱 Телефоны: <code>{values.get('phones', 0)}</code> "
            f"(⭐<code>{values.get('good_phones', 0)}</code>)\n"
            f"🎰 Рулетка: <code>{values.get('roulette', 0)}</code>\n"
            f"⛏ Майнинг: <code>{values.get('mining', 0)}</code>\n"
            f"🎁 Daily: <code>{values.get('daily', 0)}</code>\n"
            f"💸 Выведено: <code>{values.get('paid', 0)}</code>\n"
            f"🔄 Обмен: <code>{values.get('exchanged', 0)}</code>\n"
            f"📦 Контейнеры: <code>{values.get('containers_bought', 0)}</code>"
        )

    def _stats_menu(self, acc: dict, period_key: str) -> InlineKeyboardMarkup:
        aid = acc["id"]
        rows = []
        for i in range(0, len(self._STATS_PERIODS), 2):
            row = []
            for key, label, _ in self._STATS_PERIODS[i:i + 2]:
                mark = "✅ " if key == period_key else ""
                row.append(_btn(f"{mark}{label}", f"statsperiod:{aid}:{key}"))
            rows.append(row)
        rows.append([_btn("⬅️ Назад", "home")])
        return InlineKeyboardMarkup(rows)

    async def _open_dashboard(self, uid: int, chat_id: int) -> None:
        """Первый показ дэшборда — новым сообщением (приветственный экран NEXUSCARDS —
        фото с подписью, дэшборд поверх него не отредактировать, так что это отдельное
        текстовое сообщение, открывается по кнопке «🚀 Начать работу»)."""
        text = await self._dashboard_text(uid)
        markup = self._dashboard_menu(uid)
        sent = await self.app.send_message(chat_id, text, reply_markup=markup)
        self.dashboards.register(uid, sent.chat.id, sent.id)
        self.render_engine.should_render(sent.chat.id, "dashboard", text, markup)  # засеваем кэш

    async def _redraw_dashboard(self, q: CallbackQuery) -> None:
        uid = q.from_user.id
        text = await self._dashboard_text(uid)
        markup = self._dashboard_menu(uid)
        self.dashboards.register(uid, q.message.chat.id, q.message.id)
        if self.render_engine.should_render(q.message.chat.id, "dashboard", text, markup):
            await self._safe_edit(q, text, markup)

    async def refresh_dashboard(self, user_id: int) -> None:
        """Дёргается из redis_bridge.py по Pub/Sub-событию (Celery-тик или алерт фарм-
        воркера) — перерисовывает ТОЛЬКО реально открытый дэшборд, и только если
        содержимое действительно изменилось (RenderEngine)."""
        loc = self.dashboards.get(user_id)
        if not loc:
            return
        chat_id, message_id = loc
        text = await self._dashboard_text(user_id)
        markup = self._dashboard_menu(user_id)
        if not self.render_engine.should_render(chat_id, "dashboard", text, markup):
            return
        try:
            await self.app.edit_message_text(chat_id, message_id, text, reply_markup=markup)
        except MessageNotModified:
            pass
        except Exception as e:  # noqa: BLE001
            print(f"[dashboard] не удалось обновить {user_id}: {e}")

    def _accounts_text(self, uid: int) -> str:
        n = len(self._my_accounts(uid))
        if n == 0:
            return (
                "📱 <b>ПОДКЛЮЧЕНИЕ АККАУНТА</b>\n\n"
                "У вас пока нет добавленных профилей. Выберите способ авторизации "
                "первого аккаунта:"
            )
        return f"📋 <b>Мои аккаунты</b> (<code>{n}</code>)\n───"

    def _accounts_menu(self, uid: int) -> InlineKeyboardMarkup:
        rows = []
        accs = sorted(self._my_accounts(uid), key=lambda a: 0 if a.get("is_main") else 1)
        acc_buttons = []
        for acc in accs:
            w = self.manager.workers.get(acc["id"])
            mark = "🟢" if (w and w.running and acc.get("enabled", True)) else "🔴"
            crown = "👑 " if acc.get("is_main") else ""
            acc_buttons.append(_btn(f"{mark} {crown}{acc.get('name')}", f"acc:{acc['id']}"))
        if len(acc_buttons) >= 6:
            # с 6+ аккаунтами список в один столбец растягивается и неудобно скроллить —
            # 2 колонки разгружают экран (порядок сохраняется: 1-2, 3-4, 5-6, ...)
            for i in range(0, len(acc_buttons), 2):
                rows.append(acc_buttons[i:i + 2])
        else:
            rows.extend([b] for b in acc_buttons)
        rows.append([_btn("📱 По номеру телефона", "add_phone"),
                     _btn("🔑 По Pyrogram / Telethon Session", "add_session")])
        if self._my_accounts(uid):
            rows.append([_btn("☎️ Показать все номера", "allphones")])
        rows.append([_btn("⬅️ Назад", "home")])
        return InlineKeyboardMarkup(rows)

    def _account_menu(self, acc: dict) -> InlineKeyboardMarkup:
        aid = acc["id"]
        en = acc.get("enabled", True)
        farm_badge = Design.status_badge_plain(acc.get("farm_enabled", True))
        autosend_badge = Design.status_badge_plain(acc.get("autosend_enabled", True))
        rows = [
            [_btn("⏸ Выключить" if en else "▶️ Включить", f"toggle:{aid}")],
            [_btn(f"📱 Фарм карточек — {farm_badge}", f"farm:{aid}"),
             _btn(f"📨 Автоотправка — {autosend_badge}", f"autosend:{aid}")],
            [_btn("⚙️ Настройки", f"settings:{aid}")],
            [_btn("🔄 Обновить", f"acc:{aid}"), _btn("⬅️ Назад", "list")],
        ]
        return InlineKeyboardMarkup(rows)

    # ---------- ⚙️ Настройки аккаунта (алерты/акк/имя/удаление) ----------
    def _settings_text(self, acc: dict) -> str:
        return f"⚙️ <b>Настройки</b> — {acc.get('name')}\n───"

    def _settings_menu(self, acc: dict) -> InlineKeyboardMarkup:
        aid = acc["id"]
        al = Design.status_badge_plain(acc.get("alerts_enabled", True))
        rows = [[_btn(f"🔔 Алерты — {al}", f"talerts:{aid}")]]
        if acc.get("owner_id") in self.admin_ids:
            n = len(acc.get("msg_log", []))
            rows.append([_btn(f"📩 Акк ({n})" if n else "📩 Акк", f"msgacc:{aid}")])
        rows += [
            [_btn("✏️ Имя", f"rename:{aid}"), _btn("🗑 Удалить", f"del:{aid}")],
            [_btn("⬅️ Назад", f"acc:{aid}")],
        ]
        return InlineKeyboardMarkup(rows)

    # ---------- 📩 Акк (только создатель) ----------
    def _msgacc_text(self, acc: dict) -> str:
        ma = Design.status_badge(acc.get("msg_alert_enabled", False))
        log = acc.get("msg_log", [])
        if not log:
            body = "Сообщений пока нет."
        else:
            body = "\n\n".join(
                f"🕐 {html.escape(m.get('ts', '—'))} — <b>{html.escape(m.get('from', '—'))}</b>:\n"
                f"{html.escape(m.get('text', ''))}"
                for m in reversed(log)
            )
        return (f"📩 <b>Акк</b> — {acc.get('name')}\n───\n"
                f"Улавливать новые сообщения: {ma}\n\n{body}")

    def _msgacc_menu(self, acc: dict) -> InlineKeyboardMarkup:
        aid = acc["id"]
        ma = Design.status_badge_plain(acc.get("msg_alert_enabled", False))
        return InlineKeyboardMarkup([
            [_btn(f"🔔 Улавливать — {ma}", f"tmsgacc:{aid}")],
            [_btn("🔄 Обновить", f"msgacc:{aid}"), _btn("🗑 Очистить историю", f"clrmsgacc:{aid}")],
            [_btn("⬅️ Назад", f"settings:{aid}")],
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
        fe = Design.status_badge(acc.get("farm_enabled", True))
        return f"📱 <b>Фарм карточек</b> — {acc.get('name')}\n───\nМодуль: {fe}"

    def _targets_menu(self, acc: dict) -> InlineKeyboardMarkup:
        aid = acc["id"]
        return InlineKeyboardMarkup([
            [_btn(f"💸 Вывод (/pay) -> {acc.get('payout_target') or 'не задано'}",
                  f"setpaytarget:{aid}")],
            [_btn(f"🤝 Трейд -> {acc.get('trade_target') or 'не задано'}",
                  f"settradetarget:{aid}")],
            [_btn("👑 Получатель для всех моих акков", f"copytargets:{aid}")],
            [_btn("⬅️ Назад", f"farm:{aid}")],
        ])

    # ---------- 📨 Автоотправка (независимый модуль) ----------
    def _autosend_menu(self, acc: dict) -> InlineKeyboardMarkup:
        aid = acc["id"]
        ae = acc.get("autosend_enabled", True)
        sc = Design.status_badge_plain(acc.get("self_commands_enabled", True))
        return InlineKeyboardMarkup([
            [_btn("⏸ Выключить автоотправку" if ae else "▶️ Включить автоотправку", f"tautosend:{aid}")],
            [_btn("🧩 Мои задачи", f"tasks:{aid}")],
            [_btn(f"💬 Команды .trade/.pay в личках — {sc}", f"tselfcmd:{aid}")],
            [_btn("⬅️ Назад", f"acc:{aid}")],
        ])

    def _autosend_text(self, acc: dict) -> str:
        ae = Design.status_badge(acc.get("autosend_enabled", True))
        n = len(acc.get("tasks", []))
        return (f"📨 <b>Автоотправка</b> — {acc.get('name')}\n───\nМодуль: {ae}\n"
                f"Задач настроено: <code>{n}</code>")

    @staticmethod
    def _s(acc: dict, f: str, d: bool = True) -> str:
        return Design.status_badge_plain(acc.get(f, d))

    @staticmethod
    def _category_on(acc: dict, fields: list[tuple[str, bool]]) -> bool:
        return any(acc.get(f, d) for f, d in fields)

    def _automation_text(self, acc: dict) -> str:
        return f"⚙️ <b>Автоматизация</b> — {acc.get('name')}\nВыбери раздел:"

    def _automation_menu(self, acc: dict) -> InlineKeyboardMarkup:
        """Верхний уровень «⚙️ Автоматизация» — только навигация по группам,
        сами тумблеры живут в подменю ниже (было одним длинным плоским списком
        из 17 кнопок вперемешку — разложено по смыслу). В коде реально 6 групп
        (не 4, как в куда более общем ТЗ-мокапе) — категории не сливаем, чтобы не
        трогать существующую логику, только добавляем 🟢/🔴-индикатор на каждую."""
        aid = acc["id"]
        game_on = self._category_on(acc, [
            ("card_enabled", True), ("roulette_enabled", True),
            ("mining_enabled", True), ("daily_reward_enabled", True),
        ])
        fin_on = self._category_on(acc, [("autopay_enabled", True), ("autotrade_enabled", False)])
        cnt_on = self._category_on(acc, [("containers_api_enabled", False)])
        rep_on = self._category_on(acc, [
            ("auto_repair_enabled", False), ("auto_accept_enabled", False), ("auto_review_enabled", False),
        ])
        frm_on = self._category_on(acc, [
            ("farm_maintenance_enabled", False), ("pcoin_exchange_enabled", False),
            ("pcoin_send_enabled", False), ("power_watchdog_enabled", False),
        ])
        shop_on = self._category_on(acc, [("phone_shop_enabled", False)])
        return InlineKeyboardMarkup([
            [_btn(f"🎮 Игровой цикл — {Design.status_badge_plain(game_on)}", f"autgame:{aid}")],
            [_btn(f"💰 Финансы — {Design.status_badge_plain(fin_on)}", f"autfin:{aid}")],
            [_btn(f"🔧 Ферма — {Design.status_badge_plain(frm_on)}", f"autfrm:{aid}")],
            [_btn(f"🛠 Мастерская (ремонт) — {Design.status_badge_plain(rep_on)}", f"autrep:{aid}")],
            [_btn(f"📦 Магазин контейнеров — {Design.status_badge_plain(cnt_on)}", f"autcnt:{aid}")],
            [_btn(f"🏪 Магазин телефонов — {Design.status_badge_plain(shop_on)}", f"autphn:{aid}")],
            [_btn("⬅️ Назад", f"farm:{aid}")],
        ])

    def _autom_game_menu(self, acc: dict) -> InlineKeyboardMarkup:
        aid = acc["id"]
        return InlineKeyboardMarkup([
            # 🃏 Карточки — теперь отдельный экран-модуль (см. _cards_module_menu),
            # остальные 3 строки этого меню не тронуты (не входят в пилот)
            [_btn(f"🎴 Карточки — {Design.status_badge_plain(acc.get('card_enabled', True))} →", f"cardmod:{aid}")],
            [_btn(f"🎰 Рулетка — {self._s(acc, 'roulette_enabled')}", f"troul:{aid}")],
            [_btn(f"⛏ Майнинг — {self._s(acc, 'mining_enabled')}", f"tmine:{aid}")],
            [_btn(f"🎁 Ежедневная награда — {self._s(acc, 'daily_reward_enabled')}", f"tdaily:{aid}")],
            [_btn("⬅️ Назад", f"autom:{aid}")],
        ])

    # ---------- 🎴 Карточки — выделенный экран-модуль (пилотный редизайн) ----------
    # Объединяет то, что раньше было разбросано между _autom_game_menu (тоггл
    # card_enabled) и _intervals_menu (card_interval) — те callback-префиксы
    # (tcard:/setcard:) не меняются, только собраны на одном экране + статус из Redis.
    # Из ТЗ-мокапа сознательно убраны «Авто-продажа»/«Фильтр продаж» (такой логики
    # в проекте нет — не пилотный UI, а новая функциональность) и «Запустить сейчас»
    # (нет ручного триггера вытягивания карты) — см. согласованный план.
    async def _cards_module_text(self, acc: dict) -> str:
        aid = acc["id"]
        enabled = acc.get("card_enabled", True)
        interval = acc.get("card_interval", 3600)
        next_ts = await self._live_field(aid, "card_next_ts")
        last = await self._live_field(aid, "last_card", "—")
        lines = [
            "🎴 <b>АВТОМАТИЗАЦИЯ: КАРТОЧКИ</b>",
            "───",
            f"Модуль отправляет команду в @phonegetcardsbot и забирает выпавшие "
            f"телефоны — {html.escape(str(acc.get('name', '')))}.",
            "",
            "<b>Параметры:</b>",
            "├ Режим:    <code>команда в чат @phonegetcardsbot</code>",
            f"└ Интервал: <code>{interval}с</code>",
            "",
        ]
        if enabled:
            lines.append(f"<b>Статус:</b> {Design.status_badge(True)} | "
                         f"{Design.wait_badge()} Next: <code>{Design.countdown(next_ts)}</code>")
        else:
            lines.append(f"<b>Статус:</b> {Design.status_badge(False)}")
        lines.append(f"<b>Последний результат:</b> {html.escape(str(last))}")
        return "\n".join(lines)

    def _cards_module_menu(self, acc: dict) -> InlineKeyboardMarkup:
        aid = acc["id"]
        en = acc.get("card_enabled", True)
        return InlineKeyboardMarkup([
            [_btn("🔴 Выключить модуль" if en else "🟢 Включить модуль", f"tcard:{aid}")],
            [_btn(f"⏱️ Изменить интервал ({acc.get('card_interval', 3600)}с)", f"setcard:{aid}")],
            [_btn("⬅️ Назад в Автоматизацию", f"autgame:{aid}")],
        ])

    def _autom_finance_menu(self, acc: dict) -> InlineKeyboardMarkup:
        aid = acc["id"]
        return InlineKeyboardMarkup([
            [_btn(f"💸 Авто-вывод — {self._s(acc, 'autopay_enabled')}", f"tpay:{aid}")],
            [_btn(f"💸 Процент вывода: {acc.get('autopay_percent', 100)}%", f"setpaypercent:{aid}")],
            [_btn(f"🤝 Авто-трейд — {self._s(acc, 'autotrade_enabled', False)}", f"ttrade:{aid}")],
            [_btn("⬅️ Назад", f"autom:{aid}")],
        ])

    def _autom_containers_menu(self, acc: dict) -> InlineKeyboardMarkup:
        aid = acc["id"]
        return InlineKeyboardMarkup([
            [_btn(f"📦⚡ Покупка через API — {self._s(acc, 'containers_api_enabled', False)}",
                  f"tcontapi:{aid}")],
            [_btn(f"📦⚡ Приоритет типов: {acc.get('containers_api_priority') or 'donate,expensive,budget'}",
                  f"setcontapipri:{aid}")],
            [_btn("📦⚡ Проверить/купить сейчас", f"contapinow:{aid}")],
            [_btn("⬅️ Назад", f"autom:{aid}")],
        ])

    def _autom_repair_menu(self, acc: dict) -> InlineKeyboardMarkup:
        aid = acc["id"]
        return InlineKeyboardMarkup([
            [_btn(f"🛠 Автопочинка телефонов — {self._s(acc, 'auto_repair_enabled', False)}", f"trepair:{aid}")],
            [_btn(f"🌍 Чужая мастерская при нехватке — "
                  f"{self._s(acc, 'repair_external_workshop_enabled', False)}", f"textworkshop:{aid}")],
            [_btn(f"🌍 Владелец чужой мастерской: "
                  f"{acc.get('repair_external_workshop_owner') or '—'}", f"setworkshopowner:{aid}")],
            [_btn(f"🌍 Имя чужой мастерской: {acc.get('repair_external_workshop_name') or 'любая'}",
                  f"setworkshop:{aid}")],
            [_btn(f"📥 Автопринятие заказов — {self._s(acc, 'auto_accept_enabled', False)}", f"taccept:{aid}")],
            [_btn(f"⭐ Авто-отзыв (Критик) — {self._s(acc, 'auto_review_enabled', False)}",
                  f"treview:{aid}")],
            [_btn("⬅️ Назад", f"autom:{aid}")],
        ])

    def _autom_farm_menu(self, acc: dict) -> InlineKeyboardMarkup:
        aid = acc["id"]
        return InlineKeyboardMarkup([
            [_btn(f"🔧 Обслуживание фермы — "
                  f"{self._s(acc, 'farm_maintenance_enabled', False)}", f"tfarmmaint:{aid}")],
            [_btn(f"🔧 Проверять раз в: {acc.get('farm_maintenance_interval', 3600)}с", f"setfarmtimes:{aid}")],
            [_btn(f"🎯 Целевое число телефонов: {acc.get('farm_target_phones', 11)}",
                  f"setfarmtarget:{aid}")],
            [_btn(f"📱 Модель для пополнения: {acc.get('farm_fill_model') or '—'}",
                  f"setfarmfill:{aid}")],
            [_btn(f"🏪 Редкость модели: {acc.get('farm_fill_rarity') or 'как в автозакупке'}",
                  f"setfarmfillrarity:{aid}")],
            [_btn(f"💱 Продавать P-Coins за ТОчки — {self._s(acc, 'pcoin_exchange_enabled', False)}",
                  f"texchange:{aid}")],
            [_btn(f"💱 Переводить P-Coins получателю — {self._s(acc, 'pcoin_send_enabled', False)}",
                  f"tpcoinsend:{aid}")],
            [_btn(f"⚡ Сброс перегрузки питания — {self._s(acc, 'power_watchdog_enabled', False)}",
                  f"tpowerwatch:{aid}")],
            [_btn(f"⚡ Проверять раз в: {acc.get('power_watchdog_interval', 300)}с", f"setpowerwatch:{aid}")],
            [_btn("⬅️ Назад", f"autom:{aid}")],
        ])

    def _autom_phoneshop_menu(self, acc: dict) -> InlineKeyboardMarkup:
        aid = acc["id"]
        return InlineKeyboardMarkup([
            [_btn(f"🏪 Автозакупка телефонов — {self._s(acc, 'phone_shop_enabled', False)}", f"tshop:{aid}")],
            [_btn("🏪 Настройки автозакупки →", f"phoneshop:{aid}")],
            [_btn("⬅️ Назад", f"autom:{aid}")],
        ])

    def _phone_shop_settings_menu(self, acc: dict) -> InlineKeyboardMarkup:
        aid = acc["id"]
        qty = acc.get("phone_shop_quantity", 0) or "макс"
        return InlineKeyboardMarkup([
            [_btn(f"🏪 Редкость закупки: {acc.get('phone_shop_rarity', 'Ширпотреб')}", f"setrarity:{aid}")],
            [_btn(f"🏪 Кол-во за раз: {qty}", f"setqty:{aid}")],
            [_btn("⬅️ Назад", f"autphn:{aid}")],
        ])

    def _intervals_text(self, acc: dict) -> str:
        crown = "👑 " if acc.get("is_main") else ""

        def line(emoji: str, label: str, seconds) -> str:
            s = int(seconds)
            return f"• {emoji} {label}: <code>{fmt_duration(s)}</code> ({s}с)"

        body = "\n".join([
            line("🎴", "Карточки", acc.get("card_interval", 3600)),
            line("🎰", "Рулетка", acc.get("roulette_interval", 3600)),
            line("⛏️", "Майнинг", acc.get("mining_check_interval", 14400)),
            f"• 🎁 Ежедневная: <code>{acc.get('mining_time', '01:00')} МСК</code>",
            line("💸", "Авто-вывод", acc.get("autopay_interval", 14400)),
            line("🤝", "Авто-трейд", acc.get("autotrade_interval", 14400)),
            line("💱", "P-Coins", acc.get("pcoin_exchange_interval", 14400)),
        ])
        return (
            "⏱️ <b>НАСТРОЙКА ИНТЕРВАЛОВ</b>\n"
            f"Профиль: {crown}{html.escape(str(acc.get('name', '')))}\n\n"
            f"<blockquote>Текущие задержки автосбора:\n{body}</blockquote>\n\n"
            "Нажмите на кнопку ниже, чтобы изменить интервал:"
        )

    def _intervals_menu(self, acc: dict) -> InlineKeyboardMarkup:
        aid = acc["id"]
        ci = fmt_duration(int(acc.get("card_interval", 3600)))
        ri = fmt_duration(int(acc.get("roulette_interval", 3600)))
        mi = fmt_duration(int(acc.get("mining_check_interval", 14400)))
        mt = acc.get("mining_time", "01:00")
        pi = fmt_duration(int(acc.get("autopay_interval", 14400)))
        ti = fmt_duration(int(acc.get("autotrade_interval", 14400)))
        pci = fmt_duration(int(acc.get("pcoin_exchange_interval", 14400)))
        return InlineKeyboardMarkup([
            [_btn(f"🎴 Карточки: {ci}", f"setcard:{aid}"), _btn(f"🎰 Рулетка: {ri}", f"setroul:{aid}")],
            [_btn(f"⛏️ Майнинг: {mi}", f"setmininterval:{aid}"), _btn(f"🎁 Награда: {mt}", f"setmtime:{aid}")],
            [_btn(f"💸 Вывод: {pi}", f"setpayinterval:{aid}"), _btn(f"🤝 Трейд: {ti}", f"settradeinterval:{aid}")],
            [_btn(f"💱 P-Coins: {pci}", f"setpcoininterval:{aid}")],
            [_btn("⬅️ Назад в меню", f"farm:{aid}")],
        ])

    def _actions_text(self, acc: dict) -> str:
        crown = "👑 " if acc.get("is_main") else ""
        return (
            "⚡️ <b>РУЧНОЙ ЗАПУСК ДЕЙСТВИЙ</b>\n"
            f"Профиль: {crown}{html.escape(str(acc.get('name', '')))}\n\n"
            "<blockquote>Выберите модуль для немедленного выполнения команды "
            "в обход таймеров.</blockquote>"
        )

    def _actions_menu(self, acc: dict) -> InlineKeyboardMarkup:
        """Верхний уровень «▶️ Действия» — навигация по группам, 2 колонки.
        ▶️ — открывает подменю группы, ⚡️ — сразу выполняет (см. actall:)."""
        aid = acc["id"]
        return InlineKeyboardMarkup([
            [_btn("▶️ 🎮 Игра", f"actgame:{aid}"), _btn("▶️ 🔧 Ферма", f"actfarm:{aid}")],
            [_btn("▶️ 🛠 Мастерская", f"actrepair:{aid}"), _btn("▶️ 🏪 Магазин", f"actshop:{aid}")],
            [_btn("▶️ 💰 Финансы", f"actfin:{aid}")],
            [_btn("⚡️ Запустить всё", f"actall:{aid}")],
            [_btn("⬅️ Назад к аккаунту", f"farm:{aid}")],
        ])

    def _act_game_menu(self, acc: dict) -> InlineKeyboardMarkup:
        aid = acc["id"]
        return InlineKeyboardMarkup([
            [_btn("⛏ Собрать майнинг сейчас", f"mine:{aid}")],
            [_btn("⬅️ Назад", f"actions:{aid}")],
        ])

    def _act_farm_menu(self, acc: dict) -> InlineKeyboardMarkup:
        aid = acc["id"]
        rows = [
            [_btn("🔧 Обслужить ферму сейчас", f"farmmaintnow:{aid}")],
            [_btn("🧩 Заполнить ферму", f"fillfarmnow:{aid}")],
            [_btn("⚡ Проверить перегрузку сейчас", f"powerwatchnow:{aid}")],
        ]
        if acc.get("pcoin_exchange_enabled", False):
            rows.append([_btn("💱 Продать P-Coins сейчас", f"pcoincheck:{aid}")])
        if acc.get("pcoin_send_enabled", False):
            rows.append([_btn("💱 Перевести P-Coins сейчас", f"pcoinsendnow:{aid}")])
        rows.append([_btn("⬅️ Назад", f"actions:{aid}")])
        return InlineKeyboardMarkup(rows)

    def _act_repair_menu(self, acc: dict) -> InlineKeyboardMarkup:
        aid = acc["id"]
        return InlineKeyboardMarkup([
            [_btn("🛠 Почистить нерабочие сейчас", f"repairnow:{aid}")],
            [_btn("🔩 Починить оборудование", f"repaireqnow:{aid}")],
            [_btn("⬅️ Назад", f"actions:{aid}")],
        ])

    def _act_shop_menu(self, acc: dict) -> InlineKeyboardMarkup:
        aid = acc["id"]
        return InlineKeyboardMarkup([
            [_btn("🏪 Купить телефоны сейчас", f"shopnow:{aid}")],
            [_btn("⬅️ Назад", f"actions:{aid}")],
        ])

    def _act_fin_menu(self, acc: dict) -> InlineKeyboardMarkup:
        aid = acc["id"]
        rows = [
            [_btn("💸 Перевести человеку", f"paystart:{aid}")],
            [_btn("🤝 Трейд с человеком", f"tradestart:{aid}")],
            [_btn("🔄 Слить телефоны получателю", f"exch:{aid}")],
        ]
        if not acc.get("autopay_enabled", True):
            rows.append([_btn("💸 Вывести всё", f"payoutall:{aid}")])
        if acc.get("is_main"):
            rows.append([_btn(f"💰 Сумма слива твинкам: {acc.get('drain_amount', 0)}", f"setdrain:{aid}")])
            rows.append([_btn("💧 Слить твинкам сейчас", f"draindo:{aid}")])
            rows.append([_btn("📥 Собрать всё с твинков", f"collectall:{aid}")])
        rows.append([_btn("⬅️ Назад", f"actions:{aid}")])
        return InlineKeyboardMarkup(rows)

    _WEEKDAY_LABELS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

    @classmethod
    def _task_sched(cls, t: dict) -> str:
        base = (f"{t.get('interval', 3600)}с" if t.get("mode") == "interval"
                else f"{t.get('time', '00:00')} МСК")
        days = t.get("days")
        if days:
            base += " (" + "/".join(cls._WEEKDAY_LABELS[int(d) % 7] for d in days) + ")"
        if int(t.get("jitter_seconds", 0) or 0) > 0:
            base += f" ±{t['jitter_seconds']}с"
        return base

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
        """Свёрнутая карточка: только «через сколько что» по фичам, которые СЕЙЧАС
        включены — выключенная фича просто не показывается (не путать с прошлым
        вариантом, где стояло «выкл»: тут бот реально отслеживает тумблеры, а не
        показывает устаревший last_* текст). Подробности — кнопки в ▶️ Действия.
        Визуал переоформлен под общую дизайн-систему (см. ui_engine.Design) — сами
        источники данных (w.card_remaining() и т.п.) и условия показа не менялись."""
        w = self.manager.workers.get(acc["id"])
        crown = "👑 " if acc.get("is_main") else ""
        name = f"{crown}<b>{acc.get('name')}</b>"

        if not acc.get("enabled", True):
            return f"🔴 {name}\n───\nАккаунт выключен."
        if not w or not w.running:
            return f"{Design.alert_badge()} {name}\n───\nАккаунт не запущен."

        farm_on = acc.get("farm_enabled", True)

        def feat(emoji: str, label: str, field: str, remaining_fn, default: bool = True) -> str | None:
            if not farm_on or not acc.get(field, default):
                return None
            return f"{emoji} {label}: {Design.wait_badge()} через <code>{remaining_fn()}</code>"

        rows = [
            feat("🎴", "Карточка", "card_enabled", w.card_remaining),
            feat("🎰", "Рулетка", "roulette_enabled", w.roulette_remaining),
            feat("⛏", "Майнинг", "mining_enabled", w.mining_remaining),
        ]
        body = [r for r in rows if r]
        lines = [f"🟢 {name}", "───"]
        lines += body if body else ["<i>Все фарм-модули выключены.</i>"]
        return "\n".join(lines)

    # ============ callback ============
    async def _on_callback(self, q: CallbackQuery) -> None:
        data = q.data
        uid = q.from_user.id
        if data != "home":
            # ушли с экрана Dashboard на любой другой (то же сообщение, просто
            # edit_text) — если не снять регистрацию, redis_bridge.py на ближайшем
            # Celery-тике/алерте молча перерисует ЭТО ЖЕ сообщение обратно в Dashboard
            # (RenderEngine пропускает перерисовку, только если её собственный текст
            # не изменился — а countdown на дэшборде меняется почти каждый тик), и
            # пользователя «выкидывает» из Автоматизации/Действий на главный экран —
            # баг, воспроизведён вживую. "home" — единственный путь, что оставляет
            # сообщение Dashboard-ом, поэтому только он не снимает регистрацию.
            self.dashboards.forget(uid)
        try:
            if data == "welcome_start":
                self.states.pop(uid, None)
                await self._open_dashboard(uid, q.message.chat.id)
                return await q.answer()
            if data == "welcome_features":
                await q.message.edit_caption(_FEATURES_STUB_TEXT, reply_markup=_WELCOME_BACK_KEYBOARD)
                return await q.answer()
            if data == "welcome_faq":
                await q.message.edit_caption(_FAQ_TEXT, reply_markup=_faq_menu())
                return await q.answer()
            if data.startswith("faqtopic:"):
                key = data.split(":")[1]
                await q.message.edit_caption(_faq_topic_text(key), reply_markup=_FAQ_TOPIC_BACK_KEYBOARD)
                return await q.answer()
            if data == "faqcontact":
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("👨‍💻 Открыть чат с администратором",
                                          url=f"https://t.me/{_ADMIN_CONTACT_USERNAME}")],
                    [_btn("⬅️ Назад", "welcome_faq")],
                ])
                text = ("👨‍💻 <b>Прямой контакт</b>\n───\n"
                        "Нажмите кнопку ниже, чтобы открыть чат с администратором.")
                await q.message.edit_caption(text, reply_markup=markup)
                return await q.answer()
            if data == "welcome_back":
                await q.message.edit_caption(_WELCOME_TEXT, reply_markup=_WELCOME_KEYBOARD)
                return await q.answer()
            if data == "home":
                self.states.pop(uid, None)
                await self._redraw_dashboard(q)
                return await q.answer()
            if data == "list":
                await q.message.edit_text(self._accounts_text(uid), reply_markup=self._accounts_menu(uid))
                return await q.answer()
            if data == "add_phone":
                self.states[uid] = {
                    "flow": "add", "method": "phone", "step": "phone",
                    "data": {"api_id": int(self.settings["api_id"]), "api_hash": self.settings["api_hash"]},
                }
                await q.message.edit_text("➕ Добавление по номеру.\nОтправь <b>номер</b> "
                                          "в формате +79991234567:")
                return await q.answer()
            if data == "add_session":
                # один шаг принимает И session string текстом, И .session файл документом —
                # тип определяется по самому сообщению, см. _handle_add
                self.states[uid] = {
                    "flow": "add", "method": "session", "step": "session",
                    "data": {"api_id": int(self.settings["api_id"]), "api_hash": self.settings["api_hash"]},
                }
                await q.message.edit_text(
                    "🔑 Добавление по Session.\nПришли <b>session string</b> текстом (Pyrogram/Telethon) "
                    "ИЛИ файл <b>.session</b> документом — определю сам по типу сообщения. Для файла "
                    "api_id/api_hash не нужны (берём те же, что у управляющего бота — если файл создан "
                    "под другим api_id, подключение может не сработать, тогда пришли session string):")
                return await q.answer()
            if data == "allphones":
                await self._start_all_phones(q, uid)
                return
            if data == "broadcast":
                if not self._is_creator(uid):
                    return await q.answer("Только для создателя бота", show_alert=True)
                self.states[uid] = {"flow": "broadcast", "step": "text"}
                await q.message.edit_text(
                    "📣 Текст рассылки для ВСЕХ пользователей бота "
                    "(например, кратко что изменилось в обновлении):")
                return await q.answer()
            if data == "bcast_cancel":
                self.states.pop(uid, None)
                await self._redraw_dashboard(q)
                return await q.answer("❌ Рассылка отменена")
            if data == "bcast_go":
                if not self._is_creator(uid):
                    return await q.answer("Только для создателя бота", show_alert=True)
                state = self.states.get(uid)
                text = (state or {}).get("text")
                if not text:
                    return await q.answer("Текст рассылки потерян, начни заново", show_alert=True)
                self.states.pop(uid, None)
                await q.answer("⏳ Рассылаю...")
                asyncio.create_task(self._run_broadcast(q, uid, text))
                return

            # дальше всё про конкретный аккаунт — проверяем владение
            aid = int(data.split(":")[1])
            acc = self._owned(uid, aid)
            if not acc:
                return await q.answer("Это не ваш аккаунт", show_alert=True)

            if data.startswith("acc:"):
                await self._show(q, acc)
            elif data.startswith("dashstats:"):
                await q.message.edit_text(await self._stats_text(acc, "all"),
                                          reply_markup=self._stats_menu(acc, "all"))
            elif data.startswith("statsperiod:"):
                period_key = data.split(":")[2]
                await q.message.edit_text(await self._stats_text(acc, period_key),
                                          reply_markup=self._stats_menu(acc, period_key))
            elif data.startswith("farm:"):
                await q.message.edit_text(self._farm_text(acc), reply_markup=self._farm_menu(acc))
            elif data.startswith("autosend:"):
                await q.message.edit_text(self._autosend_text(acc), reply_markup=self._autosend_menu(acc))
            elif data.startswith("autom:"):
                await q.message.edit_text(self._automation_text(acc), reply_markup=self._automation_menu(acc))
            elif data.startswith("autgame:"):
                await q.message.edit_text(f"🎮 Игровой цикл — <b>{acc.get('name')}</b>\n───",
                                          reply_markup=self._autom_game_menu(acc))
            elif data.startswith("cardmod:"):
                await q.message.edit_text(await self._cards_module_text(acc), reply_markup=self._cards_module_menu(acc))
            elif data.startswith("autfin:"):
                await q.message.edit_text(f"💰 Финансы — <b>{acc.get('name')}</b>\n───",
                                          reply_markup=self._autom_finance_menu(acc))
            elif data.startswith("autcnt:"):
                await q.message.edit_text(f"📦 Магазин контейнеров — <b>{acc.get('name')}</b>\n───",
                                          reply_markup=self._autom_containers_menu(acc))
            elif data.startswith("autrep:"):
                await q.message.edit_text(f"🛠 Мастерская (ремонт) — <b>{acc.get('name')}</b>\n───",
                                          reply_markup=self._autom_repair_menu(acc))
            elif data.startswith("autfrm:"):
                await q.message.edit_text(f"🔧 Ферма — <b>{acc.get('name')}</b>\n───",
                                          reply_markup=self._autom_farm_menu(acc))
            elif data.startswith("autphn:"):
                await q.message.edit_text(f"🏪 Магазин телефонов — <b>{acc.get('name')}</b>\n───",
                                          reply_markup=self._autom_phoneshop_menu(acc))
            elif data.startswith("phoneshop:"):
                await q.message.edit_text(f"🏪 Настройки автозакупки телефонов — <b>{acc.get('name')}</b>\n───",
                                          reply_markup=self._phone_shop_settings_menu(acc))
            elif data.startswith("intervals:"):
                await q.message.edit_text(self._intervals_text(acc), reply_markup=self._intervals_menu(acc))
            elif data.startswith("actions:"):
                await q.message.edit_text(self._actions_text(acc), reply_markup=self._actions_menu(acc))
            elif data.startswith("actall:"):
                await self._start_run_all(q, aid)
            elif data.startswith("actgame:"):
                await q.message.edit_text(f"🎮 Игра — <b>{acc.get('name')}</b>\n───",
                                          reply_markup=self._act_game_menu(acc))
            elif data.startswith("actfarm:"):
                await q.message.edit_text(f"🔧 Ферма — <b>{acc.get('name')}</b>\n───",
                                          reply_markup=self._act_farm_menu(acc))
            elif data.startswith("actrepair:"):
                await q.message.edit_text(f"🛠 Мастерская — <b>{acc.get('name')}</b>\n───",
                                          reply_markup=self._act_repair_menu(acc))
            elif data.startswith("actshop:"):
                await q.message.edit_text(f"🏪 Магазин — <b>{acc.get('name')}</b>\n───",
                                          reply_markup=self._act_shop_menu(acc))
            elif data.startswith("actfin:"):
                await q.message.edit_text(f"💰 Финансы — <b>{acc.get('name')}</b>\n───",
                                          reply_markup=self._act_fin_menu(acc))
            elif data.startswith("targets:"):
                await q.message.edit_text(f"🎯 Получатели — <b>{acc.get('name')}</b>\n"
                                          f"Введи @username или числовой id получателя.",
                                          reply_markup=self._targets_menu(acc))
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
                await q.message.edit_text(f"🎯 Получатели — <b>{acc.get('name')}</b>\n───",
                                          reply_markup=self._targets_menu(acc))
            elif data.startswith("toggle:"):
                await self._toggle(q, acc, "enabled", redisplay="account")
            elif data.startswith("settings:"):
                await q.message.edit_text(self._settings_text(acc), reply_markup=self._settings_menu(acc))
            elif data.startswith("talerts:"):
                await self._toggle(q, acc, "alerts_enabled", redisplay="settings")
            elif data.startswith("msgacc:"):
                if acc.get("owner_id") not in self.admin_ids:
                    return await q.answer("Доступно только для аккаунтов создателя", show_alert=True)
                await q.message.edit_text(self._msgacc_text(acc), reply_markup=self._msgacc_menu(acc))
            elif data.startswith("tmsgacc:"):
                if acc.get("owner_id") not in self.admin_ids:
                    return await q.answer("Доступно только для аккаунтов создателя", show_alert=True)
                await self._toggle(q, acc, "msg_alert_enabled", redisplay="msgacc")
            elif data.startswith("clrmsgacc:"):
                if acc.get("owner_id") not in self.admin_ids:
                    return await q.answer("Доступно только для аккаунтов создателя", show_alert=True)
                acc["msg_log"] = []
                self.storage.save()
                await q.message.edit_text(self._msgacc_text(acc), reply_markup=self._msgacc_menu(acc))
            elif data.startswith("tfarm:"):
                await self._toggle(q, acc, "farm_enabled", redisplay="farm")
            elif data.startswith("tautosend:"):
                await self._toggle(q, acc, "autosend_enabled", redisplay="autosend")
            elif data.startswith("tcard:"):
                await self._toggle(q, acc, "card_enabled", redisplay="cardmod")
            elif data.startswith("troul:"):
                await self._toggle(q, acc, "roulette_enabled", redisplay="autgame")
            elif data.startswith("tmine:"):
                await self._toggle(q, acc, "mining_enabled", redisplay="autgame")
            elif data.startswith("tdaily:"):
                await self._toggle(q, acc, "daily_reward_enabled", redisplay="autgame")
            elif data.startswith("tpay:"):
                await self._toggle(q, acc, "autopay_enabled", redisplay="autfin")
            elif data.startswith("ttrade:"):
                await self._toggle(q, acc, "autotrade_enabled", redisplay="autfin")
            elif data.startswith("tcontapi:"):
                await self._toggle(q, acc, "containers_api_enabled", redisplay="autcnt")
            elif data.startswith("setcontapipri:"):
                self._ask(uid, aid, "containers_api_priority")
                await q.message.edit_text(
                    "📦⚡ Порядок типов контейнеров при покупке через API, через запятую — "
                    "например «donate,expensive,budget» (по умолчанию). Допустимые значения: "
                    "donate, expensive, budget:")
            elif data.startswith("contapinow:"):
                await self._start_containers_api(q, aid)
            elif data.startswith("tselfcmd:"):
                await self._toggle(q, acc, "self_commands_enabled", redisplay="autosend")
            elif data.startswith("setcard:"):
                self._ask(uid, aid, "card_interval")
                await q.message.edit_text("Отправь интервал карточек в секундах (>=10):")
            elif data.startswith("setroul:"):
                self._ask(uid, aid, "roulette_interval")
                await q.message.edit_text("Отправь интервал рулетки в секундах (>=10):")
            elif data.startswith("setmininterval:"):
                self._ask(uid, aid, "mining_check_interval")
                await q.message.edit_text("Отправь раз во сколько секунд проверять/собирать майнинг (>=60):")
            elif data.startswith("setmtime:"):
                self._ask(uid, aid, "mining_time")
                await q.message.edit_text(
                    "Отправь время ежедневной награды по МСК ЧЧ:ММ (напр. 01:00) — "
                    "на сбор майнинга больше не влияет, у него свой интервал в минутах:"
                )
            elif data.startswith("setpayinterval:"):
                self._ask(uid, aid, "autopay_interval")
                await q.message.edit_text("Отправь интервал авто-вывода очков в секундах (>=60):")
            elif data.startswith("settradeinterval:"):
                self._ask(uid, aid, "autotrade_interval")
                await q.message.edit_text("Отправь интервал авто-трейда в секундах (>=60):")
            elif data.startswith("setpcoininterval:"):
                self._ask(uid, aid, "pcoin_exchange_interval")
                await q.message.edit_text(
                    "Отправь интервал проверки биржи P-Coins (продажа/перевод) в секундах (>=60):")
            elif data.startswith("setpaypercent:"):
                self._ask(uid, aid, "autopay_percent")
                await q.message.edit_text(
                    "Какой процент баланса выводить автовыводом (1-100)? Остальное "
                    "останется на балансе (например, на ремонт телефонов):")
            elif data.startswith("rename:"):
                self._ask(uid, aid, "name")
                await q.message.edit_text("Отправь новое имя аккаунта:")
            elif data.startswith("mine:"):
                await self._collect_mining(q, aid)
            elif data.startswith("exch:"):
                await self._start_trade(q, aid)
            elif data.startswith("payoutall:"):
                await self._start_payout_all(q, aid)
            elif data.startswith("taxx:"):
                await self._start_show_balance(q, aid)
            elif data.startswith("paystart:"):
                self.states[uid] = {"flow": "manualpay", "acc_id": aid, "step": "target"}
                await q.message.edit_text(
                    "💸 Перевод очков человеку.\n"
                    "Введи получателя — <b>@username</b> или <b>числовой id</b>:")
            elif data.startswith("tradestart:"):
                self.states[uid] = {"flow": "manualtrade", "acc_id": aid, "step": "target"}
                await q.message.edit_text(
                    "🤝 Трейд с человеком.\n"
                    "Введи получателя — <b>@username</b> или <b>числовой id</b> "
                    "(разово, независимо от настроенного получателя трейда):")
            elif data.startswith("setdrain:"):
                if not acc.get("is_main"):
                    return await q.answer("Только для 👑 главного аккаунта", show_alert=True)
                self._ask(uid, aid, "drain_amount")
                await q.message.edit_text(
                    "💰 Сумма слива твинкам (очков КАЖДОМУ твинку). "
                    "Пришли число, либо 0 чтобы отключить:")
            elif data.startswith("draindo:"):
                await self._start_drain(q, acc)
            elif data.startswith("collectall:"):
                await self._start_collect_all(q, acc)
            elif data.startswith("trepair:"):
                await self._toggle(q, acc, "auto_repair_enabled", redisplay="autrep")
            elif data.startswith("textworkshop:"):
                await self._toggle(q, acc, "repair_external_workshop_enabled", redisplay="autrep")
            elif data.startswith("setworkshopowner:"):
                self._ask(uid, aid, "repair_external_workshop_owner")
                await q.message.edit_text(
                    "🌍 @username владельца конкретной чужой мастерской — надёжнее имени (ищем по общему "
                    "списку мастерских, а не встроенным поиском игры, который не всегда находит "
                    "декорированные эмодзи имена). Если своих мастерских несколько — перечисли через "
                    "запятую (напр. «jstrzw, jstrzw2»), возьмём первую найденную по счёту. Пришли «-» "
                    "чтобы очистить:")
            elif data.startswith("setworkshop:"):
                self._ask(uid, aid, "repair_external_workshop_name")
                await q.message.edit_text(
                    "🌍 Имя конкретной чужой мастерской (поиск игры «найти по названию»). Пришли «-» "
                    "чтобы очистить — тогда при нехватке своего инструмента (и если владелец выше не "
                    "задан/не найден) будет браться первая мастерская из списка:")
            elif data.startswith("taccept:"):
                await self._toggle(q, acc, "auto_accept_enabled", redisplay="autrep")
            elif data.startswith("treview:"):
                await self._toggle(q, acc, "auto_review_enabled", redisplay="autrep")
            elif data.startswith("tfarmmaint:"):
                await self._toggle(q, acc, "farm_maintenance_enabled", redisplay="autfrm")
            elif data.startswith("setfarmtimes:"):
                self._ask(uid, aid, "farm_maintenance_interval")
                await q.message.edit_text("Отправь раз во сколько секунд проверять обслуживание фермы (>=60):")
            elif data.startswith("setfarmtarget:"):
                self._ask(uid, aid, "farm_target_phones")
                await q.message.edit_text(
                    "🎯 Сколько телефонов должно стоять на ферме одновременно (число) — если уже занято "
                    "от этого числа, пополнение новыми не запускается и ферму не трогаем:")
            elif data.startswith("setfarmfill:"):
                self._ask(uid, aid, "farm_fill_model")
                await q.message.edit_text(
                    "📱 Модель для пополнения пустых слотов, у которых ещё нет «памяти» (как называется "
                    "модель в игре, например «Samsung Galaxy Z TriFold»). Пришли «-» чтобы очистить:")
            elif data.startswith("setfarmfillrarity:"):
                self._ask(uid, aid, "farm_fill_rarity")
                await q.message.edit_text(
                    "🏪 Редкость в «Магазине телефонов», где искать модель для докупки кнопкой «Заполнить "
                    "ферму» (напр. Ширпотреб, Необычный, Редкий, Мистический, Хроматический, Аркана, "
                    "Платиновый). Пришли «-», чтобы брать текущую редкость автозакупки как есть:")
            elif data.startswith("texchange:"):
                await self._toggle(q, acc, "pcoin_exchange_enabled", redisplay="autfrm")
            elif data.startswith("tpcoinsend:"):
                await self._toggle(q, acc, "pcoin_send_enabled", redisplay="autfrm")
            elif data.startswith("tpowerwatch:"):
                await self._toggle(q, acc, "power_watchdog_enabled", redisplay="autfrm")
            elif data.startswith("setpowerwatch:"):
                self._ask(uid, aid, "power_watchdog_interval")
                await q.message.edit_text(
                    "⚡ Раз во сколько секунд проверять питание/охлаждение фермы на аварийную "
                    "перегрузку (число, >=60):")
            elif data.startswith("tshop:"):
                await self._toggle(q, acc, "phone_shop_enabled", redisplay="autphn")
            elif data.startswith("setrarity:"):
                self._ask(uid, aid, "phone_shop_rarity")
                await q.message.edit_text(
                    "Редкость для автозакупки — как называется кнопка в магазине "
                    "(напр. Ширпотреб, Необычный, Редкий, Мистический, Хроматический, Аркана, Платиновый):")
            elif data.startswith("setqty:"):
                self._ask(uid, aid, "phone_shop_quantity")
                await q.message.edit_text("Сколько покупать за раз (число), либо 0 — максимум доступного:")
            elif data.startswith("repairnow:"):
                await self._start_repair_now(q, aid)
            elif data.startswith("repaireqnow:"):
                await self._start_repair_equipment_now(q, aid)
            elif data.startswith("farmmaintnow:"):
                await self._start_farm_maintenance_now(q, aid)
            elif data.startswith("fillfarmnow:"):
                await self._start_fill_farm(q, aid)
            elif data.startswith("shopnow:"):
                await self._start_shop_now(q, aid)
            elif data.startswith("achieve:"):
                await self._start_achievements(q, aid)
            elif data.startswith("setflipprice:"):
                self._ask(uid, aid, "avito_flip_price")
                await q.message.edit_text(
                    "📢 Цена объявления на Авито (число ТОчек) — должна быть заметно выше официальной/"
                    "закупочной цены Ширпотреба (по умолчанию 5000), чтобы засчитались "
                    "«Рыночек зарешал»/«Барыга»:")
            elif data.startswith("setachievemin:"):
                self._ask(uid, aid, "achievements_min_balance")
                await q.message.edit_text(
                    "📜 Минимальный баланс «Такк» (ТОчки), при котором «Выполнить достижения» вообще "
                    "запускается (число, по умолчанию 150000):")
            elif data.startswith("upgradeacc:"):
                await self._start_upgrade_account(q, aid)
            elif data.startswith("pcoincheck:"):
                await self._start_dump_pcoins(q, aid)
            elif data.startswith("pcoinsendnow:"):
                await self._start_send_pcoins(q, aid)
            elif data.startswith("powerwatchnow:"):
                await self._start_power_watchdog(q, aid)
            elif data.startswith("capgo:"):
                cat = data.split(":")[2]
                w = self.manager.workers.get(aid)
                if not w or not w.running:
                    return await q.answer("Аккаунт не запущен", show_alert=True)
                await q.answer("⏳ Пробую открыть магазин...")
                asyncio.create_task(self._run_resume_containers(q, aid, w, cat))
                return
            elif data.startswith("capskip:"):
                w = self.manager.workers.get(aid)
                if w:
                    retry = int(self.settings.get("containers", {}).get("unknown_retry", 600))
                    w.container_next_ts = time.time() + retry
                await q.answer("Пропущено")
                await q.message.edit_text(f"⏭ Пропущено — «{acc.get('name')}»",
                                          reply_markup=self._account_menu(acc))
                return
            elif data.startswith("del:"):
                await q.message.edit_text(
                    "Точно удалить аккаунт?",
                    reply_markup=InlineKeyboardMarkup([[
                        _btn("✅ Да", f"delyes:{aid}"), _btn("❌ Нет", f"settings:{aid}")]]))
            elif data.startswith("delyes:"):
                await self.manager.stop_account(aid)
                self.storage.remove(aid)
                await q.message.edit_text("🗑 Удалён.", reply_markup=self._accounts_menu(uid))

            elif data.startswith("tasks:"):
                await q.message.edit_text(f"🧩 Задачи — <b>{acc.get('name')}</b>\n───",
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
                w = self.manager.workers.get(aid)
                if w:
                    # если этот id переиспользуют для новой задачи (_handle_addtask
                    # берёт max(existing id)+1 — удалённый id снова свободен), воркер
                    # не должен унаследовать старое время следующего запуска
                    w._task_next.pop(tid, None)
                await q.message.edit_text(f"🧩 Задачи — <b>{acc.get('name')}</b>\n───",
                                          reply_markup=self._tasks_menu(acc))
            await q.answer()
        except MessageNotModified:
            # содержимое (текст+клавиатура) не изменилось с прошлого раза — не ошибка
            # пользователя, просто нечего перерисовывать (например, «Обновить» без
            # новых данных)
            try:
                await q.answer()
            except Exception:
                pass
        except Exception as e:  # noqa: BLE001
            try:
                await q.answer(f"Ошибка: {e}", show_alert=True)
            except Exception:
                pass

    async def _show(self, q: CallbackQuery, acc: dict) -> None:
        text, markup = self._account_text(acc), self._account_menu(acc)
        if self.render_engine.should_render(q.message.chat.id, f"account:{acc['id']}", text, markup):
            await self._safe_edit(q, text, markup)

    def _ask(self, uid: int, aid: int, field: str) -> None:
        self.states[uid] = {"flow": "set", "field": field, "acc_id": aid}

    async def _toggle(self, q: CallbackQuery, acc: dict, field: str, redisplay: str = "automation") -> None:
        default = False if field in (
            "autotrade_enabled",
            "auto_repair_enabled", "auto_accept_enabled", "phone_shop_enabled",
            "msg_alert_enabled", "farm_maintenance_enabled",
            "repair_external_workshop_enabled", "pcoin_exchange_enabled", "pcoin_send_enabled",
            "auto_review_enabled", "power_watchdog_enabled", "containers_api_enabled",
        ) else True
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
        elif redisplay == "msgacc":
            await q.message.edit_text(self._msgacc_text(acc), reply_markup=self._msgacc_menu(acc))
        elif redisplay == "settings":
            await q.message.edit_text(self._settings_text(acc), reply_markup=self._settings_menu(acc))
        elif redisplay == "autgame":
            await q.message.edit_text(f"🎮 Игровой цикл — <b>{acc.get('name')}</b>\n───",
                                      reply_markup=self._autom_game_menu(acc))
        elif redisplay == "cardmod":
            await q.message.edit_text(await self._cards_module_text(acc), reply_markup=self._cards_module_menu(acc))
        elif redisplay == "autfin":
            await q.message.edit_text(f"💰 Финансы — <b>{acc.get('name')}</b>\n───",
                                      reply_markup=self._autom_finance_menu(acc))
        elif redisplay == "autcnt":
            await q.message.edit_text(f"📦 Магазин контейнеров — <b>{acc.get('name')}</b>\n───",
                                      reply_markup=self._autom_containers_menu(acc))
        elif redisplay == "autrep":
            await q.message.edit_text(f"🛠 Мастерская (ремонт) — <b>{acc.get('name')}</b>\n───",
                                      reply_markup=self._autom_repair_menu(acc))
        elif redisplay == "autfrm":
            await q.message.edit_text(f"🔧 Ферма — <b>{acc.get('name')}</b>\n───",
                                      reply_markup=self._autom_farm_menu(acc))
        elif redisplay == "autphn":
            await q.message.edit_text(f"🏪 Магазин телефонов — <b>{acc.get('name')}</b>\n───",
                                      reply_markup=self._autom_phoneshop_menu(acc))
        else:  # "automation" — верхний уровень (группы)
            await q.message.edit_text(self._automation_text(acc), reply_markup=self._automation_menu(acc))

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

    async def _run_resume_containers(self, q: CallbackQuery, aid: int, w, cat: str) -> None:
        result = await w.resume_container_check(prefer=None if cat == "default" else cat)
        acc = self.storage.get(aid)
        await self._safe_edit(q, self._account_text(acc) + f"\n\n📦 {result}",
                              self._account_menu(acc))

    async def _start_show_balance(self, q: CallbackQuery, aid: int) -> None:
        w = self.manager.workers.get(aid)
        if not w or not w.running:
            return await q.answer("Аккаунт не запущен", show_alert=True)
        await q.answer("⏳ Запрашиваю такс...")
        asyncio.create_task(self._run_show_balance(q, aid, w))

    async def _run_show_balance(self, q: CallbackQuery, aid: int, w) -> None:
        info = await w.fetch_balance_info()
        acc = self.storage.get(aid)
        text = f"{self._account_text(acc)}\n\n🧾 <b>Такс:</b>\n{info}"
        await self._safe_edit(q, text, self._account_menu(acc))

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

    async def _start_payout_all(self, q: CallbackQuery, aid: int) -> None:
        w = self.manager.workers.get(aid)
        if not w or not w.running:
            return await q.answer("Аккаунт не запущен", show_alert=True)
        await q.answer("⏳ Вывожу всё...")
        asyncio.create_task(self._run_payout_all(q, aid, w))

    async def _run_payout_all(self, q: CallbackQuery, aid: int, w) -> None:
        result = await w.payout_all_now()
        acc = self.storage.get(aid)
        await self._safe_edit(q, self._account_text(acc) + f"\n\n💸 {result}",
                              self._act_fin_menu(acc))

    @staticmethod
    def _twink_ident(acc: dict) -> str:
        """Числовой tg_id надёжнее username (тот можно сменить/потерять) — тот же
        принцип, что и в Manager.copy_targets."""
        if acc.get("tg_id"):
            return str(acc["tg_id"])
        return f"@{acc['username']}" if acc.get("username") else ""

    async def _start_drain(self, q: CallbackQuery, acc: dict) -> None:
        """💧 Слив: 👑 главный аккаунт рассылает фиксированную сумму (drain_amount)
        каждому своему остальному аккаунту (твинку) — вручную по кнопке, обычно
        перед контами. Сумма настраивается отдельной кнопкой «💰 Сумма слива»."""
        if not acc.get("is_main"):
            return await q.answer("Только для 👑 главного аккаунта", show_alert=True)
        amount = int(acc.get("drain_amount", 0) or 0)
        if amount <= 0:
            return await q.answer("Сначала задай сумму слива (кнопка выше)", show_alert=True)
        w = self.manager.workers.get(acc["id"])
        if not w or not w.running:
            return await q.answer("Аккаунт не запущен", show_alert=True)
        twinks = [a for a in self._my_accounts(acc.get("owner_id")) if a["id"] != acc["id"]]
        if not twinks:
            return await q.answer("Нет других аккаунтов (твинков)", show_alert=True)
        await q.answer(f"⏳ Сливаю по {amount} на {len(twinks)} твинк(ов)...")
        asyncio.create_task(self._run_drain(q, acc["id"], w, twinks, amount))

    async def _run_drain(self, q: CallbackQuery, aid: int, w, twinks: list[dict], amount: int) -> None:
        results = []
        for t in twinks:
            ident = self._twink_ident(t)
            if not ident:
                results.append(f"• {t.get('name')}: ⚠️ нет username/id, пропущен")
                continue
            res = await w.manual_pay(ident, amount)
            results.append(f"• {t.get('name')}: {res}")
        acc = self.storage.get(aid)
        text = self._account_text(acc) + "\n\n💧 <b>Слив твинкам:</b>\n" + "\n".join(results)
        await self._safe_edit(q, text, self._account_menu(acc))

    async def _start_collect_all(self, q: CallbackQuery, acc: dict) -> None:
        """📥 Собрать всё с твинков — обратное «💧 Слить твинкам»: КАЖДЫЙ твинк сам
        выводит 100% очков, трейдит телефоны и переводит P-Coins на главный аккаунт,
        независимо от своих обычно настроенных получателей (target_override), по очереди."""
        if not acc.get("is_main"):
            return await q.answer("Только для 👑 главного аккаунта", show_alert=True)
        main_ident = self._twink_ident(acc)
        if not main_ident:
            return await q.answer("У главного аккаунта нет username/tg_id — некуда собирать", show_alert=True)
        twinks = [a for a in self._my_accounts(acc.get("owner_id")) if a["id"] != acc["id"]]
        if not twinks:
            return await q.answer("Нет других аккаунтов (твинков)", show_alert=True)
        await q.answer(f"⏳ Собираю с {len(twinks)} твинк(ов) на главный (это надолго)...")
        asyncio.create_task(self._run_collect_all(q, acc["id"], twinks, main_ident))

    async def _run_collect_all(self, q: CallbackQuery, aid: int, twinks: list[dict], main_ident: str) -> None:
        results = []
        for t in twinks:
            tw = self.manager.workers.get(t["id"])
            if not tw or not tw.running:
                results.append(f"• {t.get('name')}: аккаунт не запущен, пропущен")
                continue
            pay_res = await tw.payout_to_now(main_ident)
            trade_res = await self.manager.run_trade(t["id"], target_override=main_ident)
            pcoin_res = await tw.send_pcoins_to_now(main_ident)
            # следующий авто-цикл этого твинка не должен сработать почти сразу же —
            # сдвигаем его так же, как это делают сами _autopay_loop/_autotrade_loop/
            # _pcoin_exchange_loop после обычного срабатывания (см. farm.py)
            tw.autopay_next_ts = time.time() + max(60, int(t.get("autopay_interval", 14400)))
            tw.autotrade_next_ts = time.time() + max(60, int(t.get("autotrade_interval", 14400)))
            tw.pcoin_exchange_next_ts = time.time() + max(60, int(t.get("pcoin_exchange_interval", 14400)))
            results.append(f"• {t.get('name')}: 💸 {pay_res} | 🔄 {trade_res} | 💱 {pcoin_res}")
        acc = self.storage.get(aid)
        text = self._account_text(acc) + "\n\n📥 <b>Собрано с твинков:</b>\n" + "\n".join(results)
        await self._safe_edit(q, text, self._account_menu(acc))

    async def _start_repair_now(self, q: CallbackQuery, aid: int) -> None:
        w = self.manager.workers.get(aid)
        if not w or not w.running:
            return await q.answer("Аккаунт не запущен", show_alert=True)
        await q.answer("⏳ Проверяю нерабочие телефоны...")
        asyncio.create_task(self._run_repair_now(q, aid, w))

    async def _run_repair_now(self, q: CallbackQuery, aid: int, w) -> None:
        result = await w.repair_now()
        acc = self.storage.get(aid)
        await self._safe_edit(q, self._account_text(acc) + f"\n\n🛠 {result}", self._account_menu(acc))

    async def _start_repair_equipment_now(self, q: CallbackQuery, aid: int) -> None:
        w = self.manager.workers.get(aid)
        if not w or not w.running:
            return await q.answer("Аккаунт не запущен", show_alert=True)
        await q.answer("⏳ Чиню оборудование мастерской...")
        asyncio.create_task(self._run_repair_equipment_now(q, aid, w))

    async def _run_repair_equipment_now(self, q: CallbackQuery, aid: int, w) -> None:
        result = await w.repair_equipment_now()
        acc = self.storage.get(aid)
        await self._safe_edit(q, f"🛠 Мастерская — <b>{acc.get('name')}</b>\n───\n🔩 {result}",
                              self._act_repair_menu(acc))

    async def _start_run_all(self, q: CallbackQuery, aid: int) -> None:
        w = self.manager.workers.get(aid)
        if not w or not w.running:
            return await q.answer("Аккаунт не запущен", show_alert=True)
        await q.answer("⚡️ Запускаю полный цикл...")
        asyncio.create_task(self._run_all_now(q, aid, w))

    async def _run_all_now(self, q: CallbackQuery, aid: int, w) -> None:
        """«Запустить всё» — прогоняет по очереди все безопасные (без ручного ввода
        получателя/суммы) команды «...сейчас» из всех разделов. Намеренно НЕ трогает
        Финансы (перевод человеку/трейд/слив телефонов — нужен получатель; вывод всего/
        слив твинкам — реально перемещают деньги) — это одноразовые действия с явным
        подтверждением, а не часть автоматического цикла фарма."""
        acc = self.storage.get(aid)
        results: list[str] = []

        async def step(label: str, coro) -> None:
            try:
                res = await coro
            except Exception as e:  # noqa: BLE001
                res = f"ошибка: {e}"
            results.append(f"{label}: {res}")

        await w.collect_mining()
        results.append(f"⛏ Майнинг: {w.last_mining}")
        await step("🔧 Обслуживание фермы", w.farm_maintenance_now())
        await step("🧩 Заполнить ферму", w.fill_farm_now())
        await step("⚡ Перегрузка питания", w.relieve_overload_now())
        if acc.get("pcoin_exchange_enabled", False):
            await step("💱 P-Coins → ТОчки", w.dump_pcoins_now())
        if acc.get("pcoin_send_enabled", False):
            await step("💱 P-Coins получателю", w.send_pcoins_now())
        await step("🛠 Нерабочие телефоны", w.repair_now())
        await step("🔩 Оборудование", w.repair_equipment_now())
        await step("🏪 Магазин телефонов", w.buy_phones_now())
        if acc.get("containers_api_enabled", False):
            await step("📦⚡ Контейнеры", w.buy_containers_api_now())

        acc = self.storage.get(aid)  # могло измениться (напр. name) за время выполнения
        summary = "\n".join(f"• {r}" for r in results)
        await self._safe_edit(
            q, f"⚡️ <b>Запустить всё</b> — {acc.get('name')}\n───\n{summary}",
            self._actions_menu(acc),
        )

    async def _start_farm_maintenance_now(self, q: CallbackQuery, aid: int) -> None:
        w = self.manager.workers.get(aid)
        if not w or not w.running:
            return await q.answer("Аккаунт не запущен", show_alert=True)
        await q.answer("⏳ Обслуживаю ферму (снимаю сломанные / возвращаю починенные)...")
        asyncio.create_task(self._run_farm_maintenance_now(q, aid, w))

    async def _run_farm_maintenance_now(self, q: CallbackQuery, aid: int, w) -> None:
        result = await w.farm_maintenance_now()
        acc = self.storage.get(aid)
        await self._safe_edit(q, self._account_text(acc) + f"\n\n🔧 {result}", self._act_farm_menu(acc))

    async def _start_fill_farm(self, q: CallbackQuery, aid: int) -> None:
        w = self.manager.workers.get(aid)
        if not w or not w.running:
            return await q.answer("Аккаунт не запущен", show_alert=True)
        await q.answer("⏳ Проверяю пустые слоты, докупаю недостающее...")
        asyncio.create_task(self._run_fill_farm(q, aid, w))

    async def _run_fill_farm(self, q: CallbackQuery, aid: int, w) -> None:
        result = await w.fill_farm_now()
        acc = self.storage.get(aid)
        await self._safe_edit(q, self._account_text(acc) + f"\n\n🧩 {result}", self._act_farm_menu(acc))

    async def _start_shop_now(self, q: CallbackQuery, aid: int) -> None:
        w = self.manager.workers.get(aid)
        if not w or not w.running:
            return await q.answer("Аккаунт не запущен", show_alert=True)
        await q.answer("⏳ Покупаю телефоны...")
        asyncio.create_task(self._run_shop_now(q, aid, w))

    async def _run_shop_now(self, q: CallbackQuery, aid: int, w) -> None:
        result = await w.buy_phones_now()
        acc = self.storage.get(aid)
        await self._safe_edit(q, self._account_text(acc) + f"\n\n🏪 {result}", self._account_menu(acc))

    async def _start_achievements(self, q: CallbackQuery, aid: int) -> None:
        w = self.manager.workers.get(aid)
        if not w or not w.running:
            return await q.answer("Аккаунт не запущен", show_alert=True)
        await q.answer("⏳ Проверяю баланс и выполняю достижения...")
        asyncio.create_task(self._run_achievements(q, aid, w))

    async def _run_achievements(self, q: CallbackQuery, aid: int, w) -> None:
        result = await w.complete_achievements_now()
        acc = self.storage.get(aid)
        await self._safe_edit(q, self._account_text(acc) + f"\n\n📜 {result}", self._act_game_menu(acc))

    async def _start_dump_pcoins(self, q: CallbackQuery, aid: int) -> None:
        w = self.manager.workers.get(aid)
        if not w or not w.running:
            return await q.answer("Аккаунт не запущен", show_alert=True)
        await q.answer("⏳ Пробую биржу...")
        asyncio.create_task(self._run_dump_pcoins(q, aid, w))

    async def _run_dump_pcoins(self, q: CallbackQuery, aid: int, w) -> None:
        result = await w.dump_pcoins_now()
        acc = self.storage.get(aid)
        await self._safe_edit(q, self._account_text(acc) + f"\n\n💱 {result}", self._act_farm_menu(acc))

    async def _start_send_pcoins(self, q: CallbackQuery, aid: int) -> None:
        w = self.manager.workers.get(aid)
        if not w or not w.running:
            return await q.answer("Аккаунт не запущен", show_alert=True)
        await q.answer("⏳ Пробую перевод P-Coins...")
        asyncio.create_task(self._run_send_pcoins(q, aid, w))

    async def _run_send_pcoins(self, q: CallbackQuery, aid: int, w) -> None:
        result = await w.send_pcoins_now()
        acc = self.storage.get(aid)
        await self._safe_edit(q, self._account_text(acc) + f"\n\n💱 {result}", self._act_farm_menu(acc))

    async def _start_power_watchdog(self, q: CallbackQuery, aid: int) -> None:
        w = self.manager.workers.get(aid)
        if not w or not w.running:
            return await q.answer("Аккаунт не запущен", show_alert=True)
        await q.answer("⏳ Проверяю питание/охлаждение...")
        asyncio.create_task(self._run_power_watchdog(q, aid, w))

    async def _run_power_watchdog(self, q: CallbackQuery, aid: int, w) -> None:
        result = await w.relieve_overload_now()
        acc = self.storage.get(aid)
        await self._safe_edit(q, self._account_text(acc) + f"\n\n⚡ {result}", self._act_farm_menu(acc))

    async def _start_containers_api(self, q: CallbackQuery, aid: int) -> None:
        w = self.manager.workers.get(aid)
        if not w or not w.running:
            return await q.answer("Аккаунт не запущен", show_alert=True)
        await q.answer("⏳ Проверяю магазин контейнеров...")
        asyncio.create_task(self._run_containers_api(q, aid, w))

    async def _run_containers_api(self, q: CallbackQuery, aid: int, w) -> None:
        result = await w.buy_containers_api_now()
        acc = self.storage.get(aid)
        await self._safe_edit(q, self._account_text(acc) + f"\n\n📦 {result}",
                              self._autom_containers_menu(acc))

    async def _start_upgrade_account(self, q: CallbackQuery, aid: int) -> None:
        w = self.manager.workers.get(aid)
        if not w or not w.running:
            return await q.answer("Аккаунт не запущен", show_alert=True)
        await q.answer("⏳ Прокачиваю аккаунт (магазин улучшений)...")
        asyncio.create_task(self._run_upgrade_account(q, aid, w))

    async def _run_upgrade_account(self, q: CallbackQuery, aid: int, w) -> None:
        result = await w.upgrade_account()
        acc = self.storage.get(aid)
        await self._safe_edit(q, self._account_text(acc) + f"\n\n💰 {result}", self._act_fin_menu(acc))

    async def _run_broadcast(self, q: CallbackQuery, uid: int, text: str) -> None:
        owners = {a.get("owner_id") for a in self.storage.accounts if a.get("owner_id")}
        sent, failed = 0, 0
        for owner_id in owners:
            try:
                await self.app.send_message(owner_id, f"📣 {text}")
                sent += 1
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"[broadcast] не удалось отправить {owner_id}: {e}")
        await self._safe_edit(
            q, f"📣 Рассылка готова: доставлено {sent}, не удалось {failed}.",
            self._main_menu(uid))

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
        except MessageNotModified:
            pass  # содержимое не изменилось — нечего перерисовывать, это не ошибка
        except Exception:
            try:
                await q.message.reply(text)
            except Exception:
                pass

    # ============ текстовый ввод (FSM) ============
    async def _on_text(self, m: Message) -> None:
        uid = m.from_user.id
        state = self.states.get(uid)
        if not state:
            return
        if state.get("flow") == "set":
            await self._handle_set(m, state)
        elif state.get("flow") == "add":
            await self._handle_add(m, state)
        elif state.get("flow") == "addtask":
            await self._handle_addtask(m, state)
        elif state.get("flow") == "broadcast":
            await self._handle_broadcast(m, state)
        elif state.get("flow") == "manualpay":
            await self._handle_manual_pay(m, state)
        elif state.get("flow") == "manualtrade":
            await self._handle_manual_trade(m, state)

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
        elif field in ("autopay_interval", "autotrade_interval", "mining_check_interval",
                       "pcoin_exchange_interval"):
            if not val.isdigit() or int(val) < 60:
                await m.reply("Нужно число секунд (>=60). Ещё раз:")
                return
            acc[field] = int(val)
            self.storage.save()
            self.states.pop(uid, None)
            await m.reply(self._intervals_text(acc), reply_markup=self._intervals_menu(acc))
            return
        elif field == "drain_amount":
            if not val.isdigit():
                await m.reply("Нужно число очков (0 — отключить). Ещё раз:")
                return
            acc[field] = int(val)
        elif field in ("repair_external_workshop_name", "repair_external_workshop_owner"):
            if field == "repair_external_workshop_owner" and val not in ("-", "—", "нет"):
                val = ", ".join(p.strip().lstrip("@") for p in val.split(",") if p.strip())
            acc[field] = "" if val in ("-", "—", "нет") else val
            self.storage.save()
            self.states.pop(uid, None)
            await m.reply(f"🛠 Мастерская (ремонт) — <b>{acc.get('name')}</b>\n───",
                          reply_markup=self._autom_repair_menu(acc))
            return
        elif field == "containers_api_priority":
            parts = [p.strip().lower() for p in val.split(",") if p.strip()]
            valid = [p for p in parts if p in ("donate", "expensive", "budget")]
            if not valid:
                await m.reply("Нужны значения donate/expensive/budget через запятую. Ещё раз:")
                return
            acc[field] = ",".join(valid)
            self.storage.save()
            self.states.pop(uid, None)
            await m.reply(f"📦 Магазин контейнеров — <b>{acc.get('name')}</b>\n───",
                          reply_markup=self._autom_containers_menu(acc))
            return
        elif field == "farm_maintenance_interval":
            if not val.isdigit() or int(val) < 60:
                await m.reply("Нужно число секунд (>=60). Ещё раз:")
                return
            acc[field] = int(val)
            self.storage.save()
            self.states.pop(uid, None)
            await m.reply(f"🔧 Обслуживание фермы — <b>{acc.get('name')}</b>\n───",
                          reply_markup=self._autom_farm_menu(acc))
            return
        elif field == "farm_target_phones":
            if not val.isdigit():
                await m.reply("Нужно число. Ещё раз:")
                return
            acc[field] = int(val)
            self.storage.save()
            self.states.pop(uid, None)
            await m.reply(f"🔧 Обслуживание фермы — <b>{acc.get('name')}</b>\n───",
                          reply_markup=self._autom_farm_menu(acc))
            return
        elif field in ("farm_fill_model", "farm_fill_rarity"):
            acc[field] = "" if val in ("-", "—", "нет") else val
            self.storage.save()
            self.states.pop(uid, None)
            await m.reply(f"🔧 Обслуживание фермы — <b>{acc.get('name')}</b>\n───",
                          reply_markup=self._autom_farm_menu(acc))
            return
        elif field == "power_watchdog_interval":
            if not val.isdigit() or int(val) < 60:
                await m.reply("Нужно число секунд (>=60). Ещё раз:")
                return
            acc[field] = int(val)
            self.storage.save()
            self.states.pop(uid, None)
            await m.reply(f"🔧 Обслуживание фермы — <b>{acc.get('name')}</b>\n───",
                          reply_markup=self._autom_farm_menu(acc))
            return
        elif field in ("avito_flip_price", "achievements_min_balance"):
            if not val.isdigit() or int(val) <= 0:
                await m.reply("Нужно положительное число ТОчек. Ещё раз:")
                return
            acc[field] = int(val)
            self.storage.save()
            self.states.pop(uid, None)
            await m.reply(f"🎮 Игра — <b>{acc.get('name')}</b>\n───", reply_markup=self._act_game_menu(acc))
            return
        elif field == "phone_shop_rarity":
            if not val:
                await m.reply("Не пусто. Ещё раз:")
                return
            acc[field] = val
            self.storage.save()
            self.states.pop(uid, None)
            await m.reply(f"🏪 Настройки автозакупки телефонов — <b>{acc.get('name')}</b>\n───",
                          reply_markup=self._phone_shop_settings_menu(acc))
            return
        elif field == "phone_shop_quantity":
            if not val.isdigit():
                await m.reply("Нужно число (0 — максимум доступного). Ещё раз:")
                return
            acc[field] = int(val)
            self.storage.save()
            self.states.pop(uid, None)
            await m.reply(f"🏪 Настройки автозакупки телефонов — <b>{acc.get('name')}</b>\n───",
                          reply_markup=self._phone_shop_settings_menu(acc))
            return
        elif field == "autopay_percent":
            if not val.isdigit() or not (1 <= int(val) <= 100):
                await m.reply("Нужно число от 1 до 100 (%). Ещё раз:")
                return
            acc[field] = int(val)
            self.storage.save()
            self.states.pop(uid, None)
            await m.reply(f"💰 Финансы — <b>{acc.get('name')}</b>\n───",
                          reply_markup=self._autom_finance_menu(acc))
            return
        elif field == "mining_time":
            mt = re.match(r"^\s*(\d{1,2})[:.\s](\d{1,2})\s*$", val)
            if not mt or not (0 <= int(mt.group(1)) < 24 and 0 <= int(mt.group(2)) < 60):
                await m.reply("Формат ЧЧ:ММ, напр. 01:00. Ещё раз:")
                return
            acc[field] = f"{int(mt.group(1)):02d}:{int(mt.group(2)):02d}"
            self.storage.save()
            self.states.pop(uid, None)
            await m.reply(self._intervals_text(acc), reply_markup=self._intervals_menu(acc))
            return
        elif field in ("payout_target", "trade_target"):
            if val in ("-", "—", "нет"):
                acc[field] = ""
            elif " " in val or len(val) > 64:
                await m.reply("Один токен без пробелов — @username или числовой id. Ещё раз:")
                return
            else:
                bare = val.lstrip("@").strip()
                if not bare:
                    await m.reply("Пусто. Нужен @username или числовой id. Ещё раз:")
                    return
                # цифры — считаем это id как есть; иначе username, сами добавим @
                acc[field] = bare if bare.isdigit() else f"@{bare}"
            self.storage.save()
            self.states.pop(uid, None)
            await m.reply(f"🎯 Получатели — <b>{acc.get('name')}</b>\n───",
                          reply_markup=self._targets_menu(acc))
            return
        else:  # name
            acc[field] = val
        self.storage.save()
        self.states.pop(uid, None)
        await m.reply(self._account_text(acc), reply_markup=self._account_menu(acc))

    async def _handle_broadcast(self, m: Message, state: dict) -> None:
        uid = m.from_user.id
        if not self._is_creator(uid):
            self.states.pop(uid, None)
            return
        text = m.text.strip()
        if not text:
            await m.reply("Пустой текст. Ещё раз:")
            return
        state["text"] = text
        owners = {a.get("owner_id") for a in self.storage.accounts if a.get("owner_id")}
        await m.reply(
            f"📣 Получат {len(owners)} пользователь(ей):\n\n{text}\n\nОтправляем?",
            reply_markup=InlineKeyboardMarkup([[
                _btn("✅ Отправить всем", "bcast_go"), _btn("❌ Отмена", "bcast_cancel")]]))

    @staticmethod
    def _parse_target(val: str) -> str | None:
        bare = val.strip().lstrip("@").strip()
        if not bare or " " in bare:
            return None
        return bare if bare.isdigit() else f"@{bare}"

    async def _handle_manual_pay(self, m: Message, state: dict) -> None:
        uid = m.from_user.id
        acc = self._owned(uid, state["acc_id"])
        if not acc:
            self.states.pop(uid, None)
            return
        val = m.text.strip()
        if state["step"] == "target":
            target = self._parse_target(val)
            if not target:
                await m.reply("Нужен @username или числовой id, без пробелов. Ещё раз:")
                return
            state["target"] = target
            state["step"] = "amount"
            await m.reply("Сколько очков перевести (число)?")
            return
        if not val.isdigit() or int(val) <= 0:
            await m.reply("Нужно положительное число очков. Ещё раз:")
            return
        amount = int(val)
        target = state["target"]
        self.states.pop(uid, None)
        w = self.manager.workers.get(acc["id"])
        if not w or not w.running:
            await m.reply("⚠️ Аккаунт не запущен.", reply_markup=self._act_fin_menu(acc))
            return
        await m.reply(f"⏳ Перевожу {amount} -> {target}...")
        asyncio.create_task(self._run_manual_pay(m, acc["id"], w, target, amount))

    async def _run_manual_pay(self, m: Message, aid: int, w, target: str, amount: int) -> None:
        result = await w.manual_pay(target, amount)
        acc = self.storage.get(aid)
        await m.reply(f"💸 {result}", reply_markup=self._act_fin_menu(acc))

    async def _handle_manual_trade(self, m: Message, state: dict) -> None:
        uid = m.from_user.id
        acc = self._owned(uid, state["acc_id"])
        if not acc:
            self.states.pop(uid, None)
            return
        target = self._parse_target(m.text.strip())
        if not target:
            await m.reply("Нужен @username или числовой id, без пробелов. Ещё раз:")
            return
        self.states.pop(uid, None)
        w = self.manager.workers.get(acc["id"])
        if not w or not w.running:
            await m.reply("⚠️ Аккаунт не запущен.", reply_markup=self._act_fin_menu(acc))
            return
        await m.reply(f"⏳ Запускаю трейд с {target} (это надолго)...")
        asyncio.create_task(self._run_manual_trade(m, acc["id"], target))

    async def _run_manual_trade(self, m: Message, aid: int, target: str) -> None:
        result = await self.manager.run_trade(aid, target_override=target)
        acc = self.storage.get(aid)
        await m.reply(f"🤝 {result}", reply_markup=self._act_fin_menu(acc))

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
        # шаг "session" принимает И session string текстом, И .session файл документом —
        # объединённая точка входа «🔑 По Pyrogram / Telethon Session» (см. add_session);
        # документ проверяем ДО val=... (m.text может быть None для файла)
        if step == "session" and m.document:
            await self._handle_add_sessionfile(m, state)
            return
        val = (m.text or "").strip()
        try:
            if step == "session":
                if not val:
                    await m.reply("Пришли session string текстом ИЛИ файл .session документом. "
                                  "Ещё раз, либо /start чтобы отменить:")
                    return
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

    async def _handle_add_sessionfile(self, m: Message, state: dict) -> None:
        """Добавление аккаунта загрузкой готового .session файла — api_id/api_hash
        НЕ спрашиваем, используем те же, что у управляющего бота (settings.json),
        по просьбе пользователя. Если файл создан под ДРУГИМ api_id — подключение
        может не пройти (сообщаем ошибку явно, не гадаем дальше)."""
        uid = m.from_user.id
        data = state["data"]
        if not m.document:
            await m.reply("Нужно прислать именно файл сессии (.session) — как документ, "
                          "не текст. Ещё раз, либо /start чтобы отменить:")
            return
        tmpdir = tempfile.mkdtemp(prefix="sessimport_")
        sess_name = f"import_{uid}_{int(time.time())}"
        dest = os.path.join(tmpdir, f"{sess_name}.session")
        client: Client | None = None
        try:
            await m.download(file_name=dest)
            client = Client(name=sess_name, api_id=data["api_id"], api_hash=data["api_hash"], workdir=tmpdir)
            await client.connect()
            me = await client.get_me()
            data["session_string"] = await client.export_session_string()
            await client.disconnect()
            client = None
            await self._finalize_add(m, data, name=me.username or me.first_name or None)
        except Exception as e:  # noqa: BLE001
            await m.reply(
                f"⚠️ Не удалось подключиться по этому файлу сессии: {e}\n\n"
                f"Частая причина — файл создан под ДРУГИМ api_id/api_hash, не тем, что у "
                f"управляющего бота. Тогда добавь аккаунт через «➕ Session» (session string) "
                f"с тем api_id/hash, под которым файл реально создавался. Начни заново: /start")
            self.states.pop(uid, None)
        finally:
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            shutil.rmtree(tmpdir, ignore_errors=True)

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
