@echo off
rem Добавляет автозапуск бота при входе в Windows (ярлык в папке Автозагрузка).
rem Просто двойной клик по этому файлу.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws=New-Object -ComObject WScript.Shell;" ^
  "$lnk=$ws.CreateShortcut((Join-Path $ws.SpecialFolders('Startup') 'PhoneGetBot.lnk'));" ^
  "$lnk.TargetPath='wscript.exe';" ^
  "$lnk.Arguments='\"%~dp0start_hidden.vbs\"';" ^
  "$lnk.WorkingDirectory='%~dp0';" ^
  "$lnk.Save();" ^
  "Write-Host 'Автозапуск добавлен: бот будет стартовать в трее при входе в Windows.'"
echo.
pause
