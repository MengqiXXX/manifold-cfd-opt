@echo off
setlocal
cd /d %~dp0..
powershell -NoProfile -ExecutionPolicy Bypass -File monitor\start_monitor.ps1
