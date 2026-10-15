import json
import sys
from importlib.resources import files

import pytest
from pydantic import ValidationError

from resume_builder.config import KeyVault, Settings, load_settings, save_settings, validate_folders
from resume_builder.library import Library, digest, validate_prompt
from resume_builder.models import BaseResume, Job


def test_packaged_resources_and_seed_does_not_overwrite(tmp_path):
    library = Library(tmp_path)
    assert BaseResume.model_validate(library.load_base("fictional-example")).is_example
    path = tmp_path / "prompts/resume-default.txt"
    path.write_text("My edited prompt", encoding="utf-8")
    Library(tmp_path)
    assert library.load_prompt("resume-default") == "My edited prompt"
    fixture = json.loads(files("resume_builder").joinpath("assets/sample-job.json").read_text())
    assert Job.model_validate(fixture["jobs"][0]).title


def test_prompt_validation_atomic_backup_and_new(tmp_path):
    library = Library(tmp_path)
    original = library.load_prompt("resume-default")
    library.save_prompt("resume-default", "Default", "resume", "Tailor {evidence} for {job_description}")
    assert next((tmp_path / "history").glob("*.txt")).read_text() == original
    new = library.save_prompt(None, "My profile", "letter", "Write a truthful letter")
    assert library.load_prompt(new) == "Write a truthful letter"
    assert next(p for p in library.prompts() if p["id"] == new)["name"] == "My profile"
    library.save_prompt(new, "Renamed profile", "letter", "Write a truthful letter")
    assert next(p for p in library.prompts() if p["id"] == new)["name"] == "Renamed profile"
    for invalid in ("", "{api_key}", "{evidence.__class__}", "{job_description!r}", "{", "{evidence} {evidence}"):
        with pytest.raises(ValueError):
            validate_prompt(invalid)
    with pytest.raises(ValueError):
        library.save_prompt("resume-default", "Default", "letter", "Hello")
    assert not list((tmp_path / "prompts").glob(".write-*"))


def test_base_validation_and_backup_without_silent_loss(tmp_path):
    library = Library(tmp_path)
    base = library.load_base("fictional-example")
    identifier = library.save_base(None, base)
    assert library.load_base(identifier) == base
    base["profile"] = "Edited by user"
    library.save_base(identifier, base)
    assert library.load_base(identifier)["profile"] == "Edited by user"
    assert list((tmp_path / "history").glob("*.json"))
    with pytest.raises(ValidationError):
        library.save_base(None, {**base, "unknown_v2_field": "must not be discarded"})
    with pytest.raises(ValueError):
        library.load_base("../escape")


def test_settings_persist_and_snapshot_freezes_content(tmp_path):
    library = Library(tmp_path)
    settings = Settings(inbox_folder=str(tmp_path / "inbox"), output_folder=str(tmp_path / "output"))
    save_settings(tmp_path, settings)
    assert load_settings(tmp_path) == settings
    snapshot = library.snapshot(settings)
    library.save_prompt("resume-default", "Changed", "resume", "Changed after enqueue")
    assert snapshot["resume_prompt"] != library.load_prompt("resume-default")
    assert snapshot["hashes"]["base"] == digest(snapshot["base"])
    assert "api_key" not in json.dumps(snapshot)
    settings.provider = "openai"
    with pytest.raises(ValueError, match="Live generation requires"):
        library.snapshot(settings)


def test_secure_settings_and_folders(tmp_path):
    with pytest.raises(ValidationError):
        Settings(api_key="must never persist")
    for url in ("http://api.example/v1", "******api.example", "https://api.example/?key=x"):
        with pytest.raises(ValidationError):
            Settings(base_url=url)
    for inbox, output in ((tmp_path, tmp_path), (tmp_path, tmp_path / "nested")):
        with pytest.raises(ValueError):
            validate_folders(str(inbox), str(output))
    with pytest.raises(ValueError):
        validate_folders("relative", str(tmp_path / "output"))
    validate_folders(str(tmp_path / "inbox"), str(tmp_path / "output"))


def test_ephemeral_key_and_no_plaintext_fallback(monkeypatch, tmp_path):
    vault = KeyVault()
    vault.set("fictional-test-value", persist=False)
    assert vault.get() == "fictional-test-value"
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(ValueError, match="not saved"):
        vault.set("fictional-test-value", persist=True)
    vault.delete()
    assert vault.get() is None
    save_settings(tmp_path, Settings())
    assert "fictional-test-value" not in (tmp_path / "settings.json").read_text()


def test_malformed_settings_preserved(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="Preserve"):
        load_settings(tmp_path)
    assert path.read_text() == "{broken"


def test_required_base_fields_reject_whitespace_without_rewriting_facts(tmp_path):
    base = Library(tmp_path).load_base("fictional-example")
    base["identity"]["name"] = "   "
    with pytest.raises(ValidationError):
        BaseResume.model_validate(base)
    base["identity"]["name"] = " Alex Example "
    assert BaseResume.model_validate(base).identity.name == " Alex Example "
