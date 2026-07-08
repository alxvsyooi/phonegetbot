@echo off
chcp 65001 >nul
rem Видимая консоль для отладки (логи [trade] видно вживую). Авто-перезапуск при падении.
cd /d "%~dp0"
:loop
python main.py
echo.
echo [%date% %time%] Bot exited. Restarting in 10 seconds... (Ctrl+C to stop)
timeout /t 10 /nobreak >nul
goto loop
