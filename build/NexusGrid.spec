# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec. Builds a single-file NexusGrid binary that bundles
# the entire `nexus` package plus the served UI assets.
#
# Always run from the repository root so the relative paths below
# (`nexus/__main__.py`, the data files) resolve correctly:
#
#     pyinstaller --clean --noconfirm build/NexusGrid.spec

import os

from PyInstaller.utils.hooks import collect_submodules

# Spec lives in build/; the source root is its parent. PyInstaller
# resolves relative paths against the spec's directory, so anchor everything
# on PROJECT_ROOT to keep the spec runnable from any CWD.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(SPEC), '..'))

hidden = []
hidden += collect_submodules('nexus')
hidden += [
    'aiosqlite',
    'uvicorn.logging',
    'uvicorn.lifespan',
    'uvicorn.lifespan.on',
    'uvicorn.protocols',
    'uvicorn.protocols.http',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.loops',
    'uvicorn.loops.auto',
    'websockets',
    'websockets.legacy',
    'websockets.legacy.client',
    'websockets.legacy.server',
    # Foreign-storage crypto.
    'argon2',
    'argon2.low_level',
    'cryptography.hazmat.primitives.ciphers.aead',
    # Credential-crypto HKDF.
    'cryptography.hazmat.primitives.kdf.hkdf',
]

# Optional GDrive driver SDK. Only added to hidden imports if the build
# environment has the SDK installed; otherwise the dev still gets a working
# binary that lacks the GDrive eviction tier.
try:
    import googleapiclient  # noqa: F401

    hidden += [
        'googleapiclient.discovery',
        'googleapiclient.http',
        'google.oauth2.service_account',
        'google.auth.transport.requests',
    ]
except ImportError:
    pass

a = Analysis(
    [os.path.join(PROJECT_ROOT, 'nexus', '__main__.py')],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=[
        # Classic UI shell, served at /classic via get_resource_dir().
        (os.path.join(PROJECT_ROOT, 'nexus', 'ui', 'index.html'), 'nexus/ui'),
        # relay_codeprint hashes this file at startup to derive the relay code
        # fingerprint. Bundle the .py source verbatim so the digest matches what
        # dev installs compute. (Also collected as a module via collect_submodules.)
        (os.path.join(PROJECT_ROOT, 'nexus', 'relay', 'server.py'), 'nexus/relay'),
        # React UI (served at /app). esbuild emits webui/dist/bundle.js before
        # PyInstaller runs (see build.bat); ship the served assets with the
        # same relative layout serve.py reads (<resource>/webui/...).
        (os.path.join(PROJECT_ROOT, 'webui', 'index.html'), 'webui'),
        (os.path.join(PROJECT_ROOT, 'webui', 'styles.css'), 'webui'),
        (os.path.join(PROJECT_ROOT, 'webui', 'dist', 'bundle.js'), 'webui/dist'),
        # In-app "What's new" reads this at runtime via get_resource_dir().
        (os.path.join(PROJECT_ROOT, 'nexus', 'CHANGELOG.md'), 'nexus'),
    ],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='NexusGrid',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
