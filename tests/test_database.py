from datetime import date, datetime, timezone

from models import RegulatoryDocument
from storage.database import DocumentDatabase, build_dedup_key


def test_build_dedup_key_is_normalized() -> None:
    key = build_dedup_key(
        source="SEBI",
        title="  Circular   On  Margin  ",
        published_date="2026-05-16",
        reference_no=" Ref/01 ",
    )
    assert key == "sebi|circular on margin|2026-05-16|ref/01"


def test_upsert_document_deduplicates(tmp_path) -> None:
    database = DocumentDatabase(tmp_path / "test.db")
    document = RegulatoryDocument(
        source="sebi",
        regulator="SEBI",
        document_type="circular",
        title="Margin circular",
        reference_no="SEBI/1",
        published_date=date(2026, 5, 16),
        department="Markets",
        category="regulatory",
        url="https://example.com/doc",
        pdf_url="https://example.com/doc.pdf",
        pdf_sha256="abc123",
        text_content="first version",
        scraped_at=datetime.now(timezone.utc),
    )
    database.upsert_document(document)
    database.upsert_document(document.model_copy(update={"text_content": "second version"}))

    rows = database.fetch_all()
    assert len(rows) == 1
    assert rows[0]["text_content"] == "second version"

