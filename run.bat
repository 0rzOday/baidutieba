@echo off
chcp 65001 >nul
rem PYTHONIOENCODING is not optional here. When stdout is a FILE or a PIPE,
rem python picks the locale encoding (cp936 here), not the console codepage --
rem so without this the log comes out GBK and the first check mark collect.py
rem prints dies with UnicodeEncodeError on the success branch, which looks
rem exactly like a failure. It happens to work today only because importing
rem skip_vc reconfigures sys.stdout as a side effect; do not rely on that.
set "PYTHONIOENCODING=utf-8"
rem ============================================================
rem  run.bat -- run one collection pass
rem
rem    run.bat                                  crawl from the top until the end
rem    run.bat 5                                up to 5 comments per post
rem    run.bat --minutes 360                    stop cleanly after 6 hours
rem    run.bat 5 --rounds 1 --max-per-round 3   smoke test
rem    run.bat --tid 123,456                    these posts only, no list page
rem    run.bat --force --tid 123                re-fetch, overwrite post.json
rem
rem  Args go straight through to collect.py; see its docstring for the rest.
rem  Log lands in logs\collect_STAMP.log as UTF-8.
rem  Unattended runs: use loop.bat, which restarts on the right exit codes.
rem
rem  KEEP THIS FILE PURE ASCII. cmd.exe seeks inside a batch file by BYTE
rem  offset, so non-ASCII bytes in a UTF-8 .bat make it lose its place and
rem  start executing fragments of lines. We did hit exactly that here:
rem    'o' is not recognized as an internal or external command
rem    '... :done' is not recognized as an internal or external command
rem  Chinese text belongs in what python prints, not in this file.
rem
rem  If typing "run.bat" says it is not recognized, use ".\run.bat" --
rem  NoDefaultCurrentDirectoryInExePath removes the cwd from the search path.
rem ============================================================
setlocal
cd /d "%~dp0"

rem ---- pick the interpreter ----
rem requirements.txt was exported from 3.11.9 and install_deps.py refuses
rem anything but 3.11: numpy/opencv wheels may not exist for other minors,
rem and pip then falls back to a source build that fails for unrelated
rem reasons.
set "PY="
py -3.11 -c "import sys" >nul 2>&1 && set "PY=py -3.11"
if not defined PY (
    python -c "import sys;sys.exit(0 if sys.version_info[:2]==(3,11) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo [x] Python 3.11 not found. Install 3.11.x, then run install_deps.py
    exit /b 9
)

rem ---- dependency check ----
rem "deps not installed" and "browser will not start" are different failures.
rem Without this the former surfaces as "Executable doesn't exist" on the
rem browser launch line, which reads like a code bug.
%PY% -c "import scrapling, patchright, cv2, skip_vc, collect" >nul 2>&1
if errorlevel 1 (
    echo [x] Missing deps, at least one of: scrapling patchright cv2 skip_vc collect
    echo     Run:  %PY% install_deps.py
    exit /b 9
)

rem ---- log name: let python build it ---
rem %date% carries the weekday name, and that name is localized: it is a
rem Chinese word on a Chinese Windows and "Thu" on an English one. Parsing
rem it by position is guaranteed to break on one of them.
set "STAMP="
for /f %%i in ('%PY% -c "import time;print(time.strftime('%%Y%%m%%d-%%H%%M%%S'))"') do set "STAMP=%%i"
if not defined STAMP set "STAMP=nostamp-%RANDOM%"

if not exist "logs" mkdir "logs"
set "LOG=logs\collect_%STAMP%.log"

echo [i] python : %PY%
echo [i] log    : %LOG%
echo [i] start  : %date% %time%
echo.

rem Output goes straight into the log. We do NOT also tee it to the console,
rem because Tee-Object on Windows PowerShell 5.1 writes UTF-16 -- the log
rem would come out unreadable to grep and python. A quiet screen beats a
rem corrupt log.
rem To watch live, in another window:  powershell -c "Get-Content -Wait %LOG%"
%PY% -u collect.py %* > "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"

echo.
echo -------- last 20 lines of the log --------
%PY% -c "import sys;print(''.join(open(sys.argv[1],encoding='utf-8',errors='replace').readlines()[-20:]),end='')" "%LOG%"
echo ------------------------------------------
echo.
echo [i] full log  : %LOG%
echo [i] exit code : %RC%
if "%RC%"=="0" echo     0 = reached the real bottom, whole forum done
if "%RC%"=="2" echo     2 = brake, needs a human
if "%RC%"=="3" echo     3 = stopped on a limit (--minutes / --rounds), not the bottom yet
if "%RC%"=="4" echo     4 = browser gone, the next pass relaunches it
if "%RC%"=="9" echo     9 = environment not ready, see above
endlocal & exit /b %RC%
