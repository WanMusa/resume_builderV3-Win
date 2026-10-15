import json
from pathlib import Path

import pytest

from resume_builder.inbox import InboxScanner
from resume_builder.storage import Store


def job(id="source-1"):
    return {
        "id": id, "source_url": "https://example.com/jobs/1", "site": "Example",
        "captured_at": "2026-01-01T10:00:00Z", "description_text": "Build systems",
    }


@pytest.fixture
def inbox(tmp_path):
    folder = tmp_path / "inbox"
    folder.mkdir()
    store = Store(tmp_path / "jobs.sqlite")
    yield folder, store, InboxScanner(store)
    store.close()


def write(folder, jobs, **extra):
    path = folder / "jobs.json"
    path.write_text(json.dumps({"schema_version": 2, "jobs": jobs, **extra}), encoding="utf-8")
    return path


def test_two_stable_observations_and_changed_revisions(inbox):
    folder, store, scanner = inbox
    write(folder, [job()])
    assert scanner.scan(folder) == []
    assert store.list_jobs() == []
    assert scanner.scan(folder) == []
    assert len(store.list_jobs()) == 1
    write(folder, [{**job(), "description_text": "Changed job description"}])
    scanner.scan(folder)
    assert len(store.list_jobs()) == 1
    scanner.scan(folder)
    assert len(store.list_jobs()) == 2
    store.reset_history()
    scanner.scan(folder)
    assert store.list_jobs() == []


def test_only_completed_lowercase_json_regular_files(inbox):
    folder, store, scanner = inbox
    data = json.dumps({"schema_version": 2, "jobs": [job()]})
    (folder / "jobs.JSON").write_text(data)
    (folder / "jobs.json.crdownload").write_text(data)
    (folder / "directory.json").mkdir()
    (folder / "link.json").symlink_to(folder / "jobs.JSON")
    scanner.scan(folder)
    scanner.scan(folder)
    assert store.list_jobs() == []


@pytest.mark.parametrize("envelope", [
    {"schema_version": 1, "jobs": [job()]},
    {"schema_version": 2, "jobs": []},
    {"schema_version": 2, "jobs": [job()], "exported_at": "2026-01-01"},
    {"schema_version": 2, "jobs": [job()], "unknown": "private"},
    [job()],
])
def test_strict_envelope_and_invalid_deduplication(inbox, envelope):
    folder, store, scanner = inbox
    (folder / "bad.json").write_text(json.dumps(envelope))
    assert scanner.scan(folder) == []
    assert len(scanner.scan(folder)) == 1
    assert scanner.scan(folder) == []
    assert len(store.list_jobs()) == 1
    assert store.list_jobs()[0]["status"] == "invalid"
    assert store.list_jobs()[0]["job"] is None
    assert "private" not in store.list_jobs()[0]["error"]


def test_partial_valid_envelope_and_safe_validation_errors(inbox):
    folder, store, scanner = inbox
    invalid = {**job("bad"), "description_text": 99, "legacy_field": "PRIVATE SENTINEL"}
    write(folder, [job(), invalid, job("second")], exported_at="2026-01-01T00:00:00Z")
    scanner.scan(folder)
    errors = scanner.scan(folder)
    assert len(errors) == 1
    assert "description_text" in errors[0]
    assert "legacy_field" in errors[0]
    assert "PRIVATE SENTINEL" not in errors[0]
    assert sorted(row["status"] for row in store.list_jobs()) == ["invalid", "ready", "ready"]


def test_malformed_and_oversized_files_are_not_truncated(inbox):
    folder, store, scanner = inbox
    scanner.MAX_FILE_BYTES = 100
    (folder / "bad.json").write_bytes(b'{"private": invalid')
    (folder / "big.json").write_bytes(b" " * 101)
    scanner.scan(folder)
    errors = scanner.scan(folder)
    assert len(errors) == 2
    assert any("size limit" in error for error in errors)
    assert any("Malformed JSON" in error for error in errors)
    assert all(row["job"] is None for row in store.list_jobs())


def test_transient_read_failure_retried(inbox, monkeypatch):
    folder, store, scanner = inbox
    write(folder, [job()])
    scanner.scan(folder)
    from resume_builder import inbox as module
    original = module.os.open
    def fail(*args, **kwargs):
        raise PermissionError()
    monkeypatch.setattr(module.os, "open", fail)
    assert "retry" in scanner.scan(folder)[0]
    assert store.list_jobs() == []
    monkeypatch.setattr(module.os, "open", original)
    assert scanner.scan(folder) == []
    assert len(store.list_jobs()) == 1


def test_file_change_between_observations_restarts_stability(inbox):
    folder, store, scanner = inbox
    path = folder / "jobs.json"
    path.write_text('{"schema_version":2,')
    scanner.scan(folder)
    write(folder, [job()])
    assert scanner.scan(folder) == []
    assert store.list_jobs() == []
    scanner.scan(folder)
    assert len(store.list_jobs()) == 1


@pytest.mark.parametrize("duplicate", ["schema_version", "jobs", "job_id"])
def test_duplicate_json_keys_rejected_without_stopping_other_imports(inbox, duplicate):
    folder, store, scanner = inbox
    encoded_job = json.dumps(job("bad"))
    if duplicate == "schema_version":
        malformed = '{"schema_version":1,"schema_version":2,"jobs":[' + encoded_job + "]}"
    elif duplicate == "jobs":
        malformed = '{"schema_version":2,"jobs":[],"jobs":[' + encoded_job + "]}"
    else:
        encoded_job = encoded_job.replace('"id": "bad"', '"id":"first","id":"bad"')
        malformed = '{"schema_version":2,"jobs":[' + encoded_job + "]}"
    (folder / "a-bad.json").write_text(malformed)
    write(folder, [job("good")])
    scanner.scan(folder)
    errors = scanner.scan(folder)
    assert len(errors) == 1
    assert "Malformed JSON" in errors[0]
    rows = store.list_jobs()
    assert sorted(row["status"] for row in rows) == ["invalid", "ready"]
    assert next(row for row in rows if row["job"])["job"]["id"] == "good"


def test_pathological_json_nesting_does_not_halt_other_imports(inbox):
    folder, store, scanner = inbox
    malformed = '{"schema_version":2,"jobs":[' + "[" * 20000 + "0" + "]" * 20000 + "]}"
    (folder / "a-nested.json").write_text(malformed)
    write(folder, [job("good")])
    scanner.scan(folder)
    errors = scanner.scan(folder)
    assert len(errors) == 1
    assert "Malformed JSON" in errors[0]
    assert sorted(row["status"] for row in store.list_jobs()) == ["invalid", "ready"]
