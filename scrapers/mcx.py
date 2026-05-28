from __future__ import annotations

import csv
import json
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from loguru import logger

from extraction.metadata_cleaner import normalize_text, parse_indian_date
from models import RegulatoryDocument
from scrapers.base import BaseScraper


MCX_SOURCE_LABEL = "MCX"
MCX_CIRCULARS_URL = "https://www.mcxindia.com/circulars/all-circulars"
MCX_ADVANCED_SEARCH_URL = "https://www.mcxindia.com/backpage.aspx/GetCircularAdvanceSearch"
MCX_BASIC_SEARCH_URL = "https://www.mcxindia.com/backpage.aspx/GetCircularSearch"
MCX_HTTP_USER_AGENT = "Mozilla/5.0"
EXPECTED_OUTPUT_HEADERS = ["date", "subject", "circular_no", "link", "source_url", "scraped_at"]
MCX_CATEGORY_MAP = {
    "all": "ALL",
    "t&s": "t-s",
    "t&s.": "t-s",
    "t-s": "t-s",
    "ddr": "ddr",
    "membership and compliance": "membership-and-compliance",
    "membership-and-compliance": "membership-and-compliance",
    "c&s": "c-s",
    "c-s": "c-s",
    "ctcl": "ctcl",
    "legal": "legal",
    "general": "general",
    "tech": "tech",
    "warehousing & logistics": "warehousing-logistics",
    "warehousing and logistics": "warehousing-logistics",
    "warehousing-logistics": "warehousing-logistics",
    "ipf": "ipf",
    "investor services": "investor-services",
    "investor-services": "investor-services",
    "others": "others",
    "mcxccl": "mcxccl",
}


@dataclass
class MCXCircularRecord:
    date: str
    subject: str
    circular_no: str
    link: str
    source_url: str
    scraped_at: str
    category: Optional[str] = None
    raw_date: Optional[str] = None


@dataclass
class MCXEndpointResult:
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
class MCXChunk:
    index: int
    from_date: date
    to_date: date


@dataclass
class MCXCheckpoint:
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


class MCXScraper(BaseScraper):
    source = "mcx"
    regulator = MCX_SOURCE_LABEL

    def __init__(self, config: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.config = config
        self.last_fetch_transport = "httpx"

    def fetch_index(self, from_date: date, to_date: date) -> Any:
        return self.fetch_advanced_payload(from_date=from_date, to_date=to_date, category="ALL")

    def parse_listing(self, response: Any) -> list[dict[str, Any]]:
        records = self.parse_circular_records(response, MCX_CIRCULARS_URL)
        return [
            {
                "title": record.subject,
                "url": record.link,
                "document_type": "Circular",
                "reference_no": record.circular_no or None,
                "published_date": date.fromisoformat(record.date),
                "department": None,
                "category": record.category or "All Circulars",
                "pdf_url": None,
            }
            for record in records
        ]

    def fetch_document(self, record: dict[str, Any]) -> RegulatoryDocument:
        return RegulatoryDocument(
            source=self.source,
            regulator=self.regulator,
            document_type=record.get("document_type", "Circular"),
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

    def inspect_circulars(self, url: str) -> dict[str, Any]:
        fixture_dir = Path("tests/fixtures/mcx")
        fixture_dir.mkdir(parents=True, exist_ok=True)

        page_response = self.fetch_page(url)
        raw_html = page_response.text
        raw_fixture_path = fixture_dir / "all_circulars.html"
        raw_fixture_path.write_text(raw_html, encoding="utf-8")

        soup = BeautifulSoup(raw_html, "html.parser")
        direct_rows = self.parse_rendered_rows(raw_html, url)
        page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
        category_values = [
            normalize_text(option.get_text(" ", strip=True))
            for option in soup.select("#cph_InnerContainerMiddlePageContains_C009_ddlCircularTypes option")
            if normalize_text(option.get_text(" ", strip=True))
        ]
        month_values = [
            normalize_text(option.get_text(" ", strip=True))
            for option in soup.select("#ddlMonth option")
            if normalize_text(option.get_text(" ", strip=True))
        ]
        api_urls_found = sorted(
            {
                MCX_ADVANCED_SEARCH_URL,
                MCX_BASIC_SEARCH_URL,
                *self.extract_api_urls_from_html(raw_html),
            }
        )

        endpoint_results = [
            self.inspect_advanced_endpoint(
                from_date=date(2026, 5, 1),
                to_date=date(2026, 5, 16),
                category="ALL",
            ),
            self.inspect_basic_endpoint(year="2026", month="05"),
        ]

        rendered_fixture_path: str | None = None
        network_capture_path: str | None = None
        rendered_rows: list[MCXCircularRecord] = []
        if not direct_rows:
            rendered = self.inspect_with_playwright(url)
            rendered_fixture_path = rendered["rendered_fixture_path"]
            network_capture_path = rendered["network_capture_path"]
            rendered_rows = rendered["rows"]

        rows_to_print = direct_rows or rendered_rows
        print(f"Page title: {page_title}")
        print(f"Fixture saved: {raw_fixture_path}")
        print(f"Basic filter present: {bool(soup.select('#btnSimple, #ddlMonth, #cph_InnerContainerMiddlePageContains_C009_ddlYear'))}")
        print(f"Advanced filter present: {bool(soup.select('#btnAdvance, #txtNumber, #txtTitle, #txtFromDate, #txtToDate'))}")
        print(f"Category filter values: {category_values}")
        print(
            "Month/year/date controls found: "
            f"{[field for field in ['#ddlMonth', '#cph_InnerContainerMiddlePageContains_C009_ddlYear', '#txtFromDate', '#txtToDate'] if soup.select(field)]}"
        )
        print(
            "Pagination controls found: "
            f"{[field for field in ['#ddlPager', '#ddlPager1', '#pager', '#pager1'] if soup.select(field)]}"
        )
        print(f"Table/listing selectors found: {[selector for selector in ['#tblCircular', '#dvSimple', '#dvAdvance'] if soup.select(selector)]}")
        print("API URLs found in scripts/markup:")
        for api_url in api_urls_found:
            print(api_url)
        if rendered_fixture_path:
            print(f"Rendered HTML saved: {rendered_fixture_path}")
        if network_capture_path:
            print(f"Network capture saved: {network_capture_path}")
        print("First 10 rendered/listed rows:")
        if not rows_to_print:
            print("No rows detected in direct HTML or rendered DOM.")
        for record in rows_to_print[:10]:
            print(f"{record.date} | {record.subject} | {record.circular_no} | {record.link}")
        print("Endpoint inspection results:")
        for result in endpoint_results:
            print(json.dumps(asdict(result), indent=2))

        return {
            "page_title": page_title,
            "category_values": category_values,
            "month_values": month_values,
            "direct_rows_detected": len(direct_rows),
            "rendered_rows_detected": len(rendered_rows),
            "api_urls_found": api_urls_found,
            "endpoint_results": [asdict(result) for result in endpoint_results],
        }

    def inspect_with_playwright(self, url: str) -> dict[str, Any]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Playwright is required for MCX rendered inspection when direct HTML does not expose rows") from exc

        fixture_dir = Path("tests/fixtures/mcx")
        fixture_dir.mkdir(parents=True, exist_ok=True)
        rendered_fixture_path = fixture_dir / "all_circulars_rendered.html"
        network_capture_path = fixture_dir / "all_circulars_network_capture.json"

        network_events: list[dict[str, Any]] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=self.headers["User-Agent"],
                locale="en-IN",
                viewport={"width": 1440, "height": 900},
            )
            page = context.new_page()

            def capture_response(response: Any) -> None:
                if "GetCircular" not in response.url:
                    return
                try:
                    body_text = response.text()
                except Exception:
                    body_text = ""
                sample_records: list[dict[str, Any]] = []
                keys: list[str] = []
                record_count = 0
                if body_text:
                    try:
                        payload = json.loads(body_text)
                        data = payload.get("d") or []
                        record_count = len(data)
                        if data:
                            keys = sorted(data[0].keys())
                            sample_records = data[:3]
                    except json.JSONDecodeError:
                        pass
                network_events.append(
                    {
                        "url": response.url,
                        "method": response.request.method,
                        "status_code": response.status,
                        "content_type": response.headers.get("content-type"),
                        "response_size": len(body_text),
                        "record_count": record_count,
                        "keys_or_headers": keys,
                        "sample_records": sample_records,
                        "post_data": response.request.post_data,
                    }
                )

            page.on("response", capture_response)
            page.goto(url, wait_until="networkidle", timeout=120000)
            rendered_html = page.content()
            rendered_fixture_path.write_text(rendered_html, encoding="utf-8")
            browser.close()

        network_capture_path.write_text(json.dumps(network_events, indent=2), encoding="utf-8")
        return {
            "rendered_fixture_path": str(rendered_fixture_path),
            "network_capture_path": str(network_capture_path),
            "rows": self.parse_rendered_rows(rendered_html, url),
            "network_events": network_events,
        }

    def discover_circular_range(self, url: str) -> dict[str, Any]:
        newest_available_date = self.fetch_newest_available_date()
        oldest_available_date, earliest_records = self.discover_oldest_available_date(newest_available_date)

        result = {
            "working_endpoint_or_page_flow": {
                "page": url,
                "advanced_search_endpoint": MCX_ADVANCED_SEARCH_URL,
            },
            "working_query_params_or_form_fields": {
                "advanced_search": ["CircularType", "CircularNo", "Title", "FromDate", "ToDate"],
            },
            "direct_http_worked": True,
            "playwright_used": False,
            "newest_available_circular_date": newest_available_date.isoformat(),
            "oldest_available_circular_date": oldest_available_date.isoformat(),
            "total_count": None,
            "sample_earliest_records": [asdict(record) for record in earliest_records[:5]],
            "limitation": (
                "The listing table itself is client-rendered, but the public JSON webmethod "
                "/backpage.aspx/GetCircularAdvanceSearch returns the underlying circular records directly. "
                "Discovery proves the oldest and newest available dates from live rows; total archive count is not returned by the API."
            ),
        }

        print(f"Working endpoint or page-flow: {json.dumps(result['working_endpoint_or_page_flow'])}")
        print(f"Working query parameters/form fields: {json.dumps(result['working_query_params_or_form_fields'])}")
        print(f"Direct HTTP worked: {result['direct_http_worked']}")
        print(f"Playwright used: {result['playwright_used']}")
        print(f"Newest circular date found: {result['newest_available_circular_date']}")
        print(f"Oldest circular date found: {result['oldest_available_circular_date']}")
        print(f"Total count: {result['total_count']}")
        print("Sample earliest 5 records:")
        for record in earliest_records[:5]:
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
        category: str | None = None,
        resume: bool = False,
        checkpoint_path: str | Path | None = None,
        max_chunks_this_run: int | None = None,
        delay_seconds: float = 1.5,
        retries: int = 5,
        retry_base_delay: float = 3.0,
        retry_max_delay: float = 60.0,
        all_available: bool = False,
    ) -> list[MCXCircularRecord]:
        out_path = Path(out_path)
        checkpoint_file = Path(checkpoint_path) if checkpoint_path else Path(f"{out_path}.checkpoint.json")
        self.ensure_output_writable(out_path, resume=resume)

        resolved_category = self.resolve_category(category)
        if to_date is None:
            to_date = self.fetch_newest_available_date()

        oldest_available_date: date | None = None
        total_records_detected: int | None = None
        if all_available and from_date is None:
            oldest_available_date, _ = self.discover_oldest_available_date(to_date)
            from_date = oldest_available_date
            total_records_detected = self.count_all_available_records(from_date, to_date, category=resolved_category)

        if from_date is None:
            from_date = date(to_date.year, to_date.month, 1)

        if from_date > to_date:
            raise ValueError("from_date must be less than or equal to to_date")

        chunks = self.build_chunks(from_date, to_date)
        existing_records = self.load_existing_output_records(out_path) if resume and out_path.exists() else []
        existing_keys = {self.record_dedup_key(record) for record in existing_records}
        existing_count = len(existing_records)
        output_mode = "append" if resume and out_path.exists() else "overwrite"

        if resume and checkpoint_file.exists():
            checkpoint = self.load_checkpoint(checkpoint_file)
        else:
            checkpoint = MCXCheckpoint(
                source_url=url,
                output_path=str(out_path),
                newest_available_date=to_date.isoformat(),
                oldest_available_date=(oldest_available_date or from_date).isoformat(),
                total_records_detected=total_records_detected,
                chunk_strategy="yearly_date_chunks",
                last_completed_chunk=0,
                records_written=existing_count,
                unique_records_written=existing_count,
                started_at=datetime.now(timezone.utc).isoformat(),
                updated_at=datetime.now(timezone.utc).isoformat(),
                completed=False,
                errors=[],
            )
            if resume or checkpoint_path:
                self.save_checkpoint(checkpoint_file, checkpoint)

        chunk_window = self.compute_chunk_window(
            total_chunks=len(chunks),
            previous_last_completed_chunk=checkpoint.last_completed_chunk,
            max_chunks_this_run=max_chunks_this_run,
        )
        start_chunk = int(chunk_window["resume_from_chunk"])
        expected_end_chunk = int(chunk_window["expected_end_chunk"])
        chunks_this_run = int(chunk_window["chunks_this_run"])
        completed = bool(chunk_window["completed"])
        target_chunks = [chunk for chunk in chunks if start_chunk <= chunk.index <= expected_end_chunk]
        expected_rows = None
        if total_records_detected is None and target_chunks:
            expected_rows = sum(
                self.count_records_for_range(chunk.from_date, chunk.to_date, category=resolved_category)
                for chunk in target_chunks
            )

        print(f"Oldest date: {from_date.isoformat()}")
        print(f"Newest date: {to_date.isoformat()}")
        print(f"Expected records: {total_records_detected}")
        print(f"Output path: {out_path}")
        print(f"Output mode: {output_mode}")
        print(f"total_chunks: {len(chunks)}")
        print(f"CSV rows detected: {existing_count}")
        print(f"previous last_completed_chunk: {checkpoint.last_completed_chunk}")
        print(f"resume_from_chunk: {start_chunk}")
        print(f"max_chunks_this_run: {max_chunks_this_run}")
        print(f"expected_end_chunk: {expected_end_chunk}")
        print(f"actual chunk range: {start_chunk}-{expected_end_chunk}" if chunks_this_run else "actual chunk range: none")
        print(f"chunks_processed_this_run: {chunks_this_run}")
        if expected_rows is not None:
            print(f"estimated rows this run: {expected_rows}")

        if completed:
            print("Run already completed. No new chunks to process.")
            return []

        if not resume and output_mode == "overwrite":
            self.write_output([], out_path)

        written_records: list[MCXCircularRecord] = []
        duplicates_skipped = 0
        retry_triggered = False

        for chunk in target_chunks:
            payload, chunk_retry_triggered = self.fetch_chunk_payload_with_retry(
                chunk=chunk,
                category=resolved_category,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
            )
            retry_triggered = retry_triggered or chunk_retry_triggered
            records = self.parse_circular_records(payload, url)
            records = self.filter_records(records, from_date=from_date, to_date=to_date, category=resolved_category)

            fresh_records: list[MCXCircularRecord] = []
            for record in records:
                dedupe_key = self.record_dedup_key(record)
                if dedupe_key in existing_keys:
                    duplicates_skipped += 1
                    continue
                existing_keys.add(dedupe_key)
                fresh_records.append(record)

            self.append_output(fresh_records, out_path)
            written_records.extend(fresh_records)
            self.assert_non_regressing_checkpoint(
                previous_last_completed_chunk=checkpoint.last_completed_chunk,
                new_last_completed_chunk=chunk.index,
            )
            checkpoint.last_completed_chunk = chunk.index
            checkpoint.records_written = existing_count + len(written_records)
            checkpoint.unique_records_written = checkpoint.records_written
            checkpoint.updated_at = datetime.now(timezone.utc).isoformat()
            checkpoint.completed = chunk.index == chunks[-1].index
            checkpoint.total_records_detected = total_records_detected
            if checkpoint_path or resume:
                self.save_checkpoint(checkpoint_file, checkpoint)
            if delay_seconds > 0:
                time.sleep(delay_seconds)

        final_row_count = len(existing_records) + len(written_records)
        print(f"Rows written: {len(written_records)}")
        print(f"Duplicates skipped: {duplicates_skipped}")
        print(f"Final CSV row count: {final_row_count}")
        if checkpoint_path or resume:
            print(f"Checkpoint state: {json.dumps(asdict(checkpoint), indent=2)}")
        if retry_triggered:
            print("Retry logic triggered during this run.")

        return written_records

    def validate_export(self, file_path: str | Path) -> dict[str, Any]:
        file_path = Path(file_path)
        report_path = file_path.parent / "mcx_circulars_validation_report.json"
        year_counts_path = file_path.parent / "mcx_circulars_year_counts.csv"

        malformed_csv_rows = 0
        total_rows = 0
        missing_date = 0
        missing_subject = 0
        missing_circular_no = 0
        missing_link = 0
        empty_links = 0
        pdf_links = 0
        detail_links = 0
        other_links = 0
        duplicate_key_count = 0
        suspicious_rows: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, ...]] = set()
        year_counts: dict[int, int] = {}
        dates_seen: list[str] = []

        with open(file_path, "r", newline="", encoding="utf-8") as file_obj:
            reader = csv.reader(file_obj)
            try:
                headers = next(reader)
            except StopIteration as exc:
                raise RuntimeError(f"MCX export is empty: {file_path}") from exc
            bad_headers = headers != EXPECTED_OUTPUT_HEADERS

            for row_number, row in enumerate(reader, start=2):
                if len(row) != len(EXPECTED_OUTPUT_HEADERS):
                    malformed_csv_rows += 1
                    suspicious_rows.append({"row_number": row_number, "reason": "malformed_csv_row", "row": row})
                    continue

                total_rows += 1
                row_data = dict(zip(EXPECTED_OUTPUT_HEADERS, row, strict=True))
                row_date = row_data["date"].strip()
                subject = row_data["subject"].strip()
                circular_no = row_data["circular_no"].strip()
                link = row_data["link"].strip()

                if not row_date:
                    missing_date += 1
                else:
                    try:
                        parsed_date = date.fromisoformat(row_date)
                        dates_seen.append(row_date)
                        year_counts[parsed_date.year] = year_counts.get(parsed_date.year, 0) + 1
                    except ValueError:
                        missing_date += 1
                        suspicious_rows.append({"row_number": row_number, "reason": "invalid_iso_date", "date": row_date})

                if not subject:
                    missing_subject += 1
                elif len(subject) < 5:
                    suspicious_rows.append({"row_number": row_number, "reason": "subject_shorter_than_5", "subject": subject})

                if not circular_no:
                    missing_circular_no += 1
                    suspicious_rows.append({"row_number": row_number, "reason": "missing_circular_no"})

                if not link:
                    missing_link += 1
                    empty_links += 1
                else:
                    lowered = link.lower().split("?", 1)[0]
                    if lowered.endswith(".pdf"):
                        pdf_links += 1
                    elif "/circulars/" in lowered or lowered.endswith(".aspx") or lowered.endswith(".html"):
                        detail_links += 1
                    else:
                        other_links += 1
                    if not link.startswith("https://www.mcxindia.com/"):
                        suspicious_rows.append({"row_number": row_number, "reason": "unexpected_link_prefix", "link": link})

                dedupe_key = self.record_dedup_key(
                    MCXCircularRecord(
                        date=row_data["date"],
                        subject=row_data["subject"],
                        circular_no=row_data["circular_no"],
                        link=row_data["link"],
                        source_url=row_data["source_url"],
                        scraped_at=row_data["scraped_at"],
                    )
                )
                if dedupe_key in seen_keys:
                    duplicate_key_count += 1
                else:
                    seen_keys.add(dedupe_key)

        sorted_year_counts = dict(sorted(year_counts.items()))
        report = {
            "file": str(file_path),
            "headers_ok": not bad_headers,
            "expected_headers": EXPECTED_OUTPUT_HEADERS,
            "total_rows": total_rows,
            "malformed_csv_rows": malformed_csv_rows,
            "missing_date": missing_date,
            "missing_subject": missing_subject,
            "missing_circular_no": missing_circular_no,
            "missing_link": missing_link,
            "duplicate_key_count": duplicate_key_count,
            "link_type_counts": {
                "pdf": pdf_links,
                "detail_page": detail_links,
                "other": other_links,
                "empty": empty_links,
            },
            "min_date": min(dates_seen) if dates_seen else None,
            "max_date": max(dates_seen) if dates_seen else None,
            "rows_per_year": sorted_year_counts,
            "suspicious_rows": suspicious_rows,
        }

        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        with open(year_counts_path, "w", newline="", encoding="utf-8") as file_obj:
            writer = csv.writer(file_obj)
            writer.writerow(["year", "row_count"])
            for year, row_count in sorted_year_counts.items():
                writer.writerow([year, row_count])

        print(json.dumps(report, indent=2))
        print(f"Validation report saved: {report_path}")
        print(f"Year counts saved: {year_counts_path}")
        return report

    def fetch_page(self, url: str, *, retries: int = 5, retry_base_delay: float = 3.0, retry_max_delay: float = 60.0) -> Any:
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                self.rate_limit()
                with httpx.Client(timeout=httpx.Timeout(self.timeout), follow_redirects=True) as client:
                    response = client.get(
                        url,
                        headers={
                            "User-Agent": MCX_HTTP_USER_AGENT,
                            "Accept-Language": self.headers["Accept-Language"],
                        },
                    )
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
                logger.warning("MCX page request failed for {}. Retry {}/{} after {:.1f}s.", url, attempt, retries, delay)
                time.sleep(delay)

        assert last_exc is not None
        raise last_exc

    def post_json(self, url: str, payload: dict[str, str], *, retries: int = 5, retry_base_delay: float = 3.0, retry_max_delay: float = 60.0) -> Any:
        page_headers = {
            "User-Agent": MCX_HTTP_USER_AGENT,
            "Accept-Language": self.headers["Accept-Language"],
        }
        headers = {
            **self.headers,
            "User-Agent": MCX_HTTP_USER_AGENT,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://www.mcxindia.com",
            "Referer": MCX_CIRCULARS_URL,
        }
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                self.rate_limit()
                with httpx.Client(timeout=httpx.Timeout(self.timeout), follow_redirects=True) as client:
                    warmup_response = client.get(MCX_CIRCULARS_URL, headers=page_headers)
                    warmup_response.raise_for_status()
                    response = client.post(url, headers=headers, content=json.dumps(payload))
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
                logger.warning("MCX API request failed for {}. Retry {}/{} after {:.1f}s.", url, attempt, retries, delay)
                time.sleep(delay)

        assert last_exc is not None
        raise last_exc

    def fetch_advanced_payload(
        self,
        *,
        from_date: date,
        to_date: date,
        category: str,
        retries: int = 5,
        retry_base_delay: float = 3.0,
        retry_max_delay: float = 60.0,
    ) -> dict[str, Any]:
        payload = {
            "CircularType": category,
            "CircularNo": "",
            "Title": "",
            "FromDate": from_date.strftime("%Y%m%d"),
            "ToDate": to_date.strftime("%Y%m%d"),
        }
        response = self.post_json(
            MCX_ADVANCED_SEARCH_URL,
            payload,
            retries=retries,
            retry_base_delay=retry_base_delay,
            retry_max_delay=retry_max_delay,
        )
        return response.json()

    def inspect_advanced_endpoint(self, *, from_date: date, to_date: date, category: str) -> MCXEndpointResult:
        try:
            response = self.post_json(
                MCX_ADVANCED_SEARCH_URL,
                {
                    "CircularType": category,
                    "CircularNo": "",
                    "Title": "",
                    "FromDate": from_date.strftime("%Y%m%d"),
                    "ToDate": to_date.strftime("%Y%m%d"),
                },
            )
            payload = response.json()
            data = payload.get("d") or []
            return MCXEndpointResult(
                url=MCX_ADVANCED_SEARCH_URL,
                method="POST",
                status_code=response.status_code,
                content_type=response.headers.get("content-type"),
                response_size=len(response.text),
                format="json",
                record_count=len(data),
                sample_records=data[:3],
                keys_or_headers=sorted(data[0].keys()) if data else [],
            )
        except Exception as exc:
            return MCXEndpointResult(
                url=MCX_ADVANCED_SEARCH_URL,
                method="POST",
                status_code=getattr(getattr(exc, "response", None), "status_code", None),
                content_type=getattr(getattr(exc, "response", None), "headers", {}).get("content-type") if getattr(exc, "response", None) else None,
                response_size=0,
                format="json",
                record_count=0,
                sample_records=[],
                keys_or_headers=[],
                error=str(exc),
            )

    def inspect_basic_endpoint(self, *, year: str, month: str) -> MCXEndpointResult:
        try:
            response = self.post_json(
                MCX_BASIC_SEARCH_URL,
                {
                    "CircularType": "0",
                    "Year": year,
                    "Month": month,
                },
            )
            payload = response.json()
            data = payload.get("d") or []
            return MCXEndpointResult(
                url=MCX_BASIC_SEARCH_URL,
                method="POST",
                status_code=response.status_code,
                content_type=response.headers.get("content-type"),
                response_size=len(response.text),
                format="json",
                record_count=len(data),
                sample_records=data[:3],
                keys_or_headers=sorted(data[0].keys()) if data else [],
            )
        except Exception as exc:
            response = getattr(exc, "response", None)
            return MCXEndpointResult(
                url=MCX_BASIC_SEARCH_URL,
                method="POST",
                status_code=getattr(response, "status_code", None),
                content_type=response.headers.get("content-type") if response is not None else None,
                response_size=len(response.text) if response is not None else 0,
                format="json",
                record_count=0,
                sample_records=[],
                keys_or_headers=[],
                error=str(exc) if response is None else response.text[:500],
            )

    def parse_circular_records(self, payload: dict[str, Any], source_url: str) -> list[MCXCircularRecord]:
        items = payload.get("d") or []
        scraped_at = datetime.now(timezone.utc).isoformat()
        records: list[MCXCircularRecord] = []
        for item in items:
            subject = normalize_text(item.get("DisplayTitle") or item.get("Title")) or ""
            raw_date = normalize_text(item.get("DisplayCircularDate")) or ""
            parsed_date = parse_indian_date(raw_date)
            if parsed_date is None and item.get("CircularDate"):
                parsed_date = self.parse_mcx_json_date(str(item["CircularDate"]))
            if parsed_date is None:
                continue
            circular_no = normalize_text(str(item.get("CircularNo") or item.get("CircularNumber") or "")) or ""
            link = self.normalize_mcx_link(item.get("Documents") or "", source_url)
            if not subject or not link:
                continue
            records.append(
                MCXCircularRecord(
                    date=parsed_date.isoformat(),
                    subject=subject,
                    circular_no=circular_no,
                    link=link,
                    source_url=source_url,
                    scraped_at=scraped_at,
                    category=normalize_text(item.get("CircularTypesName")),
                    raw_date=raw_date or None,
                )
            )
        records.sort(key=lambda record: (record.date, record.circular_no, record.subject), reverse=True)
        return records

    def parse_rendered_rows(self, html: str, source_url: str) -> list[MCXCircularRecord]:
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("#tblCircular tbody tr")
        scraped_at = datetime.now(timezone.utc).isoformat()
        records: list[MCXCircularRecord] = []
        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 4:
                continue
            raw_date = normalize_text(cells[0].get_text(" ", strip=True)) or ""
            parsed_date = parse_indian_date(raw_date)
            if parsed_date is None:
                continue
            category = normalize_text(cells[1].get_text(" ", strip=True))
            anchor = cells[2].find("a", href=True)
            subject = normalize_text(anchor.get_text(" ", strip=True) if anchor else cells[2].get_text(" ", strip=True)) or ""
            circular_no = normalize_text(cells[3].get_text(" ", strip=True)) or ""
            link = self.normalize_mcx_link(anchor.get("href") if anchor else "", source_url)
            if not subject or not link:
                continue
            records.append(
                MCXCircularRecord(
                    date=parsed_date.isoformat(),
                    subject=subject,
                    circular_no=circular_no,
                    link=link,
                    source_url=source_url,
                    scraped_at=scraped_at,
                    category=category,
                    raw_date=raw_date,
                )
            )
        return records

    def fetch_newest_available_date(self) -> date:
        today = datetime.now(timezone.utc).date()
        records = self.parse_circular_records(
            self.fetch_advanced_payload(
                from_date=date(today.year, 1, 1),
                to_date=today,
                category="ALL",
            ),
            MCX_CIRCULARS_URL,
        )
        if not records:
            raise RuntimeError("MCX advanced search returned zero rows for the current year")
        return max(date.fromisoformat(record.date) for record in records)

    def discover_oldest_available_date(self, newest_available_date: date) -> tuple[date, list[MCXCircularRecord]]:
        earliest_year_records: list[MCXCircularRecord] | None = None
        earliest_year: int | None = None
        seen_non_empty = False
        for year in range(newest_available_date.year, 0, -1):
            probe_end = newest_available_date if year == newest_available_date.year else date(year, 12, 31)
            records = self.parse_circular_records(
                self.fetch_advanced_payload(from_date=date(year, 1, 1), to_date=probe_end, category="ALL"),
                MCX_CIRCULARS_URL,
            )
            if records:
                earliest_year = year
                earliest_year_records = records
                seen_non_empty = True
                continue
            if seen_non_empty:
                break

        if earliest_year is None or earliest_year_records is None:
            raise RuntimeError("MCX oldest-year discovery failed after locating a non-empty boundary window")

        earliest_record = min(earliest_year_records, key=lambda record: (record.date, record.circular_no))
        earliest_date = date.fromisoformat(earliest_record.date)
        earliest_sample = sorted(earliest_year_records, key=lambda record: (record.date, record.circular_no))[:5]
        return earliest_date, earliest_sample

    def count_all_available_records(self, from_date: date, to_date: date, *, category: str = "ALL") -> int:
        total = 0
        for chunk in self.build_chunks(from_date, to_date):
            total += self.count_records_for_range(chunk.from_date, chunk.to_date, category=category)
        return total

    def count_records_for_range(self, from_date: date, to_date: date, *, category: str = "ALL") -> int:
        payload = self.fetch_advanced_payload(from_date=from_date, to_date=to_date, category=category)
        return len(payload.get("d") or [])

    def build_chunks(self, from_date: date, to_date: date) -> list[MCXChunk]:
        chunks: list[MCXChunk] = []
        chunk_start = from_date
        index = 1
        while chunk_start <= to_date:
            chunk_end = min(to_date, date(chunk_start.year, 12, 31))
            chunks.append(MCXChunk(index=index, from_date=chunk_start, to_date=chunk_end))
            chunk_start = chunk_end + timedelta(days=1)
            index += 1
        return chunks

    def fetch_chunk_payload_with_retry(
        self,
        *,
        chunk: MCXChunk,
        category: str,
        retries: int,
        retry_base_delay: float,
        retry_max_delay: float,
    ) -> tuple[dict[str, Any], bool]:
        retry_triggered = False
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                payload = self.fetch_advanced_payload(
                    from_date=chunk.from_date,
                    to_date=chunk.to_date,
                    category=category,
                    retries=1,
                    retry_base_delay=retry_base_delay,
                    retry_max_delay=retry_max_delay,
                )
                return payload, retry_triggered
            except Exception as exc:
                if not self.is_retryable_http_exception(exc):
                    raise
                last_exc = exc
                retry_triggered = True
                if attempt >= retries:
                    break
                delay = self.compute_retry_delay(attempt, base_delay=retry_base_delay, max_delay=retry_max_delay)
                message = (
                    f"Chunk {chunk.index} ({chunk.from_date.isoformat()} to {chunk.to_date.isoformat()}) "
                    f"failed with {exc.__class__.__name__}. Retry {attempt}/{retries} after {delay:.1f}s."
                )
                print(message)
                logger.warning(message)
                time.sleep(delay)

        assert last_exc is not None
        raise last_exc

    def parse_mcx_json_date(self, raw_value: str) -> date | None:
        normalized = normalize_text(raw_value) or ""
        if not normalized.startswith("/Date("):
            return None
        digits = "".join(character for character in normalized if character.isdigit())
        if not digits:
            return None
        # MCX uses .NET epoch milliseconds.
        timestamp = int(digits) / 1000
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).date()

    def resolve_category(self, category: str | None) -> str:
        if category is None:
            return "ALL"
        normalized = normalize_text(category)
        if not normalized:
            return "ALL"
        return MCX_CATEGORY_MAP.get(normalized.casefold(), normalized)

    def normalize_mcx_link(self, raw_value: str, source_url: str) -> str:
        value = normalize_text(raw_value) or ""
        if not value:
            return ""
        absolute = urljoin(source_url, value)
        return absolute.replace("http://www.mcxindia.com/", "https://www.mcxindia.com/")

    def filter_records(
        self,
        records: list[MCXCircularRecord],
        *,
        from_date: date | None,
        to_date: date | None,
        category: str,
    ) -> list[MCXCircularRecord]:
        normalized_category = category.casefold()
        filtered: list[MCXCircularRecord] = []
        for record in records:
            record_date = date.fromisoformat(record.date)
            if from_date and record_date < from_date:
                continue
            if to_date and record_date > to_date:
                continue
            record_category = self.resolve_category(record.category)
            if normalized_category != "all" and record_category.casefold() != normalized_category:
                continue
            filtered.append(record)
        return filtered

    def record_dedup_key(self, record: MCXCircularRecord) -> tuple[str, str, str, str] | tuple[str, str, str]:
        normalized_subject = (normalize_text(record.subject) or "").casefold()
        if record.circular_no:
            return (record.date, normalized_subject, (normalize_text(record.circular_no) or "").casefold(), record.link)
        return (record.date, normalized_subject, record.link)

    def extract_api_urls_from_html(self, html: str) -> list[str]:
        urls: set[str] = set()
        if "/backpage.aspx/GetCircularSearch" in html:
            urls.add(MCX_BASIC_SEARCH_URL)
        if "/backpage.aspx/GetCircularAdvanceSearch" in html:
            urls.add(MCX_ADVANCED_SEARCH_URL)
        return sorted(urls)

    def load_existing_output_records(self, out_path: str | Path) -> list[MCXCircularRecord]:
        out_path = Path(out_path)
        if not out_path.exists():
            return []
        if out_path.suffix.lower() == ".csv":
            with open(out_path, "r", newline="", encoding="utf-8") as file_obj:
                reader = csv.DictReader(file_obj)
                return [
                    MCXCircularRecord(
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
            return [MCXCircularRecord(**row) for row in payload]
        raise ValueError("Output path must end with .csv or .json")

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

    def append_output(self, records: list[MCXCircularRecord], out_path: str | Path) -> None:
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

    def write_output(self, records: list[MCXCircularRecord], out_path: str | Path) -> None:
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

    def record_to_output_row(self, record: MCXCircularRecord) -> dict[str, str]:
        return {
            "date": record.date,
            "subject": record.subject,
            "circular_no": record.circular_no,
            "link": record.link,
            "source_url": record.source_url,
            "scraped_at": record.scraped_at,
        }

    def load_checkpoint(self, checkpoint_path: str | Path) -> MCXCheckpoint:
        payload = json.loads(Path(checkpoint_path).read_text(encoding="utf-8"))
        return MCXCheckpoint(**payload)

    def save_checkpoint(self, checkpoint_path: str | Path, checkpoint: MCXCheckpoint) -> None:
        Path(checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
        Path(checkpoint_path).write_text(json.dumps(asdict(checkpoint), indent=2), encoding="utf-8")
