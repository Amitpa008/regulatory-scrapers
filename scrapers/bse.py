from __future__ import annotations

import csv
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from dateutil.relativedelta import relativedelta
from loguru import logger

from extraction.metadata_cleaner import normalize_text
from models import RegulatoryDocument
from scrapers.base import BaseScraper


BSE_SOURCE_LABEL = "BSE"
BSE_HOME_URL = "https://www.bseindia.com/"
BSE_NOTICES_PAGE_URL = "https://www.bseindia.com/markets/marketinfo/noticescirculars?id=0&txtscripcd=&pagecont=&subject="
BSE_ARCHIVE_URL = "https://www.bseindia.com/markets/MarketInfo/NoticesCirculars_archive?id=0&txtscripcd="
BSE_ARCHIVE_BETA_URL = "https://beta.bseindia.com/markets/MarketInfo/NoticesCircularsArchive.aspx?id=0&pagecont=&subject=&txtscripcd="
BSE_MOBILE_CURRENT_URL = "https://m.bseindia.com/ncp.aspx"
BSE_MOBILE_ARCHIVE_URL = "https://m.bseindia.com/NCPArchive.aspx"
BSE_CURRENT_API_URL = "https://api.bseindia.com/BseIndiaAPI/api/getCurrPreNextNoticesData_New/w"
BSE_CURRENT_AUTO_DATE_API_URL = "https://api.bseindia.com/BseIndiaAPI/api/getautodate_New/w"
BSE_FILTER_API_URL = "https://api.bseindia.com/BseIndiaAPI/api/getFillNoticesDDL_New/w"
EXPECTED_OUTPUT_HEADERS = ["date", "subject", "circular_no", "link", "source_url", "scraped_at"]
UNRESOLVED_OUTPUT_HEADERS = ["raw_date", "subject", "circular_no", "link", "source_url", "scraped_at", "reason"]
ARCHIVE_MAX_RANGE_YEARS = 3
ARCHIVE_EXPORT_CHUNK_MONTHS = 3


@dataclass
class BSENoticeRecord:
    date: str
    subject: str
    circular_no: str
    link: str
    source_url: str
    scraped_at: str
    page_url: Optional[str] = None
    raw_date: Optional[str] = None


@dataclass
class BSEEndpointResult:
    label: str
    url: str
    method: str
    status_code: Optional[int]
    content_type: Optional[str]
    response_size: int
    format: str
    record_count: int
    sample_records: list[dict[str, Any]]
    keys_or_headers: list[str]
    error: Optional[str] = None


@dataclass
class BSEChunk:
    index: int
    label: str
    kind: str
    from_date: str
    to_date: str


@dataclass
class BSECheckpoint:
    source_url: str
    output_path: str
    newest_available_date: Optional[str]
    oldest_available_date: Optional[str]
    total_records_detected: Optional[int]
    chunk_strategy: str
    last_completed_chunk: int
    records_written: int
    unique_records_written: int
    started_at: str
    updated_at: str
    completed: bool
    errors: list[str]


@dataclass
class BSEUnresolvedNoticeRecord:
    raw_date: str
    subject: str
    circular_no: str
    link: str
    source_url: str
    scraped_at: str
    reason: str
    page_url: Optional[str] = None


class BSEScraper(BaseScraper):
    source = "bse"
    regulator = BSE_SOURCE_LABEL

    def __init__(self, config: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.config = config
        self.last_fetch_transport = "httpx"

    def fetch_index(self, from_date: date, to_date: date) -> Any:
        return self.fetch_page(BSE_NOTICES_PAGE_URL)

    def parse_listing(self, response: Any) -> list[dict[str, Any]]:
        text = response.text if isinstance(response, httpx.Response) else str(response)
        records = self.parse_notice_records(text, BSE_NOTICES_PAGE_URL, source_url=BSE_NOTICES_PAGE_URL)
        return [
            {
                "title": record.subject,
                "url": record.link,
                "document_type": "Notice/Circular",
                "reference_no": record.circular_no or None,
                "published_date": date.fromisoformat(record.date),
                "department": None,
                "category": "Notices & Circulars",
                "pdf_url": None,
            }
            for record in records
        ]

    def fetch_document(self, record: dict[str, Any]) -> RegulatoryDocument:
        return RegulatoryDocument(
            source=self.source,
            regulator=self.regulator,
            document_type=record.get("document_type", "Notice/Circular"),
            title=record["title"],
            reference_no=record.get("reference_no"),
            published_date=record["published_date"],
            department=record.get("department"),
            category=record.get("category"),
            url=record["url"],
            pdf_url=record.get("pdf_url"),
            pdf_sha256=None,
            text_content=None,
            scraped_at=datetime.now(timezone.utc),
        )

    def inspect_notices(self, url: str) -> dict[str, Any]:
        fixture_dir = Path("tests/fixtures/bse")
        fixture_dir.mkdir(parents=True, exist_ok=True)

        current_response = self.fetch_page(BSE_NOTICES_PAGE_URL, raise_for_status=False)
        archive_response = self.fetch_page(BSE_ARCHIVE_BETA_URL, raise_for_status=False)
        mobile_archive_response = self.fetch_page(BSE_MOBILE_ARCHIVE_URL, raise_for_status=False)
        current_payload = self.fetch_json(BSE_CURRENT_API_URL, params={"flag": ""})

        (fixture_dir / "notices_circulars.html").write_text(current_response.text, encoding="utf-8")
        (fixture_dir / "notices_archive_mobile.html").write_text(mobile_archive_response.text, encoding="utf-8")
        (fixture_dir / "notices_circulars_beta.html").write_text(archive_response.text, encoding="utf-8")
        (fixture_dir / "current_notices_api_sample.json").write_text(json.dumps(current_payload, indent=2), encoding="utf-8")

        archive_soup = BeautifulSoup(archive_response.text, "html.parser")
        current_title = BeautifulSoup(current_response.text, "html.parser").title
        page_title = current_title.get_text(" ", strip=True) if current_title else ""
        result = {
            "page_title": page_title,
            "notice_table_present": bool(archive_soup.select("#ContentPlaceHolder1_GridView1")),
            "date_filters_present": bool(archive_soup.select("#ContentPlaceHolder1_txtDate, #ContentPlaceHolder1_txtTodate")),
            "notice_no_search_present": bool(archive_soup.select("#ContentPlaceHolder1_txtNoticeNo")),
            "segment_filters_present": bool(archive_soup.select("#ContentPlaceHolder1_ddlSegment, #ContentPlaceHolder1_ddlCategory, #ContentPlaceHolder1_ddlDep")),
            "archive_link_present": "NoticesCirculars_archive" in current_response.text,
            "api_urls_found": sorted(
                {
                    BSE_CURRENT_API_URL,
                    BSE_CURRENT_AUTO_DATE_API_URL,
                    f"{BSE_FILTER_API_URL}?flag=C",
                    f"{BSE_FILTER_API_URL}?flag=D",
                    f"{BSE_FILTER_API_URL}?flag=S",
                }
            ),
            "sample_records": [asdict(record) for record in self.parse_current_api_records(current_payload, url)[:3]],
            "beta_form_fields": self.collect_form_metadata(archive_soup),
        }

        print(f"Page title: {page_title}")
        print(f"Notice table present: {result['notice_table_present']}")
        print(f"Date filters present: {result['date_filters_present']}")
        print(f"Notice No. search field present: {result['notice_no_search_present']}")
        print(f"Segment/Category/Department filters present: {result['segment_filters_present']}")
        print(f"Archive/prior-notices link present: {result['archive_link_present']}")
        print("API URLs found:")
        for item in result["api_urls_found"]:
            print(item)
        print("Sample records:")
        for record in result["sample_records"]:
            print(f"{record['date']} | {record['subject']} | {record['circular_no']} | {record['link']}")
        return result

    def inspect_browser_archive(self, url: str, *, headless: bool) -> dict[str, Any]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Playwright is required for inspect-bse-browser-archive") from exc

        fixture_dir = Path("tests/fixtures/bse")
        fixture_dir.mkdir(parents=True, exist_ok=True)
        current_html_path = fixture_dir / "browser_current_page.html"
        archive_html_path = fixture_dir / "browser_archive_page.html"
        search_html_path = fixture_dir / "archive_search_result_sample.html"
        network_capture_path = fixture_dir / "browser_network_capture.json"
        current_screenshot_path = fixture_dir / "browser_screenshot_current.png"
        archive_screenshot_path = fixture_dir / "browser_screenshot_archive.png"

        requests: list[dict[str, Any]] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=headless)
            context = browser.new_context(
                user_agent=self.headers["User-Agent"],
                locale="en-IN",
                viewport={"width": 1440, "height": 900},
            )
            page = context.new_page()

            def capture_request(request: Any) -> None:
                requests.append(
                    {
                        "method": request.method,
                        "url": request.url,
                        "resource_type": request.resource_type,
                        "post_data": request.post_data,
                    }
                )

            page.on("request", capture_request)

            page.goto(BSE_HOME_URL, wait_until="domcontentloaded", timeout=120000)
            page.goto(url, wait_until="networkidle", timeout=120000)
            current_html = page.content()
            current_html_path.write_text(current_html, encoding="utf-8")
            page.screenshot(path=str(current_screenshot_path), full_page=True)

            current_title = page.title()
            archive_link_locator = page.locator('a[href*="NoticesCirculars_archive"]').first
            archive_link = archive_link_locator.get_attribute("href") if archive_link_locator.count() else None

            archive_url = urljoin(url, archive_link) if archive_link else BSE_ARCHIVE_URL
            if archive_link:
                page.goto(archive_url, wait_until="networkidle", timeout=120000)
            page.goto(BSE_ARCHIVE_BETA_URL, wait_until="networkidle", timeout=120000)
            archive_html = page.content()
            archive_html_path.write_text(archive_html, encoding="utf-8")
            page.screenshot(path=str(archive_screenshot_path), full_page=True)
            archive_title = page.title()

            archive_soup = BeautifulSoup(archive_html, "html.parser")
            form_info = self.collect_form_metadata(archive_soup)
            initial_rows = [self.record_to_output_row(record) for record in self.parse_notice_records(archive_html, archive_url, source_url=url)[:10]]

            sample_ranges = [
                ("current_month", date(2026, 5, 1), date(2026, 5, 16)),
                ("previous_month", date(2026, 4, 1), date(2026, 4, 30)),
                ("older_month", date(2026, 3, 1), date(2026, 3, 31)),
            ]
            sample_results: list[dict[str, Any]] = []
            for label, range_start, range_end in sample_ranges:
                before = len(requests)
                page.goto(BSE_ARCHIVE_BETA_URL, wait_until="networkidle", timeout=120000)
                page.fill("#ContentPlaceHolder1_txtDate", range_start.strftime("%d/%m/%Y"))
                page.fill("#ContentPlaceHolder1_txtTodate", range_end.strftime("%d/%m/%Y"))
                page.click("#ContentPlaceHolder1_btnSubmit")
                page.wait_for_load_state("networkidle", timeout=120000)
                result_html = page.content()
                if label == "older_month":
                    search_html_path.write_text(result_html, encoding="utf-8")
                parsed_rows = self.parse_notice_records(result_html, page.url, source_url=url)
                sample_results.append(
                    {
                        "label": label,
                        "date_range": [range_start.isoformat(), range_end.isoformat()],
                        "row_count": len(parsed_rows),
                        "rows": [self.record_to_output_row(record) for record in parsed_rows[:5]],
                        "requests": requests[before:],
                        "result_kind": "html_postback",
                    }
                )

            browser.close()

        js_urls = sorted({item for item in self.extract_urls_from_html(archive_html) if item.lower().endswith((".js", ".aspx", ".pdf"))})
        network_capture_path.write_text(json.dumps(sample_results + [{"requests": requests}], indent=2), encoding="utf-8")

        print(f"Page title: {current_title}")
        print(f"Archive link detected: {archive_link or BSE_ARCHIVE_URL}")
        print(f"Search form action: {form_info['form_action']}")
        print(f"Hidden fields: {json.dumps(form_info['hidden_fields'], indent=2)}")
        print(f"Date field names: {form_info['date_field_names']}")
        print(f"Notice no field name: {form_info['notice_no_field_name']}")
        print(f"Subject field name: {form_info['subject_field_name']}")
        print(f"Submit/search button name: {form_info['submit_button_name']}")
        print(f"Grid/table selector: {form_info['grid_selector']}")
        print(f"Pagination controls: {form_info['pagination_controls']}")
        print("JavaScript URLs:")
        for item in js_urls[:20]:
            print(item)
        print("First 10 rendered rows:")
        for row in initial_rows:
            print(f"{row['date']} | {row['subject']} | {row['circular_no']} | {row['link']}")
        for sample in sample_results:
            print(f"Search {sample['label']} {sample['date_range'][0]} to {sample['date_range'][1]} -> {sample['row_count']} rows")
            for row in sample["rows"]:
                print(f"{row['date']} | {row['subject']} | {row['circular_no']} | {row['link']}")
            relevant_request = next(
                (
                    request
                    for request in sample["requests"]
                    if request["url"].startswith(BSE_ARCHIVE_BETA_URL) and request["method"] == "POST"
                ),
                None,
            )
            print(f"Request: {json.dumps(relevant_request, indent=2) if relevant_request else 'No matching POST request captured'}")

        return {
            "current_title": current_title,
            "archive_title": archive_title,
            "archive_link": archive_link or BSE_ARCHIVE_URL,
            "form_info": form_info,
            "sample_results": sample_results,
            "network_capture_path": str(network_capture_path),
        }

    def discover_notice_range(self, url: str, *, headless: bool = True) -> dict[str, Any]:
        del headless
        boundaries = self.discover_archive_boundaries(url)
        archive_newest = boundaries["archive_newest"]
        current_records = self.fetch_current_records(min_date=archive_newest + timedelta(days=1), source_url=url)
        if not current_records:
            raise RuntimeError("BSE current notices API returned zero rows")
        oldest_record = boundaries["oldest_record"]
        earliest_records = boundaries["earliest_records"]
        older_unparseable_range = boundaries["older_unparseable_range"]

        result = {
            "working_endpoint_or_page_flow": {
                "current_api": BSE_CURRENT_API_URL,
                "archive_form": BSE_ARCHIVE_BETA_URL,
            },
            "working_query_params_or_form_fields": {
                "current_api": ["flag"],
                "archive_form": [
                    "ctl00$ContentPlaceHolder1$txtDate",
                    "ctl00$ContentPlaceHolder1$txtTodate",
                    "ctl00$ContentPlaceHolder1$btnSubmit",
                    "__VIEWSTATE",
                    "__EVENTVALIDATION",
                    "__VIEWSTATEGENERATOR",
                ],
            },
            "direct_http_worked": True,
            "playwright_used": False,
            "newest_available_notice_date": max(date.fromisoformat(record.date) for record in current_records).isoformat(),
            "oldest_available_notice_date": oldest_record.date,
            "total_count": None,
            "sample_earliest_records": [asdict(record) for record in earliest_records],
            "limitation": (
                "BSE does not expose a public total-count field for archive search results. "
                "Current notices are served by a JSON endpoint, while archive notices are served by ASP.NET HTML postbacks "
                "with a 3-year date-range validation rule."
                + (
                    f" Older archive rows also exist in the window {older_unparseable_range[0].isoformat()} to {older_unparseable_range[1].isoformat()}, "
                    "but those listing rows do not expose an exact day-level date in the public listing, so they cannot be normalized to YYYY-MM-DD without opening detail pages."
                    if older_unparseable_range
                    else ""
                )
            ),
        }

        print(f"Working endpoint or page-flow: {json.dumps(result['working_endpoint_or_page_flow'])}")
        print(f"Working query params or form fields: {json.dumps(result['working_query_params_or_form_fields'])}")
        print(f"Direct HTTP worked: {result['direct_http_worked']}")
        print(f"Playwright used: {result['playwright_used']}")
        print(f"Newest available notice date: {result['newest_available_notice_date']}")
        print(f"Oldest available notice date: {result['oldest_available_notice_date']}")
        print(f"Total count: {result['total_count']}")
        print("Sample earliest 5 records:")
        for record in earliest_records:
            print(f"{record.date} | {record.subject} | {record.circular_no} | {record.link}")
        print(f"Limitation: {result['limitation']}")
        return result

    def scrape_listing_url(
        self,
        *,
        url: str,
        out_path: str | Path,
        from_date: date | None = None,
        to_date: date | None = None,
        resume: bool = False,
        checkpoint_path: str | Path | None = None,
        max_chunks_this_run: int | None = None,
        delay_seconds: float = 1.5,
        retries: int = 5,
        retry_base_delay: float = 3.0,
        retry_max_delay: float = 60.0,
        all_available: bool = False,
        allow_partial: bool = False,
        headless: bool = True,
    ) -> list[BSENoticeRecord]:
        del headless
        out_path = Path(out_path)
        checkpoint_file = Path(checkpoint_path) if checkpoint_path else Path(f"{out_path}.checkpoint.json")
        self.ensure_output_writable(out_path, resume=resume)

        boundaries: dict[str, Any] | None = None
        archive_newest = self.fetch_archive_newest_date()
        newest_current_date = self.fetch_current_latest_date()
        if all_available and from_date is None:
            boundaries = self.discover_archive_boundaries(url)
            from_date = date.fromisoformat(boundaries["oldest_record"].date)
        if from_date is None:
            from_date = min(archive_newest, newest_current_date)
        if to_date is None:
            to_date = newest_current_date
        if from_date > to_date:
            raise ValueError("from_date must be less than or equal to to_date")

        current_range_start = max(from_date, archive_newest + timedelta(days=1))
        current_records = self.fetch_current_records(min_date=current_range_start, source_url=url) if current_range_start <= to_date else []

        archive_range_end = min(to_date, archive_newest)
        archive_chunks = self.build_archive_chunks(from_date, archive_range_end) if from_date <= archive_range_end else []

        existing_records = self.load_existing_output_records(out_path) if (resume and out_path.exists()) else []
        csv_row_count = len(existing_records)
        started_at = datetime.now(timezone.utc).isoformat()
        checkpoint = BSECheckpoint(
            source_url=url,
            output_path=str(out_path),
            newest_available_date=newest_current_date.isoformat(),
            oldest_available_date=from_date.isoformat() if all_available else None,
            total_records_detected=None,
            chunk_strategy="current_api_plus_archive_form",
            last_completed_chunk=0,
            records_written=0,
            unique_records_written=0,
            started_at=started_at,
            updated_at=started_at,
            completed=False,
            errors=[],
        )
        if resume and checkpoint_file.exists():
            checkpoint = self.load_checkpoint(checkpoint_file)
            if csv_row_count != checkpoint.records_written:
                print(
                    "Warning: CSV and checkpoint disagree. "
                    f"CSV rows={csv_row_count}, checkpoint rows={checkpoint.records_written}. Preferring CSV for dedupe safety."
                )
                checkpoint.records_written = csv_row_count
                checkpoint.unique_records_written = len({self.record_dedup_key(record) for record in existing_records})
                checkpoint.updated_at = datetime.now(timezone.utc).isoformat()
                self.save_checkpoint(checkpoint_file, checkpoint)
        elif not resume:
            if out_path.exists():
                out_path.unlink()
            if checkpoint_file.exists():
                checkpoint_file.unlink()
            existing_records = []
            csv_row_count = 0

        chunks: list[BSEChunk] = []
        if current_records:
            chunks.append(
                BSEChunk(
                    index=1,
                    label="current_api",
                    kind="current_api",
                    from_date=current_range_start.isoformat(),
                    to_date=to_date.isoformat(),
                )
            )
        base_index = len(chunks)
        for offset, chunk in enumerate(archive_chunks, start=1):
            chunks.append(
                BSEChunk(
                    index=base_index + offset,
                    label=f"archive_{chunk['from_date']}_{chunk['to_date']}",
                    kind="archive_form",
                    from_date=chunk["from_date"],
                    to_date=chunk["to_date"],
                )
            )

        seen_keys = {self.record_dedup_key(record) for record in existing_records}
        collected_records = list(existing_records)
        rows_appended = 0
        duplicates_skipped = 0
        output_mode = "append" if resume and out_path.exists() else "overwrite"
        chunk_window = self.compute_chunk_window(
            total_chunks=len(chunks),
            previous_last_completed_chunk=checkpoint.last_completed_chunk,
            max_chunks_this_run=max_chunks_this_run,
        )
        start_chunk = int(chunk_window["resume_from_chunk"])
        end_chunk = int(chunk_window["expected_end_chunk"])
        chunks_this_run = int(chunk_window["chunks_this_run"])
        completed = bool(chunk_window["completed"])
        expected_records = None if all_available else len(current_records)

        print(f"Oldest date: {from_date.isoformat()}")
        print(f"Newest date: {to_date.isoformat()}")
        print(f"Expected records: {expected_records}")
        print(f"Output path: {out_path}")
        print(f"Output mode: {output_mode}")
        print(f"total_chunks: {len(chunks)}")
        print(f"CSV rows detected: {csv_row_count}")
        print(f"previous last_completed_chunk: {checkpoint.last_completed_chunk}")
        print(f"resume_from_chunk: {start_chunk}")
        print(f"max_chunks_this_run: {max_chunks_this_run}")
        print(f"expected_end_chunk: {end_chunk}")
        print(f"actual chunk range: {start_chunk}-{end_chunk}" if chunks_this_run else "actual chunk range: none")
        print(f"chunks_processed_this_run: {chunks_this_run}")

        if completed:
            print("Run already completed. No new chunks to process.")
            return collected_records

        for chunk in chunks:
            if chunk.index < start_chunk or chunk.index > end_chunk:
                continue
            try:
                if chunk.kind == "current_api":
                    records = self.filter_records(current_records, from_date=date.fromisoformat(chunk.from_date), to_date=date.fromisoformat(chunk.to_date))
                else:
                    records = self.fetch_archive_records_for_range(
                        date.fromisoformat(chunk.from_date),
                        date.fromisoformat(chunk.to_date),
                        source_url=url,
                        retries=retries,
                        retry_base_delay=retry_base_delay,
                        retry_max_delay=retry_max_delay,
                    )
            except Exception as exc:
                checkpoint.errors.append(f"chunk {chunk.index}: {exc.__class__.__name__}: {exc}")
                checkpoint.updated_at = datetime.now(timezone.utc).isoformat()
                self.save_checkpoint(checkpoint_file, checkpoint)
                resume_command = self.build_resume_command(
                    url=url,
                    out_path=out_path,
                    checkpoint_path=checkpoint_file,
                    max_chunks_this_run=max_chunks_this_run,
                    delay_seconds=delay_seconds,
                    retries=retries,
                    retry_base_delay=retry_base_delay,
                    retry_max_delay=retry_max_delay,
                )
                print(f"Resume with: {resume_command}")
                if not allow_partial:
                    raise RuntimeError(f"Stopping after failure on chunk {chunk.index}. Resume with: {resume_command}") from exc
                break

            new_records: list[BSENoticeRecord] = []
            for record in self.filter_records(records, from_date=from_date, to_date=to_date):
                key = self.record_dedup_key(record)
                if key in seen_keys:
                    duplicates_skipped += 1
                    continue
                seen_keys.add(key)
                new_records.append(record)

            if new_records:
                self.append_output(new_records, out_path)
                rows_appended += len(new_records)
                collected_records.extend(new_records)

            self.assert_non_regressing_checkpoint(
                previous_last_completed_chunk=checkpoint.last_completed_chunk,
                new_last_completed_chunk=chunk.index,
            )
            checkpoint.last_completed_chunk = chunk.index
            checkpoint.records_written = csv_row_count + rows_appended
            checkpoint.unique_records_written = len(seen_keys)
            checkpoint.updated_at = datetime.now(timezone.utc).isoformat()
            self.save_checkpoint(checkpoint_file, checkpoint)

            if delay_seconds > 0:
                time.sleep(delay_seconds)

        checkpoint.completed = checkpoint.last_completed_chunk >= len(chunks)
        checkpoint.updated_at = datetime.now(timezone.utc).isoformat()
        self.save_checkpoint(checkpoint_file, checkpoint)

        print(f"Rows written: {rows_appended}")
        print(f"Duplicates skipped: {duplicates_skipped}")
        print(f"Final CSV row count: {csv_row_count + rows_appended}")
        print(f"Checkpoint state: {json.dumps(asdict(checkpoint), indent=2)}")
        return collected_records

    def scrape_older_unresolved(
        self,
        *,
        url: str,
        out_path: str | Path,
        delay_seconds: float = 1.5,
        retries: int = 5,
        retry_base_delay: float = 3.0,
        retry_max_delay: float = 60.0,
        headless: bool = True,
    ) -> list[BSEUnresolvedNoticeRecord]:
        del headless
        out_path = Path(out_path)
        self.ensure_output_writable(out_path, resume=False)
        boundaries = self.discover_archive_boundaries(url)
        older_range = boundaries["older_unparseable_range"]
        if older_range is None:
            self.write_unresolved_output([], out_path)
            print("Older unresolved rows found: 0")
            return []
        records = self.fetch_archive_unresolved_records_for_range(
            older_range[0],
            older_range[1],
            source_url=url,
            retries=retries,
            retry_base_delay=retry_base_delay,
            retry_max_delay=retry_max_delay,
        )
        self.write_unresolved_output(records, out_path)
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        print(f"Older unresolved rows found: {len(records)}")
        print(f"Older unresolved output: {out_path}")
        return records

    def recover_old_notice_dates(
        self,
        *,
        input_path: str | Path,
        out_path: str | Path,
        unresolved_out_path: str | Path,
        delay_seconds: float = 1.5,
        retries: int = 5,
        retry_base_delay: float = 3.0,
        retry_max_delay: float = 60.0,
        headless: bool = True,
    ) -> dict[str, Any]:
        del headless
        unresolved_rows = self.load_unresolved_output(input_path)
        self.ensure_output_writable(out_path, resume=False)
        self.ensure_output_writable(unresolved_out_path, resume=False)
        recovered: list[BSENoticeRecord] = []
        still_unresolved: list[BSEUnresolvedNoticeRecord] = []
        seen_keys: set[tuple[Any, ...]] = set()
        for row in unresolved_rows:
            try:
                response = self.fetch_page(
                    row.link,
                    retries=retries,
                    retry_base_delay=retry_base_delay,
                    retry_max_delay=retry_max_delay,
                )
                recovered_record = self.parse_notice_detail_page(response.text, row)
            except Exception as exc:
                still_unresolved.append(
                    BSEUnresolvedNoticeRecord(
                        raw_date=row.raw_date,
                        subject=row.subject,
                        circular_no=row.circular_no,
                        link=row.link,
                        source_url=row.source_url,
                        scraped_at=datetime.now(timezone.utc).isoformat(),
                        reason=f"detail_fetch_failed:{exc.__class__.__name__}",
                        page_url=row.page_url,
                    )
                )
                continue
            if recovered_record is None:
                still_unresolved.append(
                    BSEUnresolvedNoticeRecord(
                        raw_date=row.raw_date,
                        subject=row.subject,
                        circular_no=row.circular_no,
                        link=row.link,
                        source_url=row.source_url,
                        scraped_at=datetime.now(timezone.utc).isoformat(),
                        reason="detail_page_does_not_expose_exact_date",
                        page_url=row.page_url,
                    )
                )
            else:
                key = self.record_dedup_key(recovered_record)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                recovered.append(recovered_record)
            if delay_seconds > 0:
                time.sleep(delay_seconds)

        self.write_output(recovered, out_path)
        self.write_unresolved_output(still_unresolved, unresolved_out_path)
        print(f"Recovered older rows: {len(recovered)}")
        print(f"Still unresolved older rows: {len(still_unresolved)}")
        return {
            "recovered": recovered,
            "still_unresolved": still_unresolved,
        }

    def merge_export(self, *, main_path: str | Path, add_path: str | Path, out_path: str | Path) -> list[BSENoticeRecord]:
        main_records = self.load_existing_output_records(main_path)
        add_records = self.load_existing_output_records(add_path)
        merged: list[BSENoticeRecord] = []
        seen_keys: set[tuple[Any, ...]] = set()
        for record in [*main_records, *add_records]:
            key = self.record_dedup_key(record)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            merged.append(record)
        merged.sort(key=lambda record: (record.date, normalize_text(record.circular_no)), reverse=True)
        self.write_output(merged, out_path)
        print(f"Merged rows written: {len(merged)}")
        print(f"Merged output: {out_path}")
        return merged

    def validate_export(self, file_path: str | Path) -> dict[str, Any]:
        file_path = Path(file_path)
        report_path = file_path.parent / "bse_notices_validation_report.json"
        year_counts_path = file_path.parent / "bse_notices_year_counts.csv"

        total_rows = 0
        headers_ok = False
        malformed_rows = 0
        missing_date = 0
        missing_subject = 0
        missing_circular_no = 0
        missing_link = 0
        duplicate_key_count = 0
        pdf_links = 0
        detail_links = 0
        other_links = 0
        empty_links = 0
        suspicious_rows: list[dict[str, Any]] = []
        seen_keys: set[tuple[Any, ...]] = set()
        year_counts: dict[int, int] = {}
        min_date: str | None = None
        max_date: str | None = None

        with open(file_path, "r", newline="", encoding="utf-8") as file_obj:
            reader = csv.DictReader(file_obj)
            headers_ok = reader.fieldnames == EXPECTED_OUTPUT_HEADERS
            for row in reader:
                total_rows += 1
                if set(row.keys()) != set(EXPECTED_OUTPUT_HEADERS):
                    malformed_rows += 1
                    continue
                date_value = normalize_text(row.get("date", ""))
                subject = normalize_text(row.get("subject", ""))
                circular_no = normalize_text(row.get("circular_no", ""))
                link = normalize_text(row.get("link", ""))

                if not date_value:
                    missing_date += 1
                if not subject:
                    missing_subject += 1
                if not circular_no:
                    missing_circular_no += 1
                if not link:
                    missing_link += 1

                parsed_date: date | None = None
                try:
                    parsed_date = date.fromisoformat(date_value)
                except ValueError:
                    suspicious_rows.append({"row": total_rows, "reason": "invalid_date", "date": date_value, "subject": subject})

                if parsed_date is not None:
                    min_date = parsed_date.isoformat() if min_date is None else min(min_date, parsed_date.isoformat())
                    max_date = parsed_date.isoformat() if max_date is None else max(max_date, parsed_date.isoformat())
                    year_counts[parsed_date.year] = year_counts.get(parsed_date.year, 0) + 1

                dedupe_key = (
                    date_value,
                    normalize_text(subject).casefold(),
                    normalize_text(circular_no).casefold(),
                    link,
                ) if circular_no else (
                    date_value,
                    normalize_text(subject).casefold(),
                    link,
                )
                if dedupe_key in seen_keys:
                    duplicate_key_count += 1
                else:
                    seen_keys.add(dedupe_key)

                lower_link = link.lower()
                if not link:
                    empty_links += 1
                elif lower_link.endswith(".pdf"):
                    pdf_links += 1
                elif "dispnewnoticescirculars.aspx" in lower_link:
                    detail_links += 1
                else:
                    other_links += 1

                if link and not (lower_link.startswith("https://www.bseindia.com/") or lower_link.startswith("https://beta.bseindia.com/")):
                    suspicious_rows.append({"row": total_rows, "reason": "non_bse_link", "link": link})
                if subject and len(subject) < 5:
                    suspicious_rows.append({"row": total_rows, "reason": "short_subject", "subject": subject})
                if not circular_no:
                    suspicious_rows.append({"row": total_rows, "reason": "missing_circular_no", "subject": subject, "link": link})

        year_rows = [{"year": year, "count": count} for year, count in sorted(year_counts.items())]
        report = {
            "file": str(file_path),
            "headers_ok": headers_ok,
            "total_rows": total_rows,
            "malformed_rows": malformed_rows,
            "min_date": min_date,
            "max_date": max_date,
            "missing_field_counts": {
                "date": missing_date,
                "subject": missing_subject,
                "circular_no": missing_circular_no,
                "link": missing_link,
            },
            "duplicate_key_count": duplicate_key_count,
            "link_type_counts": {
                "pdf": pdf_links,
                "detail_page": detail_links,
                "other": other_links,
                "empty": empty_links,
            },
            "rows_per_year": year_rows,
            "suspicious_rows": suspicious_rows,
        }
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        with open(year_counts_path, "w", newline="", encoding="utf-8") as file_obj:
            writer = csv.DictWriter(file_obj, fieldnames=["year", "count"])
            writer.writeheader()
            writer.writerows(year_rows)

        print(f"Total rows: {total_rows}")
        print(f"Min date: {min_date}")
        print(f"Max date: {max_date}")
        print(f"Duplicate key count: {duplicate_key_count}")
        print(f"Validation report: {report_path}")
        print(f"Year counts CSV: {year_counts_path}")
        return report

    def fetch_page(
        self,
        page_url: str,
        *,
        raise_for_status: bool = True,
        retries: int = 5,
        retry_base_delay: float = 3.0,
        retry_max_delay: float = 60.0,
        method: str = "GET",
        data: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        headers = dict(self.headers)
        if "beta.bseindia.com" in page_url:
            headers["Origin"] = "https://beta.bseindia.com"
        elif "bseindia.com" in page_url:
            headers["Origin"] = "https://www.bseindia.com"
        headers["Referer"] = page_url if method == "GET" else BSE_ARCHIVE_BETA_URL

        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                if method == "POST":
                    response = self.client.post(page_url, headers=headers, data=data, params=params)
                else:
                    response = self.client.get(page_url, headers=headers, params=params)
                if raise_for_status:
                    response.raise_for_status()
                self.last_fetch_transport = "httpx"
                return response
            except Exception as exc:
                if not self.is_retryable_http_exception(exc):
                    raise
                last_exc = exc
                if attempt >= retries:
                    break
                delay = self.compute_retry_delay(attempt, base_delay=retry_base_delay, max_delay=retry_max_delay)
                message = f"BSE request failed for {page_url} with {exc.__class__.__name__}. Retry {attempt}/{retries} after {delay:.1f}s."
                print(message)
                logger.warning(message)
                time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    def fetch_json(
        self,
        page_url: str,
        *,
        params: dict[str, Any] | None = None,
        retries: int = 5,
        retry_base_delay: float = 3.0,
        retry_max_delay: float = 60.0,
    ) -> dict[str, Any]:
        response = self.fetch_page(
            page_url,
            retries=retries,
            retry_base_delay=retry_base_delay,
            retry_max_delay=retry_max_delay,
            params=params,
        )
        return response.json()

    def fetch_current_latest_date(self) -> date:
        payload = self.fetch_json(BSE_CURRENT_AUTO_DATE_API_URL)
        raw_value = payload["Table"][0]["date"]
        return datetime.strptime(raw_value, "%d/%m/%Y").date()

    def fetch_current_api_page(
        self,
        flag: str,
        *,
        retries: int = 5,
        retry_base_delay: float = 3.0,
        retry_max_delay: float = 60.0,
    ) -> dict[str, Any]:
        return self.fetch_json(
            BSE_CURRENT_API_URL,
            params={"flag": flag},
            retries=retries,
            retry_base_delay=retry_base_delay,
            retry_max_delay=retry_max_delay,
        )

    def parse_current_api_records(self, payload: dict[str, Any], source_url: str) -> list[BSENoticeRecord]:
        table = payload.get("Table") or []
        scraped_at = datetime.now(timezone.utc).isoformat()
        records: list[BSENoticeRecord] = []
        for row in table:
            raw_date = normalize_text(str(row.get("Notice_Date") or row.get("dt_tm") or ""))
            normalized_date = ""
            if raw_date:
                normalized_date = raw_date[0:10]
            circular_no = normalize_text(str(row.get("Notice_no") or ""))
            subject = normalize_text(str(row.get("Subject") or ""))
            # Live API field mapping:
            # date <- Notice_Date / dt_tm
            # subject <- Subject
            # circular_no <- Notice_no
            # link <- FileName
            link = self.normalize_bse_link(str(row.get("FileName") or ""), BSE_NOTICES_PAGE_URL)
            if not normalized_date and circular_no:
                normalized_date = self.derive_bse_date(circular_no)
            if not normalized_date or not subject or not link:
                continue
            records.append(
                BSENoticeRecord(
                    date=normalized_date,
                    subject=subject,
                    circular_no=circular_no,
                    link=link,
                    source_url=source_url,
                    scraped_at=scraped_at,
                    page_url=BSE_CURRENT_API_URL,
                    raw_date=raw_date or None,
                )
            )
        return records

    def fetch_current_records(self, *, min_date: date, source_url: str) -> list[BSENoticeRecord]:
        token = ""
        seen_flags: set[str] = set()
        collected: list[BSENoticeRecord] = []
        while True:
            payload = self.fetch_current_api_page(token)
            records = self.parse_current_api_records(payload, source_url)
            if not records:
                break
            record_date = max(date.fromisoformat(record.date) for record in records)
            if record_date < min_date:
                break
            collected.extend(record for record in records if date.fromisoformat(record.date) >= min_date)
            previous_flag = normalize_text(str((payload.get("Table") or [{}])[0].get("Previous") or ""))
            if not previous_flag or previous_flag in seen_flags:
                break
            seen_flags.add(previous_flag)
            token = previous_flag
        return collected

    def fetch_archive_newest_date(self) -> date:
        html = self.fetch_page(BSE_ARCHIVE_BETA_URL).text
        records = self.parse_notice_records(html, BSE_ARCHIVE_BETA_URL, source_url=BSE_ARCHIVE_BETA_URL)
        if not records:
            raise RuntimeError("BSE archive landing page returned zero visible rows")
        return max(date.fromisoformat(record.date) for record in records)

    def discover_archive_boundaries(self, source_url: str) -> dict[str, Any]:
        archive_newest = self.fetch_archive_newest_date()
        last_non_empty_range: tuple[date, date] | None = None
        older_unparseable_range: tuple[date, date] | None = None
        window_end = archive_newest
        attempts = 0
        while attempts < 80:
            attempts += 1
            window_start = window_end - relativedelta(years=ARCHIVE_MAX_RANGE_YEARS) + timedelta(days=1)
            first_page_html = self.execute_archive_search(
                window_start,
                window_end,
                retries=5,
                retry_base_delay=3.0,
                retry_max_delay=60.0,
            )
            parsed_records = self.parse_notice_records(first_page_html, BSE_ARCHIVE_BETA_URL, source_url=source_url)
            raw_row_count = self.count_notice_rows(first_page_html)
            if not parsed_records:
                if raw_row_count > 0:
                    older_unparseable_range = (window_start, window_end)
                break
            last_non_empty_range = (window_start, window_end)
            window_end = window_start - timedelta(days=1)
            if window_end < date.min + timedelta(days=1):
                break

        if last_non_empty_range is None:
            raise RuntimeError("BSE archive search returned zero rows")

        oldest_year_with_records: int | None = None
        for year in range(last_non_empty_range[0].year, last_non_empty_range[1].year + 1):
            year_start = max(last_non_empty_range[0], date(year, 1, 1))
            year_end = min(last_non_empty_range[1], date(year, 12, 31))
            year_html = self.execute_archive_search(year_start, year_end, retries=5, retry_base_delay=3.0, retry_max_delay=60.0)
            if self.parse_notice_records(year_html, BSE_ARCHIVE_BETA_URL, source_url=source_url):
                oldest_year_with_records = year
                break
        if oldest_year_with_records is None:
            raise RuntimeError("BSE archive narrowing failed at yearly granularity")

        oldest_month_with_records: int | None = None
        for month in range(1, 13):
            month_start = max(last_non_empty_range[0], date(oldest_year_with_records, month, 1))
            month_end = min(last_non_empty_range[1], self.month_end(date(oldest_year_with_records, month, 1)))
            if month_start > month_end:
                continue
            month_html = self.execute_archive_search(month_start, month_end, retries=5, retry_base_delay=3.0, retry_max_delay=60.0)
            if self.parse_notice_records(month_html, BSE_ARCHIVE_BETA_URL, source_url=source_url):
                oldest_month_with_records = month
                break
        if oldest_month_with_records is None:
            raise RuntimeError("BSE archive narrowing failed at monthly granularity")

        month_start = date(oldest_year_with_records, oldest_month_with_records, 1)
        month_end = self.month_end(month_start)
        oldest_chunk_records = self.fetch_archive_records_for_range(month_start, month_end, source_url=source_url)
        oldest_record = min(oldest_chunk_records, key=lambda record: (record.date, record.circular_no))
        earliest_records = sorted(oldest_chunk_records, key=lambda record: (record.date, record.circular_no))[:5]
        return {
            "archive_newest": archive_newest,
            "last_non_empty_range": last_non_empty_range,
            "older_unparseable_range": older_unparseable_range,
            "oldest_record": oldest_record,
            "earliest_records": earliest_records,
        }

    def discover_archive_chunks(self, archive_newest: date, *, source_url: str) -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = []
        window_end = archive_newest
        attempts = 0
        while attempts < 80:
            attempts += 1
            window_start = window_end - relativedelta(years=ARCHIVE_MAX_RANGE_YEARS) + timedelta(days=1)
            records = self.fetch_archive_records_for_range(window_start, window_end, source_url=source_url)
            if not records:
                break
            chunks.append({"from_date": window_start.isoformat(), "to_date": window_end.isoformat(), "records": records})
            window_end = window_start - timedelta(days=1)
            if window_end < date.min + timedelta(days=1):
                break
        return chunks

    def build_archive_chunks(self, from_date: date, to_date: date) -> list[dict[str, str]]:
        chunks: list[dict[str, str]] = []
        if from_date > to_date:
            return chunks
        current_start = from_date
        while current_start <= to_date:
            current_end = min(to_date, current_start + relativedelta(months=ARCHIVE_EXPORT_CHUNK_MONTHS) - timedelta(days=1))
            chunks.append({"from_date": current_start.isoformat(), "to_date": current_end.isoformat()})
            current_start = current_end + timedelta(days=1)
        return chunks

    def fetch_archive_records_for_range(
        self,
        from_date: date,
        to_date: date,
        *,
        source_url: str,
        retries: int = 5,
        retry_base_delay: float = 3.0,
        retry_max_delay: float = 60.0,
    ) -> list[BSENoticeRecord]:
        html = self.execute_archive_search(
            from_date,
            to_date,
            retries=retries,
            retry_base_delay=retry_base_delay,
            retry_max_delay=retry_max_delay,
        )
        records = self.parse_notice_records(html, BSE_ARCHIVE_BETA_URL, source_url=source_url)
        if not records:
            return []

        all_records = list(records)
        seen_keys = {self.record_dedup_key(record) for record in records}
        current_html = html
        current_page = 1
        while True:
            next_page = self.find_next_page_number(current_html, current_page=current_page)
            if next_page is None:
                break
            current_html = self.fetch_archive_page_number(
                current_html,
                target_page=next_page,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
            )
            page_records = self.parse_notice_records(current_html, BSE_ARCHIVE_BETA_URL, source_url=source_url)
            for record in page_records:
                key = self.record_dedup_key(record)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                all_records.append(record)
            current_page = next_page
        return all_records

    def fetch_archive_unresolved_records_for_range(
        self,
        from_date: date,
        to_date: date,
        *,
        source_url: str,
        retries: int = 5,
        retry_base_delay: float = 3.0,
        retry_max_delay: float = 60.0,
    ) -> list[BSEUnresolvedNoticeRecord]:
        html = self.execute_archive_search(
            from_date,
            to_date,
            retries=retries,
            retry_base_delay=retry_base_delay,
            retry_max_delay=retry_max_delay,
        )
        unresolved = self.parse_unresolved_notice_records(html, BSE_ARCHIVE_BETA_URL, source_url=source_url)
        all_records = list(unresolved)
        seen_keys = {(record.circular_no, record.subject, record.link) for record in unresolved}
        current_html = html
        current_page = 1
        while True:
            next_page = self.find_next_page_number(current_html, current_page=current_page)
            if next_page is None:
                break
            current_html = self.fetch_archive_page_number(
                current_html,
                target_page=next_page,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
            )
            page_records = self.parse_unresolved_notice_records(current_html, BSE_ARCHIVE_BETA_URL, source_url=source_url)
            for record in page_records:
                key = (record.circular_no, record.subject, record.link)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                all_records.append(record)
            current_page = next_page
        return all_records

    def execute_archive_search(
        self,
        from_date: date,
        to_date: date,
        *,
        retries: int,
        retry_base_delay: float,
        retry_max_delay: float,
    ) -> str:
        response = self.fetch_page(
            BSE_ARCHIVE_BETA_URL,
            retries=retries,
            retry_base_delay=retry_base_delay,
            retry_max_delay=retry_max_delay,
        )
        payload = self.build_archive_form_payload(response.text)
        payload["ctl00$ContentPlaceHolder1$txtDate"] = from_date.strftime("%d/%m/%Y")
        payload["ctl00$ContentPlaceHolder1$txtTodate"] = to_date.strftime("%d/%m/%Y")
        payload["ctl00$ContentPlaceHolder1$btnSubmit"] = "Submit"
        search_response = self.fetch_page(
            BSE_ARCHIVE_BETA_URL,
            method="POST",
            data=payload,
            retries=retries,
            retry_base_delay=retry_base_delay,
            retry_max_delay=retry_max_delay,
        )
        return search_response.text

    def fetch_archive_page_number(
        self,
        current_html: str,
        *,
        target_page: int,
        retries: int,
        retry_base_delay: float,
        retry_max_delay: float,
    ) -> str:
        payload = self.build_archive_form_payload(current_html)
        payload["__EVENTTARGET"] = "ctl00$ContentPlaceHolder1$GridView2"
        payload["__EVENTARGUMENT"] = f"Page${target_page}"
        payload.pop("ctl00$ContentPlaceHolder1$btnSubmit", None)
        response = self.fetch_page(
            BSE_ARCHIVE_BETA_URL,
            method="POST",
            data=payload,
            retries=retries,
            retry_base_delay=retry_base_delay,
            retry_max_delay=retry_max_delay,
        )
        return response.text

    def build_archive_form_payload(self, html: str) -> dict[str, str]:
        soup = BeautifulSoup(html, "html.parser")
        payload: dict[str, str] = {}
        for field in soup.select("form input[name], form select[name], form textarea[name]"):
            name = field.get("name")
            if not name:
                continue
            if field.name == "select":
                option = field.select_one("option[selected]") or field.select_one("option")
                payload[name] = option.get("value", "") if option else ""
                continue
            field_type = (field.get("type") or "").lower()
            if field_type == "image":
                continue
            if field_type in {"checkbox", "radio"}:
                if field.has_attr("checked"):
                    payload[name] = field.get("value", "on")
                continue
            payload[name] = field.get("value", "")
        return payload

    def find_next_page_number(self, html: str, *, current_page: int) -> int | None:
        soup = BeautifulSoup(html, "html.parser")
        pager_links = soup.select("#ContentPlaceHolder1_GridView2 tr td a[href*=\"Page$\"]")
        page_numbers: list[int] = []
        for link in pager_links:
            match = re.search(r"Page\$(\d+)", link.get("href", ""))
            if match:
                page_numbers.append(int(match.group(1)))
        higher_pages = sorted(number for number in set(page_numbers) if number > current_page)
        return higher_pages[0] if higher_pages else None

    def collect_form_metadata(self, soup: BeautifulSoup) -> dict[str, Any]:
        form = soup.select_one("form")
        hidden_fields = {
            name: value
            for name, value in (
                (
                    field.get("name"),
                    normalize_text(field.get("value", "")),
                )
                for field in soup.select('input[type="hidden"][name]')
            )
            if name in {"__VIEWSTATE", "__EVENTVALIDATION", "__VIEWSTATEGENERATOR"}
        }
        select_values = {
            select.get("name"): [normalize_text(option.get_text(" ", strip=True)) for option in select.select("option")]
            for select in soup.select("select[name]")
        }
        buttons = [
            {
                "name": button.get("name") or button.get("id"),
                "onclick": button.get("onclick"),
                "text": normalize_text(button.get_text(" ", strip=True) or button.get("value", "")),
            }
            for button in soup.select("input[type=submit], button, a[id]")
        ]
        return {
            "form_action": urljoin(BSE_ARCHIVE_BETA_URL, form.get("action") or "") if form else BSE_ARCHIVE_BETA_URL,
            "hidden_fields": hidden_fields,
            "input_names": [field.get("name") for field in soup.select("input[name]") if field.get("name")],
            "select_values": select_values,
            "buttons": buttons[:50],
            "date_field_names": [name for name in ["ctl00$ContentPlaceHolder1$txtDate", "ctl00$ContentPlaceHolder1$txtTodate"] if name in hidden_fields or soup.select(f'[name="{name}"]')],
            "notice_no_field_name": "ctl00$ContentPlaceHolder1$txtNoticeNo" if soup.select('[name="ctl00$ContentPlaceHolder1$txtNoticeNo"]') else None,
            "subject_field_name": "ctl00$ContentPlaceHolder1$txtSub" if soup.select('[name="ctl00$ContentPlaceHolder1$txtSub"]') else None,
            "submit_button_name": "ctl00$ContentPlaceHolder1$btnSubmit" if soup.select('[name="ctl00$ContentPlaceHolder1$btnSubmit"]') else None,
            "grid_selector": "#ContentPlaceHolder1_GridView1 or #ContentPlaceHolder1_GridView2",
            "pagination_controls": [normalize_text(link.get_text(" ", strip=True)) for link in soup.select('#ContentPlaceHolder1_GridView2 a[href*="Page$"], #ContentPlaceHolder1_lnkPreviousDay')],
        }

    def parse_notice_records(self, html: str, page_url: str, source_url: str | None = None) -> list[BSENoticeRecord]:
        soup = BeautifulSoup(html, "html.parser")
        row_nodes = soup.select("#GridView1 tr")[1:] or soup.select("#ContentPlaceHolder1_GridView1 tr")[1:] or soup.select("#ContentPlaceHolder1_GridView2 tr")[1:]
        scraped_at = datetime.now(timezone.utc).isoformat()
        records: list[BSENoticeRecord] = []
        for row in row_nodes:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            circular_no = normalize_text(cells[0].get_text(" ", strip=True))
            normalized_date = self.derive_bse_date(circular_no)
            if not normalized_date:
                continue
            subject_cell = cells[1]
            subject = normalize_text(subject_cell.get_text(" ", strip=True))
            anchor = subject_cell.find("a", href=True)
            link = self.normalize_bse_link(anchor.get("href") if anchor else "", page_url)
            if not subject or not link:
                continue
            records.append(
                BSENoticeRecord(
                    date=normalized_date,
                    subject=subject,
                    circular_no=circular_no,
                    link=link,
                    source_url=source_url or page_url,
                    scraped_at=scraped_at,
                    page_url=page_url,
                )
            )
        return records

    def parse_unresolved_notice_records(self, html: str, page_url: str, source_url: str | None = None) -> list[BSEUnresolvedNoticeRecord]:
        soup = BeautifulSoup(html, "html.parser")
        row_nodes = soup.select("#GridView1 tr")[1:] or soup.select("#ContentPlaceHolder1_GridView1 tr")[1:] or soup.select("#ContentPlaceHolder1_GridView2 tr")[1:]
        scraped_at = datetime.now(timezone.utc).isoformat()
        records: list[BSEUnresolvedNoticeRecord] = []
        for row in row_nodes:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            circular_no = normalize_text(cells[0].get_text(" ", strip=True))
            subject_cell = cells[1]
            subject = normalize_text(subject_cell.get_text(" ", strip=True))
            anchor = subject_cell.find("a", href=True)
            link = self.normalize_bse_link(anchor.get("href") if anchor else "", page_url)
            if not circular_no or not subject or not link:
                continue
            if self.derive_bse_date(circular_no):
                continue
            records.append(
                BSEUnresolvedNoticeRecord(
                    raw_date="",
                    subject=subject,
                    circular_no=circular_no,
                    link=link,
                    source_url=source_url or page_url,
                    scraped_at=scraped_at,
                    reason="listing_does_not_expose_exact_date",
                    page_url=page_url,
                )
            )
        return records

    def parse_notice_detail_page(self, html: str, unresolved_row: BSEUnresolvedNoticeRecord) -> BSENoticeRecord | None:
        soup = BeautifulSoup(html, "html.parser")
        notice_no = normalize_text((soup.select_one("#tc11") or {}).get_text(" ", strip=True) if soup.select_one("#tc11") else unresolved_row.circular_no)
        subject = normalize_text((soup.select_one("#tc31") or {}).get_text(" ", strip=True) if soup.select_one("#tc31") else unresolved_row.subject)
        raw_date = normalize_text((soup.select_one("#tc12") or {}).get_text(" ", strip=True) if soup.select_one("#tc12") else "")
        normalized_date = ""
        if raw_date:
            try:
                normalized_date = date_parser.parse(raw_date, dayfirst=True).date().isoformat()
            except (ValueError, TypeError, OverflowError):
                normalized_date = ""
        if not normalized_date:
            url_date = self.extract_date_from_link(unresolved_row.link)
            normalized_date = url_date or ""
        if not normalized_date:
            return None
        return BSENoticeRecord(
            date=normalized_date,
            subject=subject or unresolved_row.subject,
            circular_no=notice_no or unresolved_row.circular_no,
            link=unresolved_row.link,
            source_url=unresolved_row.source_url,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            page_url=unresolved_row.link,
            raw_date=raw_date or None,
        )

    def count_notice_rows(self, html: str) -> int:
        soup = BeautifulSoup(html, "html.parser")
        row_nodes = soup.select("#GridView1 tr")[1:] or soup.select("#ContentPlaceHolder1_GridView1 tr")[1:] or soup.select("#ContentPlaceHolder1_GridView2 tr")[1:]
        count = 0
        for row in row_nodes:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            circular_no = normalize_text(cells[0].get_text(" ", strip=True))
            subject = normalize_text(cells[1].get_text(" ", strip=True))
            if circular_no and subject and circular_no != "1":
                count += 1
        return count

    def derive_bse_date(self, circular_no: str) -> str:
        normalized_notice = normalize_text(circular_no)
        prefix = normalized_notice.split("-", 1)[0]
        if len(prefix) == 8 and prefix.isdigit():
            return date(int(prefix[0:4]), int(prefix[4:6]), int(prefix[6:8])).isoformat()
        return ""

    def extract_date_from_link(self, link: str) -> str:
        match = re.search(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", link)
        if not match:
            return ""
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
        except ValueError:
            return ""

    def normalize_bse_link(self, raw_value: str, page_url: str) -> str:
        value = normalize_text(raw_value)
        if not value:
            return ""
        absolute = urljoin(page_url, value)
        return absolute.replace("http://www.bseindia.com/", "https://www.bseindia.com/")

    def filter_records(self, records: list[BSENoticeRecord], *, from_date: date | None, to_date: date | None) -> list[BSENoticeRecord]:
        filtered: list[BSENoticeRecord] = []
        for record in records:
            record_date = date.fromisoformat(record.date)
            if from_date and record_date < from_date:
                continue
            if to_date and record_date > to_date:
                continue
            filtered.append(record)
        return filtered

    def record_dedup_key(self, record: BSENoticeRecord) -> tuple[str, str, str, str] | tuple[str, str, str]:
        normalized_subject = normalize_text(record.subject).casefold()
        if record.circular_no:
            return (record.date, normalized_subject, normalize_text(record.circular_no).casefold(), record.link)
        return (record.date, normalized_subject, record.link)

    def extract_urls_from_html(self, html: str) -> list[str]:
        urls: set[str] = set()
        soup = BeautifulSoup(html, "html.parser")
        for node in soup.select("[href], [src]"):
            value = node.get("href") or node.get("src")
            if value and ("api" in value.lower() or "notice" in value.lower() or "circular" in value.lower() or value.lower().endswith((".pdf", ".zip", ".aspx", ".js"))):
                urls.add(value)
        return sorted(urls)

    def month_end(self, month_start: date) -> date:
        if month_start.month == 12:
            return date(month_start.year, 12, 31)
        return date(month_start.year, month_start.month + 1, 1) - timedelta(days=1)

    def ensure_output_writable(self, out_path: str | Path, *, resume: bool) -> None:
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.exists():
                with open(path, "a", encoding="utf-8"):
                    pass
            elif not resume:
                with open(path, "a", encoding="utf-8"):
                    pass
                path.unlink(missing_ok=True)
        except PermissionError as exc:
            raise RuntimeError("Output file is locked. Close Excel/VS Code/OneDrive preview and rerun.") from exc

    def load_existing_output_records(self, out_path: str | Path) -> list[BSENoticeRecord]:
        out_path = Path(out_path)
        if not out_path.exists():
            return []
        if out_path.suffix.lower() == ".csv":
            with open(out_path, "r", newline="", encoding="utf-8") as file_obj:
                reader = csv.DictReader(file_obj)
                return [
                    BSENoticeRecord(
                        date=row["date"],
                        subject=row["subject"],
                        circular_no=row["circular_no"],
                        link=row["link"],
                        source_url=row["source_url"],
                        scraped_at=row["scraped_at"],
                    )
                    for row in reader
                ]
        if out_path.suffix.lower() == ".json":
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            return [BSENoticeRecord(**row) for row in payload]
        raise ValueError("Output path must end with .csv or .json")

    def append_output(self, records: list[BSENoticeRecord], out_path: str | Path) -> None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [self.record_to_output_row(record) for record in records]
        if out_path.suffix.lower() == ".csv":
            write_header = not out_path.exists() or out_path.stat().st_size == 0
            with open(out_path, "a", newline="", encoding="utf-8") as file_obj:
                writer = csv.DictWriter(file_obj, fieldnames=EXPECTED_OUTPUT_HEADERS)
                if write_header:
                    writer.writeheader()
                writer.writerows(rows)
            return
        if out_path.suffix.lower() == ".json":
            existing = []
            if out_path.exists():
                existing = json.loads(out_path.read_text(encoding="utf-8"))
            existing.extend(rows)
            out_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
            return
        raise ValueError("Output path must end with .csv or .json")

    def write_output(self, records: list[BSENoticeRecord], out_path: str | Path) -> None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [self.record_to_output_row(record) for record in records]
        if out_path.suffix.lower() == ".csv":
            with open(out_path, "w", newline="", encoding="utf-8") as file_obj:
                writer = csv.DictWriter(file_obj, fieldnames=EXPECTED_OUTPUT_HEADERS)
                writer.writeheader()
                writer.writerows(rows)
            return
        if out_path.suffix.lower() == ".json":
            out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            return
        raise ValueError("Output path must end with .csv or .json")

    def load_unresolved_output(self, file_path: str | Path) -> list[BSEUnresolvedNoticeRecord]:
        file_path = Path(file_path)
        if not file_path.exists():
            return []
        if file_path.suffix.lower() == ".csv":
            with open(file_path, "r", newline="", encoding="utf-8") as file_obj:
                reader = csv.DictReader(file_obj)
                return [
                    BSEUnresolvedNoticeRecord(
                        raw_date=row["raw_date"],
                        subject=row["subject"],
                        circular_no=row["circular_no"],
                        link=row["link"],
                        source_url=row["source_url"],
                        scraped_at=row["scraped_at"],
                        reason=row["reason"],
                    )
                    for row in reader
                ]
        if file_path.suffix.lower() == ".json":
            payload = json.loads(file_path.read_text(encoding="utf-8"))
            return [BSEUnresolvedNoticeRecord(**row) for row in payload]
        raise ValueError("Unresolved output path must end with .csv or .json")

    def write_unresolved_output(self, records: list[BSEUnresolvedNoticeRecord], out_path: str | Path) -> None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [self.unresolved_record_to_output_row(record) for record in records]
        if out_path.suffix.lower() == ".csv":
            with open(out_path, "w", newline="", encoding="utf-8") as file_obj:
                writer = csv.DictWriter(file_obj, fieldnames=UNRESOLVED_OUTPUT_HEADERS)
                writer.writeheader()
                writer.writerows(rows)
            return
        if out_path.suffix.lower() == ".json":
            out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            return
        raise ValueError("Unresolved output path must end with .csv or .json")

    def record_to_output_row(self, record: BSENoticeRecord) -> dict[str, str]:
        return {
            "date": record.date,
            "subject": record.subject,
            "circular_no": record.circular_no,
            "link": record.link,
            "source_url": record.source_url,
            "scraped_at": record.scraped_at,
        }

    def unresolved_record_to_output_row(self, record: BSEUnresolvedNoticeRecord) -> dict[str, str]:
        return {
            "raw_date": record.raw_date,
            "subject": record.subject,
            "circular_no": record.circular_no,
            "link": record.link,
            "source_url": record.source_url,
            "scraped_at": record.scraped_at,
            "reason": record.reason,
        }

    def build_resume_command(
        self,
        *,
        url: str,
        out_path: str | Path,
        checkpoint_path: str | Path,
        max_chunks_this_run: int | None,
        delay_seconds: float,
        retries: int,
        retry_base_delay: float,
        retry_max_delay: float,
    ) -> str:
        max_chunks_arg = f"--max-chunks-this-run {max_chunks_this_run} " if max_chunks_this_run is not None else ""
        return (
            'py -3.13 -m main scrape-url --source bse '
            f'--url "{url}" '
            f'--out "{out_path}" '
            '--resume '
            f'--checkpoint "{checkpoint_path}" '
            f"{max_chunks_arg}"
            f'--delay-seconds {delay_seconds} '
            f'--retries {retries} '
            f'--retry-base-delay {retry_base_delay} '
            f'--retry-max-delay {retry_max_delay}'
        )

    def load_checkpoint(self, checkpoint_path: str | Path) -> BSECheckpoint:
        payload = json.loads(Path(checkpoint_path).read_text(encoding="utf-8"))
        return BSECheckpoint(**payload)

    def save_checkpoint(self, checkpoint_path: str | Path, checkpoint: BSECheckpoint) -> None:
        Path(checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
        Path(checkpoint_path).write_text(json.dumps(asdict(checkpoint), indent=2), encoding="utf-8")
