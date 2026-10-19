@echo off
setlocal enabledelayedexpansion
rem Start the HyperCore WebUI on Windows. Same behaviour as run.sh.
rem
rem   run.bat                 newest run config in config\, else engine\strata.exe
rem   run.bat q2_0           config\run-q2_0.json
rem   run.bat config\my.json a path, verbatim
rem   run.bat --no-attach   flags after the name are passed to the server as they are
rem   run.bat --ui-only     the app without an engine: usable while the GPU is busy
rem
rem A fresh clone has nothing in it yet, and that is not a reason to refuse to start:
rem the Build tab is what produces the first engine and the first run config, so when
rem there is neither run.bat brings the app up with no engine and says so.
rem Host, port and API key default to 0.0.0.0, 8081 and 0000; set HYPERCORE_HOST,
rem HYPERCORE_PORT or HYPERCORE_API_KEY to change them. HYPERCORE_ENGINE_URL makes
rem this server attach to an engine another server already loaded.
cd /d "%~dp0"

set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python
if not defined HYPERCORE_HOST set HYPERCORE_HOST=0.0.0.0
if not defined HYPERCORE_PORT set HYPERCORE_PORT=8081
if not defined HYPERCORE_API_KEY set HYPERCORE_API_KEY=0000
set ENGINELESS=
set EXPLICIT=
for %%a in (%*) do if /i "%%a"=="--ui-only" set EXPLICIT=--ui-only

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
    rem The name matches nothing. That is usually a mistyped version or a stray argument
    rem (run.bat 8081), not a machine with nothing built on it: the newest run config keeps
    rem the engine running instead of starting the app engineless for a model that is
    rem sitting in engine\.
    if not defined CFG (
        for /f "delims=" %%f in ('dir /b /o-d config\run-*.json 2^>nul') do (
            if not defined CFG (
                if /i "%%f" neq "run-example.json" (
                    findstr /i /c "ABSOLUTE/PATH/TO" "config\%%f" >nul 2>&1
                    if errorlevel 1 set CFG=config\%%f
                )
            )
        )
        if defined CFG echo no config named "%~1" in config\ - using the newest, %CFG%
    )
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

if not defined CFG (
    if not exist engine\strata.exe (
        if not defined HYPERCORE_ENGINE_URL (
            rem Nothing installed yet, which is the state a fresh clone is in. The page has
            rem to come up anyway: a build is what installs engine\engine_<quant>\ and
            rem config\run-<quant>.json, and the Model dropdown lists a version from then on.
            echo nothing installed yet: no run config in config\ and no engine in engine\.
            echo starting the app without an engine so the Build tab is reachable.
            echo The first build installs the engine and its run config under this root; set
            echo the model paths in the Config view, then the Model dropdown lists the build.
            dir /b config\*.json 2>nul
            set ENGINELESS=--ui-only
        )
    )
)

set MODE=strata
if exist engine\hypercore-worker.exe set MODE=hypercore
if defined HYPERCORE_ENGINE_URL set MODE=attach

set ARGS=--host %HYPERCORE_HOST% --port %HYPERCORE_PORT% --api-key %HYPERCORE_API_KEY% --open
rem Engineless means there is no engine to name: --ui-only is the mode, so --engine is
rem left out rather than pointed at a binary that is not there.
if defined ENGINELESS (
    if not defined EXPLICIT set ARGS=!ARGS! --ui-only
) else (
    set ARGS=!ARGS! --engine %MODE%
)
if defined CFG set ARGS=%ARGS% --config "%CFG%"
if defined HYPERCORE_ENGINE_URL set ARGS=%ARGS% --engine-url %HYPERCORE_ENGINE_URL%
rem Flags after the name are passed to the server as they are, exactly as run.sh does.
for %%a in (%*) do (
    set "flag=%%a"
    if "!flag:~0,1!"=="-" set ARGS=!ARGS! %%a
)

if defined ENGINELESS (
    echo engine: none, the app only   http://%HYPERCORE_HOST%:%HYPERCORE_PORT%
) else if defined CFG (
    echo config: %CFG%   engine mode: %MODE%   http://%HYPERCORE_HOST%:%HYPERCORE_PORT%
) else (
    echo engine: engine\strata.exe   engine mode: %MODE%   http://%HYPERCORE_HOST%:%HYPERCORE_PORT%
)

"%PY%" serve\server.py %ARGS%
