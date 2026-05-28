from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Iterable
from urllib.parse import urljoin

from models import RegulatoryDocument
from scrapers.base import BaseScraper


class ConfigDrivenScraper(BaseScraper):
    def __init__(self, config: dict[str, Any], **kwargs: Any) -> None:
        self.config = config
        self.source = config["source"]
        self.regulator = config["regulator"]
        super().__init__(**kwargs)

    def fetch_index(self, from_date: date, to_date: date) -> Any:
        # TODO: Fill source-specific listing endpoint/query parameters after inspecting the target site.
        if not self.config.get("listing_url"):
            raise NotImplementedError(f"{self.source} listing_url is not configured yet")
        return self.get(self.config["listing_url"])

    def parse_listing(self, response: Any) -> Iterable[dict[str, Any]]:
        # TODO: Replace with site-specific parsing after inspecting the target HTML or API shape.
        soup = self.parse_html(response)
        selector = self.config.get("listing_selector")
        if not selector:
            return []

        for node in soup.select(selector):
            anchor = node.select_one(self.config.get("link_selector", "a"))
            if anchor is None:
                continue
            href = anchor.get("href")
            if not href:
                continue
            yield {
                "title": anchor.get_text(" ", strip=True),
                "url": urljoin(self.config["base_url"], href),
                "pdf_url": urljoin(self.config["base_url"], href) if href.lower().endswith(".pdf") else None,
                "document_type": self.config.get("default_document_type"),
                "category": self.config.get("default_category"),
                "department": self.config.get("default_department"),
            }

    def fetch_document(self, record: dict[str, Any]) -> RegulatoryDocument:
        return RegulatoryDocument(
            source=self.source,
            regulator=self.regulator,
            document_type=record.get("document_type"),
            title=record["title"],
            reference_no=record.get("reference_no"),
            published_date=record.get("published_date"),
            department=record.get("department"),
            category=record.get("category"),
            url=record["url"],
            pdf_url=record.get("pdf_url"),
            text_content=record.get("text_content"),
            scraped_at=datetime.now(timezone.utc),
        )

