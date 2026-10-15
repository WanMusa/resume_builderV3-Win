import copy

import httpx
import pytest

from resume_builder import pipeline
from resume_builder.providers import ProviderError
from test_providers import base, envelope, install_transport, mock_result
from test_rendering import output_folder


class Store:
    def __init__(self, base):
        self.row = {
            "id": "job", "job": {"description_text": "Job"},
            "snapshot": {"base": base, "provider": "mock", "model": "mock",
                         "resume_prompt": "", "letter_prompt": "",
                         "output_folder": "unused", "revision": 1},
            "checkpoints": {}, "artifacts": [],
        }
        self.events = []

    def get(self, id):
        return copy.deepcopy(self.row)

    def set_status(self, id, status, error=""):
        self.events.append(("status", status, error))
        self.row["status"] = status

    def save_checkpoint(self, id, kind, value):
        self.events.append(("checkpoint", kind))
        self.row["checkpoints"][kind] = copy.deepcopy(value)

    def save_artifacts(self, id, paths):
        self.events.append(("artifacts",))
        self.row["artifacts"] = paths


def test_letter_retry_reuses_resume_and_checkpoint_precedes_render(base, monkeypatch):
    store = Store(base)
    calls, statuses, rendered = [], [], []

    def generate(kind, *args):
        calls.append(kind)
        if kind == "letter" and calls.count(kind) == 1:
            raise ProviderError("Provider rate limit reached; wait.")
        return mock_result(base, kind)

    def render(*args):
        assert set(store.row["checkpoints"]) == {"resume", "letter"}
        rendered.append(args)
        return ["resume.docx", "letter.docx"]

    monkeypatch.setattr(pipeline, "generate", generate)
    monkeypatch.setattr(pipeline, "render_documents", render)
    processor = pipeline.Processor(store, lambda endpoint: pytest.fail("Mock requested key"))
    assert processor(store.row, statuses.append) is None
    assert set(store.row["checkpoints"]) == {"resume"}
    assert processor(store.row, statuses.append) == ["resume.docx", "letter.docx"]
    assert calls == ["resume", "letter", "letter"]
    assert store.row["checkpoints"]["resume"]["model"] == "mock"
    assert statuses == ["generating", "failed", "generating", "rendering", "completed"]
    assert len(rendered) == 1


def test_render_retry_uses_both_checkpoints_without_key_or_network(base, monkeypatch):
    store = Store(base)
    store.row["checkpoints"] = {kind: mock_result(base, kind) for kind in ("resume", "letter")}
    attempts = []

    def render(*args):
        attempts.append(args)
        if len(attempts) == 1:
            raise pipeline.RenderError("Close Word and retry.")
        return ["resume.docx", "letter.docx"]

    monkeypatch.setattr(pipeline, "generate", lambda *args: pytest.fail("Regenerated checkpoint"))
    monkeypatch.setattr(pipeline, "render_documents", render)
    processor = pipeline.Processor(store, lambda endpoint: pytest.fail("Requested key"))
    assert processor(store.row, lambda s: None) is None
    assert len(store.row["checkpoints"]) == 2
    assert processor(store.row, lambda s: None)
    assert attempts[0] == attempts[1]


def test_ephemeral_key_fetched_only_at_execution_not_persisted(base, monkeypatch):
    store = Store(base)
    store.row["snapshot"]["provider"] = "openai"
    keys = []
    seen = []

    def getter(endpoint):
        keys.append(endpoint)
        return "ephemeral-secret"

    def generate(kind, job, base, prompt, config, key):
        seen.append(key)
        return mock_result(base, kind)

    monkeypatch.setattr(pipeline, "generate", generate)
    monkeypatch.setattr(pipeline, "render_documents", lambda *a: ["resume", "letter"])
    processor = pipeline.Processor(store, getter)
    assert not keys
    processor(store.row, lambda s: None)
    assert keys == ["https://api.openai.com/v1"] and seen == ["ephemeral-secret", "ephemeral-secret"]
    assert "ephemeral-secret" not in repr(store.row)
    processor(store.row, lambda s: None)
    assert len(keys) == 1


def test_unexpected_errors_not_saved_verbatim(base, monkeypatch):
    store = Store(base)
    monkeypatch.setattr(pipeline, "generate", lambda *args: (_ for _ in ()).throw(RuntimeError("private secret")))
    pipeline.Processor(store, lambda endpoint: None)(store.row, lambda s: None)
    assert store.row["status"] == "failed"
    assert "private secret" not in repr(store.events)


def test_queue_callback_does_not_clear_actionable_failure(base, monkeypatch):
    store = Store(base)
    monkeypatch.setattr(pipeline, "generate", lambda *args: (_ for _ in ()).throw(ProviderError("Wait and retry manually.")))
    pipeline.Processor(store, lambda endpoint: None)(
        store.row, lambda status: store.set_status("job", status),
    )
    assert store.events[-1] == ("status", "failed", "Wait and retry manually.")


def test_end_to_end_mock_pipeline(base, output_folder):
    store = Store(base)
    store.row["snapshot"]["output_folder"] = str(output_folder)
    result = pipeline.Processor(store, lambda endpoint: pytest.fail("Mock requested key"))(
        store.row, lambda status: None,
    )
    assert len(result) == 2
    assert store.row["artifacts"] == result
    assert store.row["status"] == "completed"
    assert set(store.row["checkpoints"]) == {"resume", "letter"}


def test_vault_failure_is_actionable_without_disclosing_exception(base, monkeypatch):
    store = Store(base)
    store.row["snapshot"]["provider"] = "openai"
    monkeypatch.setattr(pipeline, "generate", lambda *args: pytest.fail("Generation should not run"))

    def getter(endpoint=None):
        raise RuntimeError("private key and credential backend details")

    assert pipeline.Processor(store, getter)(store.row, lambda status: None) is None
    assert store.row["status"] == "failed"
    message = store.events[-1][2]
    assert message == "Secure credential access failed; unlock Credential Manager or enter a session-only key in Settings."
    assert "private key" not in repr(store.events)
    assert not store.row["checkpoints"]


def test_provider_key_echo_never_reaches_durable_checkpoint(base, monkeypatch):
    store = Store(base)
    store.row["snapshot"].update(provider="compatible", model="test-model",
                                 base_url="https://example.test/v1", custom_endpoint_acknowledged=True)
    content = mock_result(base)["content"]
    content["summary"]["text"] += " Authorization: test-key"
    install_transport(monkeypatch, lambda request: httpx.Response(200, json=envelope(content)))
    assert pipeline.Processor(store, lambda endpoint: "test-key")(store.row, lambda status: None) is None
    assert store.row["status"] == "failed"
    assert not store.row["checkpoints"]
    assert not store.row["artifacts"]
    assert "test-key" not in repr(store.row)
    assert "test-key" not in repr(store.events)


def test_durable_row_revision_precedes_snapshot_revision_for_render_only(base, monkeypatch):
    store = Store(base)
    store.row["revision"] = 4
    store.row["snapshot"]["revision"] = 1
    store.row["snapshot"]["output_folder"] = "restored-output-folder"
    store.row["checkpoints"] = {kind: mock_result(base, kind) for kind in ("resume", "letter")}
    rendered = []
    monkeypatch.setattr(pipeline, "generate", lambda *args: pytest.fail("Render-only retry generated content"))

    def render(*args):
        rendered.append(args)
        return ["resume.docx", "letter.docx"]

    monkeypatch.setattr(pipeline, "render_documents", render)
    assert pipeline.Processor(store, lambda endpoint: pytest.fail("Render-only requested credentials"))(
        store.row, lambda status: None,
    )
    assert rendered[0][1] == 4
    assert rendered[0][-2] == "restored-output-folder"


def test_real_store_rerender_clone_publishes_without_provider_or_credentials(base, output_folder, monkeypatch):
    from resume_builder.storage import Store as DurableStore

    store = DurableStore(":memory:")
    try:
        store.import_job({
            "id": "fictional-job", "site": "Fictional Site",
            "source_url": "https://example.test/jobs/fictional",
            "captured_at": "2026-10-15T08:00:00+00:00",
            "description_text": "Fictional testing opportunity.",
            "title": "Test Engineer", "company": "Fictional Company",
        }, "fictional-job.json")
        original_id = store.list_jobs()[0]["id"]
        snapshot = {
            "base": base, "provider": "openai", "model": "test-model",
            "resume_prompt": "", "letter_prompt": "", "revision": 1,
            "output_folder": "old-output-folder",
        }
        store.enqueue(original_id, snapshot)
        for kind in ("resume", "letter"):
            store.save_checkpoint(original_id, kind, mock_result(base, kind))
        store.save_artifacts(original_id, ["user-edited-resume.docx"])
        store.set_status(original_id, "completed")
        original = store.get(original_id)
        clone_id = store.rerender(original_id, str(output_folder))
        clone = store.claim_next()
        assert clone["id"] == clone_id
        monkeypatch.setattr(pipeline, "generate", lambda *args: pytest.fail("Rerender called provider"))
        results = pipeline.Processor(store, lambda endpoint: pytest.fail("Rerender requested credentials"))(
            clone, lambda status: None,
        )
        assert len(results) == 2
        assert store.get(clone_id)["status"] == "completed"
        assert store.get(clone_id)["checkpoints"] == original["checkpoints"]
        assert store.get(original_id) == original
        assert all("-r2" in path for path in results)
    finally:
        store.close()


def test_queued_snapshot_uses_its_endpoint_key_after_settings_change(base, monkeypatch):
    store = Store(base)
    old_endpoint = "https://old.example.test/v1"
    current_endpoint = "https://new.example.test/v1"
    store.row["snapshot"].update(provider="compatible", model="test-model", base_url=old_endpoint,
                                 custom_endpoint_acknowledged=True)
    current_settings = {"base_url": current_endpoint}
    endpoint_keys = {old_endpoint: "old-endpoint-key", current_endpoint: "new-endpoint-key"}
    fetched, requested = [], []

    def getter(endpoint):
        fetched.append(endpoint)
        return endpoint_keys[endpoint]

    def generate(kind, job, source, prompt, config, key):
        requested.append((config["base_url"], key))
        return mock_result(base, kind)

    monkeypatch.setattr(pipeline, "generate", generate)
    monkeypatch.setattr(pipeline, "render_documents", lambda *args: ["resume.docx", "letter.docx"])
    assert pipeline.Processor(store, getter)(store.row, lambda status: None)
    assert current_settings["base_url"] == current_endpoint
    assert fetched == [old_endpoint]
    assert requested == [(old_endpoint, "old-endpoint-key"), (old_endpoint, "old-endpoint-key")]
    assert "old-endpoint-key" not in repr(store.row)
    assert "new-endpoint-key" not in repr(store.row)
