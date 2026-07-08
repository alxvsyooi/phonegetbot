@echo off
rem Убирает автозапуск (удаляет ярлык из папки Автозагрузка).
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws=New-Object -ComObject WScript.Shell;" ^
  "$p=Join-Path $ws.SpecialFolders('Startup') 'PhoneGetBot.lnk';" ^
  "if(Test-Path $p){Remove-Item $p -Force; Write-Host 'Автозапуск убран.'}else{Write-Host 'Автозапуск не был установлен.'}"
echo.
pause
