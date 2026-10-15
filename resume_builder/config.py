"""Writable user configuration and fail-closed Windows credential storage."""

import os
import sys
import tempfile
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from .models import StrictModel


class Settings(StrictModel):
    provider: Literal["mock", "openai", "compatible"] = "mock"
    model: str = Field(default="gpt-4.1-mini", min_length=1)
    base_url: str = "https://api.openai.com/v1"
    mode: Literal["json_schema", "json_object"] = "json_schema"
    timeout: int = Field(default=60, ge=5, le=300)
    custom_endpoint_acknowledged: bool = False
    input_budget_chars: int = Field(default=30000, ge=1000, le=200000)
    inbox_folder: str = ""
    output_folder: str = ""
    resume_prompt: str = "resume-default"
    letter_prompt: str = "letter-default"
    base_resume: str = "fictional-example"

    @field_validator("base_url")
    @classmethod
    def secure_url(cls, value):
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
        ):
            raise ValueError("Provider base URL must be HTTPS without credentials, query or fragment")
        return value.rstrip("/")


def atomic_write(path: Path, content: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".write-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def save_settings(data_dir: Path, settings: Settings) -> None:
    validated = Settings.model_validate(settings.model_dump())
    atomic_write(Path(data_dir) / "settings.json", validated.model_dump_json(indent=2))


def load_settings(data_dir: Path) -> Settings:
    path = Path(data_dir) / "settings.json"
    if not path.exists():
        return Settings()
    try:
        return Settings.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Settings could not be loaded. Preserve settings.json and repair or restore it.") from None


def validate_folders(inbox: str, output: str) -> None:
    if not inbox.strip() or not output.strip():
        raise ValueError("Choose both an inbox and an output folder")
    paths = [Path(value).expanduser() for value in (inbox, output)]
    if any(not path.is_absolute() for path in paths):
        raise ValueError("Folders must be absolute paths")
    incoming, outgoing = [path.resolve() for path in paths]
    if incoming == outgoing or incoming in outgoing.parents or outgoing in incoming.parents:
        raise ValueError("Inbox and output folders must not overlap")
    for path in (incoming, outgoing):
        if path == Path(path.anchor) or path == Path.home().resolve():
            raise ValueError("Choose a dedicated folder, not a drive root or your home directory")
        if path.exists() and not path.is_dir():
            raise ValueError("Selected path is not a directory")
        parent = path
        while not parent.exists():
            parent = parent.parent
        if not os.access(parent, os.W_OK):
            raise ValueError("Selected folder is not writable")


class KeyVault:
    """Never fall back to a plaintext or third-party keyring backend."""

    def __init__(self):
        self._ephemeral: str | None = None

    @staticmethod
    def _backend():
        if sys.platform != "win32":
            raise ValueError("Persistent keys require Windows Credential Manager. Use a session-only key here.")
        try:
            from keyring.backends.Windows import WinVaultKeyring

            return WinVaultKeyring()
        except Exception:
            raise ValueError("Windows Credential Manager is unavailable; key was not saved.") from None

    def get(self) -> str | None:
        if self._ephemeral is not None:
            return self._ephemeral
        if sys.platform != "win32":
            return None
        try:
            return self._backend().get_password("ResumeBuilderV3-Win", "provider-api-key")
        except Exception:
            raise ValueError("Cannot read Windows Credential Manager. Enter a session-only key or repair vault access.") from None

    def set(self, key: str, persist: bool = True) -> None:
        if not key.strip():
            raise ValueError("API key cannot be empty")
        if persist:
            try:
                self._backend().set_password("ResumeBuilderV3-Win", "provider-api-key", key.strip())
            except Exception:
                raise ValueError("Secure credential storage failed; key was not saved. Session-only is available.") from None
            self._ephemeral = None
        else:
            self._ephemeral = key.strip()

    def delete(self) -> None:
        self._ephemeral = None
        if sys.platform == "win32":
            backend = self._backend()
            try:
                if backend.get_password("ResumeBuilderV3-Win", "provider-api-key") is not None:
                    backend.delete_password("ResumeBuilderV3-Win", "provider-api-key")
            except Exception:
                raise ValueError("Could not remove the saved credential from Windows Credential Manager.") from None
