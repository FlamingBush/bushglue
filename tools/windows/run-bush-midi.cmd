@echo off
rem Launch bush-midi on Windows. Reads settings from %LOCALAPPDATA%\bush\midi.env
rem (plain KEY=VALUE lines, no comments) and appends output to a log next to it,
rem because a scheduled task has no console to print to.

setlocal
set "REPO=%~dp0..\.."
set "BUSHDIR=%LOCALAPPDATA%\bush"
set "ENVFILE=%BUSHDIR%\midi.env"
set "LOG=%BUSHDIR%\bush-midi.log"

if not exist "%BUSHDIR%" mkdir "%BUSHDIR%"

if exist "%ENVFILE%" (
  for /f "usebackq tokens=1,* delims==" %%A in ("%ENVFILE%") do set "%%A=%%B"
)

if not exist "%REPO%\.venv\Scripts\bush-midi.exe" (
  echo [midi] no venv at "%REPO%\.venv" -- run: uv sync --all-packages >> "%LOG%"
  exit /b 1
)

echo. >> "%LOG%"
echo [midi] starting %DATE% %TIME% (broker=%BUSH_MQTT_BROKER% port=%MIDI_PORT%) >> "%LOG%"
"%REPO%\.venv\Scripts\bush-midi.exe" >> "%LOG%" 2>&1
