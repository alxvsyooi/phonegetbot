"""Тонкая обёртка над redis.asyncio для live-статуса модулей и Pub/Sub-уведомлений.

Redis в этом проекте — не источник правды (им остаётся accounts.json/storage.py),
а быстрый слой поверх него: живой статус автоворкеров (next_run, последний
результат) и шина событий между главным процессом бота и Celery-процессами.
Поэтому клиент спроектирован «мягким»: если redis_url не задан или Redis
недоступен, все методы тихо становятся no-op (с одним предупреждением в лог),
а не роняют бота — существующая автоматизация в farm.py не должна зависеть
от того, поднят Redis или нет.
"""
from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

try:
    import redis.asyncio as aioredis
except ImportError:  # пакет redis не установлен — работаем в режиме no-op
    aioredis = None  # type: ignore[assignment]


def status_key(acc_id: int) -> str:
    return f"acct:{acc_id}:status"


def stats_history_key(acc_id: int) -> str:
    return f"acct:{acc_id}:stats_hist"


class RedisClient:
    """Создаётся один раз в main.py и раздаётся по ссылке (Manager/ControlBot/AccountWorker),
    как уже сделано для Manager.bot_app — см. manager.py."""

    def __init__(self, url: str | None) -> None:
        self.url = url or ""
        self.enabled = bool(self.url) and aioredis is not None
        self._r: Any = None
        self._warned = False
        if self.enabled:
            self._r = aioredis.from_url(self.url, decode_responses=True)

    def _warn_once(self, e: Exception) -> None:
        if not self._warned:
            self._warned = True
            print(f"[redis] недоступен, live-статус/уведомления отключены: {e}")

    async def set_status(self, acc_id: int, field: str, value: Any) -> None:
        if not self.enabled:
            return
        try:
            await self._r.hset(status_key(acc_id), field, json.dumps(value))
            await self._r.hset(status_key(acc_id), f"{field}_updated_at", time.time())
        except Exception as e:  # noqa: BLE001
            self._warn_once(e)

    async def get_status(self, acc_id: int, field: str, default: Any = None) -> Any:
        if not self.enabled:
            return default
        try:
            raw = await self._r.hget(status_key(acc_id), field)
            return json.loads(raw) if raw is not None else default
        except Exception as e:  # noqa: BLE001
            self._warn_once(e)
            return default

    async def get_all_status(self, acc_id: int) -> dict[str, Any]:
        if not self.enabled:
            return {}
        try:
            raw = await self._r.hgetall(status_key(acc_id))
        except Exception as e:  # noqa: BLE001
            self._warn_once(e)
            return {}
        out: dict[str, Any] = {}
        for k, v in raw.items():
            if k.endswith("_updated_at"):
                out[k] = float(v)
                continue
            try:
                out[k] = json.loads(v)
            except (TypeError, ValueError):
                out[k] = v
        return out

    # ---------- история статистики (для «Статистика за период», см. controlbot.py) ----------
    # sorted set: score = unix-таймстамп снимка, member = JSON-снимок account["stats"] на тот
    # момент. «Сколько получено за период» = текущая сумма МИНУС ближайший снимок ДО начала
    # периода — без этого пришлось бы отдельно копить дельты по каждому периоду.
    async def record_stats_snapshot(self, acc_id: int, stats: dict[str, Any], ts: float | None = None) -> None:
        if not self.enabled:
            return
        ts = ts if ts is not None else time.time()
        try:
            await self._r.zadd(stats_history_key(acc_id), {json.dumps(stats): ts})
        except Exception as e:  # noqa: BLE001
            self._warn_once(e)

    async def stats_snapshot_before(self, acc_id: int, ts: float) -> dict[str, Any] | None:
        """Ближайший снимок с временем <= ts, или None, если история не тянется так далеко назад."""
        if not self.enabled:
            return None
        try:
            rows = await self._r.zrevrangebyscore(stats_history_key(acc_id), ts, "-inf", start=0, num=1)
        except Exception as e:  # noqa: BLE001
            self._warn_once(e)
            return None
        if not rows:
            return None
        try:
            return json.loads(rows[0])
        except (TypeError, ValueError):
            return None

    async def earliest_stats_snapshot(self, acc_id: int) -> tuple[float, dict[str, Any]] | None:
        """Самый старый снимок в истории — используется как фоллбэк, когда запрошенный период
        (напр. «месяц») длиннее, чем реально накоплено с момента включения этой фичи."""
        if not self.enabled:
            return None
        try:
            rows = await self._r.zrange(stats_history_key(acc_id), 0, 0, withscores=True)
        except Exception as e:  # noqa: BLE001
            self._warn_once(e)
            return None
        if not rows:
            return None
        member, score = rows[0]
        try:
            return float(score), json.loads(member)
        except (TypeError, ValueError):
            return None

    async def prune_stats_history(self, acc_id: int, keep_seconds: float) -> None:
        """Не хранить снимки старше keep_seconds — история нужна только на самое длинное
        поддерживаемое окно (месяц), дальше место можно освобождать."""
        if not self.enabled:
            return
        try:
            cutoff = time.time() - keep_seconds
            await self._r.zremrangebyscore(stats_history_key(acc_id), "-inf", cutoff)
        except Exception as e:  # noqa: BLE001
            self._warn_once(e)

    async def publish(self, channel: str, payload: dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            await self._r.publish(channel, json.dumps(payload))
        except Exception as e:  # noqa: BLE001
            self._warn_once(e)

    async def subscribe(self, *channels: str) -> AsyncIterator[dict[str, Any]] | None:
        """Возвращает async-генератор декодированных payload'ов, или None если Redis выключен.
        Использование — только в redis_bridge.py (единственный подписчик в процессе)."""
        if not self.enabled:
            return None
        pubsub = self._r.pubsub()
        await pubsub.subscribe(*channels)

        async def _gen() -> AsyncIterator[dict[str, Any]]:
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                try:
                    yield json.loads(msg["data"])
                except (TypeError, ValueError, KeyError):
                    continue

        return _gen()

    async def close(self) -> None:
        if self.enabled and self._r is not None:
            try:
                await self._r.aclose()
            except Exception:  # noqa: BLE001
                pass
