"""Celery-приложение — отдельный процесс (см. Procfile: celeryworker/celerybeat).

ВАЖНО: этот процесс НЕ создаёт Pyrogram Client и не трогает accounts.json на
запись — он только планирует тики и публикует события в Redis (см. план
"Ключевое архитектурное решение" — Celery дёргает существующий persistent-
воркер аккаунта через Redis Pub/Sub, а не дублирует его). Поэтому здесь не
импортируются ни storage.Storage, ни manager.Manager, ни automation.py.

Настройка через REDIS_URL (env) в первую очередь — так проще для Railway,
где Redis-плагин сам прокидывает переменную окружения во все сервисы одного
проекта. Фоллбэк — redis_url из settings.json, для локального запуска без env.
"""
from __future__ import annotations

import json
import os

from celery import Celery


def _redis_url() -> str:
    url = os.environ.get("REDIS_URL")
    if url:
        return url
    try:
        with open(os.path.join(os.path.dirname(__file__), "settings.json"), encoding="utf-8") as f:
            return json.load(f).get("redis_url") or "redis://localhost:6379/0"
    except (FileNotFoundError, json.JSONDecodeError):
        return "redis://localhost:6379/0"


REDIS_URL = _redis_url()

app = Celery("phonegetbot", broker=REDIS_URL, backend=REDIS_URL, include=["celery_tasks"])

app.conf.beat_schedule = {
    "dashboard-tick": {
        "task": "celery_tasks.tick_dashboards",
        "schedule": 20.0,  # та же частота, что у asyncio.sleep-поллинга в farm.py — дэшборд не «моложе» факта
    },
}
app.conf.timezone = "UTC"
app.conf.task_default_queue = "phonegetbot"
