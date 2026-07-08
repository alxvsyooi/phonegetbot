"""Перелогин аккаунта (обновить session string) или добавить новый — из консоли.

Нужно, если сессия слетела (бот не может запустить аккаунт). Работает без бота.
⚠️ Перед запуском ОСТАНОВИ бота (трей → «Выход» / Ctrl+C), иначе аккаунт подключится дважды.

Запуск:  python relogin.py
"""
from __future__ import annotations

import asyncio

from pyrogram import Client
from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired

from storage import Storage


async def main() -> None:
    st = Storage()
    print("=== Перелогин / добавление аккаунта ===\n")
    if st.accounts:
        print("Текущие аккаунты:")
        for a in st.accounts:
            print(f"  id={a['id']}  {a.get('name')}  (tg_id={a.get('tg_id')}, @{a.get('username','')})")
    print()
    raw = input("ID для ПЕРЕЛОГИНА (Enter/0 — добавить новый): ").strip()
    acc = st.get(int(raw)) if raw.isdigit() and int(raw) > 0 else None

    if acc:
        api_id, api_hash = int(acc["api_id"]), acc["api_hash"]
        print(f"Перелогиниваю «{acc.get('name')}».")
    else:
        print("Добавление НОВОГО аккаунта.")
        api_id = int(input("api_id: ").strip())
        api_hash = input("api_hash: ").strip()

    phone = input("Телефон (+79991234567): ").strip()
    client = Client("relogin_tmp", api_id=api_id, api_hash=api_hash, in_memory=True)
    await client.connect()
    sent = await client.send_code(phone)
    print("\nКод отправлен. Вводи С ПРОБЕЛАМИ (1 2 3 4 5).")
    code = input("Код: ").replace(" ", "").replace("-", "")
    try:
        await client.sign_in(phone, sent.phone_code_hash, code)
    except SessionPasswordNeeded:
        await client.check_password(input("Пароль 2FA: "))
    except (PhoneCodeInvalid, PhoneCodeExpired) as e:
        print(f"Код неверный/истёк: {e}. Запусти заново.")
        await client.disconnect()
        return

    me = await client.get_me()
    session = await client.export_session_string()
    await client.disconnect()

    if acc:
        acc.update({"session_string": session, "tg_id": me.id, "username": me.username or ""})
        st.save()
        print(f"\n✅ Обновлён «{acc.get('name')}» (tg_id={me.id}, @{me.username}).")
    else:
        name = input("Имя (Enter = по юзернейму): ").strip() or (me.username or str(me.id))
        st.add({
            "name": name, "api_id": api_id, "api_hash": api_hash,
            "session_string": session, "tg_id": me.id, "username": me.username or "",
        })
        print(f"\n✅ Добавлен «{name}» (tg_id={me.id}, @{me.username}).")
    print("Готово. Запусти бота снова.")


if __name__ == "__main__":
    asyncio.run(main())
