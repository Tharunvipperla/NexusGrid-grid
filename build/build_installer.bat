@echo off
REM Build the NexusGrid Windows installer end-to-end:
REM   1) build\build.bat      -> dist\NexusGrid.exe (PyInstaller + webui bundle)
REM   2) ISCC NexusGrid.iss   -> dist\NexusGrid-Setup-<ver>.exe (Inno Setup)
REM
REM Requires Inno Setup 6 (https://jrsoftware.org/isdl.php); ISCC.exe must be on
REM PATH, or it's auto-detected at the default install location below.

setlocal
cd /d "%~dp0\.."

call build\build.bat
if errorlevel 1 ( echo [installer] exe build failed. & exit /b 1 )

set "ISCC=iscc"
where iscc >nul 2>nul || set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" if "%ISCC%"=="iscc" goto :run
if not exist "%ISCC%" (
    echo [installer] Inno Setup compiler not found. Install it from
    echo             https://jrsoftware.org/isdl.php  then re-run.
    exit /b 1
)

:run
echo Compiling installer with %ISCC% ...
"%ISCC%" build\NexusGrid.iss
if errorlevel 1 ( echo [installer] ISCC failed. & exit /b 1 )

echo.
echo Installer complete: dist\NexusGrid-Setup-1.0.0.exe
