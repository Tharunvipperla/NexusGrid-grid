# build/

Build artefacts for producing a single-file NexusGrid binary.

| File              | Purpose                                                  |
|-------------------|----------------------------------------------------------|
| `NexusGrid.spec`  | PyInstaller spec — bundles `nexus/` + `index.html`       |
| `build.bat`       | Windows entry point (`pyinstaller … build/NexusGrid.spec`) |
| `build.sh`        | POSIX entry point (Linux / macOS)                        |

## Usage

Always invoke from the repository root so relative paths in the spec resolve:

**Windows**
```cmd
build\build.bat
```

**Linux / macOS**
```bash
./build/build.sh
```

Output lands in `dist/NexusGrid[.exe]`. Intermediate PyInstaller work goes to
`build/_work/` (git-ignored) so it doesn't collide with this source folder.

## Updating the spec

Add a new runtime dependency? Extend `hiddenimports` in `NexusGrid.spec`.
`collect_submodules('nexus')` already discovers every subpackage so new
modules under `nexus/` don't require a spec edit.

New crypto deps like `argon2-cffi` (Argon2id KDF) are pure pip deps, so
re-running `pip install -r requirements.txt` before a build is enough.
