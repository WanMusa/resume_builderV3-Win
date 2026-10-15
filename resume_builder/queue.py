"""One serial daemon worker; only explicitly queued jobs are processed."""

import threading


class QueueWorker:
    def __init__(self, store, processor, notify):
        self.store = store
        self.processor = processor
        self.notify = notify
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._busy = threading.Event()
        self._thread = None
        self._lifecycle = threading.Lock()

    @property
    def is_busy(self):
        return self._busy.is_set()

    def start(self):
        with self._lifecycle:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._wake.set()
            self._thread = threading.Thread(target=self._run, name="resume-queue", daemon=True)
            self._thread.start()

    def wake(self):
        self._wake.set()

    def stop(self, timeout=5):
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        return thread is None or not thread.is_alive()

    def _notify(self, id, status):
        try:
            self.notify(id, status)
        except Exception:
            # A disconnected UI must not affect durable queue processing.
            pass

    def _run(self):
        try:
            while not self._stop.is_set():
                self._wake.wait(0.5)
                self._wake.clear()
                while not self._stop.is_set():
                    row = self.store.claim_next()
                    if row is None:
                        break
                    self._busy.set()
                    id = row["id"]
                    self._notify(id, "generating")

                    def emit_status(status, error="", job_id=id):
                        self.store.set_status(job_id, status, error)
                        self._notify(job_id, status)

                    try:
                        self.processor(row, emit_status)
                        if self.store.get(id)["status"] in ("queued", "generating", "rendering"):
                            emit_status("completed")
                    except Exception:
                        # Provider exceptions can contain credentials or private prompts.
                        failure = self.store.get(id)
                        if failure["status"] == "failed" and failure["error"]:
                            self._notify(id, "failed")
                        else:
                            emit_status("failed", "Processing failed; review settings and retry manually")
                    finally:
                        self._busy.clear()
        finally:
            self.store.close()
