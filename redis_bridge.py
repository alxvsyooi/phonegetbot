"""Redis Pub/Sub мост между Celery/фарм-воркерами и открытыми Dashboard-экранами.

Слушает два фиксированных канала:
  - "dashboard:events" — тик от Celery Beat (celery_tasks.tick_dashboards), просто
    повод перерисовать открытые дэшборды, чтобы countdown "⏳ Next: ..." не отставал.
  - "bot:notify" — публикуется из Manager.send_alert (manager.py) КАЖДЫЙ раз, когда
    фарм-воркер уже отправил алерт напрямую пользователю (captcha, restock,
    session-error и т.п.); здесь это только сигнал "у этого owner_id могли
    измениться данные — если у него открыт Dashboard, перерисуй".

Никакого Pyrogram/Storage доступа тут нет — вся перерисовка идёт через уже
существующий Pyrogram-клиент внутри ControlBot (control.refresh_dashboard).
Ошибка в одной итерации не должна убивать весь мост — вокруг обработки каждого
события try/except с логом, цикл продолжается.
"""
from __future__ import annotations

from redis_client import RedisClient


async def run(redis_client: RedisClient, control) -> None:
    if not redis_client.enabled:
        return
    stream = await redis_client.subscribe("dashboard:events", "bot:notify")
    if stream is None:
        return
    async for payload in stream:
        try:
            msg_type = payload.get("type")
            if msg_type == "tick":
                for user_id, _ in control.dashboards.items():
                    await control.refresh_dashboard(user_id)
            elif msg_type == "alert":
                owner_id = payload.get("owner_id")
                if owner_id is not None and control.dashboards.get(owner_id):
                    await control.refresh_dashboard(owner_id)
        except Exception as e:  # noqa: BLE001
            print(f"[redis_bridge] ошибка обработки события: {e}")
