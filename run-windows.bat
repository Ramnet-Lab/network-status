@echo off
REM Network Status — native Windows launcher (reliable LAN checks).
REM Requires Python 3.10+ from python.org or the Microsoft Store.
cd /d "%~dp0"

where py >nul 2>&1 && (set PY=py) || (set PY=python)

if not exist ".venv" (
  echo Creating virtual environment...
  %PY% -m venv .venv || (echo Could not create venv. Install Python 3.10+ first. & pause & exit /b 1)
)

call .venv\Scripts\activate.bat
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt

echo Starting Network Status on http://localhost:8766
start "" http://localhost:8766
python -u monitor.py

pause
