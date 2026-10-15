import threading
import time

from resume_builder.queue import QueueWorker
from resume_builder.storage import Store


def add(store, number, queue=True):
    store.import_job({
        "id": str(number), "source_url": "https://example.com/job", "site": "Example",
        "captured_at": "2026-01-01T00:00:00Z", "description_text": "Build systems",
    }, "jobs.json")
    id = store.list_jobs()[-1]["id"]
    if queue:
        store.enqueue(id, {"mode": "offline"})
    return id


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline, "Worker did not finish"
        time.sleep(0.01)


def test_serial_explicit_queue_failure_continues(tmp_path):
    store = Store(tmp_path / "jobs.sqlite")
    ready = add(store, "ready", queue=False)
    first = add(store, "first")
    second = add(store, "second")
    calls = []
    notifications = []
    def processor(row, emit_status):
        calls.append(row["id"])
        assert row["snapshot"] == {"mode": "offline"}
        if row["id"] == first:
            raise RuntimeError("PRIVATE provider prompt and API key")
        emit_status("rendering")
        store.save_checkpoint(row["id"], "resume", {"complete": True})
    worker = QueueWorker(store, processor, lambda id, status: notifications.append((id, status)))
    try:
        worker.start()
        worker.start()
        wait_for(lambda: store.get(second)["status"] == "completed")
        assert calls == [first, second]
        assert store.get(ready)["status"] == "ready"
        assert store.get(first)["status"] == "failed"
        assert "PRIVATE" not in store.get(first)["error"]
        assert (second, "rendering") in notifications
        assert store.get(second)["checkpoints"] == {"resume": {"complete": True}}
    finally:
        assert worker.stop()
        store.close()


def test_busy_stop_and_restart_do_not_cancel_or_replay(tmp_path):
    store = Store(tmp_path / "jobs.sqlite")
    first = add(store, "first")
    second = add(store, "second")
    entered = threading.Event()
    release = threading.Event()
    calls = []
    def processor(row, emit_status):
        calls.append(row["id"])
        entered.set()
        assert release.wait(5)
    worker = QueueWorker(store, processor, lambda *args: None)
    try:
        worker.start()
        assert entered.wait(5)
        assert worker.is_busy
        assert not worker.stop(timeout=0.01)
        assert store.get(second)["status"] == "queued"
        release.set()
        assert worker.stop()
        assert store.get(first)["status"] == "completed"
        assert store.get(second)["status"] == "queued"
        assert not worker.is_busy
        worker.start()
        wait_for(lambda: store.get(second)["status"] == "completed")
        assert calls == [first, second]
    finally:
        release.set()
        worker.stop()
        store.close()


def test_recovered_jobs_not_replayed_and_notify_errors_are_isolated(tmp_path):
    store = Store(tmp_path / "jobs.sqlite")
    id = add(store, "interrupted")
    assert store.recover() == 1
    called = threading.Event()
    def broken_notify(*args):
        raise RuntimeError("UI closed")
    worker = QueueWorker(store, lambda row, emit: called.set(), broken_notify)
    try:
        worker.start()
        assert not called.wait(0.1)
        assert store.get(id)["status"] == "interrupted"
        assert store.enqueue(id, {})
        worker.wake()
        assert called.wait(5)
        wait_for(lambda: store.get(id)["status"] == "completed")
    finally:
        worker.stop()
        store.close()


def test_processor_durable_sanitized_error_is_not_overwritten(tmp_path):
    store = Store(tmp_path / "jobs.sqlite")
    id = add(store, "failed")
    done = threading.Event()
    def processor(row, emit_status):
        store.set_status(row["id"], "failed", "Check the configured model and retry")
        raise ValueError("private raw provider response")
    worker = QueueWorker(store, processor, lambda id, status: done.set() if status == "failed" else None)
    try:
        worker.start()
        assert done.wait(5)
        assert store.get(id)["error"] == "Check the configured model and retry"
    finally:
        worker.stop()
        store.close()
