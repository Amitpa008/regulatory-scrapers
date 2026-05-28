from __future__ import annotations

import csv
import json
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from loguru import logger

from extraction.metadata_cleaner import normalize_text, parse_indian_date
from models import RegulatoryDocument
from scrapers.base import BaseScraper


NCDEX_SOURCE_LABEL = "NCDEX"
NCDEX_CIRCULARS_URL = "https://www.ncdex.com/circulars"
NCDEX_CIRCULAR_DATA_URL = "https://www.ncdex.com/circulars/circular_data"
NCDEX_CATEGORY_URL = "https://www.ncdex.com/circulars/getCategory"
EXPECTED_OUTPUT_HEADERS = ["date", "subject", "circular_no", "link", "source_url", "scraped_at"]
NCDEX_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class NCDEXCircularRecord:
    date: str
    subject: str
    circular_no: str
    link: str
    source_url: str
    scraped_at: str
    department: Optional[str] = None
    category: Optional[str] = None
    raw_date: Optional[str] = None


@dataclass
class NCDEXEndpointResult:
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
class NCDEXChunk:
    index: int
    year: int


@dataclass
class NCDEXCheckpoint:
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


class NCDEXScraper(BaseScraper):
    source = "ncdex"
    regulator = NCDEX_SOURCE_LABEL

    def __init__(self, config: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.config = config
        self.last_fetch_transport = "playwright"

    def fetch_index(self, from_date: date, to_date: date) -> Any:
        with self.browser_session(NCDEX_CIRCULARS_URL, headless=True) as session:
            year_value = from_date.year if from_date.year == to_date.year else 0
            payload = self.fetch_circular_payload(
                year=year_value,
                month=0,
                department=0,
                category=0,
                start=0,
                length=500,
                session=session,
            )
        return payload

    def parse_listing(self, response: Any) -> list[dict[str, Any]]:
        records = self.parse_circular_records(response, NCDEX_CIRCULARS_URL)
        return [
            {
                "title": record.subject,
                "url": record.link,
                "document_type": "Circular",
                "reference_no": record.circular_no or None,
                "published_date": date.fromisoformat(record.date),
                "department": record.department,
                "category": "Circulars",
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
            pdf_url=None,
            pdf_sha256=None,
            text_content=None,
            scraped_at=datetime.now(timezone.utc),
        )

    def inspect_circulars(self, url: str) -> dict[str, Any]:
        fixture_dir = Path("tests/fixtures/ncdex")
        fixture_dir.mkdir(parents=True, exist_ok=True)

        raw_response = self.fetch_raw_page(url)
        raw_fixture_path = fixture_dir / "circulars.html"
        raw_fixture_path.write_text(raw_response.text, encoding="utf-8")

        rendered_fixture_path: Path | None = None
        network_capture_path: Path | None = None
        rendered_records = self.parse_rendered_rows(raw_response.text, url)
        rendered_info = self.collect_page_info(raw_response.text, url)
        endpoint_results: list[NCDEXEndpointResult] = []
        playwright_used = False

        if not rendered_records:
            playwright_used = True
            rendered = self.inspect_with_playwright(url)
            rendered_fixture_path = fixture_dir / "circulars_rendered.html"
            network_capture_path = fixture_dir / "circulars_network_capture.json"
            rendered_fixture_path.write_text(rendered["html"], encoding="utf-8")
            network_capture_path.write_text(json.dumps(rendered["network_capture"], indent=2), encoding="utf-8")
            rendered_records = rendered["records"]
            rendered_info = rendered["page_info"]
            with self.browser_session(url, headless=True) as session:
                endpoint_results.append(
                    self.inspect_data_endpoint(
                        year=max(self.available_years_from_page_info(rendered_info)),
                        month=0,
                        department=0,
                        category=0,
                        start=0,
                        length=50,
                        session=session,
                    )
                )
                first_department = next(
                    (item["value"] for item in rendered_info["department_options"] if item["value"] not in {"", "0"}),
                    None,
                )
                if first_department:
                    endpoint_results.append(self.inspect_category_endpoint(first_department, session=session))

        print(f"Page title: {rendered_info['title']}")
        print(f"Basic/Advanced filters present: {rendered_info['basic_advanced_filters_present']}")
        print(f"Category filter values: {[item['label'] for item in rendered_info['category_options']]}")
        print(
            "Month/year/date controls found: "
            f"month={bool(rendered_info['month_options'])}, year={bool(rendered_info['year_options'])}"
        )
        print(f"Pagination controls found: {rendered_info['pagination_controls']}")
        print(f"Table/listing selectors found: {rendered_info['listing_selectors']}")
        print("Any API URLs found in scripts:")
        for item in sorted(self.extract_api_urls_from_html(raw_response.text) | {NCDEX_CIRCULAR_DATA_URL, NCDEX_CATEGORY_URL}):
            print(item)
        print("Any circular PDF/detail URL patterns found:")
        for item in rendered_info["pdf_patterns"][:10]:
            print(item)
        print("First 10 rendered/listed rows:")
        for record in rendered_records[:10]:
            print(f"{record.date} | {record.subject} | {record.circular_no} | {record.link}")
        for endpoint_result in endpoint_results:
            print(json.dumps(asdict(endpoint_result), indent=2))

        return {
            "page_title": rendered_info["title"],
            "direct_http_exposed_rows": bool(self.parse_rendered_rows(raw_response.text, url)),
            "playwright_used": playwright_used,
            "rendered_rows_detected": len(rendered_records),
            "raw_fixture_path": str(raw_fixture_path),
            "rendered_fixture_path": str(rendered_fixture_path) if rendered_fixture_path else None,
            "network_capture_path": str(network_capture_path) if network_capture_path else None,
            "endpoint_results": [asdict(item) for item in endpoint_results],
        }

    def discover_circular_range(self, url: str) -> dict[str, Any]:
        with self.browser_session(url, headless=True) as session:
            page_info = self.collect_page_info(session["page"].content(), url)
            years = self.available_years_from_page_info(page_info)
            if not years:
                raise RuntimeError("NCDEX did not expose any year filter values")

            full_payload = self.fetch_circular_payload(
                year=0,
                month=0,
                department=0,
                category=0,
                start=0,
                length=1000,
                session=session,
            )
            total_records = int(full_payload.get("recordsFiltered") or full_payload.get("recordsTotal") or 0) or None
            newest_records = self.fetch_all_for_year(year=max(years), session=session)
            if not newest_records:
                raise RuntimeError("NCDEX newest year returned zero rows")
            newest_date = max(date.fromisoformat(item.date) for item in newest_records)

            oldest_records: list[NCDEXCircularRecord] = []
            for year in sorted(years):
                candidate = self.fetch_all_for_year(year=year, session=session)
                if candidate:
                    oldest_records = sorted(candidate, key=lambda item: (item.date, item.circular_no, item.subject))
                    break
            if not oldest_records:
                raise RuntimeError("NCDEX oldest boundary could not be proven from live data")

        result = {
            "working_endpoint_or_page_flow": {
                "page": url,
                "data_endpoint": NCDEX_CIRCULAR_DATA_URL,
                "category_endpoint": NCDEX_CATEGORY_URL,
            },
            "working_query_parameters_or_form_fields": {
                "data_endpoint": ["draw", "start", "length", "_token", "sf", "st", "year", "month", "dept", "cat"],
                "category_endpoint": ["value", "_token"],
            },
            "direct_http_worked": False,
            "playwright_used": True,
            "newest_circular_date_found": newest_date.isoformat(),
            "oldest_circular_date_found": oldest_records[0].date,
            "total_count": total_records,
            "sample_earliest_records": [asdict(item) for item in oldest_records[:5]],
            "limitation": (
                "NCDEX serves a fingerprint challenge to raw HTTP, so live listing data requires a normal browser "
                "session to bootstrap the CSRF token and cookies before calling /circulars/circular_data."
            ),
        }

        print(f"Working endpoint or page-flow: {json.dumps(result['working_endpoint_or_page_flow'])}")
        print(f"Working query parameters/form fields: {json.dumps(result['working_query_parameters_or_form_fields'])}")
        print(f"Direct HTTP worked: {result['direct_http_worked']}")
        print(f"Playwright used: {result['playwright_used']}")
        print(f"Newest circular date found: {result['newest_circular_date_found']}")
        print(f"Oldest circular date found: {result['oldest_circular_date_found']}")
        print(f"Total count: {result['total_count']}")
        print("Sample earliest 5 records:")
        for item in oldest_records[:5]:
            print(f"{item.date} | {item.subject} | {item.circular_no} | {item.link}")
        print(f"Limitation: {result['limitation']}")
        return result

    def scrape_listing_url(
        self,
        *,
        url: str,
        out_path: str | Path,
        from_date: date | None = None,
        to_date: date | None = None,
        department: str | None = None,
        resume: bool = False,
        checkpoint_path: str | Path | None = None,
        max_chunks_this_run: int | None = None,
        delay_seconds: float = 1.5,
        retries: int = 5,
        retry_base_delay: float = 3.0,
        retry_max_delay: float = 60.0,
        all_available: bool = False,
        headless: bool = True,
    ) -> list[NCDEXCircularRecord]:
        out_path = Path(out_path)
        checkpoint_file = Path(checkpoint_path) if checkpoint_path else Path(f"{out_path}.checkpoint.json")
        self.ensure_output_writable(out_path, resume=resume)

        with self.browser_session(url, headless=headless) as session:
            page_info = self.collect_page_info(session["page"].content(), url)
            years = self.available_years_from_page_info(page_info)
            if not years:
                raise RuntimeError("NCDEX did not expose any year filter values")

            total_payload = self.fetch_circular_payload(
                year=0,
                month=0,
                department=0,
                category=0,
                start=0,
                length=1000,
                session=session,
                retries=retries,
                retry_base_delay=retry_base_delay,
                retry_max_delay=retry_max_delay,
            )
            total_records = int(total_payload.get("recordsFiltered") or total_payload.get("recordsTotal") or 0) or None
            latest_records = self.fetch_all_for_year(year=max(years), session=session)
            if not latest_records:
                raise RuntimeError("NCDEX newest year returned zero rows")
            newest_available_date = max(date.fromisoformat(item.date) for item in latest_records)

            if all_available and from_date is None:
                earliest_year_records = self.fetch_all_for_year(year=min(years), session=session)
                if not earliest_year_records:
                    raise RuntimeError("NCDEX earliest visible year returned zero rows")
                from_date = min(date.fromisoformat(item.date) for item in earliest_year_records)
            if from_date is None:
                from_date = date(newest_available_date.year, 1, 1)
            if to_date is None:
                to_date = newest_available_date
            if from_date > to_date:
                raise ValueError("from_date must be less than or equal to to_date")

            department_id = self.resolve_department_id(page_info, department)
            chunk_years = [year for year in years if from_date.year <= year <= to_date.year]
            chunks = [NCDEXChunk(index=index, year=year) for index, year in enumerate(chunk_years, start=1)]

            existing_records = self.load_existing_output_records(out_path) if resume and out_path.exists() else []
            existing_keys = {self.record_dedup_key(item) for item in existing_records}
            existing_count = len(existing_records)
            output_mode = "append" if resume and out_path.exists() else "overwrite"

            if resume and checkpoint_file.exists():
                checkpoint = self.load_checkpoint(checkpoint_file)
            else:
                checkpoint = NCDEXCheckpoint(
                    source_url=url,
                    output_path=str(out_path),
                    newest_available_date=newest_available_date.isoformat(),
                    oldest_available_date=from_date.isoformat(),
                    total_records_detected=total_records,
                    chunk_strategy="year_filters_via_browser_session",
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
            target_chunks = [item for item in chunks if start_chunk <= item.index <= expected_end_chunk]

            print(f"Oldest date: {from_date.isoformat()}")
            print(f"Newest date: {to_date.isoformat()}")
            print(f"Expected records: {total_records}")
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

            if completed:
                print("Run already completed. No new chunks to process.")
                return []

            if not resume and output_mode == "overwrite":
                self.write_output([], out_path)

            written_records: list[NCDEXCircularRecord] = []
            duplicates_skipped = 0
            retry_triggered = False

            for chunk in target_chunks:
                payload, chunk_retry_triggered = self.fetch_chunk_payload_with_retry(
                    chunk=chunk,
                    department=department_id,
                    session=session,
                    retries=retries,
                    retry_base_delay=retry_base_delay,
                    retry_max_delay=retry_max_delay,
                )
                retry_triggered = retry_triggered or chunk_retry_triggered
                records = self.filter_records(
                    self.parse_circular_records(payload, url),
                    from_date=from_date,
                    to_date=to_date,
                    department=department,
                )
                fresh_records: list[NCDEXCircularRecord] = []
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
                checkpoint.completed = bool(chunks) and chunk.index == chunks[-1].index
                if resume or checkpoint_path:
                    self.save_checkpoint(checkpoint_file, checkpoint)
                if delay_seconds > 0:
                    time.sleep(delay_seconds)

        print(f"Rows written: {len(written_records)}")
        print(f"Duplicates skipped: {duplicates_skipped}")
        print(f"Final CSV row count: {existing_count + len(written_records)}")
        if resume or checkpoint_path:
            print(f"Checkpoint state: {json.dumps(asdict(checkpoint), indent=2)}")
        if retry_triggered:
            print("Retry logic triggered during this run.")
        self.last_fetch_transport = "playwright"
        return written_records

    def validate_export(self, file_path: str | Path) -> dict[str, Any]:
        file_path = Path(file_path)
        report_path = file_path.parent / "ncdex_circulars_validation_report.json"
        year_counts_path = file_path.parent / "ncdex_circulars_year_counts.csv"

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
                raise RuntimeError(f"NCDEX export is empty: {file_path}") from exc
            headers_ok = headers == EXPECTED_OUTPUT_HEADERS

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
                    elif "/circulars/" in lowered or lowered.endswith(".html") or lowered.endswith(".php"):
                        detail_links += 1
                    else:
                        other_links += 1
                    if not link.startswith("https://www.ncdex.com/"):
                        suspicious_rows.append({"row_number": row_number, "reason": "unexpected_link_prefix", "link": link})

                dedupe_key = self.record_dedup_key(
                    NCDEXCircularRecord(
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
            "headers_ok": headers_ok,
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

    def fetch_raw_page(self, url: str) -> httpx.Response:
        client = httpx.Client(
            headers={"User-Agent": NCDEX_BROWSER_USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"},
            follow_redirects=True,
            timeout=httpx.Timeout(self.timeout),
        )
        try:
            response = client.get(url)
            response.raise_for_status()
            self.last_fetch_transport = "httpx"
            return response
        finally:
            client.close()

    def inspect_with_playwright(self, url: str) -> dict[str, Any]:
        with self.browser_session(url, headless=True) as session:
            html = session["page"].content()
            return {
                "html": html,
                "records": self.parse_rendered_rows(html, url),
                "network_capture": list(session["network_capture"]),
                "page_info": self.collect_page_info(html, url),
            }

    def collect_page_info(self, html: str, source_url: str) -> dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")
        pdf_patterns = sorted(
            {
                self.normalize_ncdex_link(anchor.get("href", ""), source_url)
                for anchor in soup.select('a[href$=".pdf"], a[href*="/public/uploads/"]')
                if anchor.get("href")
            }
        )
        return {
            "title": soup.title.get_text(" ", strip=True) if soup.title else "",
            "basic_advanced_filters_present": bool(soup.select("#sf, .circular-search-tab, .advance-search")),
            "year_options": self.extract_select_options(soup, "#year"),
            "month_options": self.extract_select_options(soup, "#month"),
            "department_options": self.extract_select_options(soup, "#dept"),
            "category_options": self.extract_select_options(soup, "#cat"),
            "pagination_controls": [
                text
                for text in [
                    normalize_text(node.get_text(" ", strip=True))
                    for node in soup.select("#file_tbl_paginate a, .paginate_button")
                ]
                if text
            ],
            "listing_selectors": [selector for selector in ["#file_tbl", "#file_tbl tbody", "#file_tbl_wrapper"] if soup.select(selector)],
            "pdf_patterns": pdf_patterns,
        }

    def extract_select_options(self, soup: BeautifulSoup, selector: str) -> list[dict[str, str]]:
        return [
            {"value": option.get("value", ""), "label": normalize_text(option.get_text(" ", strip=True)) or ""}
            for option in soup.select(f"{selector} option")
        ]

    def available_years_from_page_info(self, page_info: dict[str, Any]) -> list[int]:
        return sorted(
            {
                int(item["value"])
                for item in page_info["year_options"]
                if item["value"].isdigit() and int(item["value"]) > 0
            }
        )

    @contextmanager
    def browser_session(self, url: str, *, headless: bool) -> Iterator[dict[str, Any]]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Playwright is required for NCDEX because the page is browser-bootstrapped") from exc

        network_capture: list[dict[str, Any]] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=headless)
            context = browser.new_context(locale="en-IN", user_agent=NCDEX_BROWSER_USER_AGENT)
            page = context.new_page()

            def on_response(response: Any) -> None:
                if not any(token in response.url for token in ["/circular_data", "/getCategory", "/__verify/fp"]):
                    return
                try:
                    body_text = response.text()
                except Exception:
                    body_text = ""
                network_capture.append(
                    {
                        "url": response.url,
                        "status": response.status,
                        "method": response.request.method,
                        "content_type": response.headers.get("content-type"),
                        "response_size": len(body_text),
                        "post_data": response.request.post_data,
                    }
                )

            page.on("response", on_response)
            last_error: Exception | None = None
            for attempt in range(1, 4):
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=120000)
                    if self.wait_for_circular_controls(page):
                        last_error = None
                        break
                    html = page.content()
                    if "__verify/fp" in html or "/__verify/fp" in html:
                        page.wait_for_timeout(3000)
                        page.reload(wait_until="domcontentloaded", timeout=120000)
                        if self.wait_for_circular_controls(page):
                            last_error = None
                            break
                    last_error = RuntimeError("NCDEX browser session loaded but circular controls were not rendered")
                except Exception as exc:
                    last_error = exc
                    if attempt >= 3:
                        break
                    delay_ms = int(self.compute_retry_delay(attempt, base_delay=3.0, max_delay=15.0) * 1000)
                    page.wait_for_timeout(delay_ms)
            if last_error is not None:
                raise last_error
            yield {"page": page, "network_capture": network_capture}
            browser.close()

    def wait_for_circular_controls(self, page: Any) -> bool:
        for _ in range(10):
            try:
                if page.locator("#year option").count() > 1 and page.locator("#file_tbl").count() > 0:
                    return True
            except Exception:
                pass
            page.wait_for_timeout(1000)
        return False

    def fetch_browser_payload(
        self,
        *,
        payload: dict[str, Any],
        endpoint: str,
        session: dict[str, Any],
    ) -> dict[str, Any] | list[dict[str, Any]]:
        page = session["page"]
        result = page.evaluate(
            """
            async ({ endpoint, payload }) => {
                const token =
                    document.querySelector('meta[name="csrf-token"]')?.content ||
                    document.querySelector('input[name="_token"]')?.value ||
                    "";
                const body = new URLSearchParams();
                for (const [key, value] of Object.entries(payload)) {
                    body.set(key, String(value));
                }
                if (!body.has('_token')) {
                    body.set('_token', token);
                }
                const response = await fetch(endpoint, {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: {
                        'X-Requested-With': 'XMLHttpRequest',
                        'X-CSRF-TOKEN': token,
                        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                        'Accept': 'application/json, text/javascript, */*; q=0.01',
                    },
                    body: body.toString(),
                });
                const text = await response.text();
                return { status: response.status, text };
            }
            """,
            {"endpoint": endpoint, "payload": payload},
        )
        if result["status"] >= 400:
            request = httpx.Request("POST", endpoint)
            response = httpx.Response(result["status"], request=request, text=result["text"])
            raise httpx.HTTPStatusError(
                f"NCDEX browser-backed request failed with status {result['status']}",
                request=request,
                response=response,
            )
        return json.loads(result["text"])

    def build_datatable_payload(
        self,
        *,
        draw: int,
        start: int,
        length: int,
        year: int,
        month: int,
        department: int,
        category: int,
        sf: int = 1,
        st: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "draw": draw,
            "start": start,
            "length": length,
            "search[value]": "",
            "search[regex]": "false",
            "order[0][column]": 0,
            "order[0][dir]": "desc",
            "sf": sf,
            "st": st,
            "year": year,
            "month": month,
            "dept": department,
            "cat": category,
        }
        for index, column_name in enumerate(["date", "number", "department", "subject", "en_file", "hn_file"]):
            payload[f"columns[{index}][data]"] = column_name
            payload[f"columns[{index}][name]"] = ""
            payload[f"columns[{index}][searchable]"] = "true"
            payload[f"columns[{index}][orderable]"] = "true" if index < 4 else "false"
            payload[f"columns[{index}][search][value]"] = ""
            payload[f"columns[{index}][search][regex]"] = "false"
        return payload

    def fetch_circular_payload(
        self,
        *,
        year: int,
        month: int,
        department: int,
        category: int,
        start: int,
        length: int,
        session: dict[str, Any],
        retries: int = 5,
        retry_base_delay: float = 3.0,
        retry_max_delay: float = 60.0,
    ) -> dict[str, Any]:
        payload = self.build_datatable_payload(
            draw=1 + start // max(length, 1),
            start=start,
            length=length,
            year=year,
            month=month,
            department=department,
            category=category,
        )
        response_payload = self.fetch_browser_payload_with_retry(
            endpoint=NCDEX_CIRCULAR_DATA_URL,
            payload=payload,
            session=session,
            retries=retries,
            retry_base_delay=retry_base_delay,
            retry_max_delay=retry_max_delay,
        )
        self.last_fetch_transport = "playwright"
        assert isinstance(response_payload, dict)
        return response_payload

    def fetch_browser_payload_with_retry(
        self,
        *,
        endpoint: str,
        payload: dict[str, Any],
        session: dict[str, Any],
        retries: int,
        retry_base_delay: float,
        retry_max_delay: float,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                return self.fetch_browser_payload(endpoint=endpoint, payload=payload, session=session)
            except Exception as exc:
                if not self.is_retryable_http_exception(exc):
                    raise
                last_exc = exc
                if attempt >= retries:
                    break
                delay = self.compute_retry_delay(attempt, base_delay=retry_base_delay, max_delay=retry_max_delay)
                logger.warning(
                    "NCDEX request failed for {}. Retry {}/{} after {:.1f}s.",
                    endpoint,
                    attempt,
                    retries,
                    delay,
                )
                time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    def inspect_data_endpoint(
        self,
        *,
        year: int,
        month: int,
        department: int,
        category: int,
        start: int,
        length: int,
        session: dict[str, Any],
    ) -> NCDEXEndpointResult:
        try:
            payload = self.fetch_circular_payload(
                year=year,
                month=month,
                department=department,
                category=category,
                start=start,
                length=length,
                session=session,
            )
            rows = payload.get("data") or []
            return NCDEXEndpointResult(
                url=NCDEX_CIRCULAR_DATA_URL,
                method="POST",
                status_code=200,
                content_type="application/json",
                response_size=len(json.dumps(payload)),
                format="json",
                record_count=len(rows),
                sample_records=rows[:3],
                keys_or_headers=sorted(rows[0].keys()) if rows else [],
            )
        except Exception as exc:
            return NCDEXEndpointResult(
                url=NCDEX_CIRCULAR_DATA_URL,
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

    def inspect_category_endpoint(self, department_id: str, *, session: dict[str, Any]) -> NCDEXEndpointResult:
        try:
            payload = self.fetch_browser_payload_with_retry(
                endpoint=NCDEX_CATEGORY_URL,
                payload={"value": department_id},
                session=session,
                retries=5,
                retry_base_delay=3.0,
                retry_max_delay=60.0,
            )
            rows = payload if isinstance(payload, list) else []
            return NCDEXEndpointResult(
                url=NCDEX_CATEGORY_URL,
                method="POST",
                status_code=200,
                content_type="application/json",
                response_size=len(json.dumps(payload)),
                format="json",
                record_count=len(rows),
                sample_records=rows[:3],
                keys_or_headers=sorted(rows[0].keys()) if rows else [],
            )
        except Exception as exc:
            return NCDEXEndpointResult(
                url=NCDEX_CATEGORY_URL,
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

    def fetch_all_for_year(
        self,
        *,
        year: int,
        department: int = 0,
        session: dict[str, Any],
    ) -> list[NCDEXCircularRecord]:
        count_payload = self.fetch_circular_payload(
            year=year,
            month=0,
            department=department,
            category=0,
            start=0,
            length=1,
            session=session,
        )
        total = int(count_payload.get("recordsFiltered") or 0)
        if total == 0:
            return []
        payload = self.fetch_circular_payload(
            year=year,
            month=0,
            department=department,
            category=0,
            start=0,
            length=max(total, 1),
            session=session,
        )
        return self.parse_circular_records(payload, NCDEX_CIRCULARS_URL)

    def fetch_chunk_payload_with_retry(
        self,
        *,
        chunk: NCDEXChunk,
        department: int,
        session: dict[str, Any],
        retries: int,
        retry_base_delay: float,
        retry_max_delay: float,
    ) -> tuple[dict[str, Any], bool]:
        retry_triggered = False
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                count_payload = self.fetch_circular_payload(
                    year=chunk.year,
                    month=0,
                    department=department,
                    category=0,
                    start=0,
                    length=1,
                    session=session,
                    retries=1,
                    retry_base_delay=retry_base_delay,
                    retry_max_delay=retry_max_delay,
                )
                total = int(count_payload.get("recordsFiltered") or 0)
                payload = self.fetch_circular_payload(
                    year=chunk.year,
                    month=0,
                    department=department,
                    category=0,
                    start=0,
                    length=max(total, 1),
                    session=session,
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
                message = f"Chunk {chunk.index} ({chunk.year}) failed with {exc.__class__.__name__}. Retry {attempt}/{retries} after {delay:.1f}s."
                print(message)
                logger.warning(message)
                time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    def parse_circular_records(self, payload: dict[str, Any], source_url: str) -> list[NCDEXCircularRecord]:
        rows = payload.get("data") or []
        scraped_at = datetime.now(timezone.utc).isoformat()
        records: list[NCDEXCircularRecord] = []
        for row in rows:
            raw_date = normalize_text(row.get("date")) or ""
            parsed_date = parse_indian_date(raw_date)
            if parsed_date is None:
                continue
            subject = normalize_text(row.get("subject")) or ""
            circular_no = normalize_text(row.get("number")) or ""
            department = normalize_text(row.get("department"))
            link = self.extract_link_from_html(row.get("en_file") or "") or self.extract_link_from_html(row.get("hn_file") or "")
            if not subject or not link:
                continue
            records.append(
                NCDEXCircularRecord(
                    date=parsed_date.isoformat(),
                    subject=subject,
                    circular_no=circular_no,
                    link=self.normalize_ncdex_link(link, source_url),
                    source_url=source_url,
                    scraped_at=scraped_at,
                    department=department,
                    raw_date=raw_date or None,
                )
            )
        records.sort(key=lambda item: (item.date, item.circular_no, item.subject), reverse=True)
        return records

    def parse_rendered_rows(self, html: str, source_url: str) -> list[NCDEXCircularRecord]:
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("#file_tbl tbody tr")
        scraped_at = datetime.now(timezone.utc).isoformat()
        records: list[NCDEXCircularRecord] = []
        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 5:
                continue
            raw_date = normalize_text(cells[0].get_text(" ", strip=True)) or ""
            parsed_date = parse_indian_date(raw_date)
            if parsed_date is None:
                continue
            circular_no = normalize_text(cells[1].get_text(" ", strip=True)) or ""
            department = normalize_text(cells[2].get_text(" ", strip=True))
            subject = normalize_text(cells[3].get_text(" ", strip=True)) or ""
            link = self.extract_link_from_html(str(cells[4])) or self.extract_link_from_html(str(cells[3]))
            if not subject or not link:
                continue
            records.append(
                NCDEXCircularRecord(
                    date=parsed_date.isoformat(),
                    subject=subject,
                    circular_no=circular_no,
                    link=self.normalize_ncdex_link(link, source_url),
                    source_url=source_url,
                    scraped_at=scraped_at,
                    department=department,
                    raw_date=raw_date,
                )
            )
        return records

    def extract_link_from_html(self, html_fragment: str) -> str:
        if not html_fragment:
            return ""
        soup = BeautifulSoup(html_fragment, "html.parser")
        anchor = soup.find("a", href=True)
        return normalize_text(anchor.get("href") if anchor else "") or ""

    def normalize_ncdex_link(self, raw_value: str, source_url: str) -> str:
        value = normalize_text(raw_value) or ""
        if not value:
            return ""
        absolute = urljoin(source_url, value)
        return absolute.replace("http://www.ncdex.com/", "https://www.ncdex.com/")

    def resolve_department_id(self, page_info: dict[str, Any], department: str | None) -> int:
        if department is None:
            return 0
        normalized = (normalize_text(department) or "").casefold()
        if not normalized:
            return 0
        for option in page_info["department_options"]:
            if (option["label"] or "").casefold() == normalized:
                try:
                    return int(option["value"])
                except ValueError:
                    return 0
        raise ValueError(f"Unknown NCDEX department: {department}")

    def filter_records(
        self,
        records: list[NCDEXCircularRecord],
        *,
        from_date: date | None,
        to_date: date | None,
        department: str | None,
    ) -> list[NCDEXCircularRecord]:
        department_norm = (normalize_text(department) or "").casefold() if department else ""
        filtered: list[NCDEXCircularRecord] = []
        for record in records:
            record_date = date.fromisoformat(record.date)
            if from_date and record_date < from_date:
                continue
            if to_date and record_date > to_date:
                continue
            if department_norm and (normalize_text(record.department) or "").casefold() != department_norm:
                continue
            filtered.append(record)
        return filtered

    def record_dedup_key(self, record: NCDEXCircularRecord) -> tuple[str, str, str, str] | tuple[str, str, str]:
        normalized_subject = (normalize_text(record.subject) or "").casefold()
        if record.circular_no:
            return (record.date, normalized_subject, (normalize_text(record.circular_no) or "").casefold(), record.link)
        return (record.date, normalized_subject, record.link)

    def extract_api_urls_from_html(self, html: str) -> set[str]:
        urls: set[str] = set()
        if "/circulars/circular_data" in html:
            urls.add(NCDEX_CIRCULAR_DATA_URL)
        if "/circulars/getCategory" in html:
            urls.add(NCDEX_CATEGORY_URL)
        return urls

    def load_existing_output_records(self, out_path: str | Path) -> list[NCDEXCircularRecord]:
        out_path = Path(out_path)
        if not out_path.exists():
            return []
        if out_path.suffix.lower() == ".csv":
            with open(out_path, "r", newline="", encoding="utf-8") as file_obj:
                reader = csv.DictReader(file_obj)
                return [
                    NCDEXCircularRecord(
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
            return [NCDEXCircularRecord(**row) for row in payload]
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

    def append_output(self, records: list[NCDEXCircularRecord], out_path: str | Path) -> None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [self.record_to_output_row(item) for item in records]
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

    def write_output(self, records: list[NCDEXCircularRecord], out_path: str | Path) -> None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [self.record_to_output_row(item) for item in records]
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

    def record_to_output_row(self, record: NCDEXCircularRecord) -> dict[str, str]:
        return {
            "date": record.date,
            "subject": record.subject,
            "circular_no": record.circular_no,
            "link": record.link,
            "source_url": record.source_url,
            "scraped_at": record.scraped_at,
        }

    def load_checkpoint(self, checkpoint_path: str | Path) -> NCDEXCheckpoint:
        return NCDEXCheckpoint(**json.loads(Path(checkpoint_path).read_text(encoding="utf-8")))

    def save_checkpoint(self, checkpoint_path: str | Path, checkpoint: NCDEXCheckpoint) -> None:
        Path(checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
        Path(checkpoint_path).write_text(json.dumps(asdict(checkpoint), indent=2), encoding="utf-8")
