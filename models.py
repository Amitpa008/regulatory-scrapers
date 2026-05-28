from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class RegulatoryDocument(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, populate_by_name=True)

    source: str
    regulator: str
    document_type: Optional[str] = None
    title: str
    reference_no: Optional[str] = None
    published_date: Optional[date] = None
    department: Optional[str] = None
    category: Optional[str] = None
    url: HttpUrl
    pdf_url: Optional[HttpUrl] = None
    pdf_sha256: Optional[str] = None
    text_content: Optional[str] = None
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("title cannot be empty")
        return cleaned

    @field_validator("reference_no", "department", "category", "document_type")
    @classmethod
    def normalize_optional_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        return cleaned or None

