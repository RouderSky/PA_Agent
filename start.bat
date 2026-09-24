@echo off
chcp 65001 >nul
set HTTPS_PROXY=http://127.0.0.1:2298
set HTTP_PROXY=http://127.0.0.1:2298
call .venv\Scripts\activate
python -m pa_agent.main
pause
