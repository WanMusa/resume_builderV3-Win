"""Include installed dependency license files in the distributable."""

import importlib.metadata
import shutil
import sys
from pathlib import Path


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    destination = Path(argv[0])
    destination.mkdir(parents=True, exist_ok=True)
    
    # Vendor Qt licenses (required for PySide6/LGPL compliance)
    spec_dir = Path(__file__).parent
    gpl_source = spec_dir / "licenses" / "GPL-3.0.txt"
    lgpl_source = spec_dir / "licenses" / "LGPL-3.0.txt"
    
    if not gpl_source.is_file():
        raise FileNotFoundError(f"Missing vendored GPL-3.0.txt at {gpl_source}")
    if not lgpl_source.is_file():
        raise FileNotFoundError(f"Missing vendored LGPL-3.0.txt at {lgpl_source}")
    
    qt_dest = destination / "Qt"
    qt_dest.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(gpl_source, qt_dest / "GPL-3.0.txt")
    shutil.copyfile(lgpl_source, qt_dest / "LGPL-3.0.txt")
    
    # Collect PySide6 metadata expressions
    pyside_expressions = []
    for pyside_pkg in ("PySide6", "PySide6-Addons", "PySide6-Essentials", "shiboken6"):
        try:
            dist = importlib.metadata.distribution(pyside_pkg)
            expr = dist.metadata.get("License-Expression") or dist.metadata.get("License")
            if expr:
                pyside_expressions.append(f"{pyside_pkg} {dist.version}: {expr}")
        except importlib.metadata.PackageNotFoundError:
            pass
    
    if pyside_expressions:
        qt_dest.mkdir(parents=True, exist_ok=True)
        (qt_dest / "PYSIDE_LICENSE_METADATA.txt").write_text(
            "PySide6 Qt Binding License Declarations\n"
            "=========================================\n\n"
            + "\n".join(pyside_expressions) + "\n\n"
            "See bundled GPL-3.0.txt and LGPL-3.0.txt for complete license texts.\n",
            encoding="utf-8"
        )
    
    notices = []
    for distribution in sorted(importlib.metadata.distributions(), key=lambda item: item.metadata["Name"].lower()):
        name = distribution.metadata["Name"]
        notices.append(f"{name} {distribution.version}: {distribution.metadata.get('License-Expression') or distribution.metadata.get('License') or 'See bundled license files'}")
        for item in distribution.files or []:
            if any(term in item.name.lower() for term in ("license", "copying", "notice")):
                source = Path(distribution.locate_file(item))
                if source.is_file():
                    relative = Path(*[part for part in item.parts if part not in ("..", ".", "/")])
                    target = destination / name / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
    (destination / "installed-distributions.txt").write_text("\n".join(notices), encoding="utf-8")


if __name__ == "__main__":
    main()
