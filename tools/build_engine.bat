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
rem The HyperCore interface is ours, the engine is upstream's. If the worker target
rem is what fails against this Strata revision, the engine and the vision engine are
rem still buildable and the server drives the engine over stdin/stdout instead.
set WORKER=1
cmake --build tmp\strata-%TAG%\build --target strata strata-vision hypercore-worker
if errorlevel 1 (
    echo the hypercore-worker target failed; building strata and strata-vision only
    cmake --build tmp\strata-%TAG%\build --target strata strata-vision
    if errorlevel 1 exit /b 1
    set WORKER=0
)

if not exist engine mkdir engine
for %%b in (strata.exe strata-vision.exe hypercore-worker.exe) do (
    if exist tmp\strata-%TAG%\build\%%b copy /y tmp\strata-%TAG%\build\%%b engine\%%b
)
rem Record what was built so the Monitor can say "worker: not built" rather than guess.
python -c "import datetime,hashlib,json,sys;meta={'source':'built-from-source','version':sys.argv[1],'tag':sys.argv[1],'worker':sys.argv[2]=='1','built_at':datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z'),'src':hashlib.sha256(open(sys.argv[3]+'/CMakeLists.txt','rb').read()).hexdigest()[:16]};open('engine/BUILD.json','w').write(json.dumps(meta,indent=1))" %TAG% %WORKER% tmp\strata-%TAG%
rmdir /s /q tmp\strata-%TAG%
echo built %TAG% into engine\ (worker: %WORKER%); tmp\ is empty
