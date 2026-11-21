@echo off
chcp 65001 >nul 2>&1
title News Getter - One Click Deploy
set "NEWS_ROOT=%~dp0"
if "%NEWS_ROOT:~-1%"=="\" set "NEWS_ROOT=%NEWS_ROOT:~0,-1%"
set "RC=1"
echo.
echo   News Getter - One Click Deploy
echo   --------------------------------------------
echo   Only a GitHub Token is needed. No gh, no browser login.
echo   The .github hidden folder is pushed too (manual copy often misses it).
echo.
where python >nul 2>&1
if %ERRORLEVEL%==0 goto :RUNPY
where py >nul 2>&1
if %ERRORLEVEL%==0 goto :RUNPY3
echo   [info] Python not found, falling back to PowerShell.
goto :RUNPS

:RUNPY
python "%NEWS_ROOT%\deploy.py"
set "RC=%ERRORLEVEL%"
goto :DONE

:RUNPY3
py -3 "%NEWS_ROOT%\deploy.py"
set "RC=%ERRORLEVEL%"
goto :DONE

:RUNPS
powershell -NoProfile -ExecutionPolicy Bypass -File "%NEWS_ROOT%\deploy.ps1"
set "RC=%ERRORLEVEL%"
goto :DONE

:DONE
echo.
if "%RC%"=="0" (echo   DONE. See the messages above.) else (echo   FAILED. See the red message above.)
echo.
echo   Press any key to close...
pause >nul
exit /b %RC%
