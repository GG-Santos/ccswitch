@echo off
REM Launch claude with an optional account switch first.
REM   launch.bat            -> start claude with whatever account is active
REM   launch.bat work       -> switch to "work", then start claude
REM   launch.bat work /d    -> switch to "work", start the daemon in a new window, then claude

setlocal
set ACCOUNT=%~1
set DAEMON=%~2

if not "%ACCOUNT%"=="" (
    python -m ccswitch.cli use "%ACCOUNT%"
    if errorlevel 1 (
        echo Failed to switch account. Aborting.
        exit /b 1
    )
)

if /I "%DAEMON%"=="/d" (
    start "ccswitch-daemon" cmd /k python -m ccswitch.cli daemon
)

claude
endlocal
