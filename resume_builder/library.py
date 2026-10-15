"""Versioned, human-editable local prompt and verified evidence libraries."""

import hashlib
import json
import re
import string
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from .config import Settings, atomic_write, validate_folders
from .models import BaseResume


def digest(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_prompt(content: str) -> None:
    if not content.strip():
        raise ValueError("Prompt cannot be blank")
    try:
        for _, name, spec, conversion in string.Formatter().parse(content):
            if name is not None and (name not in {"job_description", "evidence"} or spec or conversion):
                raise ValueError("Only {job_description} and {evidence} placeholders are supported; escape literal braces as {{ and }}.")
    except ValueError:
        raise ValueError("Invalid prompt placeholders. Use only {job_description}, {evidence}; escape literal braces as {{ and }}.") from None
    for token in ("{job_description}", "{evidence}"):
        if content.count(token) > 1:
            raise ValueError("Use each context placeholder at most once to avoid duplicate input tokens")


class Library:
    def __init__(self, data_dir: Path):
        self.root = Path(data_dir)
        for folder in ("prompts", "bases", "history"):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        for resource, target in (
            ("resume-default.txt", self.root / "prompts/resume-default.txt"),
            ("letter-default.txt", self.root / "prompts/letter-default.txt"),
            ("fictional-example.json", self.root / "bases/fictional-example.json"),
        ):
            if not target.exists():
                atomic_write(target, files("resume_builder").joinpath("assets", resource).read_text(encoding="utf-8"))

    def _path(self, category: str, identifier: str, suffix: str) -> Path:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,100}", identifier):
            raise ValueError("Invalid library identifier")
        path = self.root / category / (identifier + suffix)
        if path.is_symlink():
            raise ValueError("Library files cannot be symbolic links")
        return path

    def _save(self, path: Path, content: str) -> None:
        if path.exists():
            version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + "-" + uuid4().hex[:8]
            backup = self.root / "history" / f"{path.stem}-{version}{path.suffix}"
            atomic_write(backup, path.read_text(encoding="utf-8"))
        atomic_write(path, content)

    def prompts(self) -> list[dict]:
        result = []
        for path in sorted((self.root / "prompts").glob("*.txt")):
            if path.is_symlink():
                continue
            kind = path.stem.split("-", 1)[0]
            if kind in ("resume", "letter"):
                metadata_path = self._path("prompts", path.stem, ".meta.json")
                name = path.stem
                if metadata_path.exists():
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str):
                        raise ValueError(f"Invalid prompt metadata for '{path.stem}'")
                    name = metadata["name"]
                result.append({"id": path.stem, "name": name, "kind": kind, "content": path.read_text(encoding="utf-8")})
        return result

    def load_prompt(self, identifier: str) -> str:
        content = self._path("prompts", identifier, ".txt").read_text(encoding="utf-8")
        validate_prompt(content)
        return content

    def save_prompt(self, identifier: str | None, name: str, kind: str, content: str) -> str:
        validate_prompt(content)
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ValueError("Enter a prompt name of 1–200 characters")
        if kind not in ("resume", "letter"):
            raise ValueError("Choose resume or letter prompt type")
        if identifier is None:
            slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip()).strip("-")[:50]
            if not slug:
                raise ValueError("Enter a prompt name")
            identifier = f"{kind}-{slug}-{uuid4().hex[:8]}"
        if not identifier.startswith(kind + "-"):
            raise ValueError("Cannot change the type of an existing prompt; use Save as new")
        self._save(self._path("prompts", identifier, ".txt"), content)
        self._save(self._path("prompts", identifier, ".meta.json"), json.dumps({"name": name.strip()}, ensure_ascii=False))
        return identifier

    def bases(self) -> list[dict]:
        return [
            {"id": path.stem, "content": self.load_base(path.stem)}
            for path in sorted((self.root / "bases").glob("*.json")) if not path.is_symlink()
        ]

    def load_base(self, identifier: str) -> dict:
        path = self._path("bases", identifier, ".json")
        try:
            return BaseResume.model_validate_json(path.read_text(encoding="utf-8")).model_dump()
        except ValueError:
            raise ValueError(f"Base resume '{identifier}' does not match the supported schema. No fields were imported.") from None

    def save_base(self, identifier: str | None, content: dict) -> str:
        base = BaseResume.model_validate(content)
        if not base.is_example and base.verified and not any(role.achievements for role in base.roles):
            raise ValueError("Verified resumes need at least one evidence achievement")
        if identifier is None:
            identifier = "base-" + uuid4().hex
        self._save(self._path("bases", identifier, ".json"), base.model_dump_json(indent=2))
        return identifier

    def snapshot(self, settings: Settings) -> dict:
        validate_folders(settings.inbox_folder, settings.output_folder)
        base = self.load_base(settings.base_resume)
        if settings.provider != "mock" and (base["is_example"] or not base["verified"]):
            raise ValueError("Live generation requires your own evidence: clear Example and explicitly confirm verified facts in Base Resumes.")
        if not settings.resume_prompt.startswith("resume-") or not settings.letter_prompt.startswith("letter-"):
            raise ValueError("Select the correct resume and letter prompt types")
        resume_prompt = self.load_prompt(settings.resume_prompt)
        letter_prompt = self.load_prompt(settings.letter_prompt)
        snapshot = settings.model_dump()
        snapshot.update(
            base=base, resume_prompt=resume_prompt, letter_prompt=letter_prompt,
            prompt_profiles={"resume": settings.resume_prompt, "letter": settings.letter_prompt},
            hashes={"base": digest(base), "resume_prompt": digest(resume_prompt), "letter_prompt": digest(letter_prompt)},
            snapshot_at=datetime.now(timezone.utc).isoformat(), revision=1,
        )
        return snapshot
