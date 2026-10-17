@echo off
rem Clone a Strata release, build the engine, the vision engine and the worker,
rem copy them into engine\, and leave nothing behind in tmp\.
rem
rem   tools\build_engine.bat v0.1.45
rem
rem The Build tab does the same through serve\build_api.py; this is the
rem command-line path for people who do not want to open the web app.
cd /d "%~dp0.."
set TAG=%~1
if "%TAG%"=="" set TAG=main
shift

if exist tmp\strata-%TAG% rmdir /s /q tmp\strata-%TAG%
git clone --depth 1 --branch %TAG% https://github.com/Niko1221/Strata.git tmp\strata-%TAG%
if errorlevel 1 exit /b 1

xcopy /e /i /y engine-src tmp\strata-%TAG%\hypercore-interface

python tools\extract_build_options.py tmp\strata-%TAG% --out config\build_options.json --diff config\build_options.json

cmake -G Ninja -S tmp\strata-%TAG% -B tmp\strata-%TAG%\build %*
if errorlevel 1 exit /b 1
cmake --build tmp\strata-%TAG%\build --target strata strata-vision hypercore-worker
if errorlevel 1 exit /b 1

if not exist engine mkdir engine
for %%b in (strata.exe strata-vision.exe hypercore-worker.exe) do (
    if exist tmp\strata-%TAG%\build\%%b copy /y tmp\strata-%TAG%\build\%%b engine\%%b
)
rmdir /s /q tmp\strata-%TAG%
echo built %TAG% into engine\; tmp\ is empty
