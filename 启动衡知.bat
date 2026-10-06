@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0金融大赛_集成试用版\start_integrated.ps1" %*
if errorlevel 1 pause
