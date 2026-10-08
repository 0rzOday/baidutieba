@echo off
chcp 65001 >nul
rem ============================================================
rem  loop.bat -- unattended: start another pass based on the exit code
rem
rem    loop.bat                       each pass runs --minutes 360, until the end
rem    loop.bat 5 --minutes 120       args pass through to run.bat
rem
rem  When it stops:
rem    0  real bottom reached          -> stop, whole forum done
rem    2  brake, needs a human         -> stop
rem    3  --minutes deadline           -> next pass
rem    4  browser gone                 -> wait 60s, next pass
rem    other: crashed / env not ready  -> wait 60s, stop after 5 in a row
rem
rem  This is also the throughput-decay experiment. That 39-hour run decayed
rem  from 669 posts/hour to 21 posts/hour and never recovered; if a process
rem  restart puts the per-post timing back to a few seconds, the decay
rem  accumulates inside one process and restarting is the cure. If it
rem  stays at 100s+, the server is throttling deep pages and we need a
rem  different approach.
rem
rem  KEEP THIS FILE PURE ASCII -- see the note at the top of run.bat.
rem ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "MINUTES=360"
set "PASS_MAX=200"
set "BAD_MAX=5"

if not exist "logs" mkdir "logs"
set "LLOG=logs\loop_%RANDOM%.log"

rem if the user passed their own --minutes, do not add a second one
set "EXTRA=--minutes %MINUTES%"
echo %* | find "--minutes" >nul
if not errorlevel 1 set "EXTRA="

echo [i] loop log : %LLOG%
echo [i] each pass: run.bat %* %EXTRA%
echo.

set /a PASS=0
set /a BAD=0

:again
set /a PASS+=1
if !PASS! GTR %PASS_MAX% (
    echo [!] stopped: %PASS_MAX% passes and still not at the bottom >> "%LLOG%"
    goto stop
)
echo ===== pass !PASS!   %date% %time% ===== >> "%LLOG%"

rem absolute path: NoDefaultCurrentDirectoryInExePath means a bare
rem "run.bat" may not resolve
call "%~dp0run.bat" %* %EXTRA%
set "RC=!ERRORLEVEL!"
echo pass !PASS! exited !RC!   %date% %time% >> "%LLOG%"

if "!RC!"=="0" (
    echo [!] exit 0 -- real bottom reached, whole forum done >> "%LLOG%"
    goto stop
)
if "!RC!"=="2" (
    echo [!] exit 2 -- brake, needs a human. Check the collect log. >> "%LLOG%"
    goto stop
)
if "!RC!"=="3" (
    set /a BAD=0
    echo     deadline hit, starting the next pass >> "%LLOG%"
    goto again
)
if "!RC!"=="4" (
    set /a BAD+=1
    echo     browser gone, waiting 60s to relaunch >> "%LLOG%"
) else (
    set /a BAD+=1
    echo     unexpected exit code !RC!, waiting 60s >> "%LLOG%"
)

if !BAD! GEQ %BAD_MAX% (
    echo [!] !BAD! failures in a row, stopping. Latest collect log is in logs\ >> "%LLOG%"
    goto stop
)
rem ping, not timeout: timeout errors out immediately when stdin is redirected
ping -n 61 127.0.0.1 >nul
goto again

:stop
echo.
echo [i] loop finished. Full loop log: %LLOG%
endlocal & exit /b 0
