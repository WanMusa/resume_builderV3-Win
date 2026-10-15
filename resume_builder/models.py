"""Shared, strict contracts. Fixed career facts never come from the provider."""

from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("*")
    @classmethod
    def required_text_is_not_blank(cls, value, info):
        if isinstance(value, str) and cls.model_fields[info.field_name].is_required() and not value.strip():
            raise ValueError("Required text must not be blank")
        return value


class Job(StrictModel):
    id: str = Field(min_length=1, max_length=500)
    source_url: str
    site: str = Field(min_length=1)
    captured_at: str
    description_text: str = Field(min_length=1)
    title: str | None = None
    company: str | None = None
    location: str | None = None
    employment_type: str | None = None
    salary_text: str | None = None

    @field_validator("source_url")
    @classmethod
    def http_url(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Expected an HTTP(S) source URL without credentials")
        return value

    @field_validator("captured_at")
    @classmethod
    def aware_timestamp(cls, value):
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("Timestamp must include a timezone")
        return value

    @field_validator("description_text", "site", "id")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Must not be blank")
        return value


class Identity(StrictModel):
    name: str = Field(min_length=1)
    email: str = ""
    phone: str = ""
    location: str = ""
    links: list[str] = Field(default_factory=list)


class Achievement(StrictModel):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    supporting_details: str = ""
    critical: bool = False


class Role(StrictModel):
    id: str = Field(min_length=1)
    employer: str = Field(min_length=1)
    title: str = Field(min_length=1)
    dates: str = ""
    background: str = ""
    achievements: list[Achievement] = Field(default_factory=list)


class Education(StrictModel):
    qualification: str = Field(min_length=1)
    institution: str = ""
    dates: str = ""
    details: str = ""


class FixedSection(StrictModel):
    heading: str = Field(min_length=1)
    entries: list[str] = Field(default_factory=list)


class BaseResume(StrictModel):
    schema_version: Literal[1] = 1
    name: str = Field(min_length=1)
    is_example: bool = True
    verified: bool = False
    identity: Identity
    profile: str = ""
    skills: list[str] = Field(default_factory=list)
    roles: list[Role] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    references: str = ""
    additional_sections: list[FixedSection] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [role.id for role in self.roles]
        evidence = [a.id for role in self.roles for a in role.achievements]
        if len(ids) != len(set(ids)) or len(evidence) != len(set(evidence)):
            raise ValueError("Role and evidence IDs must each be unique")
        return self


class GroundedText(StrictModel):
    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class TailoredAchievement(StrictModel):
    role_id: str
    evidence_id: str
    text: str = Field(min_length=1)


class ResumeContent(StrictModel):
    summary: GroundedText
    expertise: list[str]
    achievements: list[TailoredAchievement]


class LetterContent(StrictModel):
    paragraphs: list[GroundedText] = Field(min_length=1, max_length=6)
