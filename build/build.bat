@echo off
REM Windows build script for Phase-2. Produces dist\NexusGrid.exe via PyInstaller.
REM Run from the Phase-2 root or let the script cd there itself.

setlocal
cd /d "%~dp0\.."

if exist build\_work rmdir /s /q build\_work
if exist dist rmdir /s /q dist

REM Build the v3 React UI bundle (webui/dist/bundle.js) before packaging.
echo Building webui bundle...
pushd webui
call npm install --no-audit --no-fund --loglevel=error
if errorlevel 1 ( echo npm install failed. & popd & exit /b 1 )
call npm run build
if errorlevel 1 ( echo webui build failed. & popd & exit /b 1 )
popd

pyinstaller --clean --noconfirm ^
    --workpath build\_work ^
    --distpath dist ^
    build\NexusGrid.spec
if errorlevel 1 (
    echo Build failed.
    exit /b 1
)

echo.
echo Build complete: dist\NexusGrid.exe
endlocal
