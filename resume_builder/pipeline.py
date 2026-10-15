"""Serial processing with independent, durable generation checkpoints."""

from .providers import ProviderError, generate
from .rendering import RenderError, render_documents


class Processor:
    def __init__(self, store, key_getter):
        self.store = store
        self.key_getter = key_getter

    def __call__(self, row, emit_status):
        job_id = row["id"]
        try:
            current = self.store.get(job_id)
            snapshot = current["snapshot"]
            checkpoints = dict(current.get("checkpoints") or {})
            if current.get("artifacts"):
                self.store.set_status(job_id, "completed")
                emit_status("completed")
                return current["artifacts"]
            config = {k: v for k, v in snapshot.items()
                      if k not in ("base", "resume_prompt", "letter_prompt", "output_folder")}
            key = None
            if any(kind not in checkpoints for kind in ("resume", "letter")):
                self.store.set_status(job_id, "generating")
                emit_status("generating")
                if config.get("provider", "mock") != "mock":
                    try:
                        key = self.key_getter()
                    except Exception:
                        raise ProviderError(
                            "Secure credential access failed; unlock Credential Manager or enter a session-only key in Settings."
                        ) from None
                for kind in ("resume", "letter"):
                    if kind not in checkpoints:
                        result = generate(kind, current["job"], snapshot["base"],
                                          snapshot[kind + "_prompt"], config, key)
                        self.store.save_checkpoint(job_id, kind, result)
                        checkpoints[kind] = result
            key = None
            self.store.set_status(job_id, "rendering")
            emit_status("rendering")
            paths = render_documents(
                job_id, current.get("revision", snapshot.get("revision", 1)), current["job"], snapshot["base"],
                checkpoints["resume"]["content"], checkpoints["letter"]["content"],
                snapshot["output_folder"], config.get("provider", "mock") == "mock",
            )
            self.store.save_artifacts(job_id, paths)
            self.store.set_status(job_id, "completed")
            emit_status("completed")
            return paths
        except Exception as exc:
            # Unexpected exceptions may contain keys, SQL or personal data.
            error = str(exc) if isinstance(exc, (ProviderError, RenderError)) else (
                "Processing failed safely. Check the saved inputs and output folder, then retry."
            )
            emit_status("failed")
            # Queue callbacks may persist the emitted status with a blank error.
            self.store.set_status(job_id, "failed", error)
            return None
