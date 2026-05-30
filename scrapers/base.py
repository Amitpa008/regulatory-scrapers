from __future__ import annotations

import csv
import ssl
import sys
import time
import random
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import certifi
import httpx
from bs4 import BeautifulSoup
from loguru import logger
from tenacity import RetryError, retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from extraction.metadata_cleaner import clean_reference_no, normalize_text
from extraction.pdf_text import safe_extract_text_from_pdf_bytes
from models import RegulatoryDocument
from storage.database import DocumentDatabase
from storage.pdf_store import PDFStore


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
RETRYABLE_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}


def raise_csv_field_size_limit() -> None:
    max_size = sys.maxsize
    while True:
        try:
            csv.field_size_limit(max_size)
            return
        except OverflowError:
            max_size = int(max_size / 10)


class BaseScraper(ABC):
    source: str
    regulator: str

    def __init__(
        self,
        *,
        database: Optional[DocumentDatabase] = None,
        pdf_store: Optional[PDFStore] = None,
        timeout: float = 30.0,
        rate_limit_seconds: float = 1.0,
        user_agent: str = DEFAULT_USER_AGENT,
        use_playwright_fallback: bool = False,
    ) -> None:
        self.database = database or DocumentDatabase()
        self.pdf_store = pdf_store or PDFStore()
        self.timeout = timeout
        self.rate_limit_seconds = rate_limit_seconds
        self.use_playwright_fallback = use_playwright_fallback
        self.headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        }
        self.client = httpx.Client(
            headers=self.headers,
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
            verify=ssl.create_default_context(cafile=certifi.where()),
        )
        self.insecure_client: Optional[httpx.Client] = None

    def close(self) -> None:
        self.client.close()
        if self.insecure_client is not None:
            self.insecure_client.close()

    def __enter__(self) -> "BaseScraper":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def rate_limit(self) -> None:
        if self.rate_limit_seconds > 0:
            time.sleep(self.rate_limit_seconds)

    def compute_chunk_window(
        self,
        *,
        total_chunks: int,
        previous_last_completed_chunk: int,
        max_chunks_this_run: int | None,
    ) -> dict[str, int | bool]:
        if total_chunks < 0:
            raise RuntimeError(f"total_chunks must be non-negative; got {total_chunks}")
        if previous_last_completed_chunk < 0:
            raise RuntimeError(
                f"previous last_completed_chunk must be non-negative; got {previous_last_completed_chunk}"
            )
        if previous_last_completed_chunk > total_chunks:
            raise RuntimeError(
                f"checkpoint last_completed_chunk {previous_last_completed_chunk} exceeds total_chunks {total_chunks}"
            )

        if total_chunks == 0:
            return {
                "total_chunks": 0,
                "previous_last_completed_chunk": previous_last_completed_chunk,
                "resume_from_chunk": 0,
                "expected_end_chunk": 0,
                "chunks_this_run": 0,
                "completed": True,
            }

        if previous_last_completed_chunk >= total_chunks:
            return {
                "total_chunks": total_chunks,
                "previous_last_completed_chunk": previous_last_completed_chunk,
                "resume_from_chunk": total_chunks + 1,
                "expected_end_chunk": total_chunks,
                "chunks_this_run": 0,
                "completed": True,
            }

        resume_from_chunk = previous_last_completed_chunk + 1
        if max_chunks_this_run is None:
            expected_end_chunk = total_chunks
        else:
            expected_end_chunk = min(total_chunks, resume_from_chunk + max_chunks_this_run - 1)

        if expected_end_chunk < resume_from_chunk:
            raise RuntimeError(
                "Invalid chunk window: expected_end_chunk "
                f"{expected_end_chunk} is earlier than resume_from_chunk {resume_from_chunk}."
            )

        chunks_this_run = expected_end_chunk - resume_from_chunk + 1
        return {
            "total_chunks": total_chunks,
            "previous_last_completed_chunk": previous_last_completed_chunk,
            "resume_from_chunk": resume_from_chunk,
            "expected_end_chunk": expected_end_chunk,
            "chunks_this_run": chunks_this_run,
            "completed": False,
        }

    def assert_non_regressing_checkpoint(
        self,
        *,
        previous_last_completed_chunk: int,
        new_last_completed_chunk: int,
    ) -> None:
        if new_last_completed_chunk < previous_last_completed_chunk:
            raise RuntimeError(
                "Checkpoint regression detected: "
                f"previous last_completed_chunk={previous_last_completed_chunk}, "
                f"new last_completed_chunk={new_last_completed_chunk}."
            )

    @staticmethod
    def is_retryable_http_exception(exc: Exception) -> bool:
        if isinstance(
            exc,
            (
                httpx.RemoteProtocolError,
                httpx.ReadTimeout,
                httpx.ConnectTimeout,
                httpx.ConnectError,
                httpx.NetworkError,
            ),
        ):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in RETRYABLE_HTTP_STATUS_CODES
        return False

    @staticmethod
    def compute_retry_delay(
        attempt: int,
        *,
        base_delay: float,
        max_delay: float,
        jitter_max: float = 0.5,
    ) -> float:
        delay = min(max_delay, base_delay * (2 ** max(0, attempt - 1)))
        return min(max_delay, delay + random.uniform(0, jitter_max))

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self.rate_limit()
        try:
            response = self.client.get(url, **kwargs)
        except httpx.ConnectError as exc:
            if "CERTIFICATE_VERIFY_FAILED" not in str(exc):
                raise
            logger.warning("TLS verification failed for {}; retrying with verification disabled", url)
            if self.insecure_client is None:
                self.insecure_client = httpx.Client(
                    headers=self.headers,
                    timeout=httpx.Timeout(self.timeout),
                    follow_redirects=True,
                    verify=False,
                )
            response = self.insecure_client.get(url, **kwargs)
        response.raise_for_status()
        return response

    def parse_html(self, response: httpx.Response) -> BeautifulSoup:
        return BeautifulSoup(response.text, "html.parser")

    @abstractmethod
    def fetch_index(self, from_date: date, to_date: date) -> Any:
        raise NotImplementedError

    @abstractmethod
    def parse_listing(self, response: Any) -> Iterable[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def fetch_document(self, record: dict[str, Any]) -> RegulatoryDocument:
        raise NotImplementedError

    def download_pdf(self, pdf_url: str) -> bytes:
        try:
            response = self.get(pdf_url)
            return response.content
        except RetryError:
            raise
        except httpx.HTTPError:
            if self.use_playwright_fallback:
                return self.download_pdf_with_playwright(pdf_url)
            raise

    def download_pdf_with_playwright(self, pdf_url: str) -> bytes:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - depends on optional install
            raise RuntimeError(
                "Playwright fallback requested but not installed. Install with: pip install .[playwright]"
            ) from exc

        logger.info("Using Playwright fallback for {}", pdf_url)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(user_agent=self.headers["User-Agent"])
            try:
                response = page.goto(pdf_url, wait_until="networkidle", timeout=int(self.timeout * 1000))
                if response is None:
                    raise RuntimeError(f"No response received for {pdf_url}")
                body = response.body()
                return body
            finally:
                browser.close()

    def enrich_with_pdf(self, document: RegulatoryDocument) -> tuple[RegulatoryDocument, bool]:
        if not document.pdf_url:
            return document, False
        pdf_bytes = self.download_pdf(str(document.pdf_url))
        _, digest = self.pdf_store.save(document.source, pdf_bytes)
        text_content = safe_extract_text_from_pdf_bytes(pdf_bytes)
        return (
            document.model_copy(
                update={
                    "pdf_sha256": digest,
                    "text_content": text_content or document.text_content,
                }
            ),
            True,
        )

    def persist_document(self, document: RegulatoryDocument) -> str:
        return self.database.upsert_document(document)

    def normalize_record(self, record: dict[str, Any]) -> dict[str, Any]:
        normalized = {key: normalize_text(value) if isinstance(value, str) else value for key, value in record.items()}
        if "reference_no" in normalized:
            normalized["reference_no"] = clean_reference_no(normalized.get("reference_no"))
        return normalized

    def process_records(self, records: Iterable[dict[str, Any]]) -> dict[str, int]:
        stats = {
            "found": 0,
            "inserted": 0,
            "updated": 0,
            "duplicates_skipped": 0,
            "pdf_downloaded": 0,
            "failed": 0,
        }
        for record in records:
            stats["found"] += 1
            try:
                normalized_record = self.normalize_record(record)
                document = self.fetch_document(normalized_record)
                document, pdf_downloaded = self.enrich_with_pdf(document)
                action = self.persist_document(document)
                if action not in {"inserted", "updated", "duplicate"}:
                    raise ValueError(f"Unexpected persistence action: {action}")
                if pdf_downloaded:
                    stats["pdf_downloaded"] += 1
                if action == "inserted":
                    stats["inserted"] += 1
                elif action == "updated":
                    stats["updated"] += 1
                else:
                    stats["duplicates_skipped"] += 1
                logger.bind(
                    source=document.source,
                    title=document.title,
                    published_date=str(document.published_date),
                    pdf_sha256=document.pdf_sha256,
                    action=action,
                ).info("Stored document")
            except Exception as exc:
                stats["failed"] += 1
                logger.bind(source=self.source, record=record, error=str(exc)).exception("Failed processing record")
        return stats

    def run_backfill(self, from_date: date, to_date: date, limit: Optional[int] = None) -> dict[str, int]:
        logger.bind(
            source=self.source,
            regulator=self.regulator,
            from_date=str(from_date),
            to_date=str(to_date),
            limit=limit,
        ).info("Starting backfill")
        response = self.fetch_index(from_date, to_date)
        records = list(self.parse_listing(response))
        if limit is not None:
            records = records[:limit]
        stats = self.process_records(records)
        logger.bind(source=self.source, **stats).info("Completed backfill")
        return stats

    def run_incremental(self, days_back: int = 7) -> dict[str, int]:
        to_date = datetime.now(timezone.utc).date()
        from_date = to_date - timedelta(days=days_back)
        logger.bind(source=self.source, days_back=days_back).info("Running incremental")
        return self.run_backfill(from_date=from_date, to_date=to_date)
