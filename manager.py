"""Менеджер воркеров аккаунтов + трейд.

Получатели (для /pay и для трейда) — НЕ подразумеваются автоматически.
Каждый аккаунт хранит явные payout_target/trade_target (@username или id),
которые пользователь вводит сам через бота. Если получатель трейда совпадает
с ДРУГИМ аккаунтом ТОГО ЖЕ владельца, который сейчас запущен — обмен идёт
полностью автоматически с обеих сторон (TradeSession). Иначе — только сторона
фарма автоматизируется, получатель принимает/подтверждает вручную (SoloTradeSession).
"""
from __future__ import annotations

import asyncio
import time
from contextlib import AsyncExitStack

from pyrogram.errors import FloodWait

from automation import AccountWorker
from common import clock
from storage import Storage


class Manager:
    def __init__(
        self,
        storage: Storage,
        good_keywords=None,
        payout_delay: int = 120,
        trade_cfg: dict | None = None,
        container_cfg: dict | None = None,
        self_commands_cfg: dict | None = None,
        shop_cfg: dict | None = None,
        repair_cfg: dict | None = None,
        farm_maintenance_cfg: dict | None = None,
        exchange_cfg: dict | None = None,
        avito_cfg: dict | None = None,
        containers_api_cfg: dict | None = None,
    ) -> None:
        self.storage = storage
        self.good_keywords = good_keywords or []
        self.payout_delay = payout_delay
        self.trade_cfg = trade_cfg or {}
        self.container_cfg = container_cfg or {}
        self.self_commands_cfg = self_commands_cfg or {}
        self.shop_cfg = shop_cfg or {}
        self.repair_cfg = repair_cfg or {}
        self.farm_maintenance_cfg = farm_maintenance_cfg or {}
        self.exchange_cfg = exchange_cfg or {}
        self.avito_cfg = avito_cfg or {}
        self.containers_api_cfg = containers_api_cfg or {}
        self.workers: dict[int, AccountWorker] = {}
        self._trade_locks: dict[int, asyncio.Lock] = {}  # acc_id -> лок (см. run_trade)
        self._owner_trade_locks: dict[int, asyncio.Lock] = {}  # owner_id -> общая очередь
                                                                 # трейдов одного владельца (см. run_trade)
        # выставляется в main.py ПОСЛЕ создания ControlBot: даёт воркерам возможность
        # слать владельцу интерактивные алерты (инлайн-кнопки) через управляющего бота,
        # а не текстом от самого фарм-аккаунта (у обычных user-сессий callback-кнопки
        # физически не обрабатываются)
        self.bot_app = None
        # выставляется в main.py вместе с bot_app: даёт воркерам live-статус-хранилище
        # (Redis) и шину Pub/Sub-уведомлений для нового Dashboard — см. redis_client.py.
        # None/выключен = функции ниже становятся no-op, автоматизация не зависит от Redis.
        self.redis_client = None

    async def send_alert(self, owner_id: int, text: str, markup=None) -> None:
        if not self.bot_app or not owner_id:
            return
        try:
            await self.bot_app.send_message(owner_id, text, reply_markup=markup)
        except Exception as e:  # noqa: BLE001
            print(f"[manager] не удалось отправить алерт {owner_id}: {e}")
            return
        # алерт уже доставлен пользователю напрямую (выше) — публикация в Redis нужна
        # только чтобы уже ОТКРЫТЫЙ Dashboard-экран у этого owner_id перерисовался
        if self.redis_client is not None:
            await self.redis_client.publish("bot:notify", {"type": "alert", "owner_id": owner_id})

    # ---------- жизненный цикл ----------
    async def start_all(self) -> None:
        for acc in self.storage.accounts:
            await self.start_account(acc["id"])

    async def start_account(self, acc_id: int) -> None:
        acc = self.storage.get(acc_id)
        if not acc or acc_id in self.workers:
            return
        worker = AccountWorker(
            acc,
            storage=self.storage,
            good_keywords=self.good_keywords,
            payout_delay=self.payout_delay,
            container_cfg=self.container_cfg,
            self_commands_cfg=self.self_commands_cfg,
            shop_cfg=self.shop_cfg,
            repair_cfg=self.repair_cfg,
            farm_maintenance_cfg=self.farm_maintenance_cfg,
            exchange_cfg=self.exchange_cfg,
            avito_cfg=self.avito_cfg,
            containers_api_cfg=self.containers_api_cfg,
        )
        worker.trade_runner = self.run_trade
        worker.alert_fn = self.send_alert
        worker.redis_client = self.redis_client
        self.workers[acc_id] = worker
        print(f"[manager] подключаю «{acc.get('name')}» (id={acc_id})...")
        try:
            await asyncio.wait_for(worker.start(), timeout=60)
        except asyncio.TimeoutError:
            worker.status = "таймаут подключения"
            self.workers.pop(acc_id, None)
            print(f"[manager] ⏱ таймаут «{acc.get('name')}» — пропускаю (FloodWait/сеть/сессия?)")
            return
        except FloodWait as e:
            worker.status = f"FloodWait {e.value}s"
            self.workers.pop(acc_id, None)
            print(f"[manager] 🛑 «{acc.get('name')}»: FloodWait {e.value}с — пропускаю")
            return
        except Exception as e:  # noqa: BLE001
            worker.status = f"ошибка старта: {type(e).__name__}: {e}"
            self.workers.pop(acc_id, None)
            print(f"[manager] НЕ запустился «{acc.get('name')}»: {type(e).__name__}: {e}")
            return
        print(f"[manager] ✅ «{acc.get('name')}» подключён")

    async def stop_account(self, acc_id: int) -> None:
        worker = self.workers.pop(acc_id, None)
        if worker:
            await worker.stop()

    async def stop_all(self) -> None:
        for acc_id in list(self.workers):
            await self.stop_account(acc_id)

    # ---------- вотчдог: перезапуск воркеров, чьи циклы упали незаметно ----------
    async def watchdog_loop(self, check_interval: int = 60, max_restarts_per_hour: int = 3) -> None:
        """Каждый фоновый цикл (farm.py/shop.py/repair.py/containers_api.py/autosend.py)
        сам ловит Exception и продолжает работать через backoff — таск может завершиться
        незапланированно, только если что-то вылетело МИМО этих except (реальный баг) или
        клиент отвалился так, что ни один цикл этого не заметил. worker.running=False,
        выставленный самим _handle_dead_session (сессия мертва, нужен перелогин) — это
        осознанная остановка, не падение, вотчдог её не трогает и не перезапускает
        (перезапуск с тем же session_string ничего не исправит).

        Если worker.running всё ещё True, а часть его тасков уже .done() — значит цикл
        упал незаметно для всех остальных: аккаунт продолжает числиться «работает», но
        часть автоматизации молча стоит. Перезапускаем аккаунт (stop_account+start_account
        пересоздаёт клиента и все таски с нуля). Ограничение по частоте — чтобы аккаунт,
        падающий сразу после старта (сломанная сессия, специфичная для него), не долбился
        в бесконечном рестарт-цикле — после max_restarts_per_hour просто алертим владельца
        и перестаём трогать аккаунт до ручной проверки."""
        restart_log: dict[int, list[float]] = {}
        while True:
            await asyncio.sleep(check_interval)
            for acc_id, worker in list(self.workers.items()):
                if not worker.running:
                    continue
                dead = [t for t in worker._tasks if t.done()]
                if not dead:
                    continue
                for t in dead:
                    exc = None
                    if not t.cancelled():
                        try:
                            exc = t.exception()
                        except asyncio.CancelledError:
                            pass
                    print(f"[watchdog] «{worker.name}» (id={acc_id}): фоновый цикл упал "
                          f"незаметно{f': {exc!r}' if exc else ''}")

                now = time.time()
                hist = [ts for ts in restart_log.get(acc_id, []) if now - ts < 3600]
                if len(hist) >= max_restarts_per_hour:
                    print(f"[watchdog] «{worker.name}»: уже {len(hist)} рестартов за час — "
                          f"автоперезапуск остановлен, нужна ручная проверка")
                    owner_id = worker.account.get("owner_id")
                    if owner_id:
                        await self.send_alert(
                            owner_id,
                            f"⚠️ <b>Watchdog</b>: «{worker.name}» падает повторно "
                            f"({len(hist)}+ раз за последний час) — автоперезапуск "
                            f"остановлен, посмотри вручную (возможно, сломана сессия).",
                        )
                    continue
                restart_log[acc_id] = hist + [now]

                print(f"[watchdog] «{worker.name}»: перезапускаю аккаунт")
                await self.stop_account(acc_id)
                await self.start_account(acc_id)

    # ---------- получатели ----------
    def copy_targets(self, owner_id: int, src_id: int) -> int:
        """Скопировать payout_target/trade_target аккаунта src на ВСЕ остальные
        аккаунты того же владельца. Явное действие пользователя (кнопка), не автомат."""
        src = self.storage.get(src_id)
        if not src or src.get("owner_id") != owner_id:
            return 0
        # железная привязка: числовой tg_id надёжнее username (тот можно сменить/потерять)
        ident = str(src.get("tg_id")) if src.get("tg_id") else (
            f"@{src['username']}" if src.get("username") else ""
        )
        if not ident:
            return 0
        n = 0
        for a in self.storage.accounts:
            if a.get("owner_id") == owner_id and a["id"] != src_id:
                a["payout_target"] = ident
                a["trade_target"] = ident
                n += 1
        if n:
            self.storage.save()
        return n

    def _match_own_worker(self, owner_id: int, exclude_id: int, target: str):
        """Ищет СВОЙ запущенный аккаунт того же владельца, совпадающий с target
        (по @username или numeric id). None, если получатель — кто-то другой/не запущен."""
        tnorm = target.lstrip("@").lower()
        for w in self.workers.values():
            a = w.account
            if a.get("owner_id") != owner_id or a["id"] == exclude_id:
                continue
            uname = (a.get("username") or "").lower()
            tgid = str(a.get("tg_id") or "")
            if tnorm and (tnorm == uname or tnorm == tgid):
                return w
        return None

    # ---------- трейд ----------
    def _lock_for(self, acc_id: int) -> asyncio.Lock:
        lock = self._trade_locks.get(acc_id)
        if lock is None:
            lock = asyncio.Lock()
            self._trade_locks[acc_id] = lock
        return lock

    def _lock_for_owner(self, owner_id: int) -> asyncio.Lock:
        lock = self._owner_trade_locks.get(owner_id)
        if lock is None:
            lock = asyncio.Lock()
            self._owner_trade_locks[owner_id] = lock
        return lock

    async def run_trade(self, farm_id: int, target_override: str | None = None) -> str:
        """Слить телефоны фарма на trade_target. Повтор, пока коллекция > repeat_threshold.

        target_override — разовый получатель для ручного трейда (кнопка «🔄 Трейд с
        человеком» в ▶️ Действия), независимо от настроенного trade_target. None —
        обычный авто-трейд по настроенному получателю.

        Если получатель — тоже локально управляемый аккаунт (main), он одновременно
        может быть только в ОДНОМ обмене (обе стороны занимают клиент). Поэтому
        run_trade берёт блокировку на farm и на main (если есть): если получатель
        сейчас занят другим фармом — этот вызов встаёт в очередь и ждёт своего часа,
        а не проваливается мгновенно. Разные пары фарм/получатель работают параллельно.

        ДОПОЛНИТЕЛЬНО — общая очередь на владельца (_lock_for_owner): с несколькими
        аккаунтами на одинаковом интервале авто-трейда (напр. 11 аккаунтов раз в 48ч)
        их next_ts может почти совпасть, и раньше они все стартовали бы трейд
        параллельно ("кто успеет"). Теперь у одного владельца одновременно идёт
        РОВНО один трейд — остальные ждут здесь своей очереди (репорт пользователя:
        хочет по одному, а не всех разом)."""
        from trade import TradeSession, SoloTradeSession

        farm = self.workers.get(farm_id)
        if not farm or not farm.running:
            return "фарм-аккаунт не запущен"
        target = (target_override or farm.account.get("trade_target") or "").strip()
        if not target:
            return "не задан получатель трейда — задайте в 🎯 Получатели"

        owner = farm.account.get("owner_id")
        owner_lock = self._lock_for_owner(owner)
        if owner_lock.locked():
            farm.last_exchange = f"⏳ трейд в очереди — ждёт своего аккаунта ({clock()})"

        async with owner_lock:
            main = self._match_own_worker(owner, farm_id, target)
            if main and not main.running:
                main = None  # свой аккаунт есть, но не запущен — работаем как с внешним

            # если не нашли «свой» аккаунт под target — раньше это молча превращалось в
            # SoloTradeSession (ждёт, пока ЧЕЛОВЕК вручную нажмёт «Принять» — а получатель
            # тоже управляется этим ботом без человека рядом, обмен просто висит до
            # таймаута, репорт пользователя). Диагностика для самопроверки: показываем, с
            # какими identifiers своих аккаунтов target НЕ совпал — обычно опечатка/
            # устаревший @username (см. фикс синхронизации username в automation.py)
            mismatch_hint = ""
            if main is None:
                siblings = [
                    w for w in self.workers.values()
                    if w.account.get("owner_id") == owner and w.id != farm_id
                ]
                if siblings:
                    idents = ", ".join(
                        f"{s.name}=@{s.account.get('username') or '?'}/{s.account.get('tg_id') or '?'}"
                        for s in siblings
                    )
                    mismatch_hint = (
                        f" ⚠️ получатель «{target}» не совпал ни с одним своим аккаунтом "
                        f"({idents}) — если это должен быть свой аккаунт, обмен зависнет "
                        f"до таймаута (никто не нажмёт «Принять»); проверь username/id"
                    )
                    print(f"[manager] трейд {farm.name} -> «{target}»: {mismatch_hint}")

            # блокируем ОБОИХ участников в стабильном порядке (по id) — без этого два
            # обмена с общим участником могут задедлочиться, ожидая друг друга крест-накрест
            # (владельческая очередь выше уже гарантирует единственность в рамках owner,
            # но main может принадлежать ДРУГОМУ владельцу — эта блокировка всё ещё нужна)
            lock_ids = sorted({farm.id} | ({main.id} if main else set()))
            async with AsyncExitStack() as stack:
                for lid in lock_ids:
                    await stack.enter_async_context(self._lock_for(lid))
                result = await self._run_trade_passes(farm, main, target)
                return result + mismatch_hint

    async def _run_trade_passes(self, farm, main, target: str) -> str:
        from trade import TradeSession, SoloTradeSession

        cfg = self.trade_cfg or {}
        threshold = int(cfg.get("repeat_threshold", 1))
        max_passes = int(cfg.get("max_passes", 20))
        last = ""
        for n in range(1, max_passes + 1):
            if main:
                session = TradeSession(farm, main, cfg, main.account.get("tg_id"))
            else:
                session = SoloTradeSession(farm, target, cfg)
            res = await session.run()
            last = res
            if "успешно" not in res:
                return f"проход {n}: {res}"
            left = session.collection_left
            if left is None:
                return f"проход {n} ок, но не прочитал коллекцию. {res}"
            if left <= threshold:
                return f"✅ готово за {n} проход(ов), осталось {left}"
        return f"стоп: лимит {max_passes} проходов. Последнее: {last}"
