import re
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RecipientIn(BaseModel):
    """Validated one-by-one in the service layer (not by FastAPI), so that one bad
    recipient is recorded as a failure instead of rejecting the whole request."""

    name: str = Field(min_length=1, max_length=120)
    email: str | None = Field(default=None, max_length=320)

    @field_validator("name")
    @classmethod
    def _clean_name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("name must not be blank")
        return v

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        v = v.strip()
        if not _EMAIL_RE.match(v):
            raise ValueError("invalid email address")
        return v


class JobCreate(BaseModel):
    """Job-level input. Structural problems here -> HTTP 422 for the whole request."""

    course_name: str = Field(min_length=1, max_length=200)
    title: str = Field(default="Certificate of Completion", min_length=1, max_length=200)
    issuer: str | None = Field(default=None, max_length=200)
    issued_on: date = Field(default_factory=date.today)
    # Deliberately loose: each item is validated individually later.
    recipients: list[Any] = Field(min_length=1)


class JobOut(BaseModel):
    id: str
    status: str
    course_name: str
    total: int
    succeeded: int
    failed: int
    pending: int
    progress_percent: float
    created_at: datetime
    completed_at: datetime | None
    links: dict[str, str]


class CertificateOut(BaseModel):
    id: str
    row_index: int
    recipient_name: str | None
    recipient_email: str | None
    status: str
    error: str | None
    download_url: str | None


class CertificateList(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[CertificateOut]
