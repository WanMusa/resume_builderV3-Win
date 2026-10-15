"""Native desktop workspace. Background workers communicate only through Qt signals."""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import QDate, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateEdit,
    QFileDialog, QFormLayout, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QSplitter,
    QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .base_editor import BaseResumeEditor
from .config import Settings, save_settings, validate_folders
from .library import Library
from .models import BaseResume

ACTIVE = {"queued", "generating", "rendering"}
RETRYABLE = {"ready", "failed", "interrupted"}
DEFAULT_BASE_URL = "https://api.openai.com/v1"


class WorkerBridge(QObject):
    changed = Signal()
    finished = Signal(str, object, object)


def _button(text, callback, layout):
    button = QPushButton(text)
    button.clicked.connect(callback)
    layout.addWidget(button)
    return button


def _scroll(widget):
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(widget)
    return scroll


class MainWindow(QMainWindow):
    def __init__(self, data_dir: Path, store, settings: Settings, vault):
        super().__init__()
        self.data_dir, self.store, self.settings, self.vault = Path(data_dir), store, settings, vault
        self.library = Library(self.data_dir)
        self._prompt_dirty = self._base_dirty = self._settings_dirty = False
        self._loading = True
        self._prompt_id = self._base_id = None
        self._scanning = self._testing = False
        self._closing = False
        self._threads = set()
        self._rows = {}
        self.bridge = WorkerBridge(self)
        self.bridge.changed.connect(self.refresh_jobs)
        self.bridge.finished.connect(self._finished)
        from .inbox import InboxScanner
        from .pipeline import Processor
        from .queue import QueueWorker
        self.scanner = InboxScanner(store)
        self.worker = QueueWorker(store, Processor(store, vault.get), lambda *_: self.bridge.changed.emit())
        self.setWindowTitle("Resume Builder — local workspace")
        self.resize(1280, 850)
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self._build_jobs()
        self._build_prompts()
        self._build_bases()
        self._build_settings()
        self.setStyleSheet(
            "QMainWindow {background:#f3f5f8;} QTabWidget::pane {border:0;}"
            "QTabBar::tab {padding:12px 24px;} QPushButton {padding:7px 11px;}"
            "QLineEdit,QComboBox,QSpinBox {min-height:26px;} "
            "QTableWidget {alternate-background-color:#f0f4f8;} "
            "QLabel#heading {font-size:20px;font-weight:600;}"
        )
        self._load_libraries()
        self._load_settings()
        self._loading = False
        self.worker.start()
        self.refresh_jobs()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_jobs)
        self.refresh_timer.start(1500)
        self.scan_timer = QTimer(self)
        self.scan_timer.timeout.connect(self.scan_inbox)
        self.scan_timer.start(2500)
        QTimer.singleShot(0, self.scan_inbox)
        if settings.inbox_folder and settings.output_folder:
            self.statusBar().showMessage("Ready. Import is automatic; generation requires Process.")
        else:
            self.tabs.setCurrentIndex(3)
            self.statusBar().showMessage("Choose dedicated inbox and output folders in Settings, then save to begin.")

    def _page(self, title, description):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 16, 18, 16)
        heading = QLabel(title)
        heading.setObjectName("heading")
        layout.addWidget(heading)
        label = QLabel(description)
        label.setWordWrap(True)
        layout.addWidget(label)
        return page, layout

    def _error(self, exc):
        QMessageBox.warning(self, "Action could not be completed", str(exc))

    def _background(self, name, callback):
        def run():
            result = error = None
            try:
                result = callback()
            except Exception as exc:
                error = str(exc)
            self.bridge.finished.emit(name, result, error)
            self._threads.discard(threading.current_thread())
        thread = threading.Thread(target=run, daemon=True)
        self._threads.add(thread)
        thread.start()

    def _finished(self, name, result, error):
        if name == "scan":
            self._scanning = False
            if error or result:
                self.statusBar().showMessage("Inbox: " + (error or "; ".join(map(str, result))), 15000)
            self.refresh_jobs()
        elif name == "connection":
            self._testing = False
            self.test_button.setEnabled(True)
            if error:
                self._error(error)
            else:
                QMessageBox.information(self, "Connection test", "Provider connection succeeded.")

    def _build_jobs(self):
        page, layout = self._page(
            "Jobs", "Stable inbox files are imported locally. Select jobs and explicitly Process to generate documents. "
            "Retry resumes saved checkpoints; paid regeneration creates a new revision.")
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search title, company, description or ID…")
        self.search.textChanged.connect(self.refresh_jobs)
        self.status_filter = QComboBox()
        self.status_filter.addItems(["All statuses", "ready", "queued", "generating", "rendering", "completed", "failed", "interrupted", "invalid"])
        self.status_filter.currentTextChanged.connect(self.refresh_jobs)
        self.show_archived = QCheckBox("Include archived")
        self.show_archived.toggled.connect(self.refresh_jobs)
        filters.addWidget(self.search, 1)
        filters.addWidget(self.status_filter)
        filters.addWidget(self.show_archived)
        _button("Scan now", self.scan_inbox, filters)
        layout.addLayout(filters)
        splitter = QSplitter()
        self.jobs_table = QTableWidget(0, 8)
        self.jobs_table.setHorizontalHeaderLabels(
            ["Title / ID", "Company", "Status", "Filename", "Error", "Captured", "Updated", "Archived"])
        self.jobs_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.jobs_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.jobs_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.jobs_table.setAlternatingRowColors(True)
        self.jobs_table.setWordWrap(False)
        self.jobs_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate((260, 155, 105, 160, 250, 180, 180, 80)):
            self.jobs_table.setColumnWidth(column, width)
        self.jobs_table.itemSelectionChanged.connect(self._selection_changed)
        splitter.addWidget(self.jobs_table)
        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        detail_layout.addWidget(self.preview)
        self.artifacts = QComboBox()
        detail_layout.addWidget(self.artifacts)
        links = QHBoxLayout()
        _button("Open artifact", self.open_artifact, links)
        _button("Open source file", self.open_source, links)
        _button("Source website", self.open_website, links)
        detail_layout.addLayout(links)
        splitter.addWidget(detail)
        splitter.setSizes([760, 460])
        layout.addWidget(splitter, 1)
        actions = QHBoxLayout()
        self.process_button = _button("Process selected / retry checkpoints", self.process_selected, actions)
        self.regenerate_button = _button("Regenerate (new revision)…", self.regenerate_selected, actions)
        self.rerender_button = _button("Render saved outputs to folder…", self.rerender_selected, actions)
        actions.addStretch()
        layout.addLayout(actions)
        archive_actions = QHBoxLayout()
        self.archive_button = _button("Archive selected", lambda: self.archive_selected(True), archive_actions)
        self.restore_button = _button("Restore selected", lambda: self.archive_selected(False), archive_actions)
        _button("Archive completed", self.archive_completed, archive_actions)
        archive_actions.addStretch()
        _button("Open output folder", lambda: self._open_local(self.settings.output_folder), archive_actions)
        layout.addLayout(archive_actions)
        self.tabs.addTab(page, "Jobs")

    def _selected_ids(self):
        return [self.jobs_table.item(index.row(), 0).data(Qt.ItemDataRole.UserRole)
                for index in self.jobs_table.selectionModel().selectedRows()]

    def refresh_jobs(self, *_):
        selected = set(self._selected_ids()) if hasattr(self, "jobs_table") else set()
        try:
            rows = self.store.list_jobs(include_archived=self.show_archived.isChecked())
        except Exception as exc:
            self.statusBar().showMessage(str(exc))
            return
        self._rows = {row["id"]: row for row in rows}
        search = self.search.text().casefold()
        status = self.status_filter.currentText()
        rows = [r for r in rows if (status == "All statuses" or r["status"] == status)
                and (not search or search in json.dumps(r, ensure_ascii=False).casefold())]
        signature = json.dumps(rows, sort_keys=True, default=str)
        if signature == getattr(self, "_table_signature", None):
            self._update_busy_controls()
            return
        self._table_signature = signature
        self.jobs_table.blockSignals(True)
        self.jobs_table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            job = row.get("job") or {}
            values = [job.get("title") or row["id"], job.get("company") or "", row["status"],
                      row.get("filename") or Path(row.get("source_path") or "").name, row.get("error") or "",
                      job.get("captured_at") or "", row.get("updated_at", ""), "Yes" if row.get("archived") else ""]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.ItemDataRole.UserRole, row["id"])
                item.setToolTip(str(row.get("source_path") or value) if column == 3 else str(value))
                self.jobs_table.setItem(index, column, item)
                if row["id"] in selected:
                    item.setSelected(True)
        self.jobs_table.blockSignals(False)
        self._selection_changed()
        self._update_busy_controls()

    def _selection_changed(self):
        ids = self._selected_ids()
        row = self._rows.get(ids[0]) if ids else None
        self.artifacts.clear()
        if not row:
            self.preview.setPlainText("Select a job to inspect its full description, errors, checkpoints and artifacts.")
        else:
            job = row.get("job") or {}
            metadata = {key: value for key, value in row.items()
                        if key not in ("job", "snapshot", "checkpoints", "artifacts") and not key.endswith("_json")}
            job_details = {key: value for key, value in job.items() if key != "description_text"}
            self.preview.setPlainText(
                f"{job.get('title') or row['id']}\n{job.get('company') or ''}\n"
                f"Status: {row['status']}  •  Revision: {row.get('revision', 1)}\n\n"
                f"FULL JOB DESCRIPTION\n{job.get('description_text') or '(Invalid input — see error below)'}\n\n"
                "JOB METADATA\n" + json.dumps(job_details, indent=2, ensure_ascii=False, default=str)
                + f"\n\nSOURCE FILE\n{row.get('source_path') or 'Unknown'}"
                + f"\n\nERROR / GUIDANCE\n{row.get('error') or 'None'}"
                + "\n\nSAVED CHECKPOINTS\n"
                + json.dumps(row.get("checkpoints") or {}, indent=2, ensure_ascii=False, default=str)
                + "\n\nARTIFACTS\n" + json.dumps(row.get("artifacts") or [], indent=2, ensure_ascii=False, default=str)
                + "\n\nHISTORY METADATA\n"
                + json.dumps(metadata, indent=2, ensure_ascii=False, default=str)
                + "\n\nFROZEN INPUT SNAPSHOT\n"
                + json.dumps(row.get("snapshot"), indent=2, ensure_ascii=False, default=str)
            )
            for artifact in row.get("artifacts", []):
                if isinstance(artifact, dict):
                    path = artifact.get("path") or artifact.get("file_path")
                else:
                    path = artifact
                if path:
                    self.artifacts.addItem(Path(str(path)).name, str(path))
        eligible = any(self._rows[i]["status"] in RETRYABLE
                       and self._rows[i].get("job") and not self._rows[i].get("archived") for i in ids)
        self.process_button.setEnabled(eligible)
        retry = any(self._rows[i].get("snapshot") for i in ids)
        self.process_button.setText("Retry selected (frozen inputs / checkpoints)" if retry else "Process selected")
        archive_allowed = bool(ids) and all(self._rows[i]["status"] not in ACTIVE for i in ids)
        self.archive_button.setEnabled(archive_allowed)
        self.restore_button.setEnabled(archive_allowed)
        self.regenerate_button.setEnabled(bool(ids) and hasattr(self.store, "regenerate")
                                          and all(self._rows[i]["status"] not in ACTIVE
                                                  and self._rows[i].get("job")
                                                  and not self._rows[i].get("archived") for i in ids))
        self.rerender_button.setEnabled(
            bool(ids) and hasattr(self.store, "rerender")
            and all(self._can_rerender(self._rows[i]) for i in ids)
        )

    def scan_inbox(self):
        if self._closing or self._scanning or not self.settings.inbox_folder:
            return
        self._scanning = True
        folder = Path(self.settings.inbox_folder)
        self._background("scan", lambda: self.scanner.scan(folder))

    def process_selected(self):
        try:
            ids = [i for i in self._selected_ids() if self._rows[i]["status"] in RETRYABLE
                   and self._rows[i].get("job") and not self._rows[i].get("archived")]
            if not ids:
                return
            snapshots = {}
            fresh = None
            for job_id in ids:
                row = self.store.get(job_id)
                frozen = row.get("snapshot")
                is_new = not frozen
                if not frozen:
                    if fresh is None:
                        fresh = self.library.snapshot(self.settings)
                    frozen = fresh
                frozen = dict(frozen)
                if is_new:
                    frozen["revision"] = row.get("revision", 1)
                if not self._acknowledge_budget(row, frozen):
                    return
                snapshots[job_id] = frozen
            live = {f"{s.get('provider')}/{s.get('model')} → {s.get('base_url')}" for s in snapshots.values()
                    if s.get("provider", "mock") != "mock"}
            if live and QMessageBox.question(
                self, "Start paid generation?",
                f"Process {len(ids)} job(s) using {', '.join(sorted(live))}? "
                "Your API key, job description and career evidence will be sent to these destinations. "
                "Continue only if you trust them. Requests may incur provider charges. "
                "Retries reuse original frozen inputs and saved checkpoints. "
                "A request interrupted before checkpoint storage may be charged again.",
            ) != QMessageBox.StandardButton.Yes:
                return
            queued = 0
            for job_id in ids:
                queued += bool(self.store.enqueue(job_id, snapshots[job_id]))
            self.worker.wake()
            self.statusBar().showMessage(f"Queued {queued} job(s).")
            self.refresh_jobs()
        except Exception as exc:
            self._error(exc)

    def regenerate_selected(self):
        ids = self._selected_ids()
        destination = self.settings.base_url if self.settings.provider != "mock" else "Offline mock (no network)"
        if QMessageBox.warning(
            self, "Regenerate paid outputs?",
            "This intentionally discards checkpoint reuse and creates a NEW revision without overwriting previous documents. "
            f"Destination: {destination}. Live providers receive your key, job description and career evidence "
            "and may charge again. Continue only if you trust the endpoint.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            snapshot = self.library.snapshot(self.settings)
            snapshots = {}
            for job_id in ids:
                frozen = dict(snapshot)
                row = dict(self.store.get(job_id), checkpoints={})
                if not self._acknowledge_budget(row, frozen):
                    return
                snapshots[job_id] = frozen
            for job_id, frozen in snapshots.items():
                self.store.regenerate(job_id, frozen)
            self.worker.wake()
            self.refresh_jobs()
        except Exception as exc:
            self._error(exc)

    def _acknowledge_budget(self, row, snapshot):
        from . import providers
        estimate = getattr(providers, "estimate_input_chars", None)

        def input_size(prompt):
            if estimate is not None:
                return estimate(row["job"], snapshot["base"], prompt)
            # Conservative fallback for providers without an exact local estimator.
            return (
                len(row["job"]["description_text"]) + len(prompt)
                + len(json.dumps(snapshot["base"], ensure_ascii=False))
                + len(providers.POLICY) + 64
            )

        checkpoints = row.get("checkpoints") or {}
        sizes = {
            kind: input_size(snapshot[kind + "_prompt"])
            for kind in ("resume", "letter") if kind not in checkpoints
        }
        budget = snapshot.get("input_budget_chars", 30000)
        if not sizes or max(sizes.values()) <= budget or snapshot.get("budget_acknowledged") is True:
            return True
        estimates = ", ".join(f"{kind}: {size:,} characters" for kind, size in sizes.items())
        if QMessageBox.warning(
            self, "Input exceeds your request budget",
            f"Full input estimates including job description, career evidence and instructions:\n{estimates}\n\n"
            f"Configured limit: {budget:,} characters per request. Character counts are not exact token or price estimates. "
            "Sending the larger request can cost more. Nothing will be dropped or truncated.\n\n"
            "Choose Yes to explicitly allow this job's larger requests, or No to edit the character budget in Settings "
            "or shorten the source information yourself.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return False
        snapshot["budget_acknowledged"] = True
        return True

    @staticmethod
    def _can_rerender(row):
        return (
            row["status"] not in ACTIVE and not row.get("archived")
            and bool(row.get("snapshot")) and bool(row.get("job"))
            and {"resume", "letter"}.issubset(row.get("checkpoints") or {})
        )

    def rerender_selected(self):
        ids = self._selected_ids()
        if not ids:
            return
        folder = QFileDialog.getExistingDirectory(self, "Render saved outputs to a different folder", self.settings.output_folder)
        if not folder:
            return
        try:
            for job_id in ids:
                row = self.store.get(job_id)
                if not self._can_rerender(row):
                    raise ValueError("Render relocation requires an inactive, unarchived job with both saved resume and letter checkpoints.")
                frozen_inbox = row["snapshot"].get("inbox_folder") or self.settings.inbox_folder
                validate_folders(frozen_inbox, folder)
                if self.settings.inbox_folder and self.settings.inbox_folder != frozen_inbox:
                    validate_folders(self.settings.inbox_folder, folder)
            if QMessageBox.question(
                self, "Render saved outputs — zero API calls",
                f"Create {len(ids)} new render-only revision(s) in:\n{folder}\n\n"
                "Both saved resume and letter checkpoints are reused. No provider API requests or new token charges "
                "are made. Original snapshots, checkpoints and documents remain unchanged.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
            ) != QMessageBox.StandardButton.Yes:
                return
            for job_id in ids:
                self.store.rerender(job_id, folder)
            self.worker.wake()
            self.refresh_jobs()
        except Exception as exc:
            self._error(exc)

    def archive_selected(self, archived):
        try:
            ids = [i for i in self._selected_ids() if self._rows[i]["status"] not in ACTIVE]
            self.store.archive(ids, archived=archived)
            self.refresh_jobs()
        except Exception as exc:
            self._error(exc)

    def archive_completed(self):
        try:
            self.store.archive([i for i, row in self._rows.items() if row["status"] == "completed"])
            self.refresh_jobs()
        except Exception as exc:
            self._error(exc)

    def _open_local(self, path):
        if path:
            target = Path(path).expanduser().resolve()
            if not target.exists():
                self._error(f"Path no longer exists: {target}")
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def open_artifact(self):
        self._open_local(self.artifacts.currentData())

    def open_source(self):
        ids = self._selected_ids()
        if ids:
            self._open_local(self._rows[ids[0]].get("source_path"))

    def open_website(self):
        ids = self._selected_ids()
        if ids:
            url = (self._rows[ids[0]].get("job") or {}).get("source_url", "")
            parsed = urlsplit(url)
            if parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password:
                QDesktopServices.openUrl(QUrl(url))

    def _build_prompts(self):
        page, layout = self._page(
            "Prompt library", "Edit reusable resume and letter instructions. Supported placeholders: "
            "{job_description} and {evidence}. Unsupported placeholders are rejected on save.")
        bar = QHBoxLayout()
        self.prompt_selector = QComboBox()
        self.prompt_selector.currentIndexChanged.connect(self._switch_prompt)
        bar.addWidget(self.prompt_selector, 1)
        _button("New prompt", self.new_prompt, bar)
        _button("Save", self.save_prompt, bar)
        _button("Save as new…", lambda: self.save_prompt(as_new=True), bar)
        layout.addLayout(bar)
        form = QFormLayout()
        self.prompt_name = QLineEdit()
        self.prompt_kind = QComboBox()
        self.prompt_kind.addItems(["resume", "letter"])
        form.addRow("Name", self.prompt_name)
        form.addRow("Document", self.prompt_kind)
        layout.addLayout(form)
        self.prompt_content = QPlainTextEdit()
        self.prompt_content.setPlaceholderText("Write grounded instructions using {job_description} and {evidence}…")
        layout.addWidget(self.prompt_content, 1)
        self.prompt_name.textChanged.connect(self._mark_prompt)
        self.prompt_kind.currentTextChanged.connect(self._mark_prompt)
        self.prompt_content.textChanged.connect(self._mark_prompt)
        self.tabs.addTab(page, "Prompts")

    def _mark_prompt(self, *_):
        if not self._loading:
            self._prompt_dirty = True

    def _confirm_dirty(self, kind):
        if not getattr(self, f"_{kind}_dirty"):
            return True
        choice = QMessageBox.question(
            self, "Unsaved changes", f"Save changes to {kind} before continuing?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if choice == QMessageBox.StandardButton.Cancel:
            return False
        if choice == QMessageBox.StandardButton.Save:
            return bool({"prompt": self.save_prompt, "base": self.save_base, "settings": self.save_preferences}[kind]())
        setattr(self, f"_{kind}_dirty", False)
        return True

    def _load_libraries(self):
        self._prompts = self.library.prompts()
        self._bases = self.library.bases()
        self.prompt_selector.blockSignals(True)
        self.prompt_selector.clear()
        for prompt in self._prompts:
            self.prompt_selector.addItem(f"{prompt['name']} ({prompt['kind']})", prompt["id"])
        self.prompt_selector.setCurrentIndex(max(0, self.prompt_selector.findData(self._prompt_id)))
        self.prompt_selector.blockSignals(False)
        self.base_selector.blockSignals(True)
        self.base_selector.clear()
        for base in self._bases:
            content = base["content"]
            self.base_selector.addItem(content["name"] + (" [EXAMPLE]" if content.get("is_example") else ""), base["id"])
        self.base_selector.setCurrentIndex(max(0, self.base_selector.findData(self._base_id)))
        self.base_selector.blockSignals(False)
        if self._prompt_id is None:
            self._switch_prompt()
        if self._base_id is None:
            self._switch_base()
        if hasattr(self, "resume_prompt"):
            self._populate_library_settings()

    def _switch_prompt(self, *_):
        selected = self.prompt_selector.currentData()
        if not self._confirm_dirty("prompt"):
            self.prompt_selector.blockSignals(True)
            self.prompt_selector.setCurrentIndex(self.prompt_selector.findData(self._prompt_id))
            self.prompt_selector.blockSignals(False)
            return
        self._prompt_id = selected
        prompt = next((p for p in self._prompts if p["id"] == selected), None)
        if prompt:
            loading, self._loading = self._loading, True
            self.prompt_name.setText(prompt["name"])
            self.prompt_kind.setCurrentText(prompt["kind"])
            self.prompt_content.setPlainText(prompt["content"])
            self._loading = loading
        self._prompt_dirty = False

    def new_prompt(self):
        if self._confirm_dirty("prompt"):
            self._prompt_id = None
            self.prompt_selector.setCurrentIndex(-1)
            self.prompt_name.setText("New prompt")
            self.prompt_content.setPlainText("Job:\n{job_description}\n\nVerified evidence:\n{evidence}")
            self._prompt_dirty = True

    def save_prompt(self, _checked=False, *, as_new=False):
        try:
            from .library import validate_prompt
            validate_prompt(self.prompt_content.toPlainText())
            self._prompt_id = self.library.save_prompt(
                None if as_new else self._prompt_id, self.prompt_name.text(),
                self.prompt_kind.currentText(), self.prompt_content.toPlainText())
            self._prompt_dirty = False
            self._load_libraries()
            return True
        except Exception as exc:
            self._error(exc)
            return False

    def _build_bases(self):
        page, layout = self._page(
            "Base resumes", "Your verified source of truth. Edit every career fact below; add, edit and reorder evidence in each role. "
            "Examples and unverified resumes are blocked for live providers. Version 1 JSON only.")
        bar = QHBoxLayout()
        self.base_selector = QComboBox()
        self.base_selector.currentIndexChanged.connect(self._switch_base)
        bar.addWidget(self.base_selector, 1)
        _button("New", self.new_base, bar)
        _button("Save", self.save_base, bar)
        _button("Duplicate", lambda: self.save_base(as_new=True), bar)
        _button("Import JSON…", self.import_base, bar)
        _button("Export JSON…", self.export_base, bar)
        layout.addLayout(bar)
        self.base_editor = BaseResumeEditor()
        self.base_editor.dirty.connect(self._mark_base)
        layout.addWidget(_scroll(self.base_editor), 1)
        self.tabs.addTab(page, "Base Resumes")

    def _mark_base(self):
        if not self._loading:
            self._base_dirty = True

    def _switch_base(self, *_):
        selected = self.base_selector.currentData()
        if not self._confirm_dirty("base"):
            self.base_selector.blockSignals(True)
            self.base_selector.setCurrentIndex(self.base_selector.findData(self._base_id))
            self.base_selector.blockSignals(False)
            return
        self._base_id = selected
        base = next((b for b in self._bases if b["id"] == selected), None)
        if base:
            self.base_editor.set_data(base["content"])
        self._base_dirty = False

    def new_base(self):
        if self._confirm_dirty("base"):
            self._base_id = None
            self.base_selector.setCurrentIndex(-1)
            self.base_editor.set_data({"name": "My resume", "identity": {"name": "Your name"}})
            self._base_dirty = True

    def save_base(self, _checked=False, *, as_new=False):
        try:
            content = self.base_editor.validated_data()
            self._base_id = self.library.save_base(None if as_new else self._base_id, content)
            self._base_dirty = False
            self._load_libraries()
            return True
        except Exception as exc:
            self._error(exc)
            return False

    def import_base(self):
        if not self._confirm_dirty("base"):
            return
        filename, _ = QFileDialog.getOpenFileName(self, "Import version 1 base resume", "", "JSON (*.json)")
        if filename:
            try:
                content = BaseResume.model_validate_json(Path(filename).read_text(encoding="utf-8")).model_dump()
                if content["verified"] and not content["is_example"]:
                    content["verified"] = QMessageBox.question(
                        self, "Verify imported career facts",
                        "This imported resume is marked real and verified. Have you personally checked every career fact "
                        "and supporting evidence? Choose No to import it as unverified for review.",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
                    ) == QMessageBox.StandardButton.Yes
                self.base_selector.blockSignals(True)
                self.base_selector.setCurrentIndex(-1)
                self.base_selector.blockSignals(False)
                self._base_id = None
                self.base_editor.set_data(content)
                self._base_dirty = True
            except Exception as exc:
                self._error(exc)

    def export_base(self):
        try:
            content = self.base_editor.validated_data()
            filename, _ = QFileDialog.getSaveFileName(self, "Export base resume", "base-resume.json", "JSON (*.json)")
            if filename:
                Path(filename).write_text(json.dumps(content, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except Exception as exc:
            self._error(exc)

    def _build_settings(self):
        page, outer = self._page("Settings", "Keys never enter job snapshots, exports or logs. "
                                "Only Windows secure credential storage supports persistence; other platforms keep keys in memory.")
        body = QWidget()
        layout = QVBoxLayout(body)
        form = QFormLayout()
        self.provider = QComboBox()
        self.provider.addItems(["mock", "openai", "compatible"])
        self.model = QLineEdit()
        self.base_url = QLineEdit()
        self.mode = QComboBox()
        self.mode.addItems(["json_schema", "json_object"])
        self.mode.setToolTip("Advanced: JSON object mode is explicit opt-in for compatible endpoints without schema support.")
        self.timeout = QSpinBox()
        self.timeout.setRange(5, 300)
        self.timeout.setSuffix(" seconds")
        self.input_budget_chars = QSpinBox()
        self.input_budget_chars.setRange(1000, 200000)
        self.input_budget_chars.setSingleStep(1000)
        self.input_budget_chars.setSuffix(" characters / request")
        self.input_budget_chars.setToolTip("Full job, evidence and prompt input. Above-budget requests require explicit approval; never truncated.")
        self.inbox_folder, self.output_folder = QLineEdit(), QLineEdit()
        self.resume_prompt, self.letter_prompt, self.base_resume = QComboBox(), QComboBox(), QComboBox()
        for title, widget in (
            ("Provider", self.provider), ("Model", self.model), ("Base URL", self.base_url),
            ("Response mode (advanced)", self.mode), ("Request timeout", self.timeout),
            ("Input character budget", self.input_budget_chars),
            ("Inbox folder", self.inbox_folder), ("Output folder", self.output_folder),
            ("Resume prompt", self.resume_prompt), ("Letter prompt", self.letter_prompt),
            ("Base resume", self.base_resume),
        ):
            if widget in (self.inbox_folder, self.output_folder):
                row = QHBoxLayout()
                row.addWidget(widget)
                _button("Browse…", lambda _=False, w=widget: self._browse(w), row)
                form.addRow(title, row)
            else:
                form.addRow(title, widget)
            if isinstance(widget, QLineEdit):
                widget.textChanged.connect(self._mark_settings)
            elif isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(self._mark_settings)
            else:
                widget.valueChanged.connect(self._mark_settings)
        self.custom_endpoint_acknowledged = QCheckBox(
            "I trust this custom service to receive my API key, job descriptions and career evidence")
        self.custom_endpoint_acknowledged.toggled.connect(self._mark_settings)
        form.addRow("Custom endpoint consent", self.custom_endpoint_acknowledged)
        self.provider.currentTextChanged.connect(self._provider_changed)
        self.base_url.textChanged.connect(self._endpoint_changed)
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("Leave blank to keep the current key")
        self.key.textChanged.connect(self._mark_settings)
        form.addRow("API key (masked)", self.key)
        self.persist_key = QCheckBox("Persist in Windows secure credential store")
        self.persist_key.setChecked(sys.platform == "win32")
        self.persist_key.setEnabled(sys.platform == "win32")
        self.persist_key.toggled.connect(self._mark_settings)
        form.addRow("", self.persist_key)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        _button("Save settings and key", self.save_preferences, buttons)
        _button("Delete stored/session key", self.delete_key, buttons)
        self.test_button = _button("Test connection…", self.test_connection, buttons)
        layout.addLayout(buttons)
        title = QLabel("History maintenance")
        title.setObjectName("heading")
        layout.addWidget(title)
        help_text = QLabel("Cleanup and reset remove database history only, not source files or generated documents. "
                           "Cleanup includes old completed, failed, invalid/interrupted records and all archived inactive records, "
                           "including unprocessed jobs. The cutoff uses the history creation date. "
                           "The deduplication ledger is retained by default. Active/queued work is never deleted.")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        maintenance = QHBoxLayout()
        self.cutoff = QDateEdit(QDate.currentDate().addDays(-30))
        self.cutoff.setCalendarPopup(True)
        self.cutoff.setDisplayFormat("yyyy-MM-dd")
        maintenance.addWidget(QLabel("History created before"))
        maintenance.addWidget(self.cutoff)
        self.cleanup_button = _button("Preview cleanup…", self.cleanup_history, maintenance)
        self.backup_button = _button("Back up database…", self.backup_database, maintenance)
        layout.addLayout(maintenance)
        self.clear_ledger = QCheckBox("Also clear deduplication ledger — unchanged inbox files may be rediscovered")
        layout.addWidget(self.clear_ledger)
        reset = QHBoxLayout()
        self.reset_button = _button("Reset all history…", self.reset_history, reset)
        layout.addLayout(reset)
        layout.addStretch()
        outer.addWidget(_scroll(body), 1)
        self.tabs.addTab(page, "Settings")

    def _browse(self, widget):
        folder = QFileDialog.getExistingDirectory(self, "Choose folder", widget.text())
        if folder:
            widget.setText(folder)

    def _mark_settings(self, *_):
        if not self._loading:
            self._settings_dirty = True

    def _provider_changed(self, *_):
        compatible = self.provider.currentText() == "compatible"
        self.base_url.setEnabled(compatible)
        self.mode.setEnabled(compatible)
        self.custom_endpoint_acknowledged.setEnabled(compatible)
        if not self._loading and not compatible:
            self.base_url.setText(DEFAULT_BASE_URL)
            self.mode.setCurrentText("json_schema")
            self.custom_endpoint_acknowledged.setChecked(False)

    def _endpoint_changed(self, *_):
        if not self._loading:
            self.custom_endpoint_acknowledged.setChecked(False)

    def _populate_library_settings(self):
        for widget, kind in ((self.resume_prompt, "resume"), (self.letter_prompt, "letter"), (self.base_resume, None)):
            selected = widget.currentData() or getattr(self.settings, widget is self.resume_prompt and "resume_prompt"
                                                      or widget is self.letter_prompt and "letter_prompt" or "base_resume")
            widget.blockSignals(True)
            widget.clear()
            for item in self._prompts if kind else self._bases:
                if kind and item["kind"] != kind:
                    continue
                widget.addItem(item["name"] if kind else item["content"]["name"], item["id"])
            widget.setCurrentIndex(widget.findData(selected))
            widget.blockSignals(False)

    def _load_settings(self):
        for name in ("provider", "mode"):
            getattr(self, name).setCurrentText(getattr(self.settings, name))
        for name in ("model", "base_url", "inbox_folder", "output_folder"):
            getattr(self, name).setText(str(getattr(self.settings, name) or ""))
        self.timeout.setValue(self.settings.timeout)
        self.input_budget_chars.setValue(self.settings.input_budget_chars)
        self.custom_endpoint_acknowledged.setChecked(self.settings.custom_endpoint_acknowledged)
        self._provider_changed()
        self._populate_library_settings()
        self._settings_dirty = False

    def _read_settings(self):
        values = self.settings.model_dump()
        for name in ("provider", "mode"):
            values[name] = getattr(self, name).currentText()
        for name in ("model", "base_url", "inbox_folder", "output_folder"):
            values[name] = getattr(self, name).text().strip()
        values["timeout"] = self.timeout.value()
        values["input_budget_chars"] = self.input_budget_chars.value()
        values["custom_endpoint_acknowledged"] = self.custom_endpoint_acknowledged.isChecked()
        for name in ("resume_prompt", "letter_prompt", "base_resume"):
            values[name] = getattr(self, name).currentData() or ""
        settings = Settings.model_validate(values)
        if settings.mode == "json_object" and settings.provider != "compatible":
            raise ValueError("JSON-object mode is available only for the compatible provider. Use json_schema for OpenAI.")
        if settings.base_url != DEFAULT_BASE_URL:
            if settings.provider != "compatible":
                raise ValueError("Only the compatible provider permits a custom URL. Select compatible in Settings.")
            if not settings.custom_endpoint_acknowledged:
                raise ValueError("A custom endpoint receives your API key and career evidence. Explicitly check the trust consent in Settings.")
        return settings

    def save_preferences(self):
        try:
            settings = self._read_settings()
            validate_folders(settings.inbox_folder, settings.output_folder)
            if settings.provider != "mock" and (
                self.key.text() or settings.provider != self.settings.provider
                or settings.base_url != self.settings.base_url
            ):
                if QMessageBox.warning(
                    self, "Trust this provider destination?",
                    f"Provider: {settings.provider}\nDestination: {settings.base_url}\n\n"
                    "Your API key will be sent to this destination when you explicitly test or process a job. "
                    "Generation also sends job descriptions and career evidence. Only continue if you trust this endpoint. "
                    "Saving settings does not send a request.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
                ) != QMessageBox.StandardButton.Yes:
                    return False
            for folder in (settings.inbox_folder, settings.output_folder):
                Path(folder).expanduser().mkdir(parents=True, exist_ok=True)
            if self.key.text():
                self.vault.set(self.key.text(), persist=self.persist_key.isChecked(), base_url=settings.base_url)
            save_settings(self.data_dir, settings)
            self.settings = settings
            self.key.clear()
            self._settings_dirty = False
            self.statusBar().showMessage("Settings saved.", 5000)
            return True
        except Exception as exc:
            self._error(exc)
            return False

    def delete_key(self):
        if QMessageBox.question(self, "Delete API key?", "Remove the saved and in-memory API key?") == QMessageBox.StandardButton.Yes:
            try:
                self.vault.delete(self._read_settings().base_url)
                self.key.clear()
            except Exception as exc:
                self._error(exc)

    def test_connection(self):
        if self._testing:
            return
        try:
            config = self._read_settings().model_dump()
            key = None if config["provider"] == "mock" else (self.key.text() or self.vault.get(config["base_url"]))
            destination = config["base_url"] if config["provider"] != "mock" else "Offline mock (no network)"
            if QMessageBox.warning(
                self, "Explicit provider test", f"Destination: {destination}\n\n"
                "Send a tiny test request? For a live provider, your API key is sent to this endpoint; continue only if you trust it. "
                "Live providers may charge a small number of tokens. "
                "This does not process any jobs or upload your resume.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
            ) != QMessageBox.StandardButton.Yes:
                return
            from .providers import test_connection
            self._testing = True
            self.test_button.setEnabled(False)
            self._background("connection", lambda: test_connection(config, key))
        except Exception as exc:
            self._error(exc)

    def _maintenance_allowed(self):
        rows = self.store.list_jobs(include_archived=True)
        return not self.worker.is_busy and not any(r["status"] in ACTIVE for r in rows)

    def _update_busy_controls(self):
        allowed = self._maintenance_allowed()
        for button in (self.cleanup_button, self.reset_button, self.backup_button):
            button.setEnabled(allowed)

    def cleanup_history(self):
        if not self._maintenance_allowed():
            return
        try:
            cutoff = datetime.combine(self.cutoff.date().toPython(), datetime.min.time(), tzinfo=timezone.utc).isoformat()
            statuses = ["completed", "failed", "invalid", "interrupted"]
            clear = self.clear_ledger.isChecked()
            count = self.store.cleanup(cutoff, statuses, preview=True, clear_ledger=clear)
            warning = "\nDeduplication ledger will also be cleared; inbox files may reappear." if clear else "\nDeduplication ledger will be retained."
            if QMessageBox.warning(
                self, "Confirm cleanup", f"Delete {count} old terminal or archived inactive database history record(s)? "
                f"Unarchived unprocessed jobs and all active/queued jobs remain. Generated documents remain.{warning}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
            ) == QMessageBox.StandardButton.Yes:
                self.store.cleanup(cutoff, statuses, preview=False, clear_ledger=clear)
                self.refresh_jobs()
        except Exception as exc:
            self._error(exc)

    def reset_history(self):
        if not self._maintenance_allowed():
            return
        warning = " Inbox files may be rediscovered because you selected ledger deletion." if self.clear_ledger.isChecked() else " The deduplication ledger will remain."
        text, ok = QInputDialog.getText(self, "Reset database history", "Type RESET HISTORY to delete all inactive history." + warning)
        if ok and text == "RESET HISTORY":
            try:
                self.store.reset_history(clear_ledger=self.clear_ledger.isChecked())
                self.refresh_jobs()
            except Exception as exc:
                self._error(exc)

    def backup_database(self):
        if not self._maintenance_allowed():
            return
        filename, _ = QFileDialog.getSaveFileName(self, "Back up SQLite database", "resume-builder-backup.sqlite3", "SQLite (*.sqlite3)")
        if filename:
            try:
                self.store.backup(Path(filename))
                self.statusBar().showMessage("Database backup saved.", 5000)
            except Exception as exc:
                self._error(exc)

    def closeEvent(self, event: QCloseEvent):
        for kind in ("prompt", "base", "settings"):
            if not self._confirm_dirty(kind):
                event.ignore()
                return
        active = self.worker.is_busy or any(row["status"] in ACTIVE for row in self.store.list_jobs(include_archived=True))
        if active or self._testing:
            dialog = QMessageBox(self)
            dialog.setWindowTitle("Work is still running")
            dialog.setText("Keep the app running minimized, or quit? Quitting cannot cancel a request already received by a provider; "
                           "charges may still apply. Checkpoints allow an explicit retry next time.")
            keep = dialog.addButton("Keep running / minimize", QMessageBox.ButtonRole.RejectRole)
            quit_button = dialog.addButton("Quit anyway", QMessageBox.ButtonRole.DestructiveRole)
            dialog.setDefaultButton(keep)
            dialog.exec()
            if dialog.clickedButton() != quit_button:
                self.showMinimized()
                event.ignore()
                return
        self.scan_timer.stop()
        self.refresh_timer.stop()
        self._closing = True
        self.worker.stop()
        event.accept()
