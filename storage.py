"""Хранилище настроек и аккаунтов + игровые константы.

settings.json — общие настройки (токен бота, админы, трейд и т.п.).
accounts.json — список аккаунтов с их session string и параметрами.
"""
from __future__ import annotations

import copy
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
# биржа P-Coins -> ТОчки (проверено вживую) — см. farm.py.dump_pcoins_now
EXCHANGE_WORD = "/texchange"
DAILY_REWARD_WORD = "Ежедневная награда"
DAILY_REWARD_BUTTON = "Забрать"

# общая пауза (сек) перед КАЖДЫМ действием (клик по кнопке / отправка команды) во
# всём боте — не долбить игру слишком резко подряд (репорт: без паузы между
# действиями игра теряет/путает состояние диалога). Единственное намеренное
# исключение — «Магазин контейнеров» через прямой HTTP API (containers_api.py):
# там решает скорость реакции на восполнение запасов, поэтому он throttle=False
ACTION_DELAY = 0.5

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
    "redis_url": "",             # брокер Celery + live-статус/Pub-Sub для нового UI; пусто = функции выключены
    "containers": {
        "bot": "phonegetcardsbot",        # кому слать команду открытия магазина
        "open_command": "/tcontainershop",  # чем открываем магазин контейнеров
        "sold_out_marker": "раскуплены",  # признак ответа «всё распродано, след. через X»
        "captcha_marker": "анти-бот защита",  # признак математической капчи (решает человек)
        "shop_marker": "магазин контейнеров",  # признак того, что открылось меню магазина
        "miniapp_button_marker": "войти в магазин",  # магазин переехал в мини-апп (WebView) — кнопка,
                                                       # категорий бюджет/дорогой/донат в сообщении больше нет,
                                                       # авто-покупка кликами по кнопкам этим путём недоступна
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
        "offer_wait_timeout": 90,    # ожидание появления оффера у СВОЕГО получателя (шаг 2
                                      # TradeSession) — отдельно от общего step_timeout: доставка
                                      # уведомления игрой иногда ощутимо запаздывает, а получатель
                                      # в этот момент может быть занят своими фоновыми циклами
                                      # (карточки/майнинг/контейнеры), из-за чего короткого
                                      # step_timeout не хватало и оффер оставался непринятым
                                      # (воспроизведено вживую)
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
        # срабатывает раз в сутки, в account.mining_time по МСК (сбор майнинга —
        # отдельный настраиваемый интервал в минутах, см. mining_check_interval_minutes;
        # магазин телефонов остаётся раз в сутки, отдельного интервала опроса нет —
        # телефоны не появляются каждые 10 минут)
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
        "start_repair_button": "начать ремонт",  # прямая кнопка — если экран инструментов её не
                                                   # содержит (обычно чужая мастерская), выбираем
                                                   # конкретный инструмент и жмём repair_confirm_button
        "repair_confirm_button": "подтвердить",   # «Подтвердите запрос» после выбора инструмента —
                                                   # без этого шага заявка на ремонт не уходит вообще
                                                   # (баг: телефон снимался с фермы и висел без ремонта)
        "next_page_button": "➡",
        "capacity_marker": "занято ремонтом",
        "check_interval": 300,      # как часто автопочинка проверяет нерабочие телефоны, сек
        "request_marker": "запрос на ремонт телефона",  # входящий запрос от клиента (автопринятие)
        "accept_button": "принять заказ",
        "quiet_start": "22:00",     # тбилисское время — с этого часа автопринятие НЕ подтверждает заказы
        "quiet_end": "07:00",       # (чтобы не конкурировать за оборудование с автопочинкой ночью)
        "retry_interval": 5,        # пауза перед повтором клика, если бот принял его, но не ответил
        "retry_attempts": 3,        # сколько раз повторять один и тот же клик перед тем, как сдаться
        # инструмент своей мастерской теряет прочность (-3) за каждый ремонт — после
        # запуска починки телефона чиним всё оборудование, чтобы не остаться без инструмента
        "repair_all_equipment_button": "починить всё оборудование",
        "repair_all_confirm_button": "подтвердить",
        # поиск чужой мастерской по имени — настоящий поиск игры (кнопка -> ответ
        # текстом запроса -> «Результаты поиска...» с кнопками «Мастерская №N»),
        # а не проверка подстрокой первой из 500+ страниц общего списка
        "workshop_search_button": "найти по названию",
        "workshop_result_button": "мастерская №1",
        # поиск по ВЛАДЕЛЬЦУ (@username) — надёжнее поиска по имени: имя мастерской
        # в игре может быть декорировано эмодзи/спецсимволами, из-за чего встроенный
        # поиск игры («найти по названию») не находит её даже по точному видимому
        # тексту (проверено вживую на «Pixel Heart» — искал и полным именем, и без
        # эмодзи, оба раза «ничего не найдено»); листаем общий список и сверяем
        # «Владелец: @username» у каждой карточки
        "workshop_owner_max_pages": 30,
        # проактивное сообщение после завершения ремонта в ЧУЖОЙ мастерской —
        # игра просит оценить работу (кнопки «1⭐».."5⭐»), затем текстом — комментарий
        "review_marker": "оцените работу мастерской",
        "review_stars": "5",
        "review_comment": "нет",
    },
    "farm_maintenance": {
        "bot": "phonegetcardsbot",
        "mining_command": "Тмайнинг",       # открывает модульную ферму (та же команда, что MINING_WORD)
        "slot_button_prefix": "Слот",       # кнопки «Слот 1».."Слот 12"
        "extract_button": "извлечь сломанный",   # на карточке слота со статусом СЛОМАН
        "add_phone_button": "добавить телефон",  # на карточке пустого слота
        "working_phones_button": "рабочие телефоны",  # «Мои телефоны» -> сюда, для проверки наличия
                                                        # ДО выключения фермы (см. farm.py._has_working_phone)
        "next_page_button": "➡",            # пагинация в списке телефонов по редкости
        "power_off_button": "выключить",    # установка/извлечение телефона в слот работает только при
        "power_on_button": "включить",      # выключенной ферме — выключаем перед этим и включаем обратно
        "retry_interval": 5,       # пауза перед повтором клика, если бот принял его, но не ответил
        "retry_attempts": 3,       # сколько раз повторять один и тот же клик перед тем, как сдаться
        # у майнинга есть часовой таймер накопления, который сбрасывается КАЖДЫЙ раз, когда
        # ферма выключается (даже кратко, ради установки одного телефона) — если обслуживание
        # выключает ферму чаще этого интервала, накопление никогда не успевает дособраться.
        # farm_maintenance_now() пропускает выключение (и весь проход извлечения/установки),
        # если с прошлого выключения прошло меньше этого времени
        "min_power_toggle_interval": 3600,
        # аварийный watchdog перегрузки питания/охлаждения (см. farm.py.relieve_overload_now) —
        # НЕ подчиняется min_power_toggle_interval выше (это реакция на уже случившуюся или
        # надвигающуюся аварию, а не рутинное выключение)
        "remove_phone_button": "убрать телефон",  # на карточке РАБОЧЕГО слота — отдельная кнопка
                                                    # от «извлечь сломанный» (extract_button)
        "power_watchdog_max_removals": 6,          # не снимать больше стольки телефонов за раз
                                                     # (подстраховка от совсем странных случаев)
    },
    "exchange": {
        "bot": "phonegetcardsbot",
        "open_command": "/texchange",      # открывает «Биржа P-Coin» (курс + кнопки Купить/Продать/…)
        "sell_button": "продать",          # после клика бот просит ОТВЕТИТЬ ЧИСЛОМ (сколько продать) —
                                            # не кнопка, а обычное текстовое сообщение в чат
        "confirm_button": "подтвердить ордер",  # на экране «ОРДЕР НА ПРОДАЖУ (MARKET)»
        # перевод P-Coins другому игроку (кнопка «Отправить P-Coins» на той же бирже) — флоу
        # проверен вживую: клик -> ввести @username/id ТЕКСТОМ -> (если «Пользователь не
        # найден» — начинает заново с того же шага) -> ввести количество ТЕКСТОМ -> «Введите
        # комментарий...» -> «Пропустить» -> «ПОДТВЕРЖДЕНИЕ ПЕРЕВОДА» -> «Подтвердить». Успеха
        # отдельным сообщением нет — то же сообщение просто редактируется обратно в «Биржа P-Coin»
        # с уменьшенным балансом
        "send_button": "отправить p-coins",
        "not_found_marker": "пользователь не найден",
        "skip_comment_button": "пропустить",
        "transfer_confirm_button": "подтвердить",
    },
    # разовая РУЧНАЯ команда (кнопка в боте, не фоновая автоматизация — так и попросили:
    # не автоматизировать флип Рыночек-зарешал/Барыга целиком, а просто дать команду
    # для утомительной части) — выставляет телефон на Авито дороже официальной цены;
    # купить с другого твинка/чужого игрока владелец делает уже сам вручную в игре.
    # Флоу проверен вживую: /avito -> «Подать объявление» -> «Телефоны» -> редкость ->
    # первый телефон -> цена (текст) -> «Пропустить» описание -> «Подтвердить» (текст)
    "avito": {
        "bot": "phonegetcardsbot",
        "open_command": "/avito",
        "post_button": "подать объявление",
        "type_phones_button": "телефоны",
        "rarity": "Ширпотреб",
        "description_skip_button": "пропустить",
        "confirm_text": "Подтвердить",
    },
    # прямой HTTP к бэкенду мини-аппа магазина контейнеров (см. containers_api.py) —
    # контейнеры (особенно донатные) разбирают за секунды после рестока, ручные клики
    # по мини-аппу физически не успевают (проверено вживую)
    "containers_api": {
        "bot": "phonegetcardsbot",
        "open_command": "/tcontainershop",
        "entry_button": "войти в магазин контейнеров",
        "base_url": "https://phoneget.duckdns.org",
        "platform": "web",
        "poll_far_interval": 1800,     # далеко от рестока — раз во сколько секунд проверять next_supply
                                        # (каждая проверка — настоящий MTProto RequestSimpleWebView,
                                        # не стоит дёргать его слишком часто без нужды)
        "burst_window_seconds": 90,    # за сколько до/после next_supply переходить в плотный опрос
        "burst_interval": 1.5,         # пауза между попытками купить во время плотного опроса, сек
        "burst_duration": 90,          # сколько длится один заход плотного опроса, сек
        "active_poll_interval": 5,     # магазин УЖЕ активен прямо сейчас (state.active=true) —
                                        # раз во сколько секунд перепроверять/докупать, пока не купим
                                        # всё и не уйдём на far_interval. Раньше цикл ориентировался
                                        # ТОЛЬКО на расчётное окно перед next_supply и мог полностью
                                        # пропустить уже идущий завоз (next_supply мог указывать на
                                        # следующий по счёту, а не на текущий, уже открытый)
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
    "mining_time": "01:00",         # якорь (МСК) ТОЛЬКО для ежедневной награды — сбор майнинга
                                     # на своём отдельном интервале, см. mining_check_interval
    "mining_check_interval": 14400,  # раз во сколько секунд проверять/собирать майнинг (как и
                                       # все остальные интервалы в том же меню бота — карточки/
                                       # рулетка/авто-вывод/авто-трейд). collect_mining() сам по
                                       # себе безопасен для частого опроса — только включает ферму,
                                       # если она выключена, и никогда не выключает сам, так что не
                                       # рискует сбросить часовой таймер накопления (это риск только
                                       # у farm_maintenance_now — там своя защита min_power_toggle_interval)
    "autopay_enabled": True,
    "autopay_interval": 14400,      # раз во сколько секунд авто-вывод очков — независимо от майнинга
    "autotrade_enabled": False,
    "autotrade_interval": 14400,    # раз во сколько секунд авто-трейд (слив телефонов) — независимо от майнинга
    "containers_enabled": False,        # СТАРЫЙ путь через чат — теперь мёртв: игра перевела магазин
                                         # контейнеров на Telegram Mini App, кнопок в чате для покупки
                                         # больше нет (см. containers_api_enabled — актуальная замена)
    "containers_buy_budget": True,      # 📦 Бюджетный контейнер — покупать при рестоке
    "containers_buy_expensive": True,   # 📦 Дорогой контейнер
    "containers_buy_donation": True,    # 📦 Донатный контейнер
    "containers_qty_budget": 0,         # сколько покупать за ресток, 0 = максимум доступного (игра+баланс)
    "containers_qty_expensive": 0,
    "containers_qty_donation": 0,
    "containers_api_enabled": False,    # 📦⚡ покупка контейнеров напрямую через HTTP API мини-аппа —
                                         # успевает за секунды до раскупки, в отличие от кликов по вебвью
    "containers_api_priority": "donate,expensive,budget",  # порядок типов при покупке
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
    "auto_repair_enabled": False,  # 🛠 автопочинка своих нерабочих телефонов (своим оборудованием, либо
                                    # чужой мастерской — см. repair_external_workshop_enabled ниже)
    "auto_accept_enabled": False,  # 📥 автопринятие чужих заказов на ремонт (тихие часы — settings.repair)
    "repair_external_workshop_enabled": False,  # 🌍 если своего инструмента не хватает — слать в чужую
                                                 # мастерскую вместо того, чтобы просто пропускать поломку
    "repair_external_workshop_name": "",        # название конкретной чужой мастерской — поиск игры по
                                                 # тексту («найти по названию»); пусто = не использовать
    "repair_external_workshop_owner": "",       # @username владельца конкретной чужой мастерской — надёжнее
                                                 # поиска по имени (см. repair.py._find_workshop_by_owner);
                                                 # можно НЕСКОЛЬКО через запятую (если у владельца больше
                                                 # одной своей мастерской) — берём первую найденную по счёту
                                                 # (страницы отсортированы по рейтингу). Если задан —
                                                 # пробуем ПЕРВЫМ. Оба пустых = любая первая свободная
    "auto_review_enabled": False,   # ⭐ автоматически оценивать чужую мастерскую после ремонта (5⭐, без
                                     # комментария) — прогресс к достижению «Критик» (за КОЛИЧЕСТВО отзывов,
                                     # не разовое действие; см. Критик I/II/III — 10/20/50 отзывов)
    "avito_flip_price": 5000,       # 📢 цена флипа на Авито внутри «Выполнить достижения» —
                                     # достижения «Рыночек зарешал»/«Барыга» разовые, повторный флип бесполезен
    "achievements_min_balance": 150000,  # 📜 «Выполнить достижения» не начинает, если баланс «Такк»
                                          # (ТОчки) ниже этого — грубая подстраховка на будущее (сам флип
                                          # на Авито бесплатен, но не все возможные будущие шаги здесь тоже)
    "power_watchdog_enabled": False,   # ⚡ аварийный сброс перегрузки питания/охлаждения (случайные
                                        # «События» типа «Взрыв электростанции» режут лимит PSU) —
                                        # снимает рабочие трифолды, пока не впишется в лимит
    "power_watchdog_interval": 300,    # раз во сколько секунд проверять питание/охлаждение (не
                                        # подчиняется расписанию/кулдауну обычного обслуживания)
    "farm_maintenance_enabled": False,  # 🔧 обслуживание фермы: снять сломанные (-> автопочинка) -> вернуть починенные в слот
    "farm_maintenance_interval": 3600,  # раз во сколько секунд проверять — farm_maintenance_now() сама
                                         # решает, есть ли что делать, и не выключает ферму чаще
                                         # min_power_toggle_interval, так что можно опрашивать смело чаще
    "farm_target_phones": 11,       # сколько телефонов должно стоять на ферме одновременно — если уже
                                     # занято от этого числа, пополнение новыми не запускаем (и ферму не трогаем)
    "farm_fill_model": "Samsung Galaxy Z TriFold",  # какой моделью пополнять пустые слоты БЕЗ «памяти»
                                                     # (farm_slot_models) — обычно только что добавленные
    "farm_fill_rarity": "Платиновый",  # редкость модели farm_fill_model — используется и при докупке в
                                     # «Магазине телефонов», и при поиске уже купленных экземпляров в
                                     # «Мои телефоны -> Рабочие телефоны» (кнопка «Заполнить ферму»,
                                     # fill_farm_now): без неё поиск в «Мои телефоны» перебирал редкости
                                     # начиная с «Ширпотреб» и мог не добраться до реальной редкости
                                     # TriFold'а («Платиновый»), либо перебирал слишком долго. Пусто =
                                     # полный перебор всех редкостей (если модель другая и её редкость
                                     # неизвестна — задай её тут же)
    "pcoin_exchange_enabled": False,    # 💱 авто-продажа P-Coins на ТОчки через биржу (см. dump_pcoins_now)
    "pcoin_send_enabled": False,        # 💱 авто-перевод P-Coins получателю (payout_target) через биржу
                                         # (см. send_pcoins_now) — альтернатива продаже: если включены
                                         # ОБА тумблера на одном аккаунте, приоритет у перевода (см.
                                         # _pcoin_exchange_loop) — P-Coins ценнее оставить получателю
                                         # решать, чем сразу превращать в мгновенно тратящиеся ТОчки твинка
    "pcoin_exchange_interval": 14400,   # раз во сколько секунд пробовать биржу (продажа/перевод), если
                                         # включён хотя бы один из двух тумблеров выше
    # ПРИМЕЧАНИЕ: farm_slot_models (номер слота -> модель) НЕ здесь, хоть Storage теперь
    # и копирует list/dict-значения ACCOUNT_DEFAULTS (см. Storage.__init__/add) — у него
    # нет осмысленного дефолта (это персональная память слот->модель), заводится лениво
    # через self.account.setdefault("farm_slot_models", {}) при первом использовании.
}


def _empty_stats() -> dict[str, int]:
    return {"phones": 0, "good_phones": 0, "roulette": 0, "mining": 0, "paid": 0,
            "exchanged": 0, "daily": 0, "containers_bought": 0, "shop_bought": 0,
            "repaired": 0, "accepted_orders": 0, "farm_extracted": 0, "farm_reinstalled": 0,
            "pcoin_sold": 0, "pcoin_sent": 0, "reviews_left": 0, "avito_listed": 0,
            # repaired_external уже бампился из repair.py, но не был объявлен тут —
            # значит не показывался в "пустой" карточке статистики нового аккаунта
            "repaired_external": 0,
            # разбивка containers_bought по категориям (аналитика трат) —
            # заполняется и клик-путём (farm.py), и HTTP API-путём (containers_api.py)
            "containers_bought_donation": 0, "containers_bought_expensive": 0,
            "containers_bought_budget": 0}


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
    "REDIS_URL": ("redis_url", "str"),
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
                    # list/dict-значения — копией, а не по ссылке: иначе все аккаунты,
                    # у которых ещё нет своего k, разделяли бы ОДИН И ТОТ ЖЕ объект из
                    # ACCOUNT_DEFAULTS, и правка одного аккаунта (даже полным
                    # переприсваиванием — оно бы не спасло, если где-то до этого
                    # кто-то успел смутировать общий объект на месте) утекла бы во все
                    acc[k] = copy.deepcopy(v) if isinstance(v, (list, dict)) else v
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
            if k not in account:
                account[k] = copy.deepcopy(v) if isinstance(v, (list, dict)) else v
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
