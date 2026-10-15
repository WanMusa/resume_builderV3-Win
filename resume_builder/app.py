"""GUI entry point and isolated frozen-resource smoke check."""

import argparse
import json
import sys
from pathlib import Path

from PySide6.QtCore import QLockFile, QStandardPaths
from PySide6.QtWidgets import QApplication, QMessageBox

from .config import KeyVault, atomic_write, load_settings
from .library import Library


def main():
    parser = argparse.ArgumentParser(description="Resume Builder V3 Windows desktop")
    parser.add_argument("--data-dir", type=Path, help="Use an isolated local data directory")
    parser.add_argument("--smoke-check", type=Path, metavar="DIRECTORY", help="Initialize resources and SQLite offline, write smoke-result.json, then exit")
    args = parser.parse_args()
    application = QApplication.instance() or QApplication(sys.argv[:1])
    application.setApplicationName("ResumeBuilderV3-Win")
    application.setOrganizationName("")
    data_dir = args.smoke_check or args.data_dir or Path(
        QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    )
    data_dir = data_dir.expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(data_dir / "application.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        if not args.smoke_check:
            QMessageBox.warning(None, "Already running", "Another Resume Builder instance is using this data directory.")
        return 2
    try:
        from .storage import Store

        library = Library(data_dir)
        store = Store(data_dir / "history.sqlite3")
        store.recover()
        settings = load_settings(data_dir)
        if args.smoke_check:
            from importlib.resources import files

            from .models import Job

            sample = json.loads(files("resume_builder").joinpath("assets/sample-job.json").read_text(encoding="utf-8"))
            Job.model_validate(sample["jobs"][0])
            library.load_base("fictional-example")
            library.load_prompt("resume-default")
            store.list_jobs()
            atomic_write(data_dir / "smoke-result.json", json.dumps({"ok": True, "resources": True, "sqlite": True, "network_calls": 0}))
            return 0
        from .ui import MainWindow

        window = MainWindow(data_dir, store, settings, KeyVault())
        window.show()
        return application.exec()
    except Exception:
        if args.smoke_check:
            atomic_write(data_dir / "smoke-result.json", '{"ok": false, "error": "Initialization failed; preserve local files for recovery."}')
        else:
            QMessageBox.critical(
                None, "Startup failed",
                "Could not initialize Resume Builder. No data was recreated or erased.\n"
                f"Check writable storage and the existing settings/database in:\n{data_dir}\n"
                "Preserve this folder before attempting recovery. No credentials or private content are logged.",
            )
        return 1
    finally:
        lock.unlock()
