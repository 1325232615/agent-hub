@echo off
chcp 65001 >nul
cd /d "%~dp0"
set AGENT=%~1
if "%AGENT%"=="" set AGENT=strategy_1m
set DRAIN=
if /I "%~2"=="drain" set DRAIN=--drain
echo ================================================
echo  AgentHub Daemon  ^|  Agent = %AGENT%   %DRAIN%
echo  subscribe hub events -^> wake agent CLI -^> post result
echo  Optional 2nd arg "drain": also run this agent's existing pending tasks on start
echo  Ctrl+C to stop
echo ================================================
echo.
".venv\Scripts\python.exe" hub_daemon.py --agent %AGENT% %DRAIN%
echo.
echo [Daemon exited] exit=%ERRORLEVEL%
pause >nul
