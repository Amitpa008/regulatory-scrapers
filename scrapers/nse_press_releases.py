from __future__ import annotations

import csv
import json
import re
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from extraction.metadata_cleaner import normalize_text, parse_indian_date
from scrapers.nse import NSE_HOME_URL, NSEScraper


NSE_PRESS_RELEASES_PAGE_URL = "https://www.nseindia.com/resources/exchange-communication-press-releases"
NSE_PRESS_RELEASES_ARCHIVE_URL = "https://www.nseindia.com/resources/exchange-communication-press-releases-archives"
NSE_PRESS_RELEASES_API_URL = "https://www.nseindia.com/api/press-release-cms20"
NSE_PRESS_RELEASES_CATEGORY_URL = "https://www.nseindia.com/api/list-category-cms20?key=press-release-categories"


@dataclass
class NSEPressReleaseRecord:
    date: str
    subject: str
    circular_no: str
    link: str
    source_url: str
    scraped_at: str
    category: Optional[str] = None
    raw_date: Optional[str] = None


@dataclass
class NSEPressReleaseCheckpoint:
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
class NSEPressReleaseEndpointResult:
    url: str
    method: str
    status_code: int
    content_type: Optional[str]
    response_size: int
    json_keys: list[str]
    csv_headers: list[str]
    record_count: int
    sample_records: list[dict[str, Any]]


@dataclass
class NSEPressReleaseChunk:
    index: int
    from_date: date
    to_date: date


@dataclass
class NSEArchiveDateLink:
    date: str
    link: str
    source_url: str


class NSEPressReleasesScraper(NSEScraper):
    source = "nse-press-releases"
    regulator = "NSE"

    def request_nse_page(self, url: str, *, raise_for_status: bool = True):
        headers = dict(self.headers)
        headers["Referer"] = NSE_PRESS_RELEASES_PAGE_URL
        response = self.client.get(url, headers=headers)
        if raise_for_status:
            response.raise_for_status()
        return response

    def inspect_press_releases(self, url: str) -> dict[str, Any]:
        today = date.today()
        current_year_start = date(today.year, 1, 1)
        warmup_results = self.warmup_nse_session(url)
        page_response = self.request_nse_page(url)

        fixture_dir = Path("tests/fixtures/nse")
        fixture_dir.mkdir(parents=True, exist_ok=True)
        fixture_path = fixture_dir / "press_releases.html"
        fixture_path.write_text(page_response.text, encoding="utf-8")

        page_soup = self.parse_html(page_response)
        page_text = page_response.text
        title = page_soup.title.get_text(" ", strip=True) if page_soup.title else ""

        container_exists = bool(page_soup.select("#pressReleaseList, #PressReleaseDiv, .press_media"))
        date_controls = sorted(
            {
                item.get_text(" ", strip=True)
                for item in page_soup.select("a, button, label, span")
                if item.get_text(" ", strip=True) in {"1D", "1W", "1M", "3M", "6M", "1Y", "Custom"}
            }
        )
        filter_controls = sorted(
            {
                control.get("id") or control.get("name") or control.get_text(" ", strip=True)
                for control in page_soup.select("input, select, button, a")
                if any(
                    token in (
                        f"{control.get('id', '')} {control.get('name', '')} {control.get_text(' ', strip=True)}"
                    ).lower()
                    for token in ["search", "category", "download", "from", "to", "press"]
                )
            }
        )
        archive_route = next(
            (
                urljoin(str(page_response.url), anchor.get("href"))
                for anchor in page_soup.find_all("a", href=True)
                if "press releases - archives" in anchor.get_text(" ", strip=True).lower()
            ),
            None,
        )
        category_payload = self.fetch_category_payload()
        category_values = self.extract_category_values(category_payload)
        api_urls_found = sorted(set(self.extract_api_urls_from_html(page_text)))
        endpoint_results = [
            self.inspect_endpoint(NSE_PRESS_RELEASES_CATEGORY_URL),
            self.inspect_endpoint(
                f"{NSE_PRESS_RELEASES_API_URL}?fromDate={current_year_start.strftime('%d-%m-%Y')}&toDate={today.strftime('%d-%m-%Y')}"
            ),
            self.inspect_endpoint(
                f"{NSE_PRESS_RELEASES_API_URL}?csv=true&fromDate={current_year_start.strftime('%d-%m-%Y')}&toDate={today.strftime('%d-%m-%Y')}"
            ),
        ]
        sample_records = self.parse_press_release_records(
            self.fetch_press_release_payload(from_date=current_year_start, to_date=today),
            url,
        )[:10]

        print(f"Page title: {title}")
        print(f"Fixture saved: {fixture_path}")
        print(f"Press release container exists: {container_exists}")
        print(f"Filter controls found: {filter_controls}")
        print(f"Category values found: {category_values}")
        print(f"Date controls found: {date_controls}")
        print(f"Archive route found: {archive_route}")
        print("Warm-up results:")
        for result in warmup_results:
            print(json.dumps(result, indent=2))
        print("API URLs found in page/scripts:")
        for api_url in api_urls_found:
            print(api_url)
        print("Endpoint inspection results:")
        for endpoint_result in endpoint_results:
            print(json.dumps(asdict(endpoint_result), indent=2))
        print("First 10 records from working API:")
        for record in sample_records:
            print(f"{record.date} | {record.subject} | {record.circular_no} | {record.link}")

        return {
            "title": title,
            "container_exists": container_exists,
            "filter_controls": filter_controls,
            "category_values": category_values,
            "date_controls": date_controls,
            "archive_route": archive_route,
            "api_urls_found": api_urls_found,
            "endpoint_results": [asdict(item) for item in endpoint_results],
        }

    def inspect_press_release_archives(self, url: str) -> dict[str, Any]:
        del url
        response = self.request_nse_page(NSE_PRESS_RELEASES_ARCHIVE_URL)
        fixture_dir = Path("tests/fixtures/nse")
        fixture_dir.mkdir(parents=True, exist_ok=True)
        fixture_path = fixture_dir / "press_releases_archives.html"
        fixture_path.write_text(response.text, encoding="utf-8")

        archive_rows = self.parse_archive_index_rows(response.text, str(response.url))
        oldest_visible = archive_rows[0].date if archive_rows else None
        first_ten = archive_rows[:10]

        print(f"Archive URL: {response.url}")
        print(f"Archive fixture saved: {fixture_path}")
        print("Archive has year/month/date filters: no explicit filter controls found; static date index page")
        print(f"Archive exposes older press releases: {bool(archive_rows)}")
        print(f"Oldest date visible from archive: {oldest_visible}")
        print("First 10 archive records:")
        for record in first_ten:
            print(f"{record.date} | {record.link}")
        print(f"Archive API endpoint used by page: none detected; static route {response.url}")

        return {
            "archive_url": str(response.url),
            "oldest_visible_date": oldest_visible,
            "record_count": len(archive_rows),
            "sample_records": [asdict(item) for item in first_ten],
        }

    def discover_press_release_range(self, url: str) -> dict[str, Any]:
        del url
        newest_available_date, oldest_api_date, total_api_records = self.get_api_range()
        archive_response = self.request_nse_page(NSE_PRESS_RELEASES_ARCHIVE_URL)
        archive_rows = self.parse_archive_index_rows(archive_response.text, str(archive_response.url))
        oldest_archive_date = archive_rows[0].date if archive_rows else None
        earliest_api_records = sorted(
            self.parse_press_release_records(
                self.fetch_press_release_payload(from_date=oldest_api_date, to_date=oldest_api_date + timedelta(days=14)),
                NSE_PRESS_RELEASES_PAGE_URL,
            ),
            key=lambda record: record.date,
        )[:5]

        limitation = (
            "The live press-release API exposes full row metadata from "
            f"{oldest_api_date.isoformat()} onward. The archive route exposes daily links back to "
            f"{oldest_archive_date or 'unknown'}, but many pre-{oldest_api_date.year} entries are PDF-only "
            "daily bundles with no subject in the archive listing. Because PDF download/content extraction is out of scope here, "
            "the scraper can fully export API-backed records but only partially cover older archive-only entries."
        )
        result = {
            "working_endpoint": NSE_PRESS_RELEASES_API_URL,
            "working_query_params": {
                "fromDate": oldest_api_date.strftime("%d-%m-%Y"),
                "toDate": newest_available_date.strftime("%d-%m-%Y"),
            },
            "direct_http_worked": True,
            "playwright_used": False,
            "newest_press_release_date": newest_available_date.isoformat(),
            "oldest_press_release_date": oldest_archive_date or oldest_api_date.isoformat(),
            "oldest_api_full_metadata_date": oldest_api_date.isoformat(),
            "total_count": total_api_records,
            "sample_earliest_records": [asdict(record) for record in earliest_api_records],
            "limitation": limitation,
        }

        print(f"Working endpoint/page-flow: {result['working_endpoint']} plus static archive route {archive_response.url}")
        print(f"Working query parameters: {json.dumps(result['working_query_params'])}")
        print(f"Direct HTTP worked: {result['direct_http_worked']}")
        print(f"Playwright used: {result['playwright_used']}")
        print(f"Newest press release date found: {result['newest_press_release_date']}")
        print(f"Oldest press release date found: {result['oldest_press_release_date']}")
        print(f"Total API-backed record count: {result['total_count']}")
        print("Sample earliest 5 records:")
        for record in earliest_api_records:
            print(f"{record.date} | {record.subject} | {record.circular_no} | {record.link}")
        print(f"Limitation: {limitation}")
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
    ) -> list[NSEPressReleaseRecord]:
        out_path = Path(out_path)
        checkpoint_file = Path(checkpoint_path) if checkpoint_path else Path(f"{out_path}.checkpoint.json")
        self.ensure_output_writable(out_path, resume=resume)
        newest_available_date, oldest_api_date, total_api_records = self.get_api_range()

        scrape_from = from_date or (oldest_api_date if all_available else newest_available_date - timedelta(days=365))
        scrape_to = to_date or newest_available_date
        if scrape_from > scrape_to:
            raise ValueError("from_date must be less than or equal to to_date")

        if scrape_from < oldest_api_date:
            print(
                f"Warning: requested start date {scrape_from.isoformat()} is older than the oldest full-metadata API date "
                f"{oldest_api_date.isoformat()}. This scraper will export API-backed records only."
            )
            scrape_from = oldest_api_date

        chunks = self.build_chunks(scrape_from, scrape_to)
        existing_records = self.load_existing_output_records(out_path) if (resume and out_path.exists()) else []
        csv_row_count = len(existing_records)
        started_at = datetime.now(timezone.utc).isoformat()
        checkpoint = NSEPressReleaseCheckpoint(
            source_url=url,
            output_path=str(out_path),
            newest_available_date=newest_available_date.isoformat(),
            oldest_available_date=oldest_api_date.isoformat(),
            total_records_detected=total_api_records if all_available else None,
            chunk_strategy="yearly_date_windows",
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
        elif not resume:
            if out_path.exists():
                out_path.unlink()
            if checkpoint_file.exists():
                checkpoint_file.unlink()
            existing_records = []
            csv_row_count = 0

        seen_keys = {self.record_dedup_key(record) for record in existing_records}
        collected_records = list(existing_records)
        chunk_window = self.compute_chunk_window(
            total_chunks=len(chunks),
            previous_last_completed_chunk=checkpoint.last_completed_chunk,
            max_chunks_this_run=max_chunks_this_run,
        )
        start_index = int(chunk_window["resume_from_chunk"])
        chunk_stop_index = int(chunk_window["expected_end_chunk"])
        chunks_this_run = int(chunk_window["chunks_this_run"])
        completed = bool(chunk_window["completed"])
        output_mode = "append" if resume and out_path.exists() else "overwrite"

        print(f"Oldest date: {scrape_from.isoformat()}")
        print(f"Newest date: {scrape_to.isoformat()}")
        print(f"Expected records: {total_api_records if all_available else 'unknown'}")
        print(f"Output path: {out_path}")
        print(f"Output mode: {output_mode}")
        print(f"total_chunks: {len(chunks)}")
        print(f"CSV rows detected: {csv_row_count}")
        print(f"previous last_completed_chunk: {checkpoint.last_completed_chunk}")
        print(f"resume_from_chunk: {start_index}")
        print(f"max_chunks_this_run: {max_chunks_this_run}")
        print(f"expected_end_chunk: {chunk_stop_index}")
        print(f"actual chunk range: {start_index}-{chunk_stop_index}" if chunks_this_run else "actual chunk range: none")
        print(f"chunks_processed_this_run: {chunks_this_run}")

        if completed:
            print("Run already completed. No new chunks to process.")
            return collected_records

        rows_appended = 0
        duplicates_skipped = 0
        retry_triggered = False
        for chunk in chunks:
            if chunk.index < start_index or chunk.index > chunk_stop_index:
                continue

            try:
                payload, chunk_retried = self.fetch_chunk_payload_with_retry(
                    chunk=chunk,
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
                    delay_seconds=delay_seconds,
                    retries=retries,
                    retry_base_delay=retry_base_delay,
                    retry_max_delay=retry_max_delay,
                )
                print(f"Chunk {chunk.index} failed after {retries} retries.")
                print(f"Resume with: {resume_command}")
                raise RuntimeError(
                    f"Stopping after repeated failures on chunk {chunk.index}. Resume with: {resume_command}"
                ) from exc

            retry_triggered = retry_triggered or chunk_retried
            records = self.parse_press_release_records(payload, url)
            filtered_records = self.filter_records(records, from_date=from_date, to_date=to_date)
            new_records: list[NSEPressReleaseRecord] = []
            for record in filtered_records:
                key = self.record_dedup_key(record)
                if key in seen_keys:
                    duplicates_skipped += 1
                    continue
                seen_keys.add(key)
                new_records.append(record)
                collected_records.append(record)

            if new_records:
                self.append_output(new_records, out_path)
                rows_appended += len(new_records)

            self.assert_non_regressing_checkpoint(
                previous_last_completed_chunk=checkpoint.last_completed_chunk,
                new_last_completed_chunk=chunk.index,
            )
            checkpoint.last_completed_chunk = chunk.index
            checkpoint.records_written = csv_row_count + rows_appended
            checkpoint.unique_records_written = len(seen_keys)
            checkpoint.updated_at = datetime.now(timezone.utc).isoformat()
            self.save_checkpoint(checkpoint_file, checkpoint)
            time.sleep(delay_seconds)

        checkpoint.completed = checkpoint.last_completed_chunk >= len(chunks)
        checkpoint.updated_at = datetime.now(timezone.utc).isoformat()
        self.save_checkpoint(checkpoint_file, checkpoint)

        print(f"Rows written: {rows_appended}")
        print(f"Duplicates skipped: {duplicates_skipped}")
        print(f"Final CSV row count: {len(collected_records)}")
        print(f"Checkpoint state: {json.dumps(asdict(checkpoint), indent=2)}")
        print(f"Retry logic triggered: {retry_triggered}")
        return collected_records

    def validate_export(self, file_path: str | Path) -> dict[str, Any]:
        file_path = Path(file_path)
        expected_headers = ["date", "subject", "circular_no", "link", "source_url", "scraped_at"]
        report_path = file_path.parent / "nse_press_releases_validation_report.json"
        year_counts_path = file_path.parent / "nse_press_releases_year_counts.csv"

        malformed_csv_rows = 0
        total_rows = 0
        missing_date = 0
        missing_subject = 0
        missing_circular_no = 0
        missing_link = 0
        pdf_links = 0
        html_links = 0
        csv_links = 0
        other_links = 0
        empty_links = 0
        duplicate_key_count = 0
        seen_keys: set[tuple[str, ...]] = set()
        suspicious_rows: list[dict[str, Any]] = []
        year_counts: Counter[int] = Counter()
        dates_seen: list[str] = []

        with open(file_path, "r", newline="", encoding="utf-8") as file_obj:
            reader = csv.reader(file_obj)
            try:
                headers = next(reader)
            except StopIteration as exc:
                raise RuntimeError(f"NSE press release export is empty: {file_path}") from exc
            headers_exact = headers == expected_headers

            for row_number, row in enumerate(reader, start=2):
                if len(row) != len(expected_headers):
                    malformed_csv_rows += 1
                    suspicious_rows.append({"row_number": row_number, "reason": "malformed_csv_row", "row": row})
                    continue
                total_rows += 1
                row_data = dict(zip(expected_headers, row, strict=True))
                row_date = row_data["date"].strip()
                subject = row_data["subject"].strip()
                circular_no = row_data["circular_no"].strip()
                link = row_data["link"].strip()

                if not row_date:
                    missing_date += 1
                else:
                    try:
                        parsed = date.fromisoformat(row_date)
                        dates_seen.append(row_date)
                        year_counts[parsed.year] += 1
                    except ValueError:
                        missing_date += 1
                        suspicious_rows.append({"row_number": row_number, "reason": "invalid_iso_date", "date": row_date})

                if not subject:
                    missing_subject += 1
                elif len(subject) < 5:
                    suspicious_rows.append({"row_number": row_number, "reason": "subject_shorter_than_5", "subject": subject})

                if not circular_no:
                    missing_circular_no += 1
                if not link:
                    missing_link += 1
                    empty_links += 1
                else:
                    lowered = link.lower()
                    if lowered.endswith(".pdf"):
                        pdf_links += 1
                    elif lowered.endswith(".csv"):
                        csv_links += 1
                    elif lowered.endswith(".html") or lowered.endswith(".htm") or "/content/press/" in lowered:
                        html_links += 1
                    else:
                        other_links += 1
                    if not (
                        link.startswith("https://nsearchives.nseindia.com/")
                        or link.startswith("https://www.nseindia.com/")
                    ):
                        suspicious_rows.append({"row_number": row_number, "reason": "unexpected_link_prefix", "link": link})

                record = NSEPressReleaseRecord(
                    date=row_date,
                    subject=subject,
                    circular_no=circular_no,
                    link=link,
                    source_url=row_data["source_url"],
                    scraped_at=row_data["scraped_at"],
                )
                key = self.record_dedup_key(record)
                if key in seen_keys:
                    duplicate_key_count += 1
                else:
                    seen_keys.add(key)

        report = {
            "file": str(file_path),
            "headers_exact": headers_exact,
            "expected_headers": expected_headers,
            "total_rows": total_rows,
            "malformed_csv_rows": malformed_csv_rows,
            "min_date": min(dates_seen) if dates_seen else None,
            "max_date": max(dates_seen) if dates_seen else None,
            "rows_per_year": dict(sorted(year_counts.items())),
            "missing_fields": {
                "date": missing_date,
                "subject": missing_subject,
                "circular_no": missing_circular_no,
                "link": missing_link,
            },
            "link_counts": {
                "pdf": pdf_links,
                "html_detail": html_links,
                "csv": csv_links,
                "other": other_links,
                "empty": empty_links,
            },
            "duplicate_key_count": duplicate_key_count,
            "suspicious_row_count": len(suspicious_rows),
            "suspicious_rows": suspicious_rows[:200],
        }
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        with open(year_counts_path, "w", newline="", encoding="utf-8") as file_obj:
            writer = csv.DictWriter(file_obj, fieldnames=["year", "count"])
            writer.writeheader()
            writer.writerows({"year": year, "count": count} for year, count in sorted(year_counts.items()))

        print(f"Total rows: {total_rows}")
        print(f"Min date: {report['min_date']}")
        print(f"Max date: {report['max_date']}")
        print(f"Missing field counts: {json.dumps(report['missing_fields'])}")
        print(f"Duplicate key count: {duplicate_key_count}")
        print(f"Validation report: {report_path}")
        print(f"Year counts CSV: {year_counts_path}")
        return report

    def fetch_category_payload(self) -> list[dict[str, Any]]:
        response = self.request_nse_page(NSE_PRESS_RELEASES_CATEGORY_URL)
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError(f"Unexpected category payload type: {type(payload)!r}")
        return payload

    def extract_category_values(self, payload: list[dict[str, Any]]) -> list[str]:
        values: list[str] = []
        for item in payload:
            for child in item.get("items") or []:
                name = normalize_text((child or {}).get("name") or "")
                if name:
                    values.append(name)
        return values

    def inspect_endpoint(self, url: str) -> NSEPressReleaseEndpointResult:
        response = self.request_nse_page(url, raise_for_status=False)
        content_type = response.headers.get("content-type")
        json_keys: list[str] = []
        csv_headers: list[str] = []
        sample_records: list[dict[str, Any]] = []
        record_count = 0

        if content_type and "json" in content_type:
            payload = response.json()
            if isinstance(payload, list):
                record_count = len(payload)
                sample_records = payload[:3]
                if payload and isinstance(payload[0], dict):
                    json_keys = list(payload[0].keys())
            elif isinstance(payload, dict):
                json_keys = list(payload.keys())
                sample_records = [payload]
                record_count = 1
        elif content_type and "csv" in content_type:
            lines = response.text.splitlines()
            if lines:
                csv_headers = next(csv.reader([lines[0]]))
                record_count = max(0, len(lines) - 1)

        return NSEPressReleaseEndpointResult(
            url=url,
            method="GET",
            status_code=response.status_code,
            content_type=content_type,
            response_size=len(response.content),
            json_keys=json_keys,
            csv_headers=csv_headers,
            record_count=record_count,
            sample_records=sample_records,
        )

    def extract_api_urls_from_html(self, html: str) -> list[str]:
        urls = []
        for token in [
            NSE_PRESS_RELEASES_API_URL,
            NSE_PRESS_RELEASES_CATEGORY_URL,
            "/api/press-release-cms20",
            "/api/list-category-cms20?key=press-release-categories",
            "/resources/exchange-communication-press-releases-archives",
        ]:
            if token in html:
                urls.append(token)
        return urls

    def fetch_press_release_payload(
        self,
        *,
        from_date: date | None = None,
        to_date: date | None = None,
        csv_download: bool = False,
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if csv_download:
            params["csv"] = "true"
        if from_date is not None:
            params["fromDate"] = from_date.strftime("%d-%m-%Y")
        if to_date is not None:
            params["toDate"] = to_date.strftime("%d-%m-%Y")
        url = NSE_PRESS_RELEASES_API_URL
        if params:
            url = f"{url}?{urlencode(params)}"
        response = self.request_nse_page(url)
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError(f"NSE press release API returned unexpected payload type: {type(payload)!r}")
        return payload

    def parse_press_release_records(self, payload: list[dict[str, Any]], source_url: str) -> list[NSEPressReleaseRecord]:
        scraped_at = datetime.now(timezone.utc).isoformat()
        parsed_records: list[NSEPressReleaseRecord] = []
        for item in payload:
            content = item.get("content") or {}
            raw_date = normalize_text(content.get("field_date") or item.get("changed") or "")
            normalized_date = self.normalize_nse_date(raw_date)
            subject = self.extract_subject(content)
            category = normalize_text(content.get("field_type") or content.get("title") or "")
            link = self.normalize_nse_link(
                ((content.get("field_file_attachement") or {}).get("url"))
                or content.get("field_unique_url")
                or ""
            )
            if not normalized_date or not subject or not link:
                continue
            parsed_records.append(
                NSEPressReleaseRecord(
                    date=normalized_date,
                    subject=subject,
                    circular_no="",
                    link=link,
                    source_url=source_url,
                    scraped_at=scraped_at,
                    category=category or None,
                    raw_date=raw_date or None,
                )
            )
        return parsed_records

    def parse_archive_index_rows(self, html: str, source_url: str) -> list[NSEArchiveDateLink]:
        soup = BeautifulSoup(html, "html.parser")
        rows: list[NSEArchiveDateLink] = []
        for anchor in soup.find_all("a", href=True):
            text = normalize_text(anchor.get_text(" ", strip=True)) or ""
            if not re.fullmatch(r"[A-Z][a-z]{2} \d{1,2}, \d{4}", text):
                continue
            parsed_date = datetime.strptime(text, "%b %d, %Y").date()
            rows.append(
                NSEArchiveDateLink(
                    date=parsed_date.isoformat(),
                    link=urljoin(source_url, anchor.get("href", "")),
                    source_url=source_url,
                )
            )
        rows.sort(key=lambda item: item.date)
        return rows

    def extract_subject(self, content: dict[str, Any]) -> str:
        body_html = content.get("body") or ""
        body_soup = BeautifulSoup(body_html, "html.parser")
        li_values = [normalize_text(node.get_text(" ", strip=True)) for node in body_soup.find_all("li")]
        li_values = [value for value in li_values if value]
        if li_values:
            return normalize_text("; ".join(li_values))
        text = normalize_text(body_soup.get_text(" ", strip=True))
        if text:
            return text
        return normalize_text(content.get("title") or "")

    def normalize_nse_date(self, raw_value: str) -> str:
        parsed_date = parse_indian_date(raw_value)
        if parsed_date is None:
            raise RuntimeError(f"Unable to parse NSE press release date: {raw_value}")
        return parsed_date.isoformat()

    def normalize_nse_link(self, raw_value: str) -> str:
        value = normalize_text(raw_value)
        if not value:
            raise RuntimeError("NSE press release record is missing link")
        return urljoin("https://www.nseindia.com", value)

    def filter_records(
        self,
        records: list[NSEPressReleaseRecord],
        *,
        from_date: date | None,
        to_date: date | None,
    ) -> list[NSEPressReleaseRecord]:
        filtered: list[NSEPressReleaseRecord] = []
        for record in records:
            record_date = date.fromisoformat(record.date)
            if from_date and record_date < from_date:
                continue
            if to_date and record_date > to_date:
                continue
            filtered.append(record)
        return filtered

    def record_dedup_key(self, record: NSEPressReleaseRecord) -> tuple[str, str, str]:
        return (record.date, normalize_text(record.subject).casefold(), record.link)

    def build_chunks(self, from_date: date, to_date: date) -> list[NSEPressReleaseChunk]:
        chunks: list[NSEPressReleaseChunk] = []
        chunk_start = from_date
        index = 1
        while chunk_start <= to_date:
            chunk_end = min(to_date, chunk_start + timedelta(days=364))
            chunks.append(NSEPressReleaseChunk(index=index, from_date=chunk_start, to_date=chunk_end))
            chunk_start = chunk_end + timedelta(days=1)
            index += 1
        return chunks

    def get_api_range(self) -> tuple[date, date, int]:
        payload = self.fetch_press_release_payload(from_date=date(1990, 1, 1), to_date=date.today())
        records = self.parse_press_release_records(payload, NSE_PRESS_RELEASES_PAGE_URL)
        if not records:
            raise RuntimeError("NSE press release API returned zero records")
        newest = max(date.fromisoformat(record.date) for record in records)
        oldest = min(date.fromisoformat(record.date) for record in records)
        return newest, oldest, len(records)

    def fetch_chunk_payload_with_retry(
        self,
        *,
        chunk: NSEPressReleaseChunk,
        retries: int,
        retry_base_delay: float,
        retry_max_delay: float,
    ) -> tuple[list[dict[str, Any]], bool]:
        retry_triggered = False
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                payload = self.fetch_press_release_payload(from_date=chunk.from_date, to_date=chunk.to_date)
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
                time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    def load_existing_output_records(self, out_path: str | Path) -> list[NSEPressReleaseRecord]:
        out_path = Path(out_path)
        if not out_path.exists():
            return []
        if out_path.suffix.lower() == ".csv":
            with open(out_path, "r", newline="", encoding="utf-8") as file_obj:
                reader = csv.DictReader(file_obj)
                return [
                    NSEPressReleaseRecord(
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
            return [NSEPressReleaseRecord(**row) for row in payload]
        raise ValueError("Output path must end with .csv or .json")

    def append_output(self, records: list[NSEPressReleaseRecord], out_path: str | Path) -> None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [self.record_to_output_row(record) for record in records]
        if out_path.suffix.lower() == ".csv":
            write_header = not out_path.exists() or out_path.stat().st_size == 0
            with open(out_path, "a", newline="", encoding="utf-8") as file_obj:
                writer = csv.DictWriter(
                    file_obj,
                    fieldnames=["date", "subject", "circular_no", "link", "source_url", "scraped_at"],
                )
                if write_header:
                    writer.writeheader()
                writer.writerows(rows)
            return
        if out_path.suffix.lower() == ".json":
            existing = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else []
            existing.extend(rows)
            out_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
            return
        raise ValueError("Output path must end with .csv or .json")

    def write_output(self, records: list[NSEPressReleaseRecord], out_path: str | Path) -> None:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows = [self.record_to_output_row(record) for record in records]
        if out_path.suffix.lower() == ".csv":
            with open(out_path, "w", newline="", encoding="utf-8") as file_obj:
                writer = csv.DictWriter(
                    file_obj,
                    fieldnames=["date", "subject", "circular_no", "link", "source_url", "scraped_at"],
                )
                writer.writeheader()
                writer.writerows(rows)
            return
        if out_path.suffix.lower() == ".json":
            out_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            return
        raise ValueError("Output path must end with .csv or .json")

    def record_to_output_row(self, record: NSEPressReleaseRecord) -> dict[str, str]:
        return {
            "date": record.date,
            "subject": record.subject,
            "circular_no": record.circular_no,
            "link": record.link,
            "source_url": record.source_url,
            "scraped_at": record.scraped_at,
        }

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
    ) -> str:
        return (
            'py -3.13 -m main scrape-url --source nse-press-releases '
            f'--url "{url}" '
            f'--out "{out_path}" '
            '--resume '
            f'--checkpoint "{checkpoint_path}" '
            '--max-chunks-this-run 1 '
            f'--delay-seconds {delay_seconds} '
            f'--retries {retries} '
            f'--retry-base-delay {retry_base_delay} '
            f'--retry-max-delay {retry_max_delay}'
        )

    def load_checkpoint(self, checkpoint_path: str | Path) -> NSEPressReleaseCheckpoint:
        payload = json.loads(Path(checkpoint_path).read_text(encoding="utf-8"))
        return NSEPressReleaseCheckpoint(**payload)

    def save_checkpoint(self, checkpoint_path: str | Path, checkpoint: NSEPressReleaseCheckpoint) -> None:
        Path(checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
        Path(checkpoint_path).write_text(json.dumps(asdict(checkpoint), indent=2), encoding="utf-8")
