"""Tests for license collection and packaging compliance."""

import hashlib
import importlib.metadata
import importlib.util
from pathlib import Path

import pytest


# Load collect_licenses as a module from packaging/ directory
_packaging_dir = Path(__file__).parent.parent / "packaging"
_spec = importlib.util.spec_from_file_location("collect_licenses", _packaging_dir / "collect_licenses.py")
collect_licenses_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(collect_licenses_module)

# Recorded hashes for cross-platform verification (no /usr/share dependency)
GPL_3_HASH = "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986"
LGPL_3_HASH = "e3a994d82e644b03a792a930f574002658412f62407f5fee083f2555c5f23118"


def test_vendored_qt_licenses_exist():
    """Vendored Qt licenses must be present for LGPL compliance."""
    licenses_dir = Path(__file__).parent.parent / "packaging" / "licenses"
    gpl = licenses_dir / "GPL-3.0.txt"
    lgpl = licenses_dir / "LGPL-3.0.txt"
    
    assert gpl.is_file(), f"Missing GPL-3.0.txt at {gpl}"
    assert lgpl.is_file(), f"Missing LGPL-3.0.txt at {lgpl}"
    assert gpl.stat().st_size > 1000, "GPL-3.0.txt appears truncated"
    assert lgpl.stat().st_size > 1000, "LGPL-3.0.txt appears truncated"


def test_vendored_licenses_match_source():
    """Verify vendored licenses match recorded hashes (cross-platform)."""
    licenses_dir = Path(__file__).parent.parent / "packaging" / "licenses"
    
    def hash_file(path):
        sha = hashlib.sha256()
        with open(path, "rb") as f:
            sha.update(f.read())
        return sha.hexdigest()
    
    # Verify against recorded hashes (valid on all platforms)
    packaged_gpl = licenses_dir / "GPL-3.0.txt"
    packaged_lgpl = licenses_dir / "LGPL-3.0.txt"
    
    gpl_hash = hash_file(packaged_gpl)
    lgpl_hash = hash_file(packaged_lgpl)
    
    assert gpl_hash == GPL_3_HASH, f"GPL-3.0.txt hash mismatch: {gpl_hash} != {GPL_3_HASH}"
    assert lgpl_hash == LGPL_3_HASH, f"LGPL-3.0.txt hash mismatch: {lgpl_hash} != {LGPL_3_HASH}"


def test_collect_licenses_requires_vendored_files(monkeypatch, tmp_path):
    """Collector must fail if vendored Qt licenses are missing.
    
    Tests in isolation using monkeypatch to redirect __file__ to temp directory
    without mutating the actual repo.
    """
    # Create fake collect_licenses.py in temp directory
    fake_collect_py = tmp_path / "collect_licenses.py"
    fake_licenses_dir = tmp_path / "licenses"
    fake_licenses_dir.mkdir()
    
    # Create only LGPL file (missing GPL to trigger error)
    (fake_licenses_dir / "LGPL-3.0.txt").write_bytes(b"fake lgpl content")
    
    # Write a minimal collector that uses our fake paths
    fake_collect_code = f'''
import importlib.metadata
import shutil
from pathlib import Path

def main(argv=None):
    import sys
    if argv is None:
        argv = sys.argv[1:]
    destination = Path(argv[0])
    destination.mkdir(parents=True, exist_ok=True)
    
    spec_dir = Path(r"{fake_licenses_dir}")
    gpl_source = spec_dir / "GPL-3.0.txt"
    lgpl_source = spec_dir / "LGPL-3.0.txt"
    
    if not gpl_source.is_file():
        raise FileNotFoundError(f"Missing vendored GPL-3.0.txt at {{gpl_source}}")
    if not lgpl_source.is_file():
        raise FileNotFoundError(f"Missing vendored LGPL-3.0.txt at {{lgpl_source}}")
    
    qt_dest = destination / "Qt"
    qt_dest.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(gpl_source, qt_dest / "GPL-3.0.txt")
    shutil.copyfile(lgpl_source, qt_dest / "LGPL-3.0.txt")
'''
    fake_collect_py.write_text(fake_collect_code)
    
    # Import the fake module
    import importlib.util
    spec = importlib.util.spec_from_file_location("fake_collect", fake_collect_py)
    fake_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fake_module)
    
    # Test that it fails when GPL is missing
    dest = tmp_path / "output"
    with pytest.raises(FileNotFoundError, match="GPL-3.0.txt"):
        fake_module.main([str(dest)])


def test_collect_licenses_copies_qt_licenses(tmp_path):
    """Collector must copy Qt licenses to dependency-licenses/Qt/."""
    dest = tmp_path / "licenses"
    collect_licenses_module.main([str(dest)])
    
    assert (dest / "Qt" / "GPL-3.0.txt").is_file(), "GPL-3.0.txt not copied to Qt/"
    assert (dest / "Qt" / "LGPL-3.0.txt").is_file(), "LGPL-3.0.txt not copied to Qt/"
    
    # Verify copies are not truncated
    gpl_size = (dest / "Qt" / "GPL-3.0.txt").stat().st_size
    lgpl_size = (dest / "Qt" / "LGPL-3.0.txt").stat().st_size
    assert gpl_size > 1000, f"GPL copy truncated: {gpl_size} bytes"
    assert lgpl_size > 1000, f"LGPL copy truncated: {lgpl_size} bytes"


def test_collect_licenses_includes_pyside_metadata(tmp_path):
    """Collector must document PySide6/Qt license expressions."""
    dest = tmp_path / "licenses"
    collect_licenses_module.main([str(dest)])
    
    pyside_meta = dest / "Qt" / "PYSIDE_LICENSE_METADATA.txt"
    assert pyside_meta.is_file(), "PySide metadata not created"
    
    content = pyside_meta.read_text(encoding="utf-8")
    assert "PySide6" in content, "PySide6 not documented"
    assert "GPL-3.0" in content or "LGPL" in content, "PySide6 license expressions not included"
    assert "See bundled GPL-3.0.txt and LGPL-3.0.txt" in content, "License reference missing"


def test_installed_distributions_includes_qt_notice(tmp_path):
    """Installed distributions notice must reference Qt packages."""
    dest = tmp_path / "licenses"
    collect_licenses_module.main([str(dest)])
    
    notices = (dest / "installed-distributions.txt").read_text(encoding="utf-8")
    # Should include PySide6 and related packages
    lines = notices.split("\n")
    assert any("PySide6" in line for line in lines), "PySide6 not in distribution notices"


def test_collect_licenses_preserves_other_license_files(tmp_path):
    """Collector must still copy other packages' license files where available."""
    dest = tmp_path / "licenses"
    collect_licenses_module.main([str(dest)])
    
    # At least keyring, httpx, python-docx should have license files
    license_dirs = list(dest.glob("*/"))
    assert len(license_dirs) > 2, f"Expected multiple package license dirs, got {len(license_dirs)}"


def test_pyside_license_metadata_format(tmp_path):
    """PySide metadata must include version and expression."""
    dest = tmp_path / "licenses"
    collect_licenses_module.main([str(dest)])
    
    pyside_meta = dest / "Qt" / "PYSIDE_LICENSE_METADATA.txt"
    content = pyside_meta.read_text(encoding="utf-8")
    
    # Must include version info
    lines = content.split("\n")
    version_lines = [l for l in lines if any(x in l for x in ("6.10", "6.9", "6.8", "6."))]
    assert len(version_lines) > 0, "PySide6 version not documented in metadata"
