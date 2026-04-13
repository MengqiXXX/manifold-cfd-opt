@echo off
setlocal
set "CMD=%~1"
if "%CMD%"=="" exit /b 0

:: Remove surrounding quotes if they exist (simplistic approach for CMD)
set "CMD_CLEAN=%CMD:"=%"

powershell -NoProfile -ExecutionPolicy Bypass -Command "%CMD_CLEAN%"
exit /b %ERRORLEVEL%
