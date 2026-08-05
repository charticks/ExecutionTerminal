@echo off
REM ============================================================
REM  Charticks - one-click setup for testers
REM
REM  The Charticks installer ships the app, but the trading
REM  engine underneath it runs on Python. This script installs
REM  the Python pieces so the tester never has to type a
REM  command. Versions are pinned to the ones verified working.
REM
REM  Just double-click this file. It is safe to run twice.
REM ============================================================

title Charticks - Setup
color 0B
echo.
echo  ============================================
echo    Charticks - Setup
echo  ============================================
echo.
echo  This will install the components Charticks needs.
echo  It takes about 3-5 minutes. Please leave this window open.
echo.

REM ---- Step 1: is Python installed and on PATH? --------------
echo  [1/3] Checking for Python...
python --version >nul 2>&1
if errorlevel 1 goto NOPYTHON

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo        Found Python %PYVER%
echo.

REM ---- Step 2: upgrade pip (avoids odd install failures) -----
echo  [2/3] Updating the installer tool...
python -m pip install --upgrade pip --quiet
echo        Done.
echo.

REM ---- Step 3: the actual dependencies -----------------------
echo  [3/3] Installing Charticks components...
echo        (this is the long one - please wait)
echo.

python -m pip install --quiet ^
  fastapi==0.115.5 ^
  "uvicorn[standard]==0.32.1" ^
  pandas==2.2.3 ^
  requests==2.32.3 ^
  pyotp==2.10.0 ^
  pytz ^
  logzero==1.7.0 ^
  tkcalendar==1.6.1 ^
  smartapi-python==1.5.5 ^
  dhanhq==2.2.0 ^
  neo-api-client==2.0.0 ^
  breeze-connect==1.0.69

if errorlevel 1 goto INSTALLFAIL

echo.
echo  Verifying...
REM -I (isolated) keeps the current folder off sys.path. breeze_connect
REM does a bare "import config" internally, so running this check from a
REM folder that happens to contain a config.py would fail spuriously.
python -I -c "import fastapi,uvicorn,pandas,pyotp,logzero,SmartApi,dhanhq,neo_api_client,breeze_connect" 2>nul
if errorlevel 1 goto VERIFYFAIL

echo.
echo  ============================================
echo    SUCCESS - setup is complete.
echo  ============================================
echo.
echo  You can close this window and start Charticks
echo  from the Desktop or Start Menu.
echo.
pause
exit /b 0


:NOPYTHON
color 0E
echo.
echo  ============================================
echo    Python is not installed yet
echo  ============================================
echo.
echo  Charticks needs Python to run. Please:
echo.
echo    1. Go to:  https://www.python.org/downloads/
echo    2. Click the big yellow "Download Python" button
echo    3. Run the downloaded file
echo    4. IMPORTANT - on the FIRST screen, tick the box at the
echo       bottom that says "Add python.exe to PATH"
echo       (this is the step people miss - without it,
echo        Charticks cannot find Python)
echo    5. Click "Install Now" and wait for it to finish
echo    6. Then double-click this setup file again
echo.
pause
exit /b 1


:INSTALLFAIL
color 0C
echo.
echo  ============================================
echo    Something went wrong during install
echo  ============================================
echo.
echo  This is usually one of:
echo    - no internet connection
echo    - a company firewall blocking downloads
echo.
echo  Please screenshot this whole window and send it over.
echo.
pause
exit /b 1


:VERIFYFAIL
color 0C
echo.
echo  ============================================
echo    Installed, but the check did not pass
echo  ============================================
echo.
echo  Please screenshot this whole window and send it over.
echo.
pause
exit /b 1
