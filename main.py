"""Точка входа: управляющий бот (кнопки) + воркеры аккаунтов + ежедневный отчёт.

Мультипользовательский: каждый пользователь видит/настраивает только свои аккаунты.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime

from automation import MSK, _parse_hhmm
from controlbot import ControlBot
from manager import Manager
from storage import Storage, load_settings


def _build_report(accounts: list[dict]) -> str:
    lines = [f"📊 Отчёт за сутки ({datetime.now(MSK).strftime('%d.%m.%Y')})", ""]
    tot = {"phones": 0, "good_phones": 0, "roulette": 0, "mining": 0, "paid": 0,
           "exchanged": 0, "daily": 0, "containers_bought": 0}
    for acc in accounts:
        d = acc.get("stats_day", {})
        for k in tot:
            tot[k] += d.get(k, 0)
        lines.append(
            f"• {acc.get('name')}: 📱{d.get('phones', 0)} (⭐{d.get('good_phones', 0)}) "
            f"🎰{d.get('roulette', 0)} ⛏{d.get('mining', 0)} 🎁{d.get('daily', 0)} "
            f"💸{d.get('paid', 0)} 🔄{d.get('exchanged', 0)} 📦{d.get('containers_bought', 0)}"
        )
    lines += [
        "",
        f"ИТОГО: 📱{tot['phones']} (⭐{tot['good_phones']}) "
        f"🎰{tot['roulette']} ⛏{tot['mining']} 🎁{tot['daily']} "
        f"💸{tot['paid']} 🔄{tot['exchanged']} 📦{tot['containers_bought']}",
    ]
    return "\n".join(lines)


async def daily_report_loop(control: ControlBot, storage: Storage, settings: dict) -> None:
    hour, minute = _parse_hhmm(settings.get("report_time", "00:00"))
    last_date = None
    while True:
        try:
            now = datetime.now(MSK)
            if now.hour == hour and now.minute == minute and last_date != now.date():
                last_date = now.date()
                owners: dict[int, list[dict]] = {}
                for acc in storage.accounts:
                    owners.setdefault(acc.get("owner_id", 0), []).append(acc)
                for owner, accs in owners.items():
                    if not owner:
                        continue
                    try:
                        await control.app.send_message(owner, _build_report(accs))
                    except Exception as e:  # noqa: BLE001
                        print(f"[report] не отправить {owner}: {e}")
                empty = {"phones": 0, "good_phones": 0, "roulette": 0,
                         "mining": 0, "paid": 0, "exchanged": 0, "daily": 0,
                         "containers_bought": 0}
                for acc in storage.accounts:
                    acc["stats_day"] = dict(empty)
                storage.save()
        except Exception as e:  # noqa: BLE001
            print(f"[report] ошибка: {e}")
        await asyncio.sleep(20)


def _migrate_owners(storage: Storage, settings: dict) -> None:
    """ОДНОРАЗОВАЯ миграция аккаунтов, созданных до мультипользовательского режима
    и до явных payout_target/trade_target: назначаем владельца и переносим прежнее
    поведение (вывод/трейд на main_account_id) в явные поля, чтобы ничего не сломалось."""
    admin_ids = settings.get("admin_ids", [])
    orig_owner = settings.get("main_account_id") or (admin_ids[0] if admin_ids else 0)
    legacy_main_tg = settings.get("main_account_id")
    changed = False
    for acc in storage.accounts:
        if not acc.get("owner_id"):
            acc["owner_id"] = orig_owner
            changed = True

    by_owner: dict[int, list[dict]] = {}
    for acc in storage.accounts:
        by_owner.setdefault(acc.get("owner_id", 0), []).append(acc)

    for owner, accs in by_owner.items():
        if not owner:
            continue
        main = next((a for a in accs if a.get("is_main")), None)
        if main is None:
            main = next((a for a in accs if a.get("tg_id") == legacy_main_tg), accs[0])
            main["is_main"] = True
            changed = True
        ident = main.get("username") or (str(main.get("tg_id")) if main.get("tg_id") else "")
        if not ident:
            continue
        for a in accs:
            if a is not main and not a.get("payout_target") and not a.get("trade_target"):
                a["payout_target"] = ident
                a["trade_target"] = ident
                changed = True
    if changed:
        storage.save()


async def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except Exception:
            pass

    print("[startup] загрузка настроек...")
    settings = load_settings()  # сама бросит SystemExit с понятным текстом, если чего-то не хватает

    storage = Storage()
    _migrate_owners(storage, settings)

    manager = Manager(
        storage,
        good_keywords=settings.get("good_phone_keywords", []),
        payout_delay=int(settings.get("payout_delay", 120)),
        trade_cfg=settings.get("trade", {}),
        container_cfg=settings.get("containers", {}),
        self_commands_cfg=settings.get("self_commands", {}),
    )
    control = ControlBot(settings, storage, manager)
    manager.bot_app = control.app  # чтобы фарм-воркеры могли слать интерактивные алерты (капча и т.п.)

    print("[startup] подключаю управляющего бота...")
    await control.app.start()
    print("[startup] управляющий бот ЗАПУЩЕН ✅")
    mode = "мультипользовательский" if control.multi_user else "только админы"
    print(f"[startup] режим доступа: {mode}")
    print("[startup] запускаю аккаунты...")
    await manager.start_all()
    print(f"[startup] активных аккаунтов: {len(manager.workers)}/{len(storage.accounts)}")
    print("[startup] готово. Открой бота в Telegram и нажми /start")

    report_task = asyncio.create_task(daily_report_loop(control, storage, settings))
    try:
        await asyncio.Event().wait()
    finally:
        report_task.cancel()
        await manager.stop_all()
        await control.app.stop()


if __name__ == "__main__":
    import traceback
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Остановлено пользователем.")
    except Exception:  # noqa: BLE001
        print("!!! КРИТИЧЕСКАЯ ОШИБКА ПРИ ЗАПУСКЕ:")
        traceback.print_exc()
