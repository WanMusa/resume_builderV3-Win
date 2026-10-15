"""Stable-file inbox discovery. Importing never schedules generation."""

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import stat

from pydantic import ValidationError

from .models import Job


class InboxScanner:
    MAX_FILE_BYTES = 10 * 1024 * 1024

    def __init__(self, store):
        self.store = store
        self._observations = {}

    def scan(self, folder) -> list[str]:
        errors = []
        folder = Path(folder)
        if folder.is_symlink():
            return ["Inbox must not be a symbolic link"]
        try:
            entries = list(folder.iterdir())
        except OSError:
            return ["Inbox could not be read"]
        observations = {}
        for path in sorted(entries):
            if path.suffix != ".json":
                continue
            try:
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    continue
                signature = (info.st_size, info.st_mtime_ns)
                key = str(path.absolute())
                observations[key] = signature
                if self._observations.get(key) != signature:
                    continue
                if info.st_size > self.MAX_FILE_BYTES:
                    fingerprint = hashlib.sha256(str(signature).encode()).hexdigest()
                    self._invalid(path, fingerprint, "File exceeds the inbox size limit", errors)
                    continue
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(descriptor, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if not stat.S_ISREG(opened.st_mode) or (
                        opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns
                    ) != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
                        continue
                    data = stream.read(self.MAX_FILE_BYTES + 1)
                    after = os.fstat(stream.fileno())
                    if (after.st_size, after.st_mtime_ns) != signature:
                        continue
                if len(data) > self.MAX_FILE_BYTES:
                    continue
            except OSError:
                errors.append(f"{path.name}: File could not be read; will retry")
                continue
            fingerprint = hashlib.sha256(data).hexdigest()
            try:
                envelope = json.loads(
                    data, parse_constant=self._reject_constant, object_pairs_hook=self._unique_object
                )
            except (ValueError, UnicodeError, RecursionError):
                self._invalid(path, fingerprint, "Malformed JSON or text encoding", errors)
                continue
            error = self._envelope_error(envelope)
            if error:
                self._invalid(path, fingerprint, error, errors)
                continue
            for index, raw in enumerate(envelope["jobs"]):
                try:
                    job = Job.model_validate(raw)
                except ValidationError as exc:
                    fields = sorted({
                        ".".join(str(part) for part in issue["loc"]) or "job"
                        for issue in exc.errors(include_input=False, include_context=False)
                    })
                    # Validation paths identify fields, never include private field values.
                    message = f"Job {index + 1} has invalid or unsupported fields: {', '.join(fields)}"
                    self._invalid(path, f"{fingerprint}:{index}", message, errors)
                    continue
                self.store.import_job(job.model_dump(mode="json"), str(path))
        self._observations = observations
        return errors

    @staticmethod
    def _reject_constant(value):
        raise ValueError("Nonstandard JSON number")

    @staticmethod
    def _unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON object key")
            result[key] = value
        return result

    @staticmethod
    def _envelope_error(value):
        if not isinstance(value, dict):
            return "Expected a schema_version=2 job envelope"
        if set(value) - {"schema_version", "exported_at", "jobs"}:
            return "Envelope contains unsupported fields"
        if type(value.get("schema_version")) is not int or value["schema_version"] != 2:
            return "Expected schema_version=2"
        if not isinstance(value.get("jobs"), list) or not value["jobs"]:
            return "Envelope jobs must be a nonempty list"
        if "exported_at" in value:
            try:
                timestamp = datetime.fromisoformat(value["exported_at"].replace("Z", "+00:00"))
                if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                    raise ValueError()
            except (ValueError, AttributeError, TypeError):
                return "Envelope exported_at must be a timezone-aware timestamp"
        return None

    def _invalid(self, path, fingerprint, error, errors):
        if self.store.import_invalid(str(path), fingerprint, error):
            errors.append(f"{path.name}: {error}")
