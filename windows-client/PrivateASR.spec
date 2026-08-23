from PyInstaller.utils.hooks import collect_all

sd_datas, sd_binaries, sd_hidden = collect_all("sounddevice")
pa_datas, pa_binaries, pa_hidden = collect_all("_sounddevice_data")

a = Analysis(
    ["asr_client.py"],
    pathex=[],
    binaries=sd_binaries + pa_binaries,
    datas=sd_datas + pa_datas,
    hiddenimports=sd_hidden + pa_hidden,
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[],
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="PrivateASR", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=True, console=False, disable_windowed_traceback=False,
    argv_emulation=False, target_arch=None, codesign_identity=None,
    entitlements_file=None,
)
