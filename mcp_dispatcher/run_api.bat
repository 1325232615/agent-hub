@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [AgentHub] FastAPI starting at http://127.0.0.1:8765  (Ctrl+C to stop)
".venv\Scripts\python.exe" -m uvicorn app.api:app --host 127.0.0.1 --port 8765