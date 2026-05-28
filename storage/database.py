from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

from models import RegulatoryDocument


def build_dedup_key(source: str, title: str, published_date: Optional[str], reference_no: Optional[str]) -> str:
    parts = [
        (source or "").strip().lower(),
        " ".join((title or "").split()).strip().lower(),
        (published_date or "").strip(),
        (reference_no or "").strip().lower(),
    ]
    return "|".join(parts)


class DocumentDatabase:
    def __init__(self, db_path: str | Path = "data/regulatory_documents.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS regulatory_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedup_key TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    regulator TEXT NOT NULL,
                    document_type TEXT,
                    title TEXT NOT NULL,
                    reference_no TEXT,
                    published_date TEXT,
                    department TEXT,
                    category TEXT,
                    url TEXT NOT NULL,
                    pdf_url TEXT,
                    pdf_sha256 TEXT,
                    text_content TEXT,
                    scraped_at TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def dedup_key(document: RegulatoryDocument) -> str:
        published_date = document.published_date.isoformat() if document.published_date else None
        return build_dedup_key(
            source=document.source,
            title=document.title,
            published_date=published_date,
            reference_no=document.reference_no,
        )

    def upsert_document(self, document: RegulatoryDocument) -> str:
        payload = document.model_dump(mode="json")
        dedup_key = self.dedup_key(document)
        comparable_payload = dict(payload)
        comparable_payload.pop("scraped_at", None)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT raw_json FROM regulatory_documents WHERE dedup_key = ?",
                (dedup_key,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO regulatory_documents (
                        dedup_key, source, regulator, document_type, title, reference_no,
                        published_date, department, category, url, pdf_url, pdf_sha256,
                        text_content, scraped_at, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        dedup_key,
                        payload["source"],
                        payload["regulator"],
                        payload.get("document_type"),
                        payload["title"],
                        payload.get("reference_no"),
                        payload.get("published_date"),
                        payload.get("department"),
                        payload.get("category"),
                        payload["url"],
                        payload.get("pdf_url"),
                        payload.get("pdf_sha256"),
                        payload.get("text_content"),
                        payload["scraped_at"],
                        json.dumps(payload, ensure_ascii=True),
                    ),
                )
                return "inserted"

            existing_payload = json.loads(existing[0])
            existing_payload.pop("scraped_at", None)
            if existing_payload == comparable_payload:
                return "duplicate"

            connection.execute(
                """
                UPDATE regulatory_documents
                SET regulator = ?,
                    document_type = ?,
                    title = ?,
                    reference_no = ?,
                    published_date = ?,
                    department = ?,
                    category = ?,
                    url = ?,
                    pdf_url = ?,
                    pdf_sha256 = ?,
                    text_content = ?,
                    scraped_at = ?,
                    raw_json = ?
                WHERE dedup_key = ?
                """,
                (
                    payload["regulator"],
                    payload.get("document_type"),
                    payload["title"],
                    payload.get("reference_no"),
                    payload.get("published_date"),
                    payload.get("department"),
                    payload.get("category"),
                    payload["url"],
                    payload.get("pdf_url"),
                    payload.get("pdf_sha256"),
                    payload.get("text_content"),
                    payload["scraped_at"],
                    json.dumps(payload, ensure_ascii=True),
                    dedup_key,
                ),
            )
            return "updated"

    def fetch_all(self) -> list[dict]:
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute("SELECT * FROM regulatory_documents ORDER BY published_date DESC").fetchall()
        return [dict(row) for row in rows]
