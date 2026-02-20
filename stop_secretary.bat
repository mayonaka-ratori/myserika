@echo off
:: ============================================================
:: stop_secretary.bat  -  MY-SECRETARY 停止スクリプト
:: ============================================================

set "BASE=%~dp0"
if "%BASE:~-1%"=="\" set "BASE=%BASE:~0,-1%"

powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%BASE%\stop_secretary.ps1" "%BASE%"
