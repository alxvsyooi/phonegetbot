@echo off
rem Запуск бота в трее без консоли (через скрытый VBS-лаунчер)
cd /d "%~dp0"
start "" wscript.exe "%~dp0start_hidden.vbs"
exit
