from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3
import uuid

import pytest

from resume_builder.storage import Store


def job(id="source-1", description="Build reliable systems"):
    return {
        "id": id, "source_url": "https://example.com/jobs/1", "site": "Example",
        "captured_at": "2026-01-01T10:00:00+00:00", "description_text": description,
    }


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "jobs.sqlite")
    yield result
    result.close()


def test_import_revision_deduplication_and_decoding(store):
    assert store.import_job(job(), "/inbox/jobs.json")
    assert not store.import_job(dict(reversed(list(job().items()))), "/other/jobs.json")
    assert store.import_job(job(description="Changed description"), "/inbox/jobs.json")
    rows = store.list_jobs()
    assert [row["revision"] for row in rows] == [1, 2]
    assert rows[0]["job"]["id"] == "source-1"
    assert rows[0]["filename"] == "jobs.json"
    assert rows[0]["source_path"] == "/inbox/jobs.json"
    assert rows[0]["snapshot"] is None
    assert rows[0]["checkpoints"] == {}
    assert rows[0]["artifacts"] == []
    assert str(uuid.UUID(rows[0]["id"])) == rows[0]["id"]
    assert datetime.fromisoformat(rows[0]["created_at"]).utcoffset() == timedelta(0)


def test_enqueue_retry_and_recovery_preserve_checkpoints(store):
    store.import_job(job(), "jobs.json")
    id = store.list_jobs()[0]["id"]
    store.save_checkpoint(id, "resume", {"summary": "Validated"})
    snapshot = {"provider": "local", "config": {"timeout": 10}}
    assert store.enqueue(id, snapshot)
    assert not store.enqueue(id, {})
    row = store.claim_next()
    assert row["status"] == "generating"
    assert store.recover() == 1
    assert store.get(id)["status"] == "interrupted"
    assert store.claim_next() is None
    assert store.enqueue(id, {"mode": "changed"})
    assert store.get(id)["snapshot"] == snapshot
    store.set_status(id, "failed")
    assert store.enqueue(id, snapshot)
    assert store.get(id)["checkpoints"]["resume"] == {"summary": "Validated"}
    store.set_status(id, "completed")
    assert not store.enqueue(id, {})


def test_retry_without_checkpoints_also_preserves_frozen_snapshot(store):
    store.import_job(job(), "jobs.json")
    id = store.list_jobs()[0]["id"]
    snapshot = {"model": "original", "resume_prompt": "Original prompt"}
    assert store.enqueue(id, snapshot)
    store.set_status(id, "failed")
    assert store.enqueue(id, {"model": "changed", "resume_prompt": "Changed prompt"})
    assert store.get(id)["snapshot"] == snapshot


def test_regenerate_creates_separate_queued_revision_and_preserves_history(store):
    store.import_job(job(), "jobs.json")
    original_id = store.list_jobs()[0]["id"]
    store.enqueue(original_id, {"model": "old"})
    store.save_checkpoint(original_id, "resume", {"content": "old content"})
    store.save_artifacts(original_id, ["existing-resume.docx"])
    store.set_status(original_id, "completed")
    original = store.get(original_id)
    snapshot = {"model": "new", "resume_prompt": "New prompt", "revision": 1}
    new_id = store.regenerate(original_id, snapshot)
    assert new_id != original_id
    regenerated = store.get(new_id)
    assert regenerated["status"] == "queued"
    assert regenerated["revision"] == 2
    assert regenerated["snapshot"]["model"] == "new"
    assert regenerated["snapshot"]["revision"] == regenerated["revision"] == 2
    assert snapshot["revision"] == 1
    assert regenerated["checkpoints"] == {}
    assert regenerated["artifacts"] == []
    assert regenerated["job"] == original["job"]
    assert regenerated["canonical_hash"] == original["canonical_hash"]
    assert store.get(original_id) == original
    assert not store.import_job(job(), "elsewhere.json")


def test_regenerated_revision_survives_ledger_only_history(store):
    store.import_job(job(), "jobs.json")
    original_id = store.list_jobs()[0]["id"]
    store.set_status(original_id, "completed")
    new_id = store.regenerate(original_id, {})
    store.set_status(new_id, "completed")
    assert store.reset_history() == 2
    assert not store.import_job(job(), "jobs.json")
    assert store.import_job(job(description="Changed job"), "jobs.json")
    assert store.list_jobs()[0]["revision"] == 3


def test_regenerate_rejects_active_invalid_missing_and_credentials(store):
    store.import_job(job(), "jobs.json")
    id = store.list_jobs()[0]["id"]
    with pytest.raises(ValueError, match="credential"):
        store.regenerate(id, {"config": {"api_key": "private"}})
    store.enqueue(id, {})
    with pytest.raises(ValueError, match="inactive"):
        store.regenerate(id, {})
    store.import_invalid("invalid.json", "bad", "Invalid")
    invalid = next(row for row in store.list_jobs() if row["status"] == "invalid")
    with pytest.raises(ValueError, match="valid"):
        store.regenerate(invalid["id"], {})
    with pytest.raises(KeyError):
        store.regenerate("missing", {})
    assert len(store.list_jobs()) == 2


def test_clear_ledger_preserves_active_regenerated_identity(store):
    store.import_job(job(), "jobs.json")
    original_id = store.list_jobs()[0]["id"]
    store.set_status(original_id, "completed")
    new_id = store.regenerate(original_id, {})
    assert store.reset_history(clear_ledger=True) == 1
    assert store.get(new_id)["status"] == "queued"
    assert not store.import_job(job(), "jobs.json")
    store.recover()
    newer_id = store.regenerate(new_id, {})
    assert store.get(newer_id)["revision"] == 3


def test_rerender_clones_both_checkpoints_and_only_changes_output_folder(store):
    store.import_job(job(), "jobs.json")
    original_id = store.list_jobs()[0]["id"]
    snapshot = {"output_folder": "unwritable", "resume_prompt": "Original prompt", "model": "original"}
    store.enqueue(original_id, snapshot)
    checkpoints = {
        "resume": {"content": {"summary": "Saved resume"}, "usage": {"tokens": 8}, "model": "original"},
        "letter": {"content": {"paragraphs": ["Saved letter"]}, "usage": {"tokens": 3}, "model": "original"},
    }
    for kind, value in checkpoints.items():
        store.save_checkpoint(original_id, kind, value)
    store.save_artifacts(original_id, ["old-resume.docx"])
    store.set_status(original_id, "failed", "Output folder is not writable")
    original = store.get(original_id)
    new_id = store.rerender(original_id, "new-output")
    clone = store.get(new_id)
    assert new_id != original_id
    assert clone["status"] == "queued"
    assert clone["revision"] == 2
    assert clone["snapshot"] == {**snapshot, "output_folder": "new-output", "revision": 2}
    assert clone["checkpoints"] == checkpoints
    assert clone["artifacts"] == []
    assert clone["error"] == ""
    assert clone["job"] == original["job"]
    assert store.get(original_id) == original
    assert not store.import_job(job(), "jobs.json")
    store.save_checkpoint(new_id, "resume", {"content": {"summary": "Different"}})
    assert store.get(original_id)["checkpoints"] == checkpoints


def test_rerender_rejects_active_or_missing_saved_outputs(store):
    store.import_job(job(), "jobs.json")
    id = store.list_jobs()[0]["id"]
    for folder in ("", " ", None):
        with pytest.raises(ValueError, match="output folder"):
            store.rerender(id, folder)
    with pytest.raises(KeyError):
        store.rerender("missing", "output")
    with pytest.raises(ValueError, match="both resume and letter"):
        store.rerender(id, "output")
    store.enqueue(id, {"output_folder": "old"})
    store.save_checkpoint(id, "resume", {"content": {}})
    store.save_checkpoint(id, "letter", {"content": {}})
    with pytest.raises(ValueError, match="inactive"):
        store.rerender(id, "output")
    store.set_status(id, "interrupted")
    clone_id = store.rerender(id, "output")
    assert store.get(clone_id)["checkpoints"] == store.get(id)["checkpoints"]
    store.set_status(id, "failed")
    with store._transaction() as conn:
        conn.execute("UPDATE jobs SET checkpoints_json='{}' WHERE id=?", (id,))
    with pytest.raises(ValueError, match="both resume and letter"):
        store.rerender(id, "output")


def test_recovery_includes_waiting_and_both_inflight_states(store):
    states = ["ready", "queued", "generating", "rendering", "completed", "failed"]
    for status in states:
        store.import_job(job(status), "jobs.json")
        store.set_status(store.list_jobs()[-1]["id"], status)
    assert store.recover() == 3
    actual = {row["source_id"]: row["status"] for row in store.list_jobs()}
    assert actual == {status: ("interrupted" if status in {"queued", "generating", "rendering"}
                               else status) for status in states}


@pytest.mark.parametrize("key", [
    "api_key", "apiKey", "API-KEY", "password", "client_secret",
    "access_token", "credentials", "authorization", "token",
])
def test_snapshot_credentials_rejected_recursively(store, key):
    store.import_job(job(), "jobs.json")
    id = store.list_jobs()[0]["id"]
    with pytest.raises(ValueError, match="credential"):
        store.enqueue(id, {"config": [{"nested": {key: "must-not-be-persisted"}}]})
    assert store.get(id)["snapshot"] is None
    assert store.get(id)["status"] == "ready"


def test_cleanup_preview_ledger_and_artifacts(store, tmp_path):
    artifact = tmp_path / "resume.docx"
    artifact.write_bytes(b"artifact")
    store.import_job(job(), "jobs.json")
    row = store.list_jobs()[0]
    store.save_artifacts(row["id"], [str(artifact)])
    store.set_status(row["id"], "completed")
    cutoff = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert store.cleanup(cutoff, ["completed"]) == 1
    assert len(store.list_jobs()) == 1
    assert store.cleanup(cutoff, ["completed"], preview=False) == 1
    assert artifact.exists()
    assert not store.import_job(job(), "jobs.json")
    store.reset_history(clear_ledger=True)
    assert store.import_job(job(), "jobs.json")


def test_cleanup_and_reset_protect_active_and_allow_explicit_ledger_clear(store):
    for index, status in enumerate(("queued", "generating", "rendering", "failed")):
        store.import_job(job(str(index)), "jobs.json")
        id = store.list_jobs()[-1]["id"]
        store.set_status(id, status)
    cutoff = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert store.cleanup(cutoff, ["queued", "generating", "rendering", "failed"], preview=False,
                         clear_ledger=True) == 1
    assert {row["status"] for row in store.list_jobs()} == {"queued", "generating", "rendering"}
    assert store.import_job(job("3"), "jobs.json")
    assert store.reset_history(clear_ledger=True) == 1
    assert not store.import_job(job("0"), "jobs.json")
    assert store.import_job(job("3"), "jobs.json")


@pytest.mark.parametrize("statuses", [[], ["completed", "failed", "invalid"], ["queued"]])
def test_cleanup_includes_archived_ready_but_never_archived_active(store, statuses):
    for status in ("ready", "queued", "generating", "rendering"):
        store.import_job(job(status), "jobs.json")
        id = store.list_jobs()[-1]["id"]
        store.set_status(id, status)
        store.archive([id])
    store.import_job(job("unarchived"), "jobs.json")
    cutoff = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert store.cleanup(cutoff, statuses) == 1
    assert len(store.list_jobs(include_archived=True)) == 5
    assert store.cleanup(cutoff, statuses, preview=False) == 1
    rows = store.list_jobs(include_archived=True)
    assert {row["source_id"] for row in rows} == {"queued", "generating", "rendering", "unarchived"}
    assert not store.import_job(job("ready"), "jobs.json")


def test_archive_invalid_and_missing(store):
    assert store.import_invalid("bad.json", "fingerprint", "Malformed JSON")
    assert not store.import_invalid("bad.json", "fingerprint", "Malformed JSON")
    row = store.list_jobs()[0]
    assert row["job"] is None
    assert not store.enqueue(row["id"], {})
    store.archive([row["id"]])
    assert store.list_jobs() == []
    assert store.list_jobs(include_archived=True)[0]["archived"]
    store.archive([row["id"]], False)
    assert len(store.list_jobs()) == 1
    with pytest.raises(KeyError):
        store.get("missing")
    with pytest.raises(ValueError):
        store.set_status(row["id"], "bogus")


def test_thread_local_writes_and_atomic_claims(store):
    def write(index):
        try:
            store.import_job(job(str(index)), "jobs.json")
            store.import_job(job(str(index)), "jobs.json")
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(write, range(20)))
    assert len(store.list_jobs()) == 20
    for row in store.list_jobs():
        store.enqueue(row["id"], {})

    def claim(_):
        try:
            return store.claim_next()["id"]
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=6) as pool:
        claimed = list(pool.map(claim, range(20)))
    assert len(set(claimed)) == 20
    assert store.claim_next() is None
    assert store._connection().execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store._connection().execute("PRAGMA busy_timeout").fetchone()[0] == 10000


def test_online_backup_and_unknown_legacy_preserved(store, tmp_path):
    store.import_job(job(), "jobs.json")
    backup = tmp_path / "backup.sqlite"
    store.backup(backup)
    restored = Store(backup)
    assert restored.list_jobs()[0]["job"]["id"] == "source-1"
    restored.close()
    legacy = tmp_path / "legacy.sqlite"
    with sqlite3.connect(legacy) as conn:
        conn.execute("CREATE TABLE old_data (value TEXT)")
        conn.execute("INSERT INTO old_data VALUES ('keep')")
    with pytest.raises(RuntimeError, match="Unsupported legacy"):
        Store(legacy)
    with sqlite3.connect(str(legacy) + ".pre-migration-v0.bak") as conn:
        assert conn.execute("SELECT value FROM old_data").fetchone()[0] == "keep"
    with sqlite3.connect(legacy) as conn:
        assert conn.execute("SELECT value FROM old_data").fetchone()[0] == "keep"


def test_newer_or_corrupt_database_not_recreated(tmp_path):
    newer = tmp_path / "newer.sqlite"
    with sqlite3.connect(newer) as conn:
        conn.execute("PRAGMA user_version=999")
    with pytest.raises(RuntimeError, match="newer"):
        Store(newer)
    corrupt = tmp_path / "corrupt.sqlite"
    corrupt.write_bytes(b"not a SQLite database")
    with pytest.raises(sqlite3.DatabaseError):
        Store(corrupt)
    assert corrupt.read_bytes() == b"not a SQLite database"


def test_backup_failure_prevents_migration(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE original (value TEXT)")
    def fail(*args):
        raise OSError("backup unavailable")
    monkeypatch.setattr(Store, "backup", fail)
    with pytest.raises(OSError, match="backup unavailable"):
        Store(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("original",)]


def test_existing_empty_version_zero_database_backed_up_before_upgrade(tmp_path):
    path = tmp_path / "empty.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("VACUUM")
    store = Store(path)
    try:
        assert store._connection().execute("PRAGMA user_version").fetchone()[0] == 1
        with sqlite3.connect(str(path) + ".pre-migration-v0.bak") as backup:
            assert backup.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        store.close()
