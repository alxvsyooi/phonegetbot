' Запускает бота в трее БЕЗ окна консоли (pythonw tray.py).
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
sh.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
' 0 = скрытое окно; pythonw и так без консоли
sh.Run "pythonw.exe tray.py", 0, False
