"""Запуск бота в фоне со значком в системном трее.

Бот крутится в фоновом потоке, в трее — иконка с меню:
  • Открыть лог  — открыть bot.log
  • Выход        — закрыть бота

Запуск без окна консоли: pythonw tray.py (или через start_hidden.vbs).
Свою иконку можно положить рядом файлом icon.ico — она подхватится автоматически.
"""
from __future__ import annotations

import os
import sys
import asyncio
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "bot.log")

_bot_thread: threading.Thread | None = None


def _make_icon_image():
    """Иконка из icon.ico рядом с файлом, либо простая нарисованная."""
    from PIL import Image, ImageDraw

    ico = os.path.join(HERE, "icon.ico")
    if os.path.exists(ico):
        try:
            return Image.open(ico)
        except Exception:
            pass
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([18, 5, 46, 59], radius=7, fill=(43, 82, 120, 255))
    d.rectangle([22, 12, 42, 49], fill=(232, 240, 255, 255))
    d.ellipse([30, 51, 34, 55], fill=(232, 240, 255, 255))
    return img


def _run_bot() -> None:
    from main import main as bot_main

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(bot_main())
    except Exception as e:  # noqa: BLE001
        print(f"[tray] бот остановился с ошибкой: {type(e).__name__}: {e}")


def _on_open_log(icon, item) -> None:
    try:
        os.startfile(LOG_PATH)  # noqa: SIM115 (Windows)
    except Exception as e:  # noqa: BLE001
        print(f"[tray] не открыть лог: {e}")


def _on_exit(icon, item) -> None:
    print("[tray] выход по запросу пользователя")
    icon.stop()
    os._exit(0)  # жёстко глушим фоновый поток бота — для персонального инструмента ок


def main() -> None:
    # консоли нет (pythonw) — весь вывод пишем в файл
    logf = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    sys.stdout = logf
    sys.stderr = logf
    print("[tray] старт")

    import pystray

    global _bot_thread
    _bot_thread = threading.Thread(target=_run_bot, daemon=True)
    _bot_thread.start()

    icon = pystray.Icon(
        "phonebot",
        _make_icon_image(),
        "PhoneGet Bot",
        menu=pystray.Menu(
            pystray.MenuItem("Открыть лог", _on_open_log),
            pystray.MenuItem("Выход", _on_exit),
        ),
    )
    icon.run()


if __name__ == "__main__":
    main()
