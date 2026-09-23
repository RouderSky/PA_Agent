@echo off
REM ============================================================
REM  PA Agent Launcher
REM  Project : Price Action AI Analysis Agent
REM  Usage   : Double-click this file to start the GUI.
REM  Location: C:\PA_Agent\start_pa_agent.bat
REM ============================================================
title PA Agent

cd /d D:\cl\PA_Agent
echo ============================================================
echo  Starting PA Agent (Price Action AI Analysis)...
echo  Project dir: D:\cl\PA_Agent
echo ============================================================
echo.

REM Use Python 3.13 (requires-python>=3.11). py launcher picks 3.13.
py -3.13 run.py

echo.
echo ============================================================
echo  PA Agent has exited.
echo  If the window closed unexpectedly, check:
echo    C:\PA_Agent\logs\pa_agent.log
echo    C:\PA_Agent\logs\crash.log
echo ============================================================
pause
