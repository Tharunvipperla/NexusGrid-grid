@echo off
REM Developer mode launcher for Phase-2.
REM Runs the backend directly from source with uvicorn --reload.
REM Any edit under nexus/ triggers a restart; no PyInstaller build required.

setlocal
REM This script lives in scripts/; run from the Phase-2 root so `nexus` imports.
cd /d "%~dp0.."
set PYTHONDONTWRITEBYTECODE=1

python -m uvicorn nexus.app:create_app --factory --reload --host 0.0.0.0 --port %1 %2 %3 %4 %5 %6 %7 %8 %9
endlocal
