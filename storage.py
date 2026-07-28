"""Хранилище настроек и аккаунтов + игровые константы.

settings.json — общие настройки (токен бота, админы, трейд и т.п.).
accounts.json — список аккаунтов с их session string и параметрами.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR — где хранить settings.json/accounts.json. Локально это папка проекта;
# в облаке (Railway) сюда монтируется постоянный Volume, иначе сессии слетают
# при каждом передеплое (контейнер эфемерный).
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
ACCOUNTS_PATH = os.path.join(DATA_DIR, "accounts.json")

_lock = threading.Lock()

# ---------- игровые боты и слова/кнопки ----------
CARDS_BOT = "phonegetcardsbot"      # он же PhoneGet — карточки/майнинг/обмен
ROULETTE_BOT = "phonegetroulettebot"

CARD_WORD = "Ткарточка"
ROULETTE_WORD = "Рулетка"
ROULETTE_BUTTON = "Крутить"
MINING_WORD = "Тмайнинг"            # открывает модульную ферму (Rack/PSU/Cooling/слоты)
FARM_WITHDRAW_BUTTON = "Снять P-Coins с фермы"
FARM_REMOVE_BUTTON = "Убрать телефон"
FARM_BROKEN_MARKER = "сломан"       # реальный текст бота — «(СЛОМАН)» (было "сломано" — не совпадало)
BALANCE_WORD = "такк"               # профиль: «Точки: N», «Телефонов в коллекции: N»
PAY_CONFIRM_BUTTON = "Подтвердить"
CONTAINER_WORD = "Магазин контейнеров"
DAILY_REWARD_WORD = "Ежедневная награда"
DAILY_REWARD_BUTTON = "Забрать"
UPGRADE_WORD = "Магазин улучшений"   # 7 категорий, все 6-уровневые (перезарядка/шансы/ферма/лимит)
UPGRADE_CATEGORIES = [
    "Перезарядка", "Шансы выпадения", "Шансы апгрейда",
    "Стойка фермы", "Охлаждение фермы", "Блок питания фермы", "Лимит покупок",
]
UPGRADE_MAX_MARKER = "максимальный уровень"
UPGRADE_BUY_BUTTON = "Улучшить за"
UPGRADE_RESERVE = 2_000_000          # неприкосновенный запас ТОчек (не тратить ниже него)

# дефолтное время майнинга по МСК (UTC+3)
MINING_HOUR = 1
MINING_MINUTE = 0
MINING_INTERVAL_HOURS = 4  # с обновления фермы майнинг собирается раз в 4 часа, не раз в сутки


DEFAULT_SETTINGS: dict[str, Any] = {
    # api_id/api_hash + bot_token — для управляющего бота (@BotFather)
    "api_id": 0,
    "api_hash": "",
    "bot_token": "",
    "admin_ids": [],            # владелец(ы); при multi_user=false — только им доступ
    "multi_user": False,        # true = бот открыт всем, каждый видит только свои аккаунты
    "main_account_id": 0,       # только для одноразовой миграции старых аккаунтов
    "default_card_interval": 3600,
    "default_roulette_interval": 3600,
    "good_phone_keywords": ["фантом", "phantom"],  # «хорошие» телефоны (подстрока)
    "report_time": "00:00",     # ежедневный отчёт, МСК
    "payout_delay": 120,        # пауза после майнинга перед выводом, сек
    "repo_url": "",              # ссылка на репозиторий (гайд самохостинга) — кнопка в главном меню, если задано
    "containers": {
        "bot": "phonegetcardsbot",        # кому слать команду открытия магазина
        "open_command": "/tcontainershop",  # чем открываем магазин контейнеров
        "sold_out_marker": "раскуплены",  # признак ответа «всё распродано, след. через X»
        "captcha_marker": "анти-бот защита",  # признак математической капчи (решает человек)
        "shop_marker": "магазин контейнеров",  # признак того, что открылось меню магазина
        "unknown_retry": 600,             # обычная пауза перед след. проверкой после цикла
        "retry_interval": 10,             # частые повторы (после расчётного времени рестока/при перегрузе)
        "retry_window": 600,              # сколько ещё секунд долбить магазин ПОСЛЕ расчётного рестока
        "early_start": 1800,              # начать опрос за N сек до расчётного рестока (не ровно в момент,
                                           # когда игровой бот перегружен и не отвечает)
        "pre_restock_interval": 60,       # редкие повторы (раз в минуту), пока ждём расчётный ресток
        "restock_alert_before": 1200,     # за сколько секунд до расчётного рестока слать алерт владельцу (по умолч. 20 мин)
        "refresh_button": "обновить",     # кнопка «🔄 Обновить» на сообщении «раскуплены» — жмём её вместо
                                           # повторной отправки команды открытия магазина (меньше сообщений в чате)
        "preferred_categories": ["Донатный", "Дорогой", "Бюджетный"],  # порядок покупки категорий
        "single_button": "купить 1",      # кнопка «Купить 1 шт.»
        "bulk_button": "купить оптом",    # кнопка «Купить оптом»
        "confirm_button": "подтвердить",  # кнопка подтверждения покупки
        "captcha_alert_text": "КАПЧА",    # что слать владельцу, когда нужна капча/ответ не распознан
        # ожидаемая цена за 1 шт. по категориям (ТОчек) — страховка от бага парсинга:
        # если реальная цена сильно выходит за диапазон, покупка этой категории
        # пропускается и владельцу шлётся алерт, вместо слепой траты неизвестной суммы
        "price_ranges": {
            "budget": [1500000, 3000000],
            "expensive": [7000000, 9000000],
            "donation": [7000000, 9500000],
        },
    },
    # «.trade» / «.pay 505050», напечатанные самим аккаунтом в личке с кем-то,
    # автоматически превращаются в команду боту карточек с получателем = собеседник.
    "self_commands": {
        "trade": "/trade {target}",
        "pay": "/pay {target} {arg}",
    },
    "trade": {
        "command": "/trade {target}",
        "step_timeout": 35,
        "solo_accept_timeout": 300,  # ожидание ручного «Принять» получателем вне бота
        "max_slots": 10,
        "action_delay": 0.6,         # человеческая пауза перед каждым нажатием
        "repeat_threshold": 1,       # повторять трейд, пока в коллекции > этого
        "max_passes": 20,            # предохранитель проходов трейда
        # маркеры в тексте (подстрока, регистр не важен)
        "offer_marker": "предложение обмена",
        "added_marker": "добавлено",
        "ready_label": "готовность",
        "give_section_marker": "вы отдаёте",
        "collection_marker": "телефонов в коллекции",
        "confirm_marker": "подтвердите обмен",  # ключ нового сообщения подтверждения
        # подписи кнопок (подстрока, эмодзи можно опускать)
        "accept_button": "принять",
        "add_phone_button": "добавить телефон",
        "working_phone_button": "рабочий телефон",
        "add_several_button": "добавить несколько",
        "add_one_button": "добавить 1",
        "confirm_button": "подтвердить",
        # навигационные подписи (НЕ телефон/редкость)
        "skip_buttons": ["назад", "вернуться", "быстрый выбор", "закрыть", "отмена", "меню"],
    },
    "phone_shop": {
        "bot": "phonegetcardsbot",
        "open_command": "Магазин телефонов",
        "bulk_button": "купить оптом",
        "confirm_button": "подтвердить",
        "next_page_button": "➡",
        # срабатывает раз в сутки, в account.mining_time по МСК (майнинг с этого же
        # времени теперь собирается чаще — см. MINING_INTERVAL_HOURS — а магазин
        # остаётся раз в сутки, отдельного интервала опроса нет: телефоны не
        # появляются каждые 10 минут)
        "retry_interval": 5,      # пауза перед повтором клика, если бот принял его, но не ответил
        "retry_attempts": 3,      # сколько раз повторять один и тот же клик перед тем, как сдаться
    },
    "repair": {
        "bot": "phonegetcardsbot",
        "my_phones_command": "Мои телефоны",
        "workshop_command": "Моя мастерская",
        "broken_button": "нерабочие телефоны",
        "equipment_button": "оборудование",
        "own_workshop_button": "в своей мастерской",
        "repair_button": "отдать в ремонт",
        "start_repair_button": "начать ремонт",
        "next_page_button": "➡",
        "capacity_marker": "занято ремонтом",
        "check_interval": 300,      # как часто автопочинка проверяет нерабочие телефоны, сек
        "request_marker": "запрос на ремонт телефона",  # входящий запрос от клиента (автопринятие)
        "accept_button": "принять заказ",
        "quiet_start": "22:00",     # тбилисское время — с этого часа автопринятие НЕ подтверждает заказы
        "quiet_end": "07:00",       # (чтобы не конкурировать за оборудование с автопочинкой ночью)
        "retry_interval": 5,        # пауза перед повтором клика, если бот принял его, но не ответил
        "retry_attempts": 3,        # сколько раз повторять один и тот же клик перед тем, как сдаться
    },
    "farm_maintenance": {
        "bot": "phonegetcardsbot",
        "mining_command": "Тмайнинг",       # открывает модульную ферму (та же команда, что MINING_WORD)
        "slot_button_prefix": "Слот",       # кнопки «Слот 1».."Слот 12"
        "extract_button": "извлечь сломанный",   # на карточке слота со статусом СЛОМАН
        "add_phone_button": "добавить телефон",  # на карточке пустого слота
        "next_page_button": "➡",            # пагинация в списке телефонов по редкости
        "check_interval": 14400,   # раз в 4 часа: снять сломанные -> они уйдут на автопочинку (repair.py) ->
                                    # уже отремонтированные той же модели вернуть в опустевший слот
        "retry_interval": 5,       # пауза перед повтором клика, если бот принял его, но не ответил
        "retry_attempts": 3,       # сколько раз повторять один и тот же клик перед тем, как сдаться
    },
}

# поля по умолчанию для нового аккаунта
ACCOUNT_DEFAULTS: dict[str, Any] = {
    "owner_id": 0,          # Telegram id пользователя-владельца (кто добавил/управляет)
    "is_main": False,       # чисто метка 👑 (последний источник для «скопировать получателей»)
    "payout_target": "",    # получатель /pay — @username или id, задаёт сам пользователь
    "trade_target": "",     # получатель трейда — @username или id, задаёт сам пользователь
    "enabled": True,
    # два НЕЗАВИСИМЫХ модуля — каждый можно включить/выключить отдельно от другого
    "farm_enabled": True,       # 🌾 Фарм карточек: карточки/рулетка/майнинг/контейнеры/вывод/трейд
    "autosend_enabled": True,   # 📨 Автоотправка: задачи по расписанию + .trade/.pay из личек
    "card_enabled": True,
    "roulette_enabled": True,
    "mining_enabled": True,
    "mining_time": "01:00",
    "autopay_enabled": True,
    "autotrade_enabled": False,
    "containers_enabled": False,
    "containers_buy_budget": True,      # 📦 Бюджетный контейнер — покупать при рестоке
    "containers_buy_expensive": True,   # 📦 Дорогой контейнер
    "containers_buy_donation": True,    # 📦 Донатный контейнер
    "containers_qty_budget": 0,         # сколько покупать за ресток, 0 = максимум доступного (игра+баланс)
    "containers_qty_expensive": 0,
    "containers_qty_donation": 0,
    "alerts_enabled": True,             # 🔔 алерты владельцу (капча, подозр. цена и т.п.) через управляющего бота
    "daily_reward_enabled": True,
    "self_commands_enabled": True,
    "card_interval": 3600,
    "roulette_interval": 3600,
    "drain_amount": 0,      # 💧 фикс. сумма очков «слива» твинкам (только с 👑 главного аккаунта), 0 = не задано
    "phone_shop_enabled": False,   # 🏪 автозакупка телефонов
    "phone_shop_rarity": "Ширпотреб",
    "phone_shop_model": "",        # пусто = первая модель в списке (как «по умолчанию» на скринах)
    "phone_shop_quantity": 0,      # 0 = максимум предложенного (в рамках баланса), иначе конкретное число
    "auto_repair_enabled": False,  # 🛠 автопочинка своих нерабочих телефонов (только своим оборудованием)
    "auto_accept_enabled": False,  # 📥 автопринятие чужих заказов на ремонт (тихие часы — settings.repair)
    "farm_maintenance_enabled": False,  # 🔧 обслуживание фермы: снять сломанные (-> автопочинка) -> вернуть починенные в слот
    # ПРИМЕЧАНИЕ: farm_slot_models (номер слота -> модель) НЕ здесь — это dict, а значения
    # ACCOUNT_DEFAULTS копируются по ССЫЛКЕ при миграции (Storage.__init__/add), общий
    # мутируемый dict на все аккаунты был бы багом. farm.py заводит его лениво через
    # self.account.setdefault("farm_slot_models", {}) при первом использовании.
}


def _empty_stats() -> dict[str, int]:
    return {"phones": 0, "good_phones": 0, "roulette": 0, "mining": 0, "paid": 0,
            "exchanged": 0, "daily": 0, "containers_bought": 0, "shop_bought": 0,
            "repaired": 0, "accepted_orders": 0, "farm_extracted": 0, "farm_reinstalled": 0}


def _read_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# Простые (не вложенные) настройки, которые можно задать переменными окружения —
# удобно для облачного хостинга (Railway и т.п.), где секреты живут в env, а не в файле.
_ENV_MAP: dict[str, tuple[str, str]] = {
    "BOT_TOKEN": ("bot_token", "str"),
    "API_ID": ("api_id", "int"),
    "API_HASH": ("api_hash", "str"),
    "ADMIN_IDS": ("admin_ids", "int_list"),
    "MULTI_USER": ("multi_user", "bool"),
    "MAIN_ACCOUNT_ID": ("main_account_id", "int"),
    "DEFAULT_CARD_INTERVAL": ("default_card_interval", "int"),
    "DEFAULT_ROULETTE_INTERVAL": ("default_roulette_interval", "int"),
    "REPORT_TIME": ("report_time", "str"),
    "PAYOUT_DELAY": ("payout_delay", "int"),
    "REPO_URL": ("repo_url", "str"),
}


def _coerce_env(raw: str, kind: str):
    if kind == "int":
        return int(raw)
    if kind == "bool":
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if kind == "int_list":
        return [int(x) for x in raw.replace(";", ",").split(",") if x.strip()]
    return raw


def load_settings() -> dict[str, Any]:
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))  # глубокая копия дефолтов

    data = _read_json(SETTINGS_PATH, None)
    if data is not None:
        for k, v in data.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v

    # переменные окружения перекрывают файл — удобно на Railway и т.п.
    for env_name, (key, kind) in _ENV_MAP.items():
        raw = os.environ.get(env_name)
        if raw:
            try:
                merged[key] = _coerce_env(raw, kind)
            except ValueError:
                pass  # битое значение в env — не роняем старт, просто игнорируем

    if not merged.get("bot_token") or not merged.get("api_id") or not merged.get("api_hash"):
        raise SystemExit(
            "Не заданы bot_token/api_id/api_hash. Либо скопируй settings.example.json -> "
            "settings.json и заполни, либо задай переменные окружения BOT_TOKEN/API_ID/API_HASH."
        )
    return merged


class Storage:
    """Список аккаунтов в accounts.json."""

    def __init__(self) -> None:
        self.accounts: list[dict[str, Any]] = _read_json(ACCOUNTS_PATH, [])
        # дополним недостающими полями (миграция старых записей)
        changed = False
        for acc in self.accounts:
            for k, v in ACCOUNT_DEFAULTS.items():
                if k not in acc:
                    acc[k] = v
                    changed = True
            acc.setdefault("stats", _empty_stats())
            acc.setdefault("stats_day", _empty_stats())
            acc.setdefault("tasks", [])
            for d in ("stats", "stats_day"):
                for k, v in _empty_stats().items():
                    if k not in acc[d]:
                        acc[d][k] = v
                        changed = True
        if changed:
            self.save()

    def save(self) -> None:
        with _lock:
            _write_json(ACCOUNTS_PATH, self.accounts)

    def next_id(self) -> int:
        return max((a["id"] for a in self.accounts), default=0) + 1

    def add(self, account: dict[str, Any]) -> dict[str, Any]:
        account.setdefault("id", self.next_id())
        for k, v in ACCOUNT_DEFAULTS.items():
            account.setdefault(k, v)
        account.setdefault("stats", _empty_stats())
        account.setdefault("stats_day", _empty_stats())
        account.setdefault("tasks", [])
        self.accounts.append(account)
        self.save()
        return account

    def get(self, acc_id: int) -> dict[str, Any] | None:
        return next((a for a in self.accounts if a["id"] == acc_id), None)

    def remove(self, acc_id: int) -> bool:
        before = len(self.accounts)
        self.accounts = [a for a in self.accounts if a["id"] != acc_id]
        self.save()
        return len(self.accounts) != before
