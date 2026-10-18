@echo off
rem Create the environment. No model download logic: the model paths are yours to
rem set in the Config view or in config\run-<model>.json.
cd /d "%~dp0"
if not exist .venv python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
if not exist engine\strata.exe (
    echo no engine in engine\. Open the Build tab to fetch or compile one,
    echo or set STRATA_ENGINE to an engine you already have.
)
echo ready. start with run.bat
