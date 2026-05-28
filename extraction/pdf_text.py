from __future__ import annotations

import io
from typing import Optional

from loguru import logger
from pypdf import PdfReader

from extraction.metadata_cleaner import normalize_text


def extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text_parts: list[str] = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            page_text = page.extract_text() or ""
        except Exception as exc:  # pragma: no cover - defensive around malformed PDFs
            logger.warning("Failed extracting text from page {}: {}", index, exc)
            page_text = ""
        if page_text:
            text_parts.append(page_text)
    return normalize_text("\n".join(text_parts)) or ""


def extract_text_from_pdf_file(path: str) -> str:
    with open(path, "rb") as file_obj:
        return extract_text_from_pdf_bytes(file_obj.read())


def safe_extract_text_from_pdf_bytes(pdf_bytes: Optional[bytes]) -> Optional[str]:
    if not pdf_bytes:
        return None
    try:
        return extract_text_from_pdf_bytes(pdf_bytes)
    except Exception as exc:
        logger.warning("PDF extraction failed: {}", exc)
        return None

