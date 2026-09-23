@echo off
chcp 65001 >nul
set HTTPS_PROXY=http://127.0.0.1:7890
set HTTP_PROXY=http://127.0.0.1:7890
call .venv\Scripts\activate
python -m pa_agent.main
pause
