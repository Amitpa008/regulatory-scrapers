from __future__ import annotations

import csv
import json
import math
import re
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag
from loguru import logger
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from extraction.metadata_cleaner import clean_reference_no, normalize_text, parse_indian_date
from models import RegulatoryDocument
from scrapers.base import BaseScraper


SEBI_SOURCE_LABEL = "SEBI"
SEBI_LISTING_TYPES = {
    "circulars": {
        "document_type": "Circulars",
        "url": "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=1&smid=0&ssid=7",
        "ssid": "7",
    },
    "master-circulars": {
        "document_type": "Master Circulars",
        "url": "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=1&smid=0&ssid=6",
        "ssid": "6",
    },
}
SEBI_LISTING_TABLE_SELECTOR = "table#sample_1"
SEBI_DETAIL_TITLE_SELECTOR = "section.department-slider h1"
SEBI_DETAIL_DATE_SELECTOR = ".date_value h5"
SEBI_TOTAL_RECORDS_SELECTOR = ".pagination .pagination_inner p"
SEBI_ARCHIVE_AJAX_URL = "https://www.sebi.gov.in/sebiweb/ajax/home/getnewslistallinfo.jsp"
SEBI_ARCHIVE_URL_FLAG = "doListingAll=yes"
SEBI_PAGINATION_CALL_RE = re.compile(r"searchFormNewsListAll\('(?P<next>[^']+)',\s*'(?P<token>[^']+)'\)")


@dataclass
class ListingPage:
    listing_type: str
    title: str
    total_records: int
    rows: list[dict[str, Any]]
    response: httpx.Response


@dataclass
class SebiListingRecord:
    date: str
    type: str
    title: str
    link: str
    source_url: str
    scraped_at: str
    raw_date: Optional[str] = None


@dataclass
class SebiFetchedPage:
    url: str
    html: str
    transport: str


@dataclass
class SebiPageState:
    source_url: str
    form_action: str
    ajax_url: str
    total_records: int
    page_size: int
    total_pages: int
    current_page: int
    next_value: str
    form_payload: dict[str, str]
    ajax_headers: dict[str, str]
    pagination_controls: list[dict[str, str]]
    hidden_fields: dict[str, str]


@dataclass
class ArchiveCheckpoint:
    source_url: str
    output_path: str
    total_records_detected: int
    page_size: int
    last_completed_page: int
    records_written: int
    unique_records_written: int
    started_at: str
    updated_at: str
    completed: bool
    errors: list[str]


@dataclass
class ArchiveResumeState:
    csv_rows_detected: int
    existing_dedupe_keys_loaded: int
    checkpoint_last_completed_page: int
    completed_pages_from_output: int
    reconciled_last_completed_page: int
    resume_from_page: int
    partial_page_rows: int
    output_mode: str


class SEBIScraper(BaseScraper):
    """SEBI scraper for detail pages and generic listing URLs.

    Pagination findings for `doListingAll=yes` are incomplete in this environment.
    We confirmed the page renders top-level pagination and a total count, but the live raw HTML/JS needed to
    safely identify the page-turn form payload is currently blocked from direct Python HTTP by SEBI with 403.
    The generic listing scraper therefore supports first-page scraping and raises a clear NotImplementedError
    when multi-page traversal is requested.
    """

    source = "sebi"
    regulator = SEBI_SOURCE_LABEL

    def __init__(self, config: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.config = config
        self.last_fetch_transport = "httpx"

    def fetch_index(self, from_date: date, to_date: date) -> dict[str, ListingPage]:
        del from_date, to_date
        pages: dict[str, ListingPage] = {}
        for listing_type in SEBI_LISTING_TYPES:
            response = self.fetch_http_or_browser(SEBI_LISTING_TYPES[listing_type]["url"])
            pages[listing_type] = self._parse_listing_page(response, listing_type)
        return pages

    def parse_listing(self, response: dict[str, ListingPage]) -> Iterable[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for page in response.values():
            records.extend(page.rows)
        return records

    def fetch_document(self, record: dict[str, Any]) -> RegulatoryDocument:
        detail_response = self.fetch_http_or_browser(record["detail_url"])
        return self._build_document_from_detail_response(record, detail_response)

    def inspect(self, listing_type: str) -> None:
        page = self.fetch_listing_page(listing_type)
        fixture_dir = Path("tests/fixtures/sebi")
        fixture_dir.mkdir(parents=True, exist_ok=True)

        listing_fixture = fixture_dir / f"{listing_type}_listing.html"
        listing_fixture.write_text(page.response.text, encoding="utf-8")

        print(f"Page title: {page.title}")
        print(f"Fixture saved: {listing_fixture}")
        print("Detected rows:")
        for row in page.rows[:10]:
            print(f"{row['published_date'].isoformat()} | {row['title']} | {row['detail_url']}")

        if not page.rows:
            raise RuntimeError(f"No rows detected on SEBI {listing_type} listing page")

        detail_response = self.fetch_http_or_browser(page.rows[0]["detail_url"])
        detail_fixture = fixture_dir / f"{listing_type}_detail.html"
        detail_fixture.write_text(detail_response.text, encoding="utf-8")
        print(f"Detail fixture saved: {detail_fixture}")

    def fetch_listing_page(self, listing_type: str) -> ListingPage:
        if listing_type not in SEBI_LISTING_TYPES:
            raise KeyError(f"Unknown SEBI listing type: {listing_type}")
        response = self.fetch_http_or_browser(SEBI_LISTING_TYPES[listing_type]["url"])
        return self._parse_listing_page(response, listing_type)

    def run_backfill(self, from_date: date, to_date: date, limit: Optional[int] = None) -> dict[str, int]:
        logger.bind(
            source=self.source,
            regulator=self.regulator,
            from_date=str(from_date),
            to_date=str(to_date),
            limit=limit,
        ).info("Starting SEBI backfill")

        listing_pages = self.fetch_index(from_date, to_date)
        all_rows = []
        oldest_seen: Optional[date] = None

        for page in listing_pages.values():
            for row in page.rows:
                published_date = row["published_date"]
                oldest_seen = published_date if oldest_seen is None else min(oldest_seen, published_date)
                if from_date <= published_date <= to_date:
                    all_rows.append(row)

        all_rows.sort(key=lambda row: row["published_date"], reverse=True)
        selected_rows = all_rows[:limit] if limit is not None else all_rows
        if not selected_rows:
            raise RuntimeError(
                f"No SEBI records found on the first rendered listing pages for {from_date.isoformat()} to {to_date.isoformat()}"
            )

        if oldest_seen and from_date < oldest_seen:
            max_from_first_pages = len(all_rows)
            if limit is None or limit > max_from_first_pages:
                raise NotImplementedError(
                    "SEBI pagination is required to reach older records. Inspected rendered pagination links, "
                    "observed total count text and pager controls, but did not confirm a safe page-turn payload "
                    "for automated traversal in this environment."
                )

        stats = self.process_records(selected_rows)
        logger.bind(source=self.source, **stats).info("Completed SEBI backfill")
        print(
            "Summary: "
            f"found={stats['found']}, inserted={stats['inserted']}, updated={stats['updated']}, "
            f"duplicates_skipped={stats['duplicates_skipped']}, pdf_downloaded={stats['pdf_downloaded']}, "
            f"failed={stats['failed']}"
        )
        return stats

    def fetch_http_or_browser(self, url: str) -> httpx.Response:
        try:
            response = super().get(url)
            self.last_fetch_transport = "httpx"
            return response
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 403:
                raise
            logger.warning("Direct HTTP returned 403 for {}; attempting Playwright fallback", url)
            fetched = self.fetch_with_playwright(url)
            self.last_fetch_transport = fetched.transport
            return httpx.Response(
                200,
                text=fetched.html,
                request=httpx.Request("GET", fetched.url),
            )

    def fetch_with_playwright(self, url: str) -> SebiFetchedPage:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "SEBI returned HTTP 403 to direct Python HTTP, and Playwright is not installed. "
                "Install with `pip install .[playwright]` and `playwright install chromium`."
            ) from exc

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(user_agent=self.headers["User-Agent"])
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
                page.wait_for_load_state("networkidle", timeout=int(self.timeout * 1000))
                html = page.content()
                if response is not None and response.status >= 400:
                    raise RuntimeError(
                        f"Playwright browser fallback reached {url} but received HTTP {response.status}."
                    )
                return SebiFetchedPage(url=page.url, html=html, transport="playwright")
            finally:
                browser.close()

    def inspect_listing_url(self, url: str, fixture_path: str | Path) -> list[SebiListingRecord]:
        fetched = self.fetch_listing_url(url)
        records, total_records = self.parse_listing_url_html(fetched.html, fetched.url)
        if not records:
            raise RuntimeError(f"Zero rows detected on SEBI listing URL: {url}")

        fixture_path = Path(fixture_path)
        fixture_path.parent.mkdir(parents=True, exist_ok=True)
        fixture_path.write_text(fetched.html, encoding="utf-8")

        soup = BeautifulSoup(fetched.html, "html.parser")
        title = normalize_text(soup.title.get_text(" ", strip=True) if soup.title else "") or ""

        print(f"Page title: {title}")
        if total_records is not None:
            print(f"Detected total records: {total_records}")
        print(f"Fixture saved: {fixture_path}")
        print("First 25 parsed rows:")
        for record in records[:25]:
            print(f"{record.date} | {record.type} | {record.title} | {record.link}")
        return records

    def inspect_pagination(
        self,
        *,
        url: str,
        fixture_path: str | Path,
        network_capture_path: str | Path,
        headless: bool = True,
    ) -> dict[str, Any]:
        del headless
        fetched = self.fetch_listing_url(url)
        state = self.build_archive_state(fetched.html, fetched.url)

        fixture_path = Path(fixture_path)
        fixture_path.parent.mkdir(parents=True, exist_ok=True)
        fixture_path.write_text(fetched.html, encoding="utf-8")

        network_capture: dict[str, Any] = {
            "page_1": {
                "method": "GET",
                "url": fetched.url,
                "query_params": parse_qs(urlparse(fetched.url).query),
                "headers": dict(self.headers),
                "transport": fetched.transport,
            },
            "pagination_requests": [],
            "playwright_used": False,
        }

        page2_request, page2_html = self.fetch_archive_page_fragment(state, target_page=2)
        page2_state = self.build_archive_state_from_fragment(page2_html, state)
        network_capture["pagination_requests"].append(page2_request)

        next_request, next_html = self.fetch_archive_page_fragment(page2_state, target_page=3)
        page3_state = self.build_archive_state_from_fragment(next_html, page2_state)
        network_capture["pagination_requests"].append(next_request)

        network_capture_path = Path(network_capture_path)
        network_capture_path.parent.mkdir(parents=True, exist_ok=True)
        network_capture_path.write_text(json.dumps(network_capture, indent=2), encoding="utf-8")

        soup = BeautifulSoup(fetched.html, "html.parser")
        title = normalize_text(soup.title.get_text(" ", strip=True) if soup.title else "") or ""
        print(f"Page title: {title}")
        print(f"Total record count: {state.total_records}")
        print(f"Page size: {state.page_size}")
        print(f"Form action: {state.form_action}")
        print("Pagination controls:")
        for control in state.pagination_controls:
            safe_label = control["label"].encode("ascii", errors="replace").decode("ascii")
            print(f"{safe_label} | {control['href']} | next={control.get('next', '')} | token={control.get('token', '')}")
        print("Forms and hidden fields:")
        print(f"Form method: POST")
        for name, value in state.hidden_fields.items():
            print(f"{name}={value}")
        print("JavaScript pagination calls:")
        for control in state.pagination_controls:
            if control.get("href", "").startswith("javascript:"):
                print(control["href"])
        print("Sample pagination requests:")
        for request in network_capture["pagination_requests"]:
            print(json.dumps(request, indent=2))

        return {
            "total_records": state.total_records,
            "page_size": state.page_size,
            "total_pages": state.total_pages,
            "transport": fetched.transport,
        }

    def scrape_listing_url(
        self,
        *,
        url: str,
        out_path: str | Path,
        limit: Optional[int] = None,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        doc_type: Optional[str] = None,
        pages: Optional[int] = None,
        all_pages: bool = False,
        store_db: bool = False,
        resume: bool = False,
        checkpoint_path: str | Path | None = None,
        start_page: int | None = None,
        end_page: int | None = None,
        max_pages_this_run: int | None = None,
        delay_seconds: float = 1.5,
        max_errors: int = 10,
        retries: int = 5,
        retry_base_delay: float = 3.0,
        retry_max_delay: float = 60.0,
        allow_partial: bool = False,
        headless: bool = True,
    ) -> list[SebiListingRecord]:
        if self.is_do_listing_all_url(url) and (all_pages or (pages is not None and pages > 1) or resume or checkpoint_path):
            return self.scrape_archive_all_pages(
                url=url,
                out_path=out_path,
                limit=limit,
                from_date=from_date,
                to_date=to_date,
                doc_type=doc_type,
                pages=pages,
                all_pages=all_pages,
                resume=resume,
                checkpoint_path=checkpoint_path,
                start_page=start_page,
                end_page=end_page,
                max_pages_this_run=max_pages_this_run,
                delay_seconds=delay_seconds,
                max_errors=max_errors,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
                allow_partial=allow_partial,
                headless=headless,
            )

        fetched = self.fetch_listing_url(url)
        records, _ = self.parse_listing_url_html(fetched.html, fetched.url)
        filtered_records = self.filter_listing_records(records, from_date=from_date, to_date=to_date, doc_type=doc_type)
        deduped_records = self.deduplicate_listing_records(filtered_records)
        if limit is not None:
            deduped_records = deduped_records[:limit]
        if not deduped_records:
            raise RuntimeError(f"No SEBI listing rows remained after parsing/filtering for URL: {url}")

        self.write_listing_output(deduped_records, out_path)
        if store_db:
            self.store_listing_records_in_db(deduped_records)
        return deduped_records

    def fetch_listing_url(self, url: str) -> SebiFetchedPage:
        response = self.fetch_http_or_browser(url)
        return SebiFetchedPage(url=str(response.request.url), html=response.text, transport=self.last_fetch_transport)

    def parse_listing_url_html(self, html: str, source_url: str) -> tuple[list[SebiListingRecord], Optional[int]]:
        soup = BeautifulSoup(html, "html.parser")
        table = soup.select_one(SEBI_LISTING_TABLE_SELECTOR)
        if table is None:
            raise RuntimeError(f"SEBI listing table `{SEBI_LISTING_TABLE_SELECTOR}` was not found on {source_url}")

        rows = table.select("tbody > tr")
        if not rows:
            rows = [row for row in table.select("tr") if row.select("td")]
        if not rows:
            raise RuntimeError(f"SEBI listing table `{SEBI_LISTING_TABLE_SELECTOR}` contained zero rows on {source_url}")

        scraped_at = datetime.now(timezone.utc).isoformat()
        parsed_records: list[SebiListingRecord] = []
        for row in rows:
            try:
                parsed_records.extend(self._parse_listing_url_row(row, source_url, scraped_at))
            except Exception as exc:
                logger.warning("Skipping malformed SEBI listing row: {}", exc)
        if not parsed_records:
            raise RuntimeError(f"SEBI listing URL parser detected zero records on {source_url}")

        return parsed_records, self._extract_total_records_from_listing_soup(soup)

    def filter_listing_records(
        self,
        records: list[SebiListingRecord],
        *,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        doc_type: Optional[str] = None,
    ) -> list[SebiListingRecord]:
        normalized_type = normalize_text(doc_type).casefold() if doc_type else None
        filtered: list[SebiListingRecord] = []
        for record in records:
            record_date = self._coerce_listing_date(record.date)
            if from_date and record_date < from_date:
                continue
            if to_date and record_date > to_date:
                continue
            if normalized_type and record.type.casefold() != normalized_type:
                continue
            filtered.append(record)
        return filtered

    def deduplicate_listing_records(self, records: list[SebiListingRecord]) -> list[SebiListingRecord]:
        deduped: list[SebiListingRecord] = []
        seen: set[tuple[str, str, str, str]] = set()
        for record in records:
            key = (
                record.date,
                record.type.casefold(),
                normalize_text(record.title).casefold(),
                record.link,
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(record)
        return deduped

    def archive_dedup_key(self, record: SebiListingRecord) -> tuple[str, str, str, str]:
        return (
            self._coerce_listing_date(record.date).isoformat(),
            normalize_text(record.type).casefold(),
            normalize_text(record.title).casefold(),
            record.link,
        )

    def write_listing_output(self, records: list[SebiListingRecord], out_path: str | Path) -> None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "date": record.date,
                "type": record.type,
                "title": record.title,
                "link": record.link,
                "source_url": record.source_url,
                "scraped_at": record.scraped_at,
            }
            for record in records
        ]
        if out_path.suffix.lower() == ".csv":
            self._write_csv_rows(out_path, rows, mode="w", write_header=True)
            return
        if out_path.suffix.lower() == ".json":
            self._write_json_payload(out_path, rows)
            return
        raise ValueError("Output path must end with .csv or .json")

    def append_listing_output(self, records: list[SebiListingRecord], out_path: str | Path) -> None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "date": record.date,
                "type": record.type,
                "title": record.title,
                "link": record.link,
                "source_url": record.source_url,
                "scraped_at": record.scraped_at,
            }
            for record in records
        ]
        if out_path.suffix.lower() == ".csv":
            write_header = not out_path.exists() or out_path.stat().st_size == 0
            self._write_csv_rows(out_path, rows, mode="a", write_header=write_header)
            return
        if out_path.suffix.lower() == ".json":
            existing = []
            if out_path.exists():
                existing = json.loads(out_path.read_text(encoding="utf-8"))
            existing.extend(rows)
            self._write_json_payload(out_path, existing)
            return
        raise ValueError("Output path must end with .csv or .json")

    def store_listing_records_in_db(self, records: list[SebiListingRecord]) -> None:
        for record in records:
            document = RegulatoryDocument(
                source=self.source,
                regulator=SEBI_SOURCE_LABEL,
                document_type=record.type,
                title=record.title,
                reference_no=None,
                published_date=date.fromisoformat(record.date),
                department=None,
                category="News Listing",
                url=record.link,
                pdf_url=None,
                pdf_sha256=None,
                text_content=None,
                scraped_at=datetime.now(timezone.utc),
            )
            self.persist_document(document)

    def scrape_archive_all_pages(
        self,
        *,
        url: str,
        out_path: str | Path,
        limit: Optional[int],
        from_date: Optional[date],
        to_date: Optional[date],
        doc_type: Optional[str],
        pages: Optional[int],
        all_pages: bool,
        resume: bool,
        checkpoint_path: str | Path | None,
        start_page: int | None,
        end_page: int | None,
        max_pages_this_run: int | None,
        delay_seconds: float,
        max_errors: int,
        retries: int,
        retry_base_delay: float,
        retry_max_delay: float,
        allow_partial: bool,
        headless: bool,
    ) -> list[SebiListingRecord]:
        del headless
        out_path = Path(out_path)
        fetched = self.execute_page_request_with_retry(
            page_number=1,
            operation=lambda: self.fetch_listing_url(url),
            retries=retries,
            base_delay=retry_base_delay,
            max_delay=retry_max_delay,
        )
        state = self.build_archive_state(fetched.html, fetched.url)

        checkpoint_file = Path(checkpoint_path) if checkpoint_path else Path(f"{out_path}.checkpoint.json")
        started_at = datetime.now(timezone.utc).isoformat()
        base_checkpoint = ArchiveCheckpoint(
            source_url=url,
            output_path=str(out_path),
            total_records_detected=state.total_records,
            page_size=state.page_size,
            last_completed_page=0,
            records_written=0,
            unique_records_written=0,
            started_at=started_at,
            updated_at=started_at,
            completed=False,
            errors=[],
        )

        if resume:
            existing_records = self.load_existing_output_records(out_path) if out_path.exists() else []
            checkpoint = self.load_checkpoint(checkpoint_file) if checkpoint_file.exists() else base_checkpoint
            if existing_records:
                resume_state = self.reconcile_archive_resume_state(
                    output_records=existing_records,
                    checkpoint=checkpoint,
                    source_url=url,
                    out_path=out_path,
                    total_records_detected=state.total_records,
                    page_size=state.page_size,
                )
                checkpoint.last_completed_page = resume_state.reconciled_last_completed_page
                checkpoint.records_written = resume_state.csv_rows_detected
                checkpoint.unique_records_written = resume_state.existing_dedupe_keys_loaded
                checkpoint.page_size = state.page_size
                checkpoint.total_records_detected = state.total_records
                checkpoint.source_url = url
                checkpoint.output_path = str(out_path)
                checkpoint.updated_at = datetime.now(timezone.utc).isoformat()
                if checkpoint.started_at == "":
                    checkpoint.started_at = checkpoint.updated_at
                self.save_checkpoint(checkpoint_file, checkpoint)

                effective_start_page = resume_state.resume_from_page
                if start_page is not None:
                    effective_start_page = max(effective_start_page, start_page)

                effective_end_page = self.resolve_archive_end_page(
                    effective_start_page=effective_start_page,
                    total_pages=state.total_pages,
                    pages=pages,
                    all_pages=all_pages,
                    end_page=end_page,
                    max_pages_this_run=max_pages_this_run,
                )

                print(f"CSV rows detected: {resume_state.csv_rows_detected}")
                print(f"checkpoint last_completed_page: {resume_state.checkpoint_last_completed_page}")
                if resume_state.checkpoint_last_completed_page != resume_state.reconciled_last_completed_page:
                    print(
                        "Warning: checkpoint and CSV disagree; preferring CSV row count "
                        f"({resume_state.completed_pages_from_output} full pages)."
                    )
                if resume_state.partial_page_rows:
                    print(
                        "Warning: existing output contains a partial final page with "
                        f"{resume_state.partial_page_rows} rows; resuming from page {resume_state.resume_from_page} "
                        "and relying on dedupe to avoid duplicates."
                    )
                print(f"reconciled last_completed_page: {resume_state.reconciled_last_completed_page}")
                print(f"resume_from_page: {effective_start_page}")
                print(f"existing dedupe keys loaded: {resume_state.existing_dedupe_keys_loaded}")
                print(f"output mode: {resume_state.output_mode}")
            else:
                checkpoint.page_size = state.page_size
                checkpoint.total_records_detected = state.total_records
                checkpoint.source_url = url
                checkpoint.output_path = str(out_path)
                checkpoint.updated_at = datetime.now(timezone.utc).isoformat()
                self.save_checkpoint(checkpoint_file, checkpoint)
                effective_start_page = max(1, start_page or (checkpoint.last_completed_page + 1))
                effective_end_page = self.resolve_archive_end_page(
                    effective_start_page=effective_start_page,
                    total_pages=state.total_pages,
                    pages=pages,
                    all_pages=all_pages,
                    end_page=end_page,
                    max_pages_this_run=max_pages_this_run,
                )
                print("CSV rows detected: 0")
                print(f"checkpoint last_completed_page: {checkpoint.last_completed_page}")
                print(f"reconciled last_completed_page: {checkpoint.last_completed_page}")
                print(f"resume_from_page: {effective_start_page}")
                print("existing dedupe keys loaded: 0")
                print("output mode: append")

            if max_pages_this_run is not None:
                print(f"max pages this run: {max_pages_this_run}")
            else:
                print("max pages this run: unlimited")
            planned_pages_this_run = max(0, effective_end_page - effective_start_page + 1)
            estimated_rows_this_run = planned_pages_this_run * state.page_size
            estimated_remaining_pages = max(0, state.total_pages - effective_end_page)
            print(f"expected end page: {effective_end_page}")
            print(f"estimated rows this run: {estimated_rows_this_run}")
            print(f"estimated remaining pages after this run: {estimated_remaining_pages}")
        else:
            checkpoint = base_checkpoint
            if out_path.exists():
                out_path.unlink()
            if checkpoint_file.exists():
                checkpoint_file.unlink()
            existing_records = []
            effective_start_page = max(1, start_page or 1)
            effective_end_page = self.resolve_archive_end_page(
                effective_start_page=effective_start_page,
                total_pages=state.total_pages,
                pages=pages,
                all_pages=all_pages,
                end_page=end_page,
                max_pages_this_run=max_pages_this_run,
            )

        seen_keys = {self.archive_dedup_key(record) for record in existing_records}
        collected_records = list(existing_records)
        duplicates_skipped = 0
        malformed_pages: list[int] = []

        if effective_start_page > effective_end_page:
            return collected_records

        errors = 0
        total_extracted = 0
        total_after_filter = 0
        current_page_state = state
        rows_appended_this_run = 0
        run_stopped_early = False

        for page_number in range(effective_start_page, effective_end_page + 1):
            try:
                page_html = self.fetch_archive_page_html_with_retry(
                    page_number=page_number,
                    source_url=url,
                    initial_fetched=fetched,
                    initial_state=state,
                    current_page_state=current_page_state,
                    retries=retries,
                    base_delay=retry_base_delay,
                    max_delay=retry_max_delay,
                )
            except Exception as exc:
                checkpoint.errors.append(f"page {page_number}: {exc.__class__.__name__}: {exc}")
                checkpoint.updated_at = datetime.now(timezone.utc).isoformat()
                self.save_checkpoint(checkpoint_file, checkpoint)
                resume_command = self.build_resume_command(
                    url=url,
                    out_path=out_path,
                    checkpoint_path=checkpoint_file,
                    delay_seconds=delay_seconds,
                    retries=retries,
                    retry_base_delay=retry_base_delay,
                    retry_max_delay=retry_max_delay,
                    next_page=page_number,
                )
                print(f"Page {page_number} failed after {retries} retries.")
                print(f"Resume with: {resume_command}")
                if allow_partial:
                    run_stopped_early = True
                    break
                raise RuntimeError(
                    f"Stopping after repeated failures on page {page_number}. Resume with: {resume_command}"
                ) from exc

            try:
                page_records, _ = self.parse_listing_url_html(page_html, url)
                if not page_records:
                    raise RuntimeError("zero rows")
            except Exception as exc:
                logger.warning("Page {} parse failed once: {}", page_number, exc)
                try:
                    time.sleep(delay_seconds)
                    retry_html = self.fetch_archive_page_html_with_retry(
                        page_number=page_number,
                        source_url=url,
                        initial_fetched=fetched,
                        initial_state=state,
                        current_page_state=current_page_state,
                        retries=retries,
                        base_delay=retry_base_delay,
                        max_delay=retry_max_delay,
                    )
                    page_records, _ = self.parse_listing_url_html(retry_html, url)
                    page_html = retry_html
                except Exception as retry_exc:
                    errors += 1
                    malformed_pages.append(page_number)
                    checkpoint.errors.append(f"page {page_number}: {retry_exc}")
                    checkpoint.updated_at = datetime.now(timezone.utc).isoformat()
                    self.save_checkpoint(checkpoint_file, checkpoint)
                    resume_command = self.build_resume_command(
                        url=url,
                        out_path=out_path,
                        checkpoint_path=checkpoint_file,
                        delay_seconds=delay_seconds,
                        retries=retries,
                        retry_base_delay=retry_base_delay,
                        retry_max_delay=retry_max_delay,
                        next_page=page_number,
                    )
                    print(f"Page {page_number} failed after retries/parse attempts.")
                    print(f"Resume with: {resume_command}")
                    if errors >= max_errors or not allow_partial:
                        raise RuntimeError(
                            f"Stopping after unrecoverable failure on page {page_number}. Resume with: {resume_command}"
                        ) from retry_exc
                    run_stopped_early = True
                    break

            total_extracted += len(page_records)
            filtered_page_records = self.filter_listing_records(
                page_records,
                from_date=from_date,
                to_date=to_date,
                doc_type=doc_type,
            )
            total_after_filter += len(filtered_page_records)
            new_records: list[SebiListingRecord] = []
            for record in filtered_page_records:
                key = self.archive_dedup_key(record)
                if key in seen_keys:
                    duplicates_skipped += 1
                    continue
                seen_keys.add(key)
                new_records.append(record)
                collected_records.append(record)
                rows_appended_this_run += 1
                if limit is not None and rows_appended_this_run >= limit:
                    break

            if new_records:
                self.append_listing_output(new_records, out_path)

            checkpoint.last_completed_page = page_number
            checkpoint.records_written = len(existing_records) + rows_appended_this_run
            checkpoint.unique_records_written = len(seen_keys)
            checkpoint.updated_at = datetime.now(timezone.utc).isoformat()
            self.save_checkpoint(checkpoint_file, checkpoint)

            if limit is not None and rows_appended_this_run >= limit:
                break

            current_page_state = self.build_archive_state_from_fragment(page_html, current_page_state)
            time.sleep(delay_seconds)

        filtered_collected = self.filter_listing_records(collected_records, from_date=from_date, to_date=to_date, doc_type=doc_type)
        if limit is not None:
            filtered_collected = filtered_collected[: len(existing_records) + rows_appended_this_run]

        requested_run_complete = checkpoint.last_completed_page >= effective_end_page
        checkpoint.completed = checkpoint.last_completed_page >= state.total_pages
        checkpoint.updated_at = datetime.now(timezone.utc).isoformat()
        self.save_checkpoint(checkpoint_file, checkpoint)

        if run_stopped_early and not allow_partial:
            raise RuntimeError("Archive scrape stopped before completion")
        if all_pages and effective_end_page >= state.total_pages and not checkpoint.completed and not allow_partial:
            raise RuntimeError("Archive scrape stopped before completion")
        if all_pages and not (from_date or to_date or doc_type):
            if checkpoint.unique_records_written < state.total_records and not allow_partial:
                raise RuntimeError(
                    f"Archive scrape ended with {checkpoint.unique_records_written} unique rows but page reported {state.total_records} total records"
                )
            if checkpoint.unique_records_written < state.total_records:
                logger.warning(
                    "Partial archive scrape: wrote {} unique rows vs detected total {} (duplicates skipped: {})",
                    checkpoint.unique_records_written,
                    state.total_records,
                    duplicates_skipped,
                )

        logger.info(
            "Archive scrape summary: extracted={}, after_filter={}, written={}, duplicates_skipped={}, pages_failed={}, run_complete={}, archive_complete={}",
            total_extracted,
            total_after_filter,
            rows_appended_this_run,
            duplicates_skipped,
            malformed_pages,
            requested_run_complete,
            checkpoint.completed,
        )
        remaining_pages = max(0, state.total_pages - checkpoint.last_completed_page)
        print(f"Completed pages {effective_start_page}-{checkpoint.last_completed_page}.")
        print(f"Rows appended: {rows_appended_this_run}.")
        print(f"New checkpoint last_completed_page: {checkpoint.last_completed_page}.")
        print(f"Remaining pages: {remaining_pages}.")
        print("Run the same command again to continue.")
        return filtered_collected

    def execute_page_request_with_retry(
        self,
        *,
        page_number: int,
        operation: Callable[[], Any],
        retries: int,
        base_delay: float,
        max_delay: float,
    ) -> Any:
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                return operation()
            except Exception as exc:
                if not self.is_retryable_http_exception(exc):
                    raise
                last_exc = exc
                if attempt >= retries:
                    break
                delay = self.compute_retry_delay(attempt, base_delay=base_delay, max_delay=max_delay)
                message = (
                    f"Page {page_number} failed with {exc.__class__.__name__}. "
                    f"Retry {attempt}/{retries} after {delay:.1f}s."
                )
                print(message)
                logger.warning(message)
                time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    def fetch_archive_page_html_with_retry(
        self,
        *,
        page_number: int,
        source_url: str,
        initial_fetched: SebiFetchedPage,
        initial_state: SebiPageState,
        current_page_state: SebiPageState,
        retries: int,
        base_delay: float,
        max_delay: float,
    ) -> str:
        def operation() -> str:
            if page_number == 1:
                return self.fetch_listing_url(source_url).html
            if page_number == 2:
                _, page_html = self.fetch_archive_page_fragment(initial_state, target_page=2)
                return page_html
            _, page_html = self.fetch_archive_page_fragment(current_page_state, target_page=page_number)
            return page_html

        if page_number == 1 and initial_fetched.html:
            return initial_fetched.html
        return self.execute_page_request_with_retry(
            page_number=page_number,
            operation=operation,
            retries=retries,
            base_delay=base_delay,
            max_delay=max_delay,
        )

    def build_resume_command(
        self,
        *,
        url: str,
        out_path: str | Path,
        checkpoint_path: str | Path,
        delay_seconds: float,
        retries: int,
        retry_base_delay: float,
        retry_max_delay: float,
        next_page: int,
    ) -> str:
        return (
            'py -3.13 -m main scrape-url --source sebi '
            f'--url "{url}" '
            f'--out "{out_path}" '
            '--resume '
            f'--checkpoint "{checkpoint_path}" '
            f'--start-page {next_page} '
            f'--delay-seconds {delay_seconds} '
            f'--retries {retries} '
            f'--retry-base-delay {retry_base_delay} '
            f'--retry-max-delay {retry_max_delay}'
        )

    def resolve_archive_end_page(
        self,
        *,
        effective_start_page: int,
        total_pages: int,
        pages: int | None,
        all_pages: bool,
        end_page: int | None,
        max_pages_this_run: int | None,
    ) -> int:
        if effective_start_page < 1:
            raise ValueError("effective_start_page must be >= 1")

        end_candidates: list[int] = []
        if pages is not None:
            end_candidates.append(effective_start_page + pages - 1)
        if max_pages_this_run is not None:
            end_candidates.append(effective_start_page + max_pages_this_run - 1)
        if end_page is not None:
            end_candidates.append(end_page)

        if all_pages:
            end_candidates.append(total_pages)
        elif not end_candidates:
            end_candidates.append(effective_start_page)

        return min(total_pages, min(end_candidates))

    def reconcile_archive_resume_state(
        self,
        *,
        output_records: list[SebiListingRecord],
        checkpoint: ArchiveCheckpoint,
        source_url: str,
        out_path: str | Path,
        total_records_detected: int,
        page_size: int,
    ) -> ArchiveResumeState:
        csv_rows_detected = len(output_records)
        completed_pages_from_output = csv_rows_detected // page_size
        partial_page_rows = csv_rows_detected % page_size
        checkpoint_last_completed_page = checkpoint.last_completed_page
        reconciled_last_completed_page = completed_pages_from_output

        checkpoint.source_url = source_url
        checkpoint.output_path = str(out_path)
        checkpoint.total_records_detected = total_records_detected
        checkpoint.page_size = page_size
        checkpoint.last_completed_page = reconciled_last_completed_page
        checkpoint.records_written = csv_rows_detected
        checkpoint.unique_records_written = len({self.archive_dedup_key(record) for record in output_records})

        return ArchiveResumeState(
            csv_rows_detected=csv_rows_detected,
            existing_dedupe_keys_loaded=checkpoint.unique_records_written,
            checkpoint_last_completed_page=checkpoint_last_completed_page,
            completed_pages_from_output=completed_pages_from_output,
            reconciled_last_completed_page=reconciled_last_completed_page,
            resume_from_page=reconciled_last_completed_page + 1,
            partial_page_rows=partial_page_rows,
            output_mode="append",
        )

    def _parse_listing_page(self, response: httpx.Response, listing_type: str) -> ListingPage:
        soup = self.parse_html(response)
        title = normalize_text(soup.title.get_text(" ", strip=True) if soup.title else "") or ""
        table = soup.select_one(SEBI_LISTING_TABLE_SELECTOR)
        if table is None:
            raise RuntimeError(f"SEBI {listing_type} listing table {SEBI_LISTING_TABLE_SELECTOR} was not found")

        rows = [self._parse_listing_row(row, response.url, listing_type) for row in table.select("tbody > tr")]
        rows = [row for row in rows if row is not None]
        if not rows:
            raise RuntimeError(f"SEBI {listing_type} listing page returned zero detected rows")

        total_records = self._extract_total_records_from_listing_soup(soup) or 0
        return ListingPage(
            listing_type=listing_type,
            title=title,
            total_records=total_records,
            rows=rows,
            response=response,
        )

    def _parse_listing_row(
        self,
        row: Tag,
        base_url: httpx.URL,
        listing_type: str,
    ) -> Optional[dict[str, Any]]:
        cells = row.find_all("td")
        if len(cells) < 2:
            return None
        anchor = cells[1].select_one("a.points")
        if anchor is None or not anchor.get("href"):
            raise RuntimeError(f"SEBI {listing_type} listing row is missing the detail link anchor")

        published_date_text = normalize_text(cells[0].get_text(" ", strip=True))
        title = normalize_text(anchor.get_text(" ", strip=True))
        if not published_date_text or not title:
            raise RuntimeError(f"SEBI {listing_type} listing row is missing date or title content")

        published_date = parse_indian_date(published_date_text)
        if published_date is None:
            raise RuntimeError(f"Could not parse SEBI published date: {published_date_text}")

        detail_url = self._normalize_detail_url(str(base_url), anchor["href"])
        return {
            "source": SEBI_SOURCE_LABEL,
            "regulator": SEBI_SOURCE_LABEL,
            "document_type": SEBI_LISTING_TYPES[listing_type]["document_type"],
            "published_date": published_date,
            "title": title,
            "detail_url": detail_url,
            "url": detail_url,
        }

    def _build_document_from_detail_response(
        self,
        record: dict[str, Any],
        response: httpx.Response,
    ) -> RegulatoryDocument:
        soup = self.parse_html(response)
        page_title = normalize_text(self._select_text(soup, SEBI_DETAIL_TITLE_SELECTOR))
        detail_date_text = normalize_text(self._select_text(soup, SEBI_DETAIL_DATE_SELECTOR))
        published_date = parse_indian_date(detail_date_text) if detail_date_text else record.get("published_date")
        if published_date is None:
            raise RuntimeError(f"Could not determine published date for SEBI detail page: {record['detail_url']}")

        reference_no = self._extract_reference_no(soup)
        pdf_url = self._extract_pdf_url(soup, str(response.url))

        return RegulatoryDocument(
            source=record.get("source", SEBI_SOURCE_LABEL),
            regulator=record.get("regulator", SEBI_SOURCE_LABEL),
            document_type=record["document_type"],
            title=page_title or record["title"],
            reference_no=reference_no,
            published_date=published_date,
            department=None,
            category="Legal",
            url=record["detail_url"],
            pdf_url=pdf_url,
            text_content=None,
            scraped_at=datetime.now(timezone.utc),
        )

    def _parse_listing_url_row(self, row: Tag, source_url: str, scraped_at: str) -> list[SebiListingRecord]:
        cells = row.find_all("td")
        if len(cells) < 3:
            raise RuntimeError("Expected at least 3 columns (Date, Type, Title) in SEBI listing row")

        raw_date = normalize_text(cells[0].get_text(" ", strip=True))
        record_type = normalize_text(cells[1].get_text(" ", strip=True))
        title_cell = cells[2]
        if not raw_date or not record_type:
            raise RuntimeError("SEBI listing row is missing Date or Type content")

        normalized_date = parse_indian_date(raw_date)
        if normalized_date is None:
            raise RuntimeError(f"Unable to parse SEBI listing date: {raw_date}")

        anchor = title_cell.select_one("a.points[href]") or title_cell.select_one("a[href]")
        if anchor is None:
            raise RuntimeError("SEBI listing row is missing the Title anchor")
        title = normalize_text(anchor.get_text(" ", strip=True))
        href = anchor.get("href")
        if not title or not href:
            raise RuntimeError("SEBI title anchor is missing title text or href")
        return [
            SebiListingRecord(
                date=normalized_date.isoformat(),
                type=record_type,
                title=title,
                link=self._normalize_detail_url(source_url, href),
                source_url=source_url,
                scraped_at=scraped_at,
                raw_date=raw_date,
            )
        ]

    def _extract_pdf_url(self, soup: BeautifulSoup, base_url: str) -> Optional[str]:
        iframe = soup.select_one("iframe[src]")
        if iframe is not None and iframe.get("src"):
            return self._resolve_pdf_candidate(base_url, iframe["src"])

        for selector in ("a[href]", "embed[src]", "object[data]"):
            for node in soup.select(selector):
                attr = "href" if node.has_attr("href") else "src" if node.has_attr("src") else "data"
                candidate = node.get(attr)
                resolved = self._resolve_pdf_candidate(base_url, candidate)
                if resolved:
                    return resolved
        return None

    def _resolve_pdf_candidate(self, base_url: str, candidate: Optional[str]) -> Optional[str]:
        if not candidate:
            return None
        absolute = urljoin(base_url, candidate)
        parsed = urlparse(absolute)
        query_file = parse_qs(parsed.query).get("file")
        if query_file:
            return query_file[0]
        if absolute.lower().endswith(".pdf"):
            return absolute
        if "attachdocs" in absolute.lower():
            return absolute
        return None

    def _extract_reference_no(self, soup: BeautifulSoup) -> Optional[str]:
        id_area = soup.select_one(".id_area")
        if id_area is None:
            return None
        spans = [normalize_text(span.get_text(" ", strip=True)) for span in id_area.select("span")]
        spans = [span for span in spans if span]
        if len(spans) >= 2 and "circular no" in spans[0].lower():
            return clean_reference_no(spans[1])
        return None

    def _normalize_detail_url(self, page_url: str, href: str) -> str:
        return urljoin(page_url, href.strip())

    def _extract_total_records_from_listing_soup(self, soup: BeautifulSoup) -> Optional[int]:
        for node in soup.select(SEBI_TOTAL_RECORDS_SELECTOR):
            text = normalize_text(node.get_text(" ", strip=True))
            if text and " of " in text and " records" in text:
                number_text = text.split(" of ", 1)[1].split(" records", 1)[0].strip()
                if number_text.isdigit():
                    return int(number_text)
        return None

    def _pagination_not_implemented(self, url: str) -> NotImplementedError:
        return NotImplementedError(
            "SEBI multi-page traversal is not safely implemented for generic listing URLs yet. "
            f"Inspected URL: {url}. Observed visible pagination and total-count text on the rendered page. "
            "Related SEBI listing pages expose hidden `nextValue` inputs and JavaScript pager calls such as "
            "`searchFormNewsList('n', token)`, but the `doListingAll=yes` page-turn payload and hidden fields "
            "could not be confirmed from live raw HTML in this environment because direct Python HTTP received 403 "
            "and Playwright fallback is not installed."
        )

    def is_do_listing_all_url(self, url: str) -> bool:
        return SEBI_ARCHIVE_URL_FLAG in url

    def build_archive_state(self, html: str, source_url: str) -> SebiPageState:
        soup = BeautifulSoup(html, "html.parser")
        form = soup.select_one("form[name='homeForm']")
        if form is None:
            raise RuntimeError("SEBI archive form `homeForm` not found")

        records, total_records = self.parse_listing_url_html(html, source_url)
        if total_records is None:
            raise RuntimeError("SEBI archive total record count not visible")
        page_size = len(records)
        total_pages = math.ceil(total_records / page_size)
        hidden_fields = {
            node.get("name"): node.get("value", "")
            for node in form.select("input[type='hidden'][name]")
        }
        payload = {
            "search": self._input_value(form, "search"),
            "fromDate": self._input_value(form, "fromDate"),
            "toDate": self._input_value(form, "toDate"),
            "deptId": self._default_select_value(form, "deptId"),
            "sid": self._default_select_value(form, "sid"),
            "ssid": self._default_select_value(form, "ssid"),
            "smid": self._default_select_value(form, "smid"),
            "cid": self._default_select_value(form, "cid"),
            "sText": self._default_select_text(form, "sid"),
            "ssText": self._default_select_text(form, "ssid"),
            "smText": self._default_select_text(form, "smid"),
            "cText": self._default_select_text(form, "cid"),
        }
        pagination_controls = self.extract_pagination_controls(soup)
        current_page = self.extract_current_page(soup)
        next_value = hidden_fields.get("nextValue", str(current_page))

        return SebiPageState(
            source_url=source_url,
            form_action=urljoin(source_url, form.get("action", "")),
            ajax_url=SEBI_ARCHIVE_AJAX_URL,
            total_records=total_records,
            page_size=page_size,
            total_pages=total_pages,
            current_page=current_page,
            next_value=next_value,
            form_payload=payload,
            ajax_headers=self._build_archive_ajax_headers(source_url),
            pagination_controls=pagination_controls,
            hidden_fields=hidden_fields,
        )

    def build_archive_state_from_fragment(self, fragment_html: str, previous_state: SebiPageState) -> SebiPageState:
        wrapped = f"<html><body>{fragment_html}</body></html>"
        soup = BeautifulSoup(wrapped, "html.parser")
        records, total_records = self.parse_listing_url_html(wrapped, previous_state.source_url)
        hidden_fields = {
            node.get("name"): node.get("value", "")
            for node in soup.select("input[type='hidden'][name]")
        }
        return SebiPageState(
            source_url=previous_state.source_url,
            form_action=previous_state.form_action,
            ajax_url=previous_state.ajax_url,
            total_records=total_records or previous_state.total_records,
            page_size=len(records),
            total_pages=previous_state.total_pages,
            current_page=self.extract_current_page(soup),
            next_value=hidden_fields.get("nextValue", previous_state.next_value),
            form_payload=dict(previous_state.form_payload),
            ajax_headers=dict(previous_state.ajax_headers),
            pagination_controls=self.extract_pagination_controls(soup),
            hidden_fields=hidden_fields,
        )

    def extract_pagination_controls(self, soup: BeautifulSoup) -> list[dict[str, str]]:
        controls: list[dict[str, str]] = []
        for anchor in soup.select(".pagination_outer a"):
            href = anchor.get("href", "")
            label = normalize_text(anchor.get_text(" ", strip=True)) or anchor.get("title") or ""
            control = {"label": label, "href": href}
            match = SEBI_PAGINATION_CALL_RE.search(href)
            if match:
                control["next"] = match.group("next")
                control["token"] = match.group("token")
            controls.append(control)
        return controls

    def extract_current_page(self, soup: BeautifulSoup) -> int:
        active = soup.select_one(".pagination_outer a.active")
        if active is not None:
            active_text = normalize_text(active.get_text(" ", strip=True))
            if active_text and active_text.isdigit():
                return int(active_text)
        range_text = soup.select_one(".pagination_inner p")
        if range_text is not None:
            text = normalize_text(range_text.get_text(" ", strip=True))
            if text and " to " in text:
                start = text.split(" to ", 1)[0].replace("\xa0", "").strip()
                if start.isdigit():
                    start_index = int(start)
                    page_size = max(1, len(soup.select("table#sample_1 tr td:first-child")))
                    return ((start_index - 1) // page_size) + 1
        raise RuntimeError("Could not determine current SEBI archive page")

    def fetch_archive_page_fragment(self, state: SebiPageState, target_page: int) -> tuple[dict[str, Any], str]:
        if target_page < 2:
            raise ValueError("target_page must be >= 2")

        if target_page == state.current_page + 1:
            next_control = next(
                (
                    control for control in state.pagination_controls
                    if control.get("token") == "-1" and control.get("next") == "n"
                ),
                None,
            )
            if next_control and state.current_page != 1:
                next_arg = next_control["next"]
                token = next_control["token"]
            else:
                next_arg = "n"
                token = str(target_page - 1)
        else:
            next_arg = "n"
            token = str(target_page - 1)

        payload = {
            "nextValue": state.next_value,
            "next": next_arg,
            "search": state.form_payload["search"],
            "fromDate": state.form_payload["fromDate"],
            "toDate": state.form_payload["toDate"],
            "deptId": state.form_payload["deptId"],
            "sid": state.form_payload["sid"],
            "ssid": state.form_payload["ssid"],
            "smid": state.form_payload["smid"],
            "cid": state.form_payload["cid"],
            "sText": state.form_payload["sText"],
            "ssText": state.form_payload["ssText"],
            "smText": state.form_payload["smText"],
            "cText": state.form_payload["cText"],
            "doDirect": token,
        }
        response = self.client.post(state.ajax_url, data=payload, headers=state.ajax_headers)
        response.raise_for_status()
        if "#@#" not in response.text:
            raise RuntimeError(f"Unexpected SEBI archive AJAX response while fetching page {target_page}")
        fragment = response.text.split("#@#", 1)[0]
        request_details = {
            "page": target_page,
            "method": "POST",
            "url": state.ajax_url,
            "query_params": {},
            "form_payload": payload,
            "headers": state.ajax_headers,
            "response_status": response.status_code,
        }
        return request_details, fragment

    def load_checkpoint(self, checkpoint_path: str | Path) -> ArchiveCheckpoint:
        payload = json.loads(Path(checkpoint_path).read_text(encoding="utf-8"))
        return ArchiveCheckpoint(**payload)

    def save_checkpoint(self, checkpoint_path: str | Path, checkpoint: ArchiveCheckpoint) -> None:
        Path(checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
        self._write_text_with_retry(Path(checkpoint_path), json.dumps(asdict(checkpoint), indent=2))

    def load_existing_output_records(self, out_path: str | Path) -> list[SebiListingRecord]:
        out_path = Path(out_path)
        if not out_path.exists():
            return []
        if out_path.suffix.lower() == ".csv":
            with open(out_path, "r", newline="", encoding="utf-8") as file_obj:
                reader = csv.DictReader(file_obj)
                return [
                    SebiListingRecord(
                        date=self._coerce_listing_date(row["date"]).isoformat(),
                        type=row["type"],
                        title=row["title"],
                        link=row["link"],
                        source_url=row["source_url"],
                        scraped_at=row["scraped_at"],
                    )
                    for row in reader
                ]
        if out_path.suffix.lower() == ".json":
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            return [
                    SebiListingRecord(
                        date=self._coerce_listing_date(row["date"]).isoformat(),
                        type=row["type"],
                        title=row["title"],
                        link=row["link"],
                        source_url=row["source_url"],
                        scraped_at=row["scraped_at"],
                )
                for row in payload
            ]
        raise ValueError("Output path must end with .csv or .json")

    def _input_value(self, form: Tag, name: str) -> str:
        node = form.select_one(f"input[name='{name}']")
        return node.get("value", "") if node is not None else ""

    def _default_select_value(self, form: Tag, name: str) -> str:
        select = form.select_one(f"select[name='{name}']")
        if select is None:
            return "-1"
        selected = select.select_one("option[selected]") or select.select_one("option")
        return selected.get("value", "-1") if selected is not None else "-1"

    def _default_select_text(self, form: Tag, name: str) -> str:
        select = form.select_one(f"select[name='{name}']")
        if select is None:
            return ""
        selected = select.select_one("option[selected]") or select.select_one("option")
        return normalize_text(selected.get_text(" ", strip=True)) or "" if selected is not None else ""

    def _build_archive_ajax_headers(self, source_url: str) -> dict[str, str]:
        return {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://www.sebi.gov.in",
            "Referer": source_url,
            "X-Requested-With": "XMLHttpRequest",
        }

    @retry(retry=retry_if_exception_type(PermissionError), wait=wait_fixed(1), stop=stop_after_attempt(5), reraise=True)
    def _write_csv_rows(
        self,
        out_path: Path,
        rows: list[dict[str, str]],
        *,
        mode: str,
        write_header: bool,
    ) -> None:
        with open(out_path, mode, newline="", encoding="utf-8") as file_obj:
            writer = csv.DictWriter(
                file_obj,
                fieldnames=["date", "type", "title", "link", "source_url", "scraped_at"],
            )
            if write_header:
                writer.writeheader()
            writer.writerows(rows)

    @retry(retry=retry_if_exception_type(PermissionError), wait=wait_fixed(1), stop=stop_after_attempt(5), reraise=True)
    def _write_json_payload(self, out_path: Path, payload: list[dict[str, str]]) -> None:
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @retry(retry=retry_if_exception_type(PermissionError), wait=wait_fixed(1), stop=stop_after_attempt(5), reraise=True)
    def _write_text_with_retry(self, out_path: Path, content: str) -> None:
        out_path.write_text(content, encoding="utf-8")

    def _coerce_listing_date(self, value: str) -> date:
        try:
            return date.fromisoformat(value)
        except ValueError:
            parsed = parse_indian_date(value)
            if parsed is None:
                raise
            return parsed

    @staticmethod
    def _select_text(soup: BeautifulSoup, selector: str) -> Optional[str]:
        node = soup.select_one(selector)
        if node is None:
            return None
        return node.get_text(" ", strip=True)
