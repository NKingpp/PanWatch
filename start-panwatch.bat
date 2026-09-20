@echo off
REM 一键启动 PanWatch（Windows 双击运行）
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-panwatch.ps1"
pause
