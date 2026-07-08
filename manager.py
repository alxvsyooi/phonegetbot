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
from contextlib import AsyncExitStack

from pyrogram.errors import FloodWait

from automation import AccountWorker
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
    ) -> None:
        self.storage = storage
        self.good_keywords = good_keywords or []
        self.payout_delay = payout_delay
        self.trade_cfg = trade_cfg or {}
        self.container_cfg = container_cfg or {}
        self.self_commands_cfg = self_commands_cfg or {}
        self.workers: dict[int, AccountWorker] = {}
        self._trade_locks: dict[int, asyncio.Lock] = {}  # acc_id -> лок (см. run_trade)

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
        )
        worker.trade_runner = self.run_trade
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

    # ---------- получатели ----------
    def copy_targets(self, owner_id: int, src_id: int) -> int:
        """Скопировать payout_target/trade_target аккаунта src на ВСЕ остальные
        аккаунты того же владельца. Явное действие пользователя (кнопка), не автомат."""
        src = self.storage.get(src_id)
        if not src or src.get("owner_id") != owner_id:
            return 0
        ident = src.get("username") or (str(src.get("tg_id")) if src.get("tg_id") else "")
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

    async def run_trade(self, farm_id: int) -> str:
        """Слить телефоны фарма на trade_target. Повтор, пока коллекция > repeat_threshold.

        Если получатель — тоже локально управляемый аккаунт (main), он одновременно
        может быть только в ОДНОМ обмене (обе стороны занимают клиент). Поэтому
        run_trade берёт блокировку на farm и на main (если есть): если получатель
        сейчас занят другим фармом — этот вызов встаёт в очередь и ждёт своего часа,
        а не проваливается мгновенно. Разные пары фарм/получатель работают параллельно."""
        from trade import TradeSession, SoloTradeSession

        farm = self.workers.get(farm_id)
        if not farm or not farm.running:
            return "фарм-аккаунт не запущен"
        target = (farm.account.get("trade_target") or "").strip()
        if not target:
            return "не задан получатель трейда — задайте в 🎯 Получатели"

        owner = farm.account.get("owner_id")
        main = self._match_own_worker(owner, farm_id, target)
        if main and not main.running:
            main = None  # свой аккаунт есть, но не запущен — работаем как с внешним

        # блокируем ОБОИХ участников в стабильном порядке (по id) — без этого два
        # обмена с общим участником могут задедлочиться, ожидая друг друга крест-накрест
        lock_ids = sorted({farm.id} | ({main.id} if main else set()))
        async with AsyncExitStack() as stack:
            for lid in lock_ids:
                await stack.enter_async_context(self._lock_for(lid))
            return await self._run_trade_passes(farm, main, target)

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
