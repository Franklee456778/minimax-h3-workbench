@echo off
rem MiniMax H3 workbench launcher. Close this window to stop.
cd /d "%~dp0"
set "H3_DATA=%~dp0data"
set "PATH=%~dp0runtime\ffmpeg;%PATH%"
set "PYTHONPATH=%~dp0runtime\lib"
set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1
title MiniMax H3
"%~dp0runtime\python\python.exe" "%~dp0app\hf.py" %*
if errorlevel 1 pause
