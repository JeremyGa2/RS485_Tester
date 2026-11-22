# -*- mode: python ; coding: utf-8 -*-
# Build Windows : pyinstaller --noconfirm --clean RS485_COM_tester_V6.spec
# Produit un DOSSIER dist\RS485_Tester\ autonome (Python + bibliothèques intégrés) :
# démarrage immédiat, aucune installation, aucune extraction temporaire.
import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = collect_data_files("customtkinter")
icon = "assets/app.ico" if os.path.exists("assets/app.ico") else None
if icon:
    datas.append((icon, "assets"))
hidden = collect_submodules("serial.tools") + ["serial.win32", "serial.serialwin32"]

a = Analysis(
    ["RS485_COM_tester_V6.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["numpy", "matplotlib", "PIL", "pandas", "scipy", "pytest", "unittest.mock"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RS485_Tester",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # UPX provoque souvent des faux positifs antivirus
    console=False,
    disable_windowed_traceback=False,
    icon=icon,
    version=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="RS485_Tester")
