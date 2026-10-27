@echo off
chcp 65001 >nul
title CardBot — Playerok setup
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "C:\CardBot\configure_playerok.ps1"
if errorlevel 1 (
  echo.
  echo Setup failed. The existing bot configuration was not reset.
)
echo.
pause
