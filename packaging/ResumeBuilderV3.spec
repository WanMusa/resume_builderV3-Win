# Build with: python -m PyInstaller --noconfirm packaging/ResumeBuilderV3.spec
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

root = Path(SPECPATH).parent
analysis = Analysis(
    [str(root / "packaging" / "launcher.py")],
    pathex=[str(root)],
    binaries=[],
    datas=collect_data_files("resume_builder", includes=["assets/*"])
    + [(str(root / "LICENSE"), "."), (str(root / "THIRD_PARTY_NOTICES.txt"), ".")],
    hiddenimports=["keyring.backends.Windows"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PyQt6"],
    noarchive=False,
)
archive = PYZ(analysis.pure)
executable = EXE(
    archive, analysis.scripts, [], exclude_binaries=True,
    name="ResumeBuilderV3", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False,
)
distribution = COLLECT(
    executable, analysis.binaries, analysis.datas,
    strip=False, upx=False, name="ResumeBuilderV3",
)
