@echo off
cd /d "%~dp0"
set "PY="
if exist "%~dp0..\.venv\Scripts\python.exe" set "PY=%~dp0..\.venv\Scripts\python.exe"
if not defined PY (
  if not exist "%~dp0.venv\Scripts\python.exe" py -3 -m venv "%~dp0.venv"
  set "PY=%~dp0.venv\Scripts\python.exe"
)
"%PY%" -m pip install -q -r "%~dp0requirements.txt"
start "" "http://127.0.0.1:8765/"
"%PY%" -m paper_split_maker.dashboard
