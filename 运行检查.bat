@echo off
chcp 65001 >nul
"%~dp0runtime\python\Scripts\python.exe" -X utf8 "%~dp0scripts\check_install.py"
pause
