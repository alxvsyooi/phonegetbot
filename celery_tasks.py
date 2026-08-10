"""Celery-задачи. В пилотной фазе — ровно одна: тик для UI-дэшборда.

Задача умышленно ничего не решает и ни к чему не подключается, кроме Redis:
она просто публикует событие, а вся логика "что перерисовать и для кого" —
на стороне главного процесса бота (redis_bridge.py), у которого есть открытые
Pyrogram-сессии и реестр открытых Dashboard-сообщений.
"""
from __future__ import annotations

import json
import time

import redis as redis_sync  # синхронный клиент — внутри Celery-таски asyncio не нужен

from celery_app import REDIS_URL, app


@app.task(name="celery_tasks.tick_dashboards")
def tick_dashboards() -> None:
    client = redis_sync.from_url(REDIS_URL, decode_responses=True)
    try:
        client.publish("dashboard:events", json.dumps({"type": "tick", "ts": time.time()}))
    finally:
        client.close()
