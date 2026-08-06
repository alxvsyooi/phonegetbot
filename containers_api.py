"""Магазин контейнеров через прямой HTTP API — БЕЗ кликов по мини-аппу.

Игра перевела «Магазин контейнеров» с обычного чата на Telegram Mini App
(веб-страница внутри Telegram: phoneget.duckdns.org/containershop). У неё
свой backend с двумя эндпоинтами (найдены вживую через анализ сети браузера
и inline-скрипта страницы):

    POST /api/containershop/state  {"initData": <tgWebAppData>}
        -> {success, tochki, state:{active, next_supply, containers:{
               budget:{left,total,price}, expensive:{...}, donate:{...}}},
            limits:{normal_bought, normal_max, donate_bought, donate_max,
                    inv_current, inv_max}, server_time, ...}
    POST /api/containershop/buy    {"initData": ..., "c_type": "budget"|
                                     "expensive"|"donate", "qty": <int>}
        -> {success: true} | {success: false, error: "..."}

Контейнеры (особенно донатные) разбирают за секунды после рестока — ручные
клики по мини-аппу физически не успевают (проверено вживую: за ~10 минут
между двумя проверками весь новый завоз был распродан). Прямой HTTP —
единственный реальный способ успеть.

initData — подпись Telegram WebApp (auth_date/hash/user), обычная браузерная
покупка получает её через открытие мини-аппа. Мы получаем её ЖЕ САМЫМ
способом, которым это делает настоящий Telegram-клиент — через MTProto
messages.requestSimpleWebView (используя УЖЕ подключённый self.client этого
аккаунта, а не отдельный сторонний скрипт — так безопаснее для боевой
сессии, см. обсуждение AUTH_KEY_UNREGISTERED при попытке отдельного теста).
initData имеет ограниченный срок жизни (auth_date) — получаем её заново
перед каждой попыткой купить, не кэшируем надолго.
"""
from __future__ import annotations

import asyncio
import time
import urllib.parse
from datetime import datetime

import aiohttp
from pyrogram import raw

from storage import CARDS_BOT
from common import clock, today_msk

_DEFAULT_PRIORITY = ["donate", "expensive", "budget"]


def _parse_iso_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


class ContainersApiModule:
    """Миксин: покупка контейнеров напрямую через HTTP API магазина (мини-аппа).
    Ожидает от связанного класса: self.account, self.client, self.running,
    self._trade_mode, self._send_and_wait, self._bump, self.containers_api_cfg
    и last_containers_api (инициализируется в automation.py)."""

    def _loops(self):
        return super()._loops() + [self._containers_api_loop()]

    def _containers_api_active(self) -> bool:
        return self._farm_active() and self.account.get("containers_api_enabled", False)

    # ---------- получение initData через настоящий MTProto-запрос вебвью ----------
    async def _fetch_webapp_init_data(self, cfg: dict) -> str | None:
        bot = cfg.get("bot") or CARDS_BOT
        open_cmd = cfg.get("open_command") or "/tcontainershop"
        entry_marker = (cfg.get("entry_button") or "войти в магазин контейнеров").lower()

        # throttle=False: единственное намеренное исключение из общей паузы ACTION_DELAY
        # (см. automation.py) — тут решает скорость реакции на восполнение запасов
        msg = await self._send_and_wait(bot, open_cmd, timeout=15, throttle=False)
        if msg is None:
            return None
        mk = getattr(msg, "reply_markup", None)
        web_btn = None
        if mk and getattr(mk, "inline_keyboard", None):
            for row in mk.inline_keyboard:
                for b in row:
                    text = (getattr(b, "text", "") or "").lower()
                    if entry_marker in text and getattr(b, "web_app", None):
                        web_btn = b
                        break
                if web_btn:
                    break
        if web_btn is None:
            return None

        peer = await self.client.resolve_peer(bot)
        try:
            result = await self.client.invoke(
                raw.functions.messages.RequestSimpleWebView(
                    bot=peer, platform=cfg.get("platform", "web"), url=web_btn.web_app.url,
                )
            )
        except Exception:
            return None
        url = getattr(result, "url", None)
        if not url or "#" not in url:
            return None
        frag = url.split("#", 1)[1]
        qs = urllib.parse.parse_qs(frag)
        values = qs.get("tgWebAppData")
        return values[0] if values else None

    # ---------- сырые вызовы API ----------
    async def _containers_api_post(self, cfg: dict, endpoint: str, payload: dict) -> dict | None:
        base = (cfg.get("base_url") or "https://phoneget.duckdns.org").rstrip("/")
        url = f"{base}/api/containershop/{endpoint}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    return await resp.json()
        except Exception:
            return None

    # ---------- получить свежую initData + состояние магазина одним заходом ----------
    async def _fetch_state(self, cfg: dict) -> tuple[str | None, dict | None]:
        init_data = await self._fetch_webapp_init_data(cfg)
        if not init_data:
            return None, None
        state = await self._containers_api_post(cfg, "state", {"initData": init_data})
        if not state or not state.get("success"):
            return init_data, None
        return init_data, state

    # ---------- собственно покупка по уже полученным initData/state ----------
    async def _execute_buy(self, cfg: dict, init_data: str, state: dict) -> str:
        s = state.get("state") or {}
        if not s.get("active"):
            self.last_containers_api = f"порт пуст, следующая поставка: {s.get('next_supply') or '?'} ({clock()})"
            return self.last_containers_api

        containers = s.get("containers") or {}
        limits = dict(state.get("limits") or {})
        priority_raw = self.account.get("containers_api_priority") or ",".join(_DEFAULT_PRIORITY)
        priority = [p.strip() for p in priority_raw.split(",") if p.strip()]

        bought_parts: list[str] = []
        for c_type in priority:
            c = containers.get(c_type)
            if not c or int(c.get("left", 0)) <= 0:
                continue
            is_donate = c_type == "donate"
            limit_left = (
                int(limits.get("donate_max", 0)) - int(limits.get("donate_bought", 0)) if is_donate
                else int(limits.get("normal_max", 0)) - int(limits.get("normal_bought", 0))
            )
            space_left = int(limits.get("inv_max", 0)) - int(limits.get("inv_current", 0))
            qty = min(int(c["left"]), limit_left, space_left)
            if qty <= 0:
                continue
            res = await self._containers_api_post(
                cfg, "buy", {"initData": init_data, "c_type": c_type, "qty": qty})
            if res and res.get("success"):
                bought_parts.append(f"{c_type} x{qty}")
                self._bump("containers_bought", qty)
                limits["inv_current"] = int(limits.get("inv_current", 0)) + qty
                if is_donate:
                    limits["donate_bought"] = int(limits.get("donate_bought", 0)) + qty
                else:
                    limits["normal_bought"] = int(limits.get("normal_bought", 0)) + qty

        if bought_parts:
            self.last_containers_api = f"📦 куплено: {', '.join(bought_parts)} ({clock()} {today_msk()})"
        else:
            self.last_containers_api = f"нечего купить (распродано/лимиты) ({clock()})"
        return self.last_containers_api

    # ---------- одна попытка: проверить и купить всё доступное по приоритету ----------
    async def buy_containers_api_now(self) -> str:
        """Получает свежую initData, проверяет состояние магазина и покупает всё
        доступное по приоритету (containers_api_priority, по умолчанию
        donate,expensive,budget) в пределах лимитов/места на складе. Используется
        циклом (containers_api_enabled) и может вызываться вручную."""
        if not self.client or not self.running:
            self.last_containers_api = "аккаунт не запущен"
            return self.last_containers_api
        if self._trade_mode:
            self.last_containers_api = "идёт трейд — попробуй чуть позже"
            return self.last_containers_api
        cfg = self.containers_api_cfg
        init_data, state = await self._fetch_state(cfg)
        if not init_data:
            self.last_containers_api = f"⚠️ не получил initData мини-аппа ({clock()})"
            return self.last_containers_api
        if not state:
            self.last_containers_api = f"⚠️ магазин не ответил ({clock()})"
            return self.last_containers_api
        return await self._execute_buy(cfg, init_data, state)

    # ---------- фоновый цикл: активен — опрашиваем часто, иначе ждём next_supply ----------
    async def _containers_api_loop(self) -> None:
        while self.running:
            try:
                if self._trade_mode or not self._containers_api_active():
                    await asyncio.sleep(30)
                    continue
                cfg = self.containers_api_cfg
                far_interval = max(30, int(cfg.get("poll_far_interval", 300)))

                init_data, state = await self._fetch_state(cfg)
                if not init_data or not state:
                    await asyncio.sleep(far_interval)
                    continue

                s = state.get("state") or {}
                if s.get("active"):
                    # магазин открыт с остатком ПРЯМО СЕЙЧАС — раньше цикл смотрел
                    # ТОЛЬКО на расчётное окно перед next_supply и мог полностью
                    # пропустить уже идущий завоз, если next_supply указывал на
                    # СЛЕДУЮЩИЙ по счёту ресток (далеко впереди), а не на текущий,
                    # уже открытый (репорт: «конты были 20 минут назад, а скрипту
                    # похуй абсолютно» — цикл ни разу не пытался купить, просто спал
                    # far_interval). Пока магазин активен, опрашиваем/покупаем часто
                    # (active_poll_interval), а не ждём далёкий far_interval
                    result = await self._execute_buy(cfg, init_data, state)
                    active_poll = max(1, float(cfg.get("active_poll_interval", 5)))
                    await asyncio.sleep(far_interval if result.startswith("📦") else active_poll)
                    continue

                next_supply_ts = _parse_iso_ts(s.get("next_supply"))
                server_time = state.get("server_time")
                if not next_supply_ts or not server_time:
                    await asyncio.sleep(far_interval)
                    continue

                # next_supply — абсолютный ISO-таймстамп с часовым поясом, server_time —
                # абсолютное время сервера на момент ЭТОГО ответа (обе метки сняты
                # практически в один момент с этим вызовом) — разница между ними и есть
                # оставшееся время, без нужды сверяться с локальными часами машины бота
                remaining = next_supply_ts - float(server_time)
                burst_window = max(10, int(cfg.get("burst_window_seconds", 90)))

                if remaining > burst_window:
                    await asyncio.sleep(min(remaining - burst_window, far_interval))
                    continue

                # в пределах окна ресто — плотный опрос/покупка, пока не купим или не истечёт окно
                burst_interval = max(0.5, float(cfg.get("burst_interval", 1.5)))
                burst_duration = max(10, int(cfg.get("burst_duration", 90)))
                burst_until = time.time() + burst_duration
                while self.running and time.time() < burst_until:
                    result = await self.buy_containers_api_now()
                    if result.startswith("📦"):
                        break
                    await asyncio.sleep(burst_interval)

                # не спамить магазин сразу же после всплеска — небольшая пауза перед
                # следующей проверкой next_supply (следующая поставка обычно через дни)
                await asyncio.sleep(far_interval)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_containers_api = f"ошибка: {e}"
                await asyncio.sleep(60)
