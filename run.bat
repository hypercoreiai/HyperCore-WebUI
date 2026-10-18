@echo off
rem Start the HyperCore WebUI on Windows. Same behaviour as run.sh.
rem
rem   run.bat                 newest run config in config\, else engine\strata.exe
rem   run.bat q2_0           config\run-q2_0.json
rem   run.bat config\my.json a path, verbatim
rem   run.bat --no-attach   flags after the name are passed to the server as they are
rem
rem Host, port and API key default to 0.0.0.0, 8081 and 0000; set HYPERCORE_HOST,
rem HYPERCORE_PORT or HYPERCORE_API_KEY to change them. HYPERCORE_ENGINE_URL makes
rem this server attach to an engine another server already loaded.
cd /d "%~dp0"

set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python
if not defined HYPERCORE_HOST set HYPERCORE_HOST=0.0.0.0
if not defined HYPERCORE_PORT set HYPERCORE_PORT=8081
if not defined HYPERCORE_API_KEY set HYPERCORE_API_KEY=0000

rem One engine per machine: if the page is already up, open it rather than
rem loading the model a second time.
"%PY%" tools\is_running.py %HYPERCORE_PORT%
if %errorlevel% equ 0 (
    echo the WebUI is already running at http://%HYPERCORE_HOST%:%HYPERCORE_PORT% - opening it.
    start "" http://127.0.0.1:%HYPERCORE_PORT%
    exit /b 0
)

set CFG=
if "%~1" neq "" (
    if exist "%~1" set CFG=%~1
    else if exist "config\%~1" set CFG=config\%~1
    else if exist "config\run-%~1" set CFG=config\run-%~1
    else if exist "config\%~1.json" set CFG=config\%~1.json
    else if exist "config\run-%~1.json" set CFG=config\run-%~1.json
) else (
    rem The template is not a run config: skip it when picking one by itself.
    for /f "delims=" %%f in ('dir /b /o-d config\run-*.json 2^>nul') do (
        if not defined CFG (
            if /i "%%f" neq "run-example.json" set CFG=config\%%f
        )
    )
)

if defined CFG (
    findstr /i /c "ABSOLUTE/PATH/TO" "%CFG%" >nul 2>&1
    if not errorlevel 1 (
        echo warning: %CFG% is the template - its paths are placeholders.
        echo Fill them in, or run.bat ^<name^> for a config with real paths.
    )
)

set MODE=strata
if exist engine\hypercore-worker.exe set MODE=hypercore
if defined HYPERCORE_ENGINE_URL set MODE=attach

set ARGS=--engine %MODE% --host %HYPERCORE_HOST% --port %HYPERCORE_PORT% --api-key %HYPERCORE_API_KEY% --open
if defined CFG set ARGS=%ARGS% --config "%CFG%"
if defined HYPERCORE_ENGINE_URL set ARGS=%ARGS% --engine-url %HYPERCORE_ENGINE_URL%
for %%a in (%*) do (
    if "%%a"=="--no-attach" set ARGS=%ARGS% --no-attach
)

if defined CFG (
    echo config: %CFG%   engine mode: %MODE%   http://%HYPERCORE_HOST%:%HYPERCORE_PORT%
) else (
    echo engine: engine\strata.exe   engine mode: %MODE%   http://%HYPERCORE_HOST%:%HYPERCORE_PORT%
)

"%PY%" serve\server.py %ARGS%
