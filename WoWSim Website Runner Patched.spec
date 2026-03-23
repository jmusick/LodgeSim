# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = []
hiddenimports += collect_submodules('flask')
hiddenimports += collect_submodules('urllib3')
hiddenimports += collect_submodules('simcore')


a = Analysis(
    ['website_sim_runner_gui.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('.\\tier_source_overrides.json', '.'),
        ('.\\config.guild.json', '.'),
        ('.\\website_sim_runner.py', '.'),
        ('.\\update-simc.ps1', '.'),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='WoWSim Website Runner Patched',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
