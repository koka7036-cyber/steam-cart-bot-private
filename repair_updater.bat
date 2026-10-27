@echo off
setlocal
chcp 65001 >nul
title CardBot — восстановление автообновления
echo =====================================================
echo Восстановление обновлятора CardBot
echo =====================================================
echo.
echo Локальные изменения в C:\CardBot_repo не будут удалены.
echo Текущий рабочий бот и настройки не изменяются.
echo.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $repo='C:\CardBot_repo'; $live='C:\CardBot'; if(-not (Test-Path -LiteralPath (Join-Path $repo '.git'))){throw 'Не найден C:\CardBot_repo. Ничего не изменено.'}; if(-not (Test-Path -LiteralPath $live)){throw 'Не найдена рабочая папка C:\CardBot. Ничего не изменено.'}; $null = & git -C $repo fetch --quiet origin '+refs/heads/main:refs/remotes/origin/main' 2>&1; if($LASTEXITCODE -ne 0){throw 'Не удалось получить main с GitHub. Ничего не изменено.'}; $stamp=Get-Date -Format 'yyyyMMdd_HHmmss'; $backupRoot=Join-Path $live 'update_backups'; New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null; $current=Join-Path $live 'update_bot.ps1'; if(Test-Path -LiteralPath $current){Copy-Item -LiteralPath $current -Destination (Join-Path $backupRoot ('update_bot_before_bootstrap_'+$stamp+'.ps1')) -Force}; $temp=Join-Path $env:TEMP ('CardBotBootstrap_'+[guid]::NewGuid().ToString('N')); $zip=$temp+'.zip'; $null = & git -C $repo archive --format=zip ('--output='+$zip) 'origin/main' 'update_bot.ps1' 2>&1; if($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $zip)){throw 'Не удалось подготовить обновлятор из GitHub. Рабочая версия не изменена.'}; Expand-Archive -LiteralPath $zip -DestinationPath $temp -Force; $source=Join-Path $temp 'update_bot.ps1'; if(-not (Test-Path -LiteralPath $source)){throw 'В архиве не найден update_bot.ps1. Рабочая версия не изменена.'}; Copy-Item -LiteralPath $source -Destination $current -Force; Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue; Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue; Write-Host 'ГОТОВО: обновлятор восстановлен. Теперь запусти C:\CardBot\start.bat.' -ForegroundColor Green"
if errorlevel 1 (
  echo.
  echo ОШИБКА: обновлятор не заменён. Скопируй текст ошибки для проверки.
) else (
  echo.
  echo Готово. Можно закрыть это окно и запустить C:\CardBot\start.bat.
)
echo.
pause
