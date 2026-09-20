@echo off
REM 一键停止 PanWatch（Windows 双击运行）
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-panwatch.ps1"
pause
