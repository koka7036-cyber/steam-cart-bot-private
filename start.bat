@echo off
chcp 65001 >nul
echo [UPDATE] Проверяю GitHub перед запуском бота...
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "C:\CardBot\update_bot.ps1"
if errorlevel 1 echo [WARN] Обновление пропущено; запускается сохранённая версия.

title Управление фермой: ASF + Steam Курьер + FunPay 24/7

echo ===================================================
echo [1/2] Проверка запуска ArchiSteamFarm (ASF)...
echo ===================================================
tasklist /FI "IMAGENAME eq ArchiSteamFarm.exe" 2>NUL | find /I /N "ArchiSteamFarm.exe">NUL
if "%ERRORLEVEL%"=="0" (
    echo [OK] ASF уже работает! Повторный запуск не требуется.
) else (
    echo [СТАРТ] Запускаю ASF в отдельном окне...
    start "ArchiSteamFarm" /D "C:\CardBot\ASF-win-x64 (1)" "ArchiSteamFarm.exe"
    echo Ждем 10 секунд, пока ASF войдет в Steam и включит IPC...
    timeout /t 10 /nobreak >nul
)

echo.
echo ===================================================
echo [2/2] Запуск единого бота (Steam + FunPay 24/7)...
echo ===================================================
:loop
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "C:\CardBot\run_bot.ps1"
if errorlevel 23 (
    echo [INFO] Бот уже запущен; повторный запуск пропущен.
    exit /b 0
)

echo Бот перезапускается через 5 секунд...
timeout /t 5 /nobreak >nul
goto loop
