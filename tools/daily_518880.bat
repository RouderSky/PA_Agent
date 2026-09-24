@echo off
REM ============================================================
REM  PA Agent 定时分析启动器（无需人工干预）
REM   运行时间：每个交易日 15:30（收盘后）—— 此时当日日线已完整收盘
REM   1) 周线 1w —— 先刷新长期方向结论（K 线未更新时自动跳过，不耗 token）
REM   2) 日线 1d —— 每天的主分析，自动把上一步的周线结论当背景注入提示词
REM   由 Windows 计划任务「PA_Agent_Daily_518880」调用
REM   产物：records\pending\*.json（完整记录）、records\daily\*.md（摘要）
REM   日志：logs\daily_analysis.out.log
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0.."

set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

if not exist "logs" mkdir logs

set "RC=0"

REM ── 1) 周线：先刷新长期方向结论，供日线当背景注入 ─────────────
"%PY%" "tools\run_daily_analysis.py" --symbol 518880 --timeframe 1w --bars 100 >> "logs\daily_analysis.out.log" 2>&1
if errorlevel 1 set "RC=1"

REM ── 2) 日线：每天的主分析（自动带上周线背景）──────────────────
"%PY%" "tools\run_daily_analysis.py" --symbol 518880 --timeframe 1d --bars 100 >> "logs\daily_analysis.out.log" 2>&1
if errorlevel 1 set "RC=1"

exit /b %RC%
