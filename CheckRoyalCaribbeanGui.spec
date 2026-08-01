# -*- mode: python ; coding: utf-8 -*-
# Windowed GUI wrapper. The child scripts are bundled as data files and run
# via the exe's --run-script dispatch (runpy), so PyInstaller's import analysis
# cannot see their dependencies: everything they import must be forced in here.
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = ['yaml', 'requests']
for pkg in ('apprise', 'bs4', 'curl_cffi'):
    tmp_ret = collect_all(pkg)
    datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# Scripts the GUI can launch (must match SCRIPT_WHITELIST in CheckRoyalCaribbeanGui.py)
bundled_scripts = [
    'CheckRoyalCaribbeanPrice.py',
    'CheckRoyalCaribbeanUpgrades.py',
    'CheckRoyalCaribbeanCasinoOffers.py',
    'CheckRoyalCaribbeanCruiseHistory.py',
    'FindBackToBackCabins.py',
    'BrowseRoyalCaribbeanPrice.py',
]
datas += [(s, '.') for s in bundled_scripts]
datas += [('SAMPLE-config.yaml', '.')]


a = Analysis(
    ['CheckRoyalCaribbeanGui.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
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
    name='CheckRoyalCaribbeanGui',
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
