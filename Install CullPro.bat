@echo off
title Installing CullPro
setlocal
set "SRC=%~dp0"
set "DEST=%LOCALAPPDATA%\CullPro"

echo.
echo   Installing CullPro...
echo   (first time takes 2 to 3 minutes, after that it opens instantly)
echo.

set PY=
py -3 --version >nul 2>&1 && set PY=py -3
if not defined PY ( python --version >nul 2>&1 && set PY=python )
if not defined PY (
  echo   Python isn't installed. Opening the download page...
  echo   Install it, then double click "Install CullPro.bat" again.
  start https://www.python.org/downloads/
  pause
  exit /b
)

if not exist "%DEST%" mkdir "%DEST%"
robocopy "%SRC%." "%DEST%" /E /NFL /NDL /NJH /NJS /NP /XF "Install CullPro.bat" /XD .venv __pycache__ >nul

if not exist "%DEST%\.venv\Scripts\pythonw.exe" (
  echo   Setting up the engine...
  %PY% -m venv "%DEST%\.venv"
)
"%DEST%\.venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r "%DEST%\requirements.txt"
if errorlevel 1 (
  echo.
  echo   Setup failed. Take a screenshot of this window and send it to Claude.
  pause
  exit /b
)
echo   Adding RAW and HEIC support...
"%DEST%\.venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r "%DEST%\requirements-optional.txt" >nul 2>&1
if errorlevel 1 echo   (RAW support didn't install. JPGs still work fine.)

if exist "%~dp0.env" copy /Y "%~dp0.env" "%DEST%\.env" >nul

echo   Making the Desktop and Start menu icons...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws=New-Object -ComObject WScript.Shell;" ^
  "foreach($p in @([Environment]::GetFolderPath('Desktop'),(Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'))){" ^
  "$s=$ws.CreateShortcut((Join-Path $p 'CullPro.lnk'));" ^
  "$s.TargetPath='%DEST%\.venv\Scripts\pythonw.exe';" ^
  "$s.Arguments='launcher.py';" ^
  "$s.WorkingDirectory='%DEST%';" ^
  "$s.IconLocation='%DEST%\cullpro.ico';" ^
  "$s.Description='CullPro gameday photo culling';$s.Save()}"

echo.
echo   Done! Opening CullPro...
echo   From now on, just use the CullPro icon on your Desktop.
echo   You can delete the downloaded zip and this folder.
start "" "%DEST%\.venv\Scripts\pythonw.exe" "%DEST%\launcher.py"
timeout /t 4 >nul
