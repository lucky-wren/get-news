@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist manifest.json goto :notunzipped
if defined GITHUB_TOKEN goto :have
if exist token.txt goto :have
echo Tip: put the token in token.txt to avoid on-screen echo; never screenshot it.
set /p "GITHUB_TOKEN=GitHub Token (only this one, paste and press Enter): "
if not defined GITHUB_TOKEN goto :notoken
:have
set "PY=python"
where python >nul 2>nul
if errorlevel 1 set "PY=py -3"
%PY% deploy.py
if errorlevel 1 goto :failed
echo Done. Actions will publish at 20:00 daily.
pause
exit /b 0
:notunzipped
echo manifest.json not found here. Unzip the package first, then run deploy.bat.
echo Do NOT upload the .zip on the GitHub web page (it will not be extracted).
pause
exit /b 1
:notoken
echo No token provided. Nothing else is needed.
pause
exit /b 1
:failed
echo Deploy failed. See message above.
pause
exit /b 1
