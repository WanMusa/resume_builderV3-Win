import json
import os
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication, QMessageBox

from resume_builder.config import Settings
from resume_builder.storage import Store
from resume_builder.ui import MainWindow


class MemoryVault:
    def __init__(self):
        self.value = None
        self.persist = None

    def get(self, base_url="https://api.openai.com/v1"):
        return self.value

    def set(self, key, persist=False, base_url="https://api.openai.com/v1"):
        self.value, self.persist = key, persist

    def delete(self, base_url="https://api.openai.com/v1"):
        self.value = None


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **kw: QMessageBox.StandardButton.No)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **kw: QMessageBox.StandardButton.No)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **kw: QMessageBox.StandardButton.Ok)
    settings = Settings(inbox_folder=str(tmp_path / "inbox"), output_folder=str(tmp_path / "output"))
    store = Store(tmp_path / "jobs.sqlite3")
    win = MainWindow(tmp_path / "data", store, settings, MemoryVault())
    win.scan_timer.stop()
    win.refresh_timer.stop()
    yield win
    win.worker.stop()
    store.recover()
    win._prompt_dirty = win._base_dirty = win._settings_dirty = False
    win.close()
    store.close()


def add_job(window, identifier="sample"):
    job = {"id": identifier, "source_url": "https://example.test/jobs/1", "site": "Example",
           "captured_at": "2026-01-01T12:00:00+00:00", "description_text": "Build useful tools with Python",
           "title": "Engineer", "company": "Fictional company"}
    window.store.import_job(job, str(window.data_dir / "source.json"))
    window.refresh_jobs()
    return window.store.list_jobs()[-1]["id"]


def wait_for(app, condition, seconds=8):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert condition()


def test_four_functional_tabs_and_full_preview(window):
    assert [window.tabs.tabText(i) for i in range(4)] == ["Jobs", "Prompts", "Base Resumes", "Settings"]
    identifier = add_job(window)
    window.jobs_table.selectRow(0)
    headers = [window.jobs_table.horizontalHeaderItem(i).text() for i in range(window.jobs_table.columnCount())]
    assert {"Filename", "Error", "Captured", "Updated"}.issubset(headers)
    assert window.jobs_table.item(0, 3).text() == "source.json"
    assert window.jobs_table.item(0, 3).toolTip() == str(window.data_dir / "source.json")
    assert identifier in window.preview.toPlainText()
    assert "Build useful tools" in window.preview.toPlainText()
    preview = window.preview.toPlainText()
    assert '"input_json"' not in preview
    assert preview.index("FULL JOB DESCRIPTION") < preview.index("JOB METADATA") < preview.index("SAVED CHECKPOINTS") < preview.index("FROZEN INPUT SNAPSHOT")
    assert window.process_button.isEnabled()
    window.store.set_status(identifier, "failed", "Fictional actionable error — check the output folder.")
    window.refresh_jobs()
    assert window.jobs_table.item(0, 4).toolTip() == "Fictional actionable error — check the output folder."
    window.search.setText("not-present")
    assert window.jobs_table.rowCount() == 0
    window.search.setText("fictional company")
    assert window.jobs_table.rowCount() == 1
    window.status_filter.setCurrentText("completed")
    assert window.jobs_table.rowCount() == 0


def test_process_is_explicit_and_mock_produces_artifacts(window, app):
    identifier = add_job(window)
    assert window.store.get(identifier)["status"] == "ready"
    window.jobs_table.selectRow(0)
    window.process_selected()
    wait_for(app, lambda: window.store.get(identifier)["status"] in ("completed", "failed"))
    row = window.store.get(identifier)
    assert row["status"] == "completed", row["error"]
    assert len(row["artifacts"]) == 2
    assert all(Path(path).is_file() for path in row["artifacts"])
    window.refresh_jobs()
    assert not window.process_button.isEnabled()


def test_prompt_and_base_saving_are_real_library_edits(window):
    window.new_prompt()
    window.prompt_name.setText("My grounded prompt")
    window.prompt_kind.setCurrentText("resume")
    window.prompt_content.setPlainText("Job: {job_description}\nUse only {evidence}")
    assert window.save_prompt()
    saved_prompt = window._prompt_id
    assert window.library.load_prompt(saved_prompt).startswith("Job:")
    window.prompt_name.setText("Renamed grounded prompt")
    assert window.save_prompt()
    assert window._prompt_id == saved_prompt
    assert window.prompt_selector.currentText() == "Renamed grounded prompt (resume)"
    assert window.save_prompt(as_new=True)
    assert window._prompt_id != saved_prompt
    original = window.base_editor.validated_data()
    assert window.save_base(as_new=True)
    assert window.library.load_base(window._base_id) == original


def test_dirty_cancel_preserves_editor_and_selection(window, monkeypatch):
    old_id = window._prompt_id
    window.prompt_content.setPlainText("Unsaved")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **kw: QMessageBox.StandardButton.Cancel)
    other = 1 if window.prompt_selector.currentIndex() == 0 else 0
    window.prompt_selector.setCurrentIndex(other)
    assert window._prompt_id == old_id
    assert window.prompt_selector.currentData() == old_id
    assert window.prompt_content.toPlainText() == "Unsaved"
    assert window._prompt_dirty


def test_archive_restore_and_local_url_handling(window, monkeypatch):
    identifier = add_job(window)
    window.jobs_table.selectRow(0)
    window.archive_selected(True)
    assert window.jobs_table.rowCount() == 0
    window.show_archived.setChecked(True)
    window.jobs_table.selectRow(0)
    window.archive_selected(False)
    assert not window.store.get(identifier)["archived"]
    urls = []
    from resume_builder.ui import QDesktopServices
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: urls.append(url) or True)
    path = window.data_dir / "document with spaces.docx"
    path.write_bytes(b"fixture")
    window._open_local(path)
    assert urls[-1].isLocalFile()
    assert urls[-1].toLocalFile() == str(path)
    window.open_website()
    assert urls[-1] == QUrl("https://example.test/jobs/1")


def test_settings_key_excluded_and_connection_runs_off_main_thread(window, app, monkeypatch):
    window.key.setText("unit-test-fixture")
    assert window.save_preferences()
    assert window.vault.value == "unit-test-fixture"
    assert not window.vault.persist
    serialized = (window.data_dir / "settings.json").read_text()
    assert "unit-test-fixture" not in serialized
    assert "api_key" not in json.loads(serialized)
    from resume_builder import providers
    threads, notices = [], []
    monkeypatch.setattr(providers, "test_connection", lambda config, key: threads.append(threading.get_ident()), raising=False)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **kw: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **kw: notices.append(threading.get_ident()))
    main = threading.get_ident()
    window.test_connection()
    wait_for(app, lambda: bool(notices))
    assert threads and threads[0] != main
    assert notices == [main]


def test_active_states_disable_duplicate_process_and_maintenance(window):
    identifier = add_job(window)
    window.worker.stop()
    window.store.enqueue(identifier, window.library.snapshot(window.settings))
    for status in ("queued", "generating", "rendering"):
        window.store.set_status(identifier, status)
        window.refresh_jobs()
        window.jobs_table.selectRow(0)
        assert not window.process_button.isEnabled()
        assert not window.archive_button.isEnabled()
        assert not window.cleanup_button.isEnabled()
        assert not window.reset_button.isEnabled()
        assert not window.backup_button.isEnabled()


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_retry_reuses_original_snapshot_without_loading_changed_library(window, monkeypatch, status):
    identifier = add_job(window)
    window.worker.stop()
    frozen = window.library.snapshot(window.settings)
    window.store.enqueue(identifier, frozen)
    window.store.set_status(identifier, status)
    window.refresh_jobs()
    window.jobs_table.selectRow(0)
    monkeypatch.setattr(window.library, "snapshot", lambda *_: pytest.fail("Retry must use original inputs"))
    window.process_selected()
    assert window.store.get(identifier)["snapshot"] == frozen
    assert window.store.get(identifier)["status"] == "queued"


def test_cleanup_previews_before_delete_and_keeps_ledger_by_default(window, monkeypatch):
    calls = []
    monkeypatch.setattr(window.store, "cleanup", lambda cutoff, statuses, **kw: calls.append((cutoff, statuses, kw)) or 3)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **kw: QMessageBox.StandardButton.No)
    window.cleanup_history()
    assert len(calls) == 1 and calls[0][2] == {"preview": True, "clear_ledger": False}
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **kw: QMessageBox.StandardButton.Yes)
    window.cleanup_history()
    assert calls[-1][2] == {"preview": False, "clear_ledger": False}
    assert calls[-1][0].endswith("+00:00")


def test_reset_requires_exact_confirmation_and_explicit_ledger_opt_in(window, monkeypatch):
    from PySide6.QtWidgets import QInputDialog
    calls = []
    monkeypatch.setattr(window.store, "reset_history", lambda **kw: calls.append(kw))
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **kw: ("reset", True))
    window.reset_history()
    assert not calls
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **kw: ("RESET HISTORY", True))
    window.reset_history()
    assert calls == [{"clear_ledger": False}]
    window.clear_ledger.setChecked(True)
    window.reset_history()
    assert calls[-1] == {"clear_ledger": True}


def test_regeneration_requires_confirmation_and_preserves_original(window, monkeypatch):
    identifier = add_job(window)
    window.worker.stop()
    window.jobs_table.selectRow(0)
    assert window.regenerate_button.isEnabled()
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **kw: QMessageBox.StandardButton.No)
    window.regenerate_selected()
    assert len(window.store.list_jobs()) == 1
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **kw: QMessageBox.StandardButton.Yes)
    window.regenerate_selected()
    rows = window.store.list_jobs()
    assert len(rows) == 2
    revision = next(row for row in rows if row["id"] != identifier)
    assert revision["status"] == "queued"
    assert revision["revision"] == 2
    assert not revision["checkpoints"]
    assert window.store.get(identifier)["status"] == "ready"


def test_live_key_save_requires_destination_trust_without_exposing_key(window, monkeypatch):
    window.provider.setCurrentText("compatible")
    window.base_url.setText("https://provider.example.test/v1")
    window.custom_endpoint_acknowledged.setChecked(True)
    window.key.setText("fictional-key-fixture")
    notices = []

    def decline(*args, **kwargs):
        notices.append(args[2])
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "warning", decline)
    assert not window.save_preferences()
    assert window.vault.value is None
    assert window.settings.provider == "mock"
    assert "https://provider.example.test/v1" in notices[0]
    assert "fictional-key-fixture" not in notices[0]
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **kw: QMessageBox.StandardButton.Yes)
    assert window.save_preferences()
    assert window.settings.provider == "compatible"
    assert window.settings.custom_endpoint_acknowledged is True
    assert window.vault.value == "fictional-key-fixture"
    settings_text = (window.data_dir / "settings.json").read_text()
    assert "fictional-key-fixture" not in settings_text
    assert json.loads(settings_text)["custom_endpoint_acknowledged"] is True


def test_custom_url_consent_is_specific_to_destination_and_provider(window):
    assert not window.base_url.isEnabled()
    window.provider.setCurrentText("compatible")
    assert window.base_url.isEnabled()
    assert window.mode.isEnabled()
    window.mode.setCurrentText("json_object")
    window.base_url.setText("https://provider.example.test/v1")
    with pytest.raises(ValueError, match="consent"):
        window._read_settings()
    window.custom_endpoint_acknowledged.setChecked(True)
    assert window._read_settings().custom_endpoint_acknowledged
    window.base_url.setText("https://other.example.test/v1")
    assert not window.custom_endpoint_acknowledged.isChecked()
    window.custom_endpoint_acknowledged.setChecked(True)
    window.provider.setCurrentText("openai")
    assert window.base_url.text() == "https://api.openai.com/v1"
    assert not window.base_url.isEnabled()
    assert not window.mode.isEnabled()
    assert window.mode.currentText() == "json_schema"
    assert not window.custom_endpoint_acknowledged.isChecked()


def test_over_budget_requires_per_job_opt_in_without_truncation(window, monkeypatch):
    from resume_builder import providers
    identifier = add_job(window)
    description = window.store.get(identifier)["job"]["description_text"]
    window.worker.stop()
    window.input_budget_chars.setValue(1000)
    assert window.save_preferences()
    assert window.settings.input_budget_chars == 1000
    assert json.loads((window.data_dir / "settings.json").read_text())["input_budget_chars"] == 1000
    monkeypatch.setattr(providers, "estimate_input_chars", lambda job, base, prompt: 1500, raising=False)
    notices = []

    def decline(*args, **kwargs):
        notices.append(args[2])
        return QMessageBox.StandardButton.No

    window.jobs_table.selectRow(0)
    monkeypatch.setattr(QMessageBox, "warning", decline)
    window.process_selected()
    assert window.store.get(identifier)["status"] == "ready"
    assert "1,500" in notices[0] and "Nothing will be dropped or truncated" in notices[0]
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **kw: QMessageBox.StandardButton.Yes)
    window.process_selected()
    row = window.store.get(identifier)
    assert row["status"] == "queued"
    assert row["snapshot"]["budget_acknowledged"] is True
    assert row["job"]["description_text"] == description


@pytest.mark.parametrize("confirmed", [False, True])
def test_verified_real_import_requires_fresh_confirmation(window, monkeypatch, confirmed):
    from PySide6.QtWidgets import QFileDialog
    content = window.base_editor.validated_data()
    content.update(is_example=False, verified=True)
    source = window.data_dir / "fictional-import-fixture.json"
    source.write_text(json.dumps(content))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **kw: (str(source), "JSON"))
    def confirm(*args, **kwargs):
        assert args[-1] == QMessageBox.StandardButton.No
        assert "unverified" in args[2]
        return QMessageBox.StandardButton.Yes if confirmed else QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", confirm)
    window.import_base()
    assert window._base_id is None
    assert window.base_editor.data()["verified"] is confirmed
    assert window.base_editor.validated_data() == dict(content, verified=confirmed)


def test_cleanup_includes_archived_unprocessed_history_and_retains_deduplication(window, monkeypatch):
    from PySide6.QtCore import QDate
    identifier = add_job(window)
    original = window.store.get(identifier)
    window.store.archive([identifier])
    window.cutoff.setDate(QDate.currentDate().addDays(1))
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **kw: QMessageBox.StandardButton.Yes)
    window.cleanup_history()
    with pytest.raises(KeyError):
        window.store.get(identifier)
    assert not window.store.import_job(original["job"], original["source_path"])


def test_render_relocation_preserves_checkpoints_and_makes_zero_provider_calls(window, app, monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    from resume_builder import pipeline, providers
    identifier = add_job(window)
    window.worker.stop()
    snapshot = window.library.snapshot(window.settings)
    window.store.enqueue(identifier, snapshot)
    job = window.store.get(identifier)["job"]
    for kind in ("resume", "letter"):
        package = providers.generate(kind, job, snapshot["base"], snapshot[kind + "_prompt"], snapshot, None)
        window.store.save_checkpoint(identifier, kind, package)
    window.store.set_status(identifier, "failed", "Fictional output-folder failure")
    original = window.store.get(identifier)
    window.refresh_jobs()
    window.jobs_table.selectRow(0)
    assert window.rerender_button.isEnabled()
    folder = window.data_dir / "relocated-output"
    folder.mkdir()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **kw: str(folder))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **kw: QMessageBox.StandardButton.Yes)
    window.rerender_selected()
    cloned = next(row for row in window.store.list_jobs() if row["id"] != identifier)
    assert cloned["status"] == "queued"
    assert cloned["checkpoints"] == original["checkpoints"]
    assert cloned["snapshot"]["output_folder"] == str(folder)
    assert not cloned["artifacts"]
    assert window.store.get(identifier) == original
    calls = []

    def forbidden_generation(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Render-only revision must never generate")

    monkeypatch.setattr(pipeline, "generate", forbidden_generation)
    window.worker.start()
    window.worker.wake()
    wait_for(app, lambda: window.store.get(cloned["id"])["status"] in ("completed", "failed"))
    result = window.store.get(cloned["id"])
    assert result["status"] == "completed", result["error"]
    assert not calls
    assert len(result["artifacts"]) == 2
    assert all(Path(path).is_relative_to(folder) and Path(path).is_file() for path in result["artifacts"])
    assert window.store.get(identifier) == original


def test_render_relocation_rejects_overlapping_inbox_without_queueing(window, monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    identifier = add_job(window)
    window.worker.stop()
    window.store.enqueue(identifier, window.library.snapshot(window.settings))
    for kind in ("resume", "letter"):
        window.store.save_checkpoint(identifier, kind, {"content": {}})
    window.store.set_status(identifier, "failed")
    window.refresh_jobs()
    window.jobs_table.selectRow(0)
    errors = []
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **kw: window.settings.inbox_folder)
    monkeypatch.setattr(window, "_error", lambda exc: errors.append(str(exc)))
    window.rerender_selected()
    assert errors and "overlap" in errors[0]
    assert len(window.store.list_jobs()) == 1


def test_first_processing_of_changed_import_uses_its_durable_revision(window):
    original_id = add_job(window)
    window.worker.stop()
    original = window.store.get(original_id)
    changed = dict(original["job"], description_text=original["job"]["description_text"] + "\nFictional changed advert.")
    assert window.store.import_job(changed, original["source_path"])
    revision = next(row for row in window.store.list_jobs() if row["id"] != original_id)
    assert revision["revision"] == 2
    window.refresh_jobs()
    window.jobs_table.selectRow(1)
    window.process_selected()
    assert window.store.get(revision["id"])["snapshot"]["revision"] == 2
    assert window.store.get(original_id)["status"] == "ready"
